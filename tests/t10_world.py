"""T10 synthetic worlds built only through public entry points.

Shared by the T10 tests and by the golden generator
(``tests/fixtures/t10-read-path-projection/generate.py``). Everything here
drives supported writes: operator initialisation and admission
(``desk_memory_runtime``), capture and the operator import
(``EpisodeStore``), source sessions, claims, links and imports
(``SessionSources``), desk profiles (``DeskProfiles``) and the operator index
writer (``EpisodicSearchIndex``). Reads go through ``EpisodicMemoryTools``.

No implementation internals are imported for computation: identities are
recomputed here from the documented content-addressing (sha256 of canonical
JSON), so the helpers stay independent of the code under test.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing, contextmanager
from pathlib import Path

INSTANCE = 't10'
TENANT_ONE = 't10-tenant-one'
TENANT_TWO = 't10-tenant-two'
REPO = 'repo-main'
FROZEN_AT = _dt.datetime(2026, 10, 2, 12, 0, 0, tzinfo=_dt.timezone.utc)
INDEX_NAME = 'episode-search.sqlite3'
STORE_NAME = 'episodes.sqlite3'
LEDGER_NAME = 'sessions.sqlite3'


# --- content addressing (independent re-statement of the sealed format) -------

def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def content_id(kind: str, value) -> str:
    return kind + ':sha256:' + hashlib.sha256(canonical(value)).hexdigest()


def binding_key(tenant: str, role: str, repo: str = REPO) -> str:
    return 'binding:' + hashlib.sha256('|'.join((tenant, role, repo)).encode()).hexdigest()


# --- frozen clock --------------------------------------------------------------

class FrozenDatetime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        value = FROZEN_AT if tz is None else FROZEN_AT.astimezone(tz)
        return value if tz is not None else value.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return FROZEN_AT.replace(tzinfo=None)


def _product_modules():
    import kp_agent_tooling._impl.service as service  # noqa: F401  (loads the package)
    import importlib
    import pkgutil
    for info in pkgutil.iter_modules(service.__path__, service.__name__ + '.'):
        try:
            importlib.import_module(info.name)
        except Exception:  # optional dependencies of unrelated modules
            pass
    for name in ('kp_agent_tooling.desk_cli', 'kp_agent_tooling.memory_cli'):
        try:
            importlib.import_module(name)
        except Exception:
            pass
    return [m for n, m in list(sys.modules.items())
            if m is not None and (n == 'kp_agent_tooling' or n.startswith('kp_agent_tooling.'))]


@contextmanager
def frozen_clock():
    """Replace ``datetime.datetime`` in every loaded product module.

    Claims, admission claims, import receipts and profile records all take
    their timestamps from ``datetime.now``; freezing it makes their IDs stable.
    """
    patched = []
    for module in _product_modules():
        for attribute in ('datetime',):
            value = getattr(module, attribute, None)
            if value is _dt.datetime:
                patched.append((module, attribute, value))
                setattr(module, attribute, FrozenDatetime)
            elif value is _dt:
                # ``import datetime`` style: patch through a proxy module object.
                proxy = type(sys)('datetime_t10_proxy')
                proxy.__dict__.update(_dt.__dict__)
                proxy.datetime = FrozenDatetime
                patched.append((module, attribute, value))
                setattr(module, attribute, proxy)
    try:
        yield
    finally:
        for module, attribute, value in patched:
            setattr(module, attribute, value)


# --- catalog, configuration and admission -------------------------------------

def private_write(path: Path, value) -> Path:
    path.write_text(json.dumps(value, indent=1, sort_keys=True))
    path.chmod(0o600)
    return path


def catalog_rows(desks):
    """``desks``: iterable of (tenant, role) in the imported catalog format."""
    rows = []
    for tenant, role in desks:
        rows.append({'binding_key': binding_key(tenant, role), 'tenant_id': tenant, 'role': role,
                     'repo_key': REPO, 'desk_label': role.capitalize() + ' desk',
                     'source': 'fixture:t10', 'memory_write_allowed': True})
    return rows


class World:
    """One state root, one imported catalog and several admitted sessions."""

    def __init__(self, root: Path, desks, sessions):
        """``desks``: list of (tenant, role). ``sessions``: {session_id: (tenant, role)}."""
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700, exist_ok=True)
        self.state.chmod(0o700)
        self.catalog = self.root / 'catalog.json'
        self.desks = list(desks)
        self.sessions = dict(sessions)
        self.write_catalog(self.desks)
        self.configs = {}
        for session in self.sessions:
            self.configs[session] = private_write(self.root / f'{session}.json', {
                'schema_version': 'ops.desk-memory.local.v1', 'state_root': str(self.state),
                'catalog_path': str(self.catalog), 'workspace_root': str(self.root),
                'provider_instance': INSTANCE, 'provider_session_id': session})

    # public operator steps
    def write_catalog(self, desks):
        private_write(self.catalog, {'schema_version': 'ops.imported-desk-catalog.v1',
                                     'approval_ref': 'fixture:t10-roster',
                                     'bindings': catalog_rows(desks)})

    def initialize(self):
        from kp_agent_tooling._impl.service.desk_memory_runtime import admit, initialize
        first = next(iter(self.configs.values()))
        initialize(first)
        for session, (tenant, role) in self.sessions.items():
            admit(self.configs[session], desk_id=binding_key(tenant, role),
                  provider_id='fixture', model_id='t10-model')
        return self

    def store(self, session=None):
        from kp_agent_tooling._impl.service.desk_memory_runtime import components
        config = self.configs[session or next(iter(self.configs))]
        return components(config)[3]

    def tools(self, session):
        from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools
        return EpisodicMemoryTools(self.store(session), session)

    def sources(self):
        from kp_agent_tooling._impl.service.session_sources import SessionSources
        return SessionSources(self.store())

    def index(self):
        from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
        store = self.store()
        return EpisodicSearchIndex(store.path.with_name(INDEX_NAME), episode_store=store)

    def binding(self, session):
        tenant, role = self.sessions[session]
        return binding_key(tenant, role)

    @property
    def store_path(self):
        return self.state / STORE_NAME

    @property
    def index_path(self):
        return self.state / INDEX_NAME

    @property
    def ledger_path(self):
        return self.state / LEDGER_NAME

    # snapshots of the SQLite state (quiescent copies)
    def snapshot(self, target: Path):
        target.mkdir(parents=True, exist_ok=True)
        for name in (STORE_NAME, INDEX_NAME, LEDGER_NAME):
            source = self.state / name
            if source.exists():
                with closing(sqlite3.connect(source)) as db, closing(sqlite3.connect(target / name)) as out:
                    db.backup(out)
            elif (target / name).exists():
                (target / name).unlink()
        shutil.copyfile(self.catalog, target / 'catalog.json')

    def restore(self, source: Path):
        for name in (STORE_NAME, INDEX_NAME, LEDGER_NAME):
            saved = source / name
            live = self.state / name
            if saved.exists():
                with closing(sqlite3.connect(saved)) as db, closing(sqlite3.connect(live)) as out:
                    db.backup(out)
                live.chmod(0o600)
            elif live.exists():
                live.unlink()
        shutil.copyfile(source / 'catalog.json', self.catalog)
        self.catalog.chmod(0o600)


# --- public write helpers ------------------------------------------------------

def events(*texts, prefix='e'):
    return [{'event_id': f'{prefix}{i}', 'role': 'user' if i % 2 == 0 else 'assistant', 'text': text}
            for i, text in enumerate(texts)]


def provenance(row_id, *, coordinates=None, include_coordinates=True):
    value = {'source_system': 'local:t10-transcript', 'row_id': row_id,
             'row_digest': hashlib.sha256(row_id.encode()).hexdigest(),
             'evidence_event_map': [], 'import_actor': 'operator:t10'}
    if include_coordinates:
        value['source_coordinates'] = coordinates
    return value


def legacy_provenance(row_id):
    return {'source_system': 'postgres:org_ops.kp_nodes', 'row_id': row_id,
            'row_digest': hashlib.sha256(row_id.encode()).hexdigest(), 'entity_type': 'Episode',
            'source_actor': 'agent:legacy', 'source_observed_at': '2026-08-01T00:00:00Z',
            'authored_at': '2026-08-01T00:00:00Z', 'legacy_host_session_id': 'legacy-host-' + row_id,
            'legacy_binding_provenance': 'fixture:t10', 'evidence_event_map': []}


def imported_events(*texts):
    return [{'event_id': f'import-{i:03d}', 'role': 'tool', 'text': text} for i, text in enumerate(texts)]


def claim_args(session_id, desk_or_object, *, predicate='session.owner', recorded_at='2026-09-01T00:00:00Z',
               **extra):
    obj = desk_or_object if isinstance(desk_or_object, dict) else {'kind': 'desk', 'id': desk_or_object}
    return dict(session_id=session_id, predicate=predicate, object=obj, asserted_by='operator:t10',
                recorded_at=recorded_at, evidence=['fixture:t10'], **extra)


def owner_claims(store_path: Path, session_id: str):
    """Sealed claims of one session read straight from the store (fixture use)."""
    with closing(sqlite3.connect(store_path)) as db:
        return [(row[0], json.loads(row[1])) for row in db.execute(
            'SELECT id,payload FROM session_claims WHERE session_id=? ORDER BY rowid', (session_id,))]


def linked_session(store_path: Path, episode_id: str):
    with closing(sqlite3.connect(store_path)) as db:
        row = db.execute('SELECT session_id FROM session_episodes WHERE episode_id=?', (episode_id,)).fetchone()
    return row[0] if row else None


# --- tool calls ------------------------------------------------------------------

def call(tools, name, arguments):
    """A tool result, or the public failure envelope for a refusal."""
    from kp_agent_tooling._impl.service.episodic_memory_tools import tool_failure
    try:
        return tools.call(name, arguments)
    except Exception as error:  # noqa: BLE001 - public envelope is the observable
        return {'error': tool_failure(name, error)}


def failure(name, error):
    from kp_agent_tooling._impl.service.episodic_memory_tools import tool_failure
    return tool_failure(name, error)


def jsonable(value):
    return json.loads(json.dumps(value, sort_keys=True, default=str))


# --- older-writer emulation (P6) -------------------------------------------------

def base_schema(path: Path):
    """Tables and their columns as recorded in ``sqlite_master`` (no internal tables)."""
    with closing(sqlite3.connect(path)) as db:
        tables = [row for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        indexes = [row for row in db.execute(
            "SELECT name,tbl_name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL ORDER BY name")]
        result = {'tables': {}, 'indexes': {}}
        for name, sql in tables:
            columns = [row[1] for row in db.execute(f'PRAGMA table_info("{name}")')]
            result['tables'][name] = {'sql': sql, 'columns': columns}
        for name, table, sql in indexes:
            result['indexes'][name] = {'table': table, 'sql': sql}
    return result


def strip_to_older_writer(source: Path, target: Path, schema):
    """Copy only the base schema's tables, columns and rowids into ``target``.

    The result is exactly what the writer at the order's base SHA leaves behind
    for the same sealed rows: no projection tables, no projection columns.
    Virtual tables are never part of the base episode store.
    """
    if target.exists():
        target.unlink()
    with closing(sqlite3.connect(target)) as out:
        out.execute('ATTACH DATABASE ? AS src', (str(source),))
        present = {row[0] for row in out.execute("SELECT name FROM src.sqlite_master WHERE type='table'")}
        for name, table in schema['tables'].items():
            out.execute(table['sql'])
            if name not in present:
                continue
            columns = ','.join(f'"{c}"' for c in table['columns'])
            out.execute(f'INSERT INTO main."{name}"(rowid,{columns}) SELECT rowid,{columns} FROM src."{name}" ORDER BY rowid')
        for index in schema['indexes'].values():
            if index['table'] in schema['tables']:
                out.execute(index['sql'])
        out.commit()
        out.execute('DETACH DATABASE src')
    target.chmod(0o600)
