"""In-process instruments named by the T10 order (Instruments section).

* ``sqlite3.connect`` is wrapped (``sqlite3``, ``sqlite3.dbapi2`` and any
  product module that bound ``connect`` directly) so every connection gets a
  trace callback (the statement list), a progress handler at N=1 (VM steps),
  an authorizer (which table columns each statement reads) and a row hook
  (which sealed payloads are returned, identified by content address).
* ``EXPLAIN QUERY PLAN`` runs, on the same connection, for every traced
  top-level SELECT/WITH/UPDATE/DELETE (and INSERT...SELECT) statement, so temp
  tables and attachments resolve as they did for the statement itself.
* A ``sys.addaudithook`` hook records every Python-level ``open`` and every
  ``sqlite3.connect`` while a recorder is active.

Definitions used by the tests:

* A *statement* is a top-level traced statement. Statements SQLite runs on
  its own behalf for a virtual table (FTS5 shadow-table reads, reported by
  the trace with a leading ``--``, or naming tables as ``'schema'.'table'``
  when the table is first connected) are recorded as *nested* and are not
  statements, but their VM steps are counted.
* A *scan* is any plan line ``SCAN <name>`` (including ``USING COVERING
  INDEX``) whose name, or the alias it stands for, is a protected table.
* A *payload read* is a returned row value that is the sealed bytes of an
  episode (or capsule); its identity is ``<kind>:sha256:<sha256 of bytes>``,
  the documented content address, so the instrument does not depend on
  column names.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sqlite3.dbapi2
import sys
import weakref
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import unquote

PROTECTED = ('episodes', 'source_episodes', 'session_episodes', 'session_claims', 'source_sessions',
             'source_projects', 'desk_session_contexts', 'indexed_episodes')
PAYLOAD_TABLES = ('episodes', 'source_episodes')
_EPISODE_MARK = b'"schema_version":"ops.episode.v'
_CAPSULE_MARK = b'"schema_version":"ops.episode-capsule.v'
_REAL_CONNECT = sqlite3.connect
# SQLite's own statements for a virtual table (e.g. FTS5 reading its ``_config``
# shadow table when the table is first connected) name tables as 'schema'.'table'.
_INTERNAL = re.compile(r"\s*(?:SELECT|INSERT|REPLACE|UPDATE|DELETE)\b.*?'[A-Za-z_]\w*'\.'[A-Za-z_]\w*'"
                       # FTS5's own data_version check: some SQLite builds trace it without the leading `--`
                       r"|\s*PRAGMA\s+'[A-Za-z_]\w*'\.data_version\s*$", re.I | re.S)
_ACTIVE = None


def normalize(path) -> str:
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    if isinstance(path, os.PathLike):
        path = os.fspath(path)
    if not isinstance(path, str):
        return str(path)
    if path.startswith('file:'):
        path = unquote(path[5:].split('?', 1)[0])
        if path.startswith('//'):
            path = path[2:]
    if path in ('', ':memory:'):
        return path
    return os.path.realpath(path)


@dataclass
class Statement:
    index: int
    db: str
    sql: str
    nested: bool
    phase: str
    conn: int
    reads: frozenset = frozenset()
    plan: list | None = None
    plan_error: str | None = None

    @property
    def verb(self):
        match = re.match(r'\s*(\w+)', self.sql)
        return match.group(1).upper() if match else ''


@dataclass
class PayloadRow:
    kind: str
    identity: str
    statement: int | None
    phase: str


@dataclass
class Recorder:
    step_cap: int | None = None
    phase: str = 'call'
    statements: list = field(default_factory=list)
    steps: Counter = field(default_factory=Counter)
    payload_rows: list = field(default_factory=list)
    opens: list = field(default_factory=list)
    connects: list = field(default_factory=list)
    capped: bool = False

    def __post_init__(self):
        self._quiet = 0
        self._pending = {}
        self._current = {}
        self._connections = []
        self._total = 0

    # ---- hooks -----------------------------------------------------------------
    def _install(self, connection, database):
        db = normalize(database)
        key = id(connection)
        reference = weakref.ref(connection)
        recorder = self

        def trace(sql):
            recorder._on_trace(reference, key, db, sql)

        def progress():
            return recorder._on_step()

        def authorize(action, first, second, dbname, source):
            if action == sqlite3.SQLITE_READ and not recorder._quiet:
                recorder._pending.setdefault(key, set()).add((first, second))
            return sqlite3.SQLITE_OK

        def rows(cursor, row):
            if not recorder._quiet and _ACTIVE is recorder:
                recorder._observe(key, row)
            user = getattr(reference(), '_t10_user_factory', None)
            return user(cursor, row) if user is not None else row

        connection.set_trace_callback(trace)
        connection.set_progress_handler(progress, 1)
        connection.set_authorizer(authorize)
        if isinstance(connection, _TracedConnection):
            sqlite3.Connection.row_factory.__set__(connection, rows)
        self._connections.append(reference)

    def _uninstall(self):
        for reference in self._connections:
            connection = reference()
            if connection is None:
                continue
            try:
                connection.set_trace_callback(None)
                connection.set_progress_handler(None, 0)
                connection.set_authorizer(None)
            except sqlite3.ProgrammingError:
                pass  # closed

    def _on_trace(self, reference, key, db, sql):
        if self._quiet:
            return
        nested = sql.startswith('--') or bool(_INTERNAL.match(sql))
        implicit_begin = sql.strip().upper() == 'BEGIN'
        reads = frozenset() if implicit_begin else frozenset(self._pending.pop(key, ()))
        statement = Statement(len(self.statements), db, sql, nested, self.phase, key, reads)
        self.statements.append(statement)
        if nested:
            return
        self._current[key] = statement.index
        verb = statement.verb
        wants_plan = verb in ('SELECT', 'WITH', 'UPDATE', 'DELETE') or (
            verb in ('INSERT', 'REPLACE') and re.search(r'\bSELECT\b', sql, re.I))
        if not wants_plan:
            return
        connection = reference()
        if connection is None:
            return
        self._quiet += 1
        try:
            statement.plan = [row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + sql).fetchall()]
        except Exception as error:  # noqa: BLE001 - recorded, asserted by the tests
            statement.plan_error = repr(error)
        finally:
            self._quiet -= 1

    def _on_step(self):
        if self._quiet:
            return 0
        self.steps[self.phase] += 1
        self._total += 1
        if self.step_cap is not None and self._total > self.step_cap:
            self.capped = True
            return 1
        return 0

    def _observe(self, key, row):
        for value in row:
            if isinstance(value, memoryview):
                value = bytes(value)
            if isinstance(value, str):
                if '"schema_version"' not in value:
                    continue
                value = value.encode()
            if not isinstance(value, bytes):
                continue
            if _CAPSULE_MARK in value:
                kind = 'episode-capsule'
            elif _EPISODE_MARK in value:
                kind = 'episode'
            else:
                continue
            identity = kind + ':sha256:' + hashlib.sha256(value).hexdigest()
            self.payload_rows.append(PayloadRow(kind, identity, self._current.get(key), self.phase))

    # ---- views -------------------------------------------------------------------
    def top(self, phases=None):
        return [s for s in self.statements if not s.nested and (phases is None or s.phase in phases)]

    def count(self, phases=('construct', 'call')):
        return len(self.top(phases))

    def vm_steps(self, phases=('construct', 'call')):
        return sum(self.steps[p] for p in phases)

    def scans(self, phases=('construct', 'call'), protected=PROTECTED):
        found = []
        for statement in self.top(phases):
            for line in statement.plan or ():
                table = scanned_table(line, statement.sql)
                if table in protected:
                    found.append((table, line, statement.sql[:300]))
        return found

    def plan_errors(self, phases=('construct', 'call')):
        return [(s.sql[:300], s.plan_error) for s in self.top(phases)
                if s.plan_error and any(re.search(r'\b%s\b' % t, s.sql) for t in PROTECTED)]

    def payloads(self, kind='episode', phases=('construct', 'call')):
        return Counter(r.identity for r in self.payload_rows if r.kind == kind and r.phase in phases)

    def star_selects(self, phases=('construct', 'call')):
        pattern = re.compile(r'\bSELECT\s+(?:DISTINCT\s+|ALL\s+)?\*|,\s*\*\s*(?:,|\bFROM\b)|\b[\w"]+\.\*', re.I)
        return [s.sql[:300] for s in self.top(phases) if pattern.search(strip_literals(s.sql))]

    def payload_access_violations(self, phases=('construct', 'call')):
        """Statements reading a sealed ``payload`` column other than by identity."""
        bad = []
        for statement in self.top(phases):
            tables = {table for table, column in statement.reads
                      if table in PAYLOAD_TABLES and column == 'payload'}
            for table in tables:
                aliases = {table} | {alias for alias, name in aliases_of(statement.sql).items() if name == table}
                lines = [line for line in statement.plan or ()
                         if re.match(r'(SCAN|SEARCH) (\S+)', line)
                         and re.match(r'(SCAN|SEARCH) (\S+)', line).group(2) in aliases]
                ok = bool(lines) and all(line.startswith('SEARCH') and re.search(
                    r'\((?:id|rowid)=\?\)|PRIMARY KEY', line) for line in lines)
                if not ok:
                    bad.append((table, lines, statement.sql[:300]))
        return bad

    def opened(self, path, phases=('call',)):
        target = normalize(path)
        return sum(1 for p, _, phase in self.opens if p == target and phase in phases)

    def opened_under(self, root, phases=('construct', 'call')):
        root = normalize(root).rstrip('/') + '/'
        return [(p, mode, phase) for p, mode, phase in self.opens if p.startswith(root) and phase in phases]

    def connected(self, path, phases=('construct', 'call')):
        target = normalize(path)
        return sum(1 for p, phase in self.connects if p == target and phase in phases)

    def matching(self, pattern, phases=('call',), db=None):
        """Top-level statements whose literal-free SQL matches ``pattern``."""
        regex = re.compile(pattern, re.I | re.S)
        return [s for s in self.top(phases)
                if regex.search(strip_literals(s.sql)) and (db is None or s.db == normalize(db))]

    def describe(self, phases=('construct', 'call'), limit=400):
        lines = []
        for s in self.top(phases)[:limit]:
            lines.append(f'[{s.phase}] {os.path.basename(s.db)}: {s.sql[:200]} :: {s.plan}')
        return '\n'.join(lines)


class _TracedConnection(sqlite3.Connection):
    """Keeps a product-assigned ``row_factory`` composable with the row hook."""
    _t10_user_factory = None

    @property
    def row_factory(self):
        return self._t10_user_factory

    @row_factory.setter
    def row_factory(self, value):
        self._t10_user_factory = value


def _traced_connect(database, *args, **kwargs):
    recorder = _ACTIVE
    if recorder is None:
        return _REAL_CONNECT(database, *args, **kwargs)
    if 'factory' not in kwargs and len(args) < 5:
        kwargs['factory'] = _TracedConnection
    if 'cached_statements' not in kwargs and len(args) < 6:
        # Every execute prepares, so the authorizer reports each statement's reads.
        kwargs['cached_statements'] = 0
    connection = _REAL_CONNECT(database, *args, **kwargs)
    recorder._install(connection, database)
    return connection


def _audit(event, args):
    recorder = _ACTIVE
    if recorder is None or recorder._quiet:
        return
    if event == 'open':
        path = args[0] if args else None
        if path is None or isinstance(path, int):
            return
        recorder.opens.append((normalize(path), args[1] if len(args) > 1 else None, recorder.phase))
    elif event == 'sqlite3.connect':
        recorder.connects.append((normalize(args[0]), recorder.phase))


if not getattr(sys, '_t10_audit_installed', False):
    sys.addaudithook(_audit)
    sys._t10_audit_installed = True


@contextmanager
def recording(step_cap=None):
    """Activate a recorder; patch ``connect`` wherever the product can reach it."""
    global _ACTIVE
    recorder = Recorder(step_cap=step_cap)
    patched = [(sqlite3, 'connect', sqlite3.connect), (sqlite3.dbapi2, 'connect', sqlite3.dbapi2.connect)]
    for name, module in list(sys.modules.items()):
        if module is not None and (name == 'kp_agent_tooling' or name.startswith('kp_agent_tooling.')):
            if getattr(module, 'connect', None) is _REAL_CONNECT:
                patched.append((module, 'connect', _REAL_CONNECT))
    for module, attribute, _ in patched:
        setattr(module, attribute, _traced_connect)
    _ACTIVE = recorder
    try:
        yield recorder
    finally:
        _ACTIVE = None
        for module, attribute, value in patched:
            setattr(module, attribute, value)
        recorder._uninstall()
        recorder.phase = 'done'


# ---- plan parsing ----------------------------------------------------------------

_KEYWORDS = {'WHERE', 'ON', 'USING', 'JOIN', 'LEFT', 'RIGHT', 'INNER', 'OUTER', 'CROSS', 'NATURAL',
             'GROUP', 'ORDER', 'LIMIT', 'UNION', 'EXCEPT', 'INTERSECT', 'SELECT', 'FROM', 'AS', 'AND',
             'OR', 'NOT', 'INDEXED', 'WINDOW', 'HAVING', 'VALUES', 'SET', 'RETURNING', 'FULL'}
_NAME = r'(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)'
_REF = re.compile(r'(?:\bFROM|\bJOIN|,)\s+(?:' + _NAME + r'\s*\.\s*)?(' + _NAME + r')(?:\s+(?:AS\s+)?(' + _NAME + r'))?', re.I)
_CTE = re.compile(r'(?:\bWITH(?:\s+RECURSIVE)?|,)\s+(' + _NAME + r')\s*(?:\([^)]*\))?\s+AS\s*(?:NOT\s+)?(?:MATERIALIZED\s+)?\(', re.I)


def _unquote(name):
    return name.strip('"`[]')


def strip_literals(sql):
    return re.sub(r"'(?:[^']|'')*'", "''", re.sub(r"X'[0-9A-Fa-f]*'", "X''", sql))


def aliases_of(sql):
    """``{alias_or_name: table}`` for every table reference (best effort, literal-free)."""
    text = strip_literals(sql)
    result = {}
    for match in _REF.finditer(text):
        table = _unquote(match.group(1))
        alias = match.group(2)
        result.setdefault(table, table)
        if alias and alias.upper() not in _KEYWORDS:
            result[_unquote(alias)] = table
    for match in _CTE.finditer(text):
        result[_unquote(match.group(1))] = '<cte>'
    return result


def scanned_table(line, sql):
    match = re.match(r'SCAN (\S+)', line)
    if not match:
        return None
    name = match.group(1)
    mapped = aliases_of(sql).get(name, name)
    return None if mapped == '<cte>' else mapped
