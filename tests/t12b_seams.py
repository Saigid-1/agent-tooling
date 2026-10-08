"""T12b seams and instruments: every name the T12b tests assume, in ONE place.

Order: docs/work/orders/T12b-one-indexer-outbox.md (frozen r5). The order names `drain(store, index,
lease, batch)`, the `indexer` role and its `--watch` command, `index_lag` beside `status` on
`memory.connection_status`, the outbox table `index_outbox(seq, reason, episode_id, session_id,
detail)`, the lease file `<state_root>/index.lock`, and leaf primitives (a non-blocking lock and a
rename-into-place primitive). Where it does not spell an import path, an argument type or a CLI
token, the reading chosen here is the most conservative one, and it is written down ONLY here, so
the meet can reconcile it with the FEATURE arm's spelling by editing this file alone:

- `drain` is `kp_agent_tooling._impl.service.episodic_search.drain` (the order: "`episodic_search.py`:
  the indexer, `drain`, the build-and-swap reindex"), called POSITIONALLY in the order's argument
  order `(store, index, lease, batch)`: `store` an EpisodeStore, `index` an EpisodicSearchIndex that is
  also os.PathLike (so a FEATURE reading "the index path" and one reading "the index object" both
  work), `lease` the path `<state_root>/index.lock`, `batch` an int;
- the indexer, inside a process, is any frame of a function named `drain` in that module (statement and
  open attribution: "only the indexer writes the index");
- the rename-into-place primitive is `leaf.rename_into_place(source, destination)` (the order's words);
- the build file is any `.sqlite3` name the leaf gains beyond its base constants (the order: "The build
  file's name is a leaf constant"), or any name beginning with the index's own stem;
- the indexer's interval is the number following the first `--interval...` option of the rendered
  manifest's `indexer` command (the order: "set in the packaged manifest's command and read from there");
- `index_lag` is the integer field `index_lag` of `memory.connection_status`;
- a reindex request that only writes its row is `episodic_search.request_reindex(store)` (meet, 2026-10-04);
- the Compose marker in-process is `compose_marker(root)`: AGENT_MEMORY_VOLUME set, and the leaf's volume root
  (`leaf.STORE_ROOT`) pointed at a temporary root, so T11b's store-path check passes for stores under it.

The B4 test helper is `drain(target)` below (the order: "In-process reliers call one test helper,
`drain()`"). It needs the product's drain and FAILS when it is absent (at base): it never falls back to
the old inline indexing.
"""
from __future__ import annotations

import os
import re
import sqlite3
import sqlite3.dbapi2
import sys
import threading
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

import pytest

# ---------------------------------------------------------------------------------------------- names

OUTBOX = 'index_outbox'
OUTBOX_COLUMNS = ('seq', 'reason', 'episode_id', 'session_id', 'detail')
REASONS = ('seal', 'desks_changed', 'reindex')
WATCHED = ('episodes', 'source_episodes', 'session_claims', 'session_episodes')
STORE_NAME = 'episodes.sqlite3'
INDEX_NAME = 'episode-search.sqlite3'
LEASE_NAME = 'index.lock'
INDEXER_SERVICE = 'indexer'
ORDER_INTERVAL_SECONDS = 2.0          # "a fixed interval of 2 s"
ORDER_STALL_DRAINS = 5                # "across 5 consecutive non-skipped drains"
DRAIN_MODULE = 'kp_agent_tooling._impl.service.episodic_search'
DRAIN_NAME = 'drain'
RENAME_PRIMITIVE = 'rename_into_place'
# The request that only writes the `reindex` row (meet, 2026-10-04: `index-history` drains it at once on a host).
REQUEST_REINDEX = 'request_reindex'
# The indexer role's loop and health, FEATURE's spelling in episodic_search (meet, 2026-10-05):
# `watch(root, interval, *, max_passes, batch, clock)` prints one JSON line per store per pass, and
# `health(root)` returns (exit code, record) from `<root>/indexer-health.json`. The per-drain line carries
# `status`, `category`, `message`, `index_lag`, `health` and the per-store stall count below.
WATCH_NAME = 'watch'
HEALTH_NAME = 'health'
STALL_COUNT = 'stalled_drains'
HEALTH_FILE = 'indexer-health.json'
# The post-swap coverage marks' bound (meet ruling, 2026-10-05): episodes per `mark_coverage` store transaction,
# its own module constant in episodic_search, not the drain's `batch`. RULED_MARK_BATCH is the ruled value, used
# only where the product has no MARK_BATCH yet, so a test there fails on its property and not on this seam.
MARK_BATCH_NAME = 'MARK_BATCH'
RULED_MARK_BATCH = 100
# The sticky signal for coverage marks lost after the index commit (meet ruling, 2026-10-05): a `scope_state` row
# whose value is JSON {category, message, seq, at}; while it is present the per-drain line carries
# LINE_COVERAGE_FIELD = LINE_COVERAGE_LOST (with the category) and the store's health is unhealthy.
LOST_MARKS_KEY = 'coverage_marks_lost'
LINE_COVERAGE_FIELD = 'coverage'
LINE_COVERAGE_LOST = 'lost'
# The line's detail of the held record (its category, seq and time), and the error line's report of a record whose
# write failed (meet ruling): `coverage_record: {status: 'unwritten', ...}`.
LINE_COVERAGE_DETAIL = 'coverage_lost'
LINE_COVERAGE_RECORD = 'coverage_record'
# The host drain-after-seal FEATURE calls at each former inline site (meet, 2026-10-04): None inside Compose.
DRAIN_AFTER_SEAL = 'drain_after_seal'
INDEX_LAG = 'index_lag'
DEFAULT_BATCH = 500
# The leaf's `.sqlite3` constants at T12b's base, so a name the leaf gains is the build file.
BASE_LEAF_SQLITE = frozenset({
    'episodes.sqlite3', 'sessions.sqlite3', 'episode-search.sqlite3', 'workspace-capture.sqlite3',
    'session-import-jobs.sqlite3', 'spool-ingest.sqlite3', 'capture.sqlite3', 'queue.sqlite3',
    'telemetry.sqlite3', 'rollout-capture.sqlite3', 'ledger.sqlite3', 'references.sqlite3'})
_REAL_CONNECT = sqlite3.connect


# ----------------------------------------------------------------------------------------- the product

def product_drain():
    """The product's `drain`, or a test failure naming the seam (at base there is none)."""
    import importlib
    module = importlib.import_module(DRAIN_MODULE)
    found = getattr(module, DRAIN_NAME, None)
    if not callable(found):
        pytest.fail(f'T12b seam: no `{DRAIN_NAME}` in {DRAIN_MODULE} (the order: "Indexing is drain(store, index, '
                    'lease, batch)"); see tests/t12b_seams.py', pytrace=False)
    return found


def request_reindex(store):
    """A full-reindex request that only writes the outbox row: `episodic_search.request_reindex(store)` (the meet's
    seam; on a host, `kp-agent-desk ... index-history` also drains it at once, so a test that must observe the build
    requests it here and drains it itself)."""
    import importlib
    found = getattr(importlib.import_module(DRAIN_MODULE), REQUEST_REINDEX, None)
    if not callable(found):
        pytest.fail(f'T12b seam: no `{REQUEST_REINDEX}` in {DRAIN_MODULE}; see tests/t12b_seams.py', pytrace=False)
    return found(store)


def indexer_function(name):
    """The product's indexer-role function `name` (WATCH_NAME, HEALTH_NAME) in episodic_search."""
    import importlib
    found = getattr(importlib.import_module(DRAIN_MODULE), name, None)
    if not callable(found):
        pytest.fail(f'T12b seam: no `{name}` in {DRAIN_MODULE}; see tests/t12b_seams.py', pytrace=False)
    return found


def mark_batch() -> tuple[int, str]:
    """(the post-swap marks' bound, where it came from): the product's `episodic_search.MARK_BATCH` ('product'), or
    RULED_MARK_BATCH ('ruled') when the product has none."""
    import importlib
    found = getattr(importlib.import_module(DRAIN_MODULE), MARK_BATCH_NAME, None)
    if type(found) is int and found >= 1:
        return found, 'product'
    return RULED_MARK_BATCH, 'ruled'


def rename_primitive():
    from kp_agent_tooling._impl import leaf
    found = getattr(leaf, RENAME_PRIMITIVE, None)
    if not callable(found):
        pytest.fail(f'T12b seam: no leaf.{RENAME_PRIMITIVE} (the order: "a new leaf rename-into-place primitive"); '
                    'see tests/t12b_seams.py', pytrace=False)
    return found


def _index_class():
    from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex

    class IndexArgument(EpisodicSearchIndex):
        """The index as an object and, through os.fspath, as its path."""

        def __fspath__(self):
            return os.fspath(self.path)
    return IndexArgument


def index_path_of(store) -> Path:
    return Path(os.fspath(store.path)).with_name(INDEX_NAME)


def lease_path_of(store) -> Path:
    return Path(os.fspath(store.path)).with_name(LEASE_NAME)


def index_argument(store):
    from kp_agent_tooling._impl import leaf
    return _index_class()(leaf.store_path(store.path, leaf.SEARCH_INDEX_DB, sibling=True), episode_store=store)


def lease_argument(store):
    from kp_agent_tooling._impl import leaf
    return leaf.store_path(store.path, LEASE_NAME, sibling=True)


def drain_once(store, *, batch=DEFAULT_BATCH):
    """One call of the product's drain, in the order's argument order."""
    return product_drain()(store, index_argument(store), lease_argument(store), batch)


def _store_of(target):
    if hasattr(target, '_connect') and hasattr(target, 'path'):
        return target
    from kp_agent_tooling._impl.service.desk_memory_runtime import components
    return components(target)[3]


def drain(target, *, batch=DEFAULT_BATCH, passes=50):
    """THE B4 helper: drain the store's outbox into its index (target: an EpisodeStore or a desk-memory
    config path). Calls the product's drain until the outbox is empty or a call changes nothing."""
    store = _store_of(target)
    results = []
    for _ in range(passes):
        before = outbox_count(store.path)
        results.append(drain_once(store, batch=batch))
        after = outbox_count(store.path)
        if not after or after == before:
            break
    return results


def has_drain() -> bool:
    import importlib
    return callable(getattr(importlib.import_module(DRAIN_MODULE), DRAIN_NAME, None))


def setup_index(store):
    """SETUP ONLY (never a measured step): index every sealed episode with what this tree offers, so a test's
    precondition (a complete index) holds at base too and the test fails on its own property: the product's drain
    when it has one, else the base operator rebuild."""
    if has_drain():
        return drain(store)
    from kp_agent_tooling._impl import leaf
    from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
    return EpisodicSearchIndex(leaf.store_path(store.path, leaf.SEARCH_INDEX_DB, sibling=True),
                               episode_store=store).rebuild()


@contextmanager
def compose_marker(root, volume='t12b-compose-marker'):
    """In-process Compose semantics (meet, 2026-10-04): `AGENT_MEMORY_VOLUME` set, so every reader of the marker
    (the leaf's `docker_runtime()`: sealers do not drain) sees the Compose runtime; and the leaf's volume root
    (`leaf.STORE_ROOT`, '/state/memory' in a role) pointed at `root`, so T11b's store-path check
    (`leaf.check_store`) passes for marked stores under `root` instead of refusing them. Both are restored."""
    from pathlib import PurePosixPath
    from kp_agent_tooling._impl import leaf
    saved = os.environ.get(leaf.VOLUME_VARIABLE), leaf.STORE_ROOT
    os.environ[leaf.VOLUME_VARIABLE] = volume
    leaf.STORE_ROOT = PurePosixPath(os.path.realpath(root))
    try:
        yield
    finally:
        if saved[0] is None:
            os.environ.pop(leaf.VOLUME_VARIABLE, None)
        else:
            os.environ[leaf.VOLUME_VARIABLE] = saved[0]
        leaf.STORE_ROOT = saved[1]


@contextmanager
def drain_after_seal_spy():
    """Record every call of the product's `drain_after_seal` (wherever a product module bound it) and its return
    value, as a list of (module that called it, returned value); the real function still runs."""
    import importlib
    module = importlib.import_module(DRAIN_MODULE)
    real = getattr(module, DRAIN_AFTER_SEAL, None)
    if not callable(real):
        pytest.fail(f'T12b seam: no `{DRAIN_AFTER_SEAL}` in {DRAIN_MODULE}; see tests/t12b_seams.py', pytrace=False)
    calls = []

    def spy(*args, **kwargs):
        value = real(*args, **kwargs)
        caller = sys._getframe(1).f_globals.get('__name__')
        calls.append((caller, value))
        return value
    bound = [(m, name) for m_name, m in list(sys.modules.items())
             if m is not None and m_name.startswith('kp_agent_tooling')
             for name in (DRAIN_AFTER_SEAL,) if getattr(m, name, None) is real]
    for target, name in bound:
        setattr(target, name, spy)
    try:
        yield calls
    finally:
        for target, name in bound:
            setattr(target, name, real)


# --------------------------------------------------------------------------------------------- reading

def ro(path):
    """A read-only connection that no instrument records (the real connect, a `file:` URI)."""
    return closing(_REAL_CONNECT(Path(path).resolve().as_uri() + '?mode=ro', uri=True))


def tables(path) -> set:
    with ro(path) as db:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def outbox_rows(path) -> list[dict]:
    with ro(path) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (OUTBOX,)).fetchone() is None:
            return []
        return [dict(zip(OUTBOX_COLUMNS, row)) for row in
                db.execute(f'SELECT {", ".join(OUTBOX_COLUMNS)} FROM {OUTBOX} ORDER BY seq')]


def outbox_count(path) -> int:
    with ro(path) as db:
        try:
            return db.execute(f'SELECT count(*) FROM {OUTBOX}').fetchone()[0]
        except sqlite3.OperationalError:
            return 0


def watched_counts(db) -> dict:
    present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    counts = {table: db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] if table in present else 0
              for table in WATCHED}
    counts[OUTBOX] = db.execute(f'SELECT count(*) FROM {OUTBOX}').fetchone()[0] if OUTBOX in present else 0
    return counts


def committed_counts(path) -> dict:
    with ro(path) as db:
        return watched_counts(db)


def index_family(path) -> bool:
    """The index file, or a build file beside it (see the module docstring)."""
    name = Path(str(path)).name
    if not name:
        return False
    if name == INDEX_NAME or name.startswith(INDEX_NAME.split('.')[0]):
        return True
    try:
        from kp_agent_tooling._impl import leaf
    except ImportError:
        return False
    gained = {value for key, value in vars(leaf).items()
              if key.isupper() and isinstance(value, str) and value.endswith('.sqlite3') and value not in BASE_LEAF_SQLITE}
    return name in gained


def indexer_interval(command) -> float | None:
    """The interval of an indexer command: the number after its first `--interval...` option."""
    if not isinstance(command, (list, tuple)):
        command = str(command or '').split()
    for position, token in enumerate(command):
        token = str(token)
        if token.startswith('--interval'):
            value = token.split('=', 1)[1] if '=' in token else (command[position + 1] if position + 1 < len(command) else None)
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


# --------------------------------------------------------------------------------------- the instrument

def normalize(database) -> str:
    if isinstance(database, bytes):
        database = os.fsdecode(database)
    database = os.fspath(database) if isinstance(database, os.PathLike) else str(database)
    if database.startswith('file:'):
        database = unquote(database[5:].split('?', 1)[0])
        if database.startswith('//'):
            database = database[2:]
    if database in ('', ':memory:'):
        return database
    return os.path.realpath(database)


def in_drain(frame=None) -> bool:
    """A frame of the indexer (`drain` in its module) is on the calling stack."""
    frame = frame or sys._getframe(1)
    while frame is not None:
        if frame.f_code.co_name == DRAIN_NAME and frame.f_globals.get('__name__') == DRAIN_MODULE:
            return True
        frame = frame.f_back
    return False


_WRITE = re.compile(r'\s*(INSERT|REPLACE|UPDATE|DELETE|CREATE|DROP|ALTER|BEGIN\s+(IMMEDIATE|EXCLUSIVE)|VACUUM|REINDEX)\b',
                    re.I)


@dataclass
class Event:
    kind: str          # 'sql' or 'connect'
    db: str
    sql: str
    drain: bool
    thread: int
    conn: int = 0

    @property
    def write(self) -> bool:
        return self.kind == 'sql' and bool(_WRITE.match(self.sql)) and not self.sql.startswith('--')


@dataclass
class Trace:
    events: list = field(default_factory=list)
    observations: int = 0
    violations: list = field(default_factory=list)
    phase: str = 'call'

    def on(self, path):
        target = normalize(path)
        return [e for e in self.events if e.db == target]

    def family(self):
        return [e for e in self.events if e.db and index_family(e.db)]

    def connects(self, path=None, *, drain=None):
        found = [e for e in self.events if e.kind == 'connect' and (path is None or e.db == normalize(path))]
        return [e for e in found if drain is None or e.drain == drain]

    def writes(self, path=None, *, drain=None):
        found = [e for e in self.events if e.write and (path is None or e.db == normalize(path))]
        return [e for e in found if drain is None or e.drain == drain]

    def statements(self, path=None, pattern=None):
        regex = re.compile(pattern, re.I | re.S) if pattern else None
        return [e for e in self.events if e.kind == 'sql' and (path is None or e.db == normalize(path))
                and (regex is None or regex.search(e.sql))]


_ACTIVE: list = []
# Extra audit handlers (event, args) -> None, for the duration of a `with audit_handler(...)` block.
_HANDLERS: list = []


@contextmanager
def audit_handler(handler):
    """Run `handler(event, args)` for every audit event while active (audit hooks cannot be removed)."""
    _HANDLERS.append(handler)
    try:
        yield handler
    finally:
        _HANDLERS.remove(handler)


def _audit(event, args):
    for handler in list(_HANDLERS):
        handler(event, args)
    if event != 'sqlite3.connect':
        return
    try:
        trace, guard, notify = _ACTIVE[-1]  # another thread may end the recording at any moment
    except IndexError:
        return
    if getattr(guard, 'busy', False):
        return
    trace.events.append(Event('connect', normalize(args[0]), '', in_drain(sys._getframe(1)), threading.get_ident()))
    notify()


if not getattr(sys, '_t12b_audit_installed', False):
    sys.addaudithook(_audit)
    sys._t12b_audit_installed = True


@contextmanager
def traced(observe=None):
    """Record every statement (expanded SQL, with the database it runs on and whether the indexer is on the
    stack) and every `sqlite3.connect`, on every connection opened while active, in every thread.
    `observe()` runs at each recorded event; it may read the database with `ro()` (never recorded)."""
    trace = Trace()
    guard = threading.local()

    def notify():
        if observe is None or getattr(guard, 'busy', False):
            return
        guard.busy = True
        try:
            trace.observations += 1
            observe(trace)
        finally:
            guard.busy = False

    def connect(database, *args, **kwargs):
        connection = _REAL_CONNECT(database, *args, **kwargs)
        db = normalize(database)
        key = id(connection)

        def callback(sql):
            if getattr(guard, 'busy', False):
                return
            trace.events.append(Event('sql', db, sql, in_drain(sys._getframe(1)), threading.get_ident(), key))
            notify()
        connection.set_trace_callback(callback)
        return connection

    patched = [(sqlite3, 'connect', sqlite3.connect), (sqlite3.dbapi2, 'connect', sqlite3.dbapi2.connect)]
    for name, module in list(sys.modules.items()):
        if module is not None and name.startswith('kp_agent_tooling') and getattr(module, 'connect', None) is _REAL_CONNECT:
            patched.append((module, 'connect', _REAL_CONNECT))
    for module, attribute, _ in patched:
        setattr(module, attribute, connect)
    _ACTIVE.append((trace, guard, notify))
    try:
        yield trace
    finally:
        _ACTIVE.pop()
        for module, attribute, value in patched:
            setattr(module, attribute, value)
        if observe is not None:
            trace.observations += 1
            observe(trace)


def same_commit_observer(store_path, baseline):
    """An observer for `traced`: at every event the COMMITTED store holds exactly one outbox row per watched
    row inserted since `baseline` (a dict from `committed_counts`). A watched row committed before its outbox
    row (a later transaction), or an outbox row committed without its insert, is recorded as a violation."""
    def observe(trace):
        try:
            now = committed_counts(store_path)
        except sqlite3.Error as error:  # a reader refused mid-commit is not an observation
            return
        inserted = sum(now[t] - baseline[t] for t in WATCHED)
        appended = now[OUTBOX] - baseline[OUTBOX]
        if inserted != appended and len(trace.violations) < 20:
            last = next((e for e in reversed(trace.events) if e.kind == 'sql'), None)
            trace.violations.append(f'committed: {inserted} watched rows inserted, {appended} outbox rows appended '
                                    f'(after {last.sql[:160] if last else "-"!r} on {Path(last.db).name if last else "-"})')
    return observe
