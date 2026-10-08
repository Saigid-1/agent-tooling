"""T12b role probe: runs INSIDE a role container (`docker exec -i <cid> python3 - <phase> <directory> ...`).

Standard library and the installed product only (no test module exists in the image). Phases:

- `setup DIR`: a desk-memory state root in DIR (under /state/memory): an imported catalog, two admitted sessions,
  one capture, and an empty search index beside the store (the operator's `initialize`). Test setup, unrecorded.
- `flows DIR`: RECORDED. The sealing flows a role runs, through the product's public entry points: capture, register,
  import, claim, link, the operator import, and a session-import job (preview, apply, advance; the board's
  `kp-agent-session-import`). Operator verbs (`index-history`, `upgrade-sources`) are not sealers and are not run.
  Reports every connect to (and Python open of) the index or a build file beside it, and every statement that
  deletes outbox rows.
- `drain DIR`: RECORDED. The product's `drain(store, index, lease, batch)` (episodic_search), called as
  tests/t12b_seams.py calls it, then reports as `flows` does (the indexer's positive control).
- `seal DIR COUNT`: COUNT operator imports (one outbox row each), for a backlog or a steady inflow.
- `lag DIR`: the outbox row count and the indexed episode count, read-only.

Every phase prints one JSON object as its last line.
"""
import json
import os
import re
import sqlite3
import sys
import time
import traceback
from contextlib import closing
from pathlib import Path

OUTBOX = 'index_outbox'
INDEX = 'episode-search.sqlite3'
LEASE = 'index.lock'
BASE_LEAF_SQLITE = {'episodes.sqlite3', 'sessions.sqlite3', 'episode-search.sqlite3', 'workspace-capture.sqlite3',
                    'session-import-jobs.sqlite3', 'spool-ingest.sqlite3', 'capture.sqlite3', 'queue.sqlite3',
                    'telemetry.sqlite3', 'rollout-capture.sqlite3', 'ledger.sqlite3', 'references.sqlite3'}
TENANT, REPO = 't12b-probe-tenant', 'repo-probe'
SESSIONS = {'probe-a': 'alpha', 'probe-b': 'beta'}
NATIVE = 'b6dd2a54-1d1f-4f5c-9c2e-0c0ffee12b00'
REAL_CONNECT = sqlite3.connect


def binding_key(role):
    import hashlib
    return 'binding:' + hashlib.sha256('|'.join((TENANT, role, REPO)).encode()).hexdigest()


def private(path, value):
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write(json.dumps(value))
    return path


def config(directory, session):
    return directory / f'{session}.json'


def index_family(name):
    name = Path(str(name)).name
    if name == INDEX or name.startswith(INDEX.split('.')[0]):
        return True
    try:
        from kp_agent_tooling._impl import leaf
    except ImportError:
        return False
    return name in {v for k, v in vars(leaf).items()
                    if k.isupper() and isinstance(v, str) and v.endswith('.sqlite3') and v not in BASE_LEAF_SQLITE}


def normalize(database):
    database = os.fsdecode(database) if isinstance(database, bytes) else os.fspath(database)
    if database.startswith('file:'):
        from urllib.parse import unquote
        database = unquote(database[5:].split('?', 1)[0])
    return os.path.realpath(database) if database not in ('', ':memory:') else database


class Record:
    def __init__(self, directory):
        self.directory = os.path.realpath(directory)
        self.store = os.path.join(self.directory, 'state', 'episodes.sqlite3')
        self.index_opens, self.outbox_deletes, self.store_connects, self.index_statements = [], [], 0, 0
        self.active = False

    def audit(self, event, args):
        if not self.active:
            return
        if event == 'sqlite3.connect':
            path = normalize(args[0])
            if path.startswith(self.directory) and index_family(path):
                self.index_opens.append(['sqlite3.connect', path])
            if path == self.store:
                self.store_connects += 1
        elif event == 'open' and args and isinstance(args[0], (str, bytes, os.PathLike)):
            path = os.path.realpath(os.fsdecode(os.fspath(args[0])))
            if path.startswith(self.directory) and index_family(path) and not path.endswith(('-journal', '-wal', '-shm')):
                self.index_opens.append(['open', path])

    def connect(self, database, *args, **kwargs):
        connection = REAL_CONNECT(database, *args, **kwargs)
        path = normalize(database)

        def trace(sql):
            if not self.active:
                return
            if index_family(path):
                self.index_statements += 1
            if path == self.store and re.match(r'\s*DELETE\s+FROM\s+"?' + OUTBOX, sql, re.I):
                self.outbox_deletes.append(sql[:200])
        connection.set_trace_callback(trace)
        return connection


def instrumented(record, call):
    sys.addaudithook(record.audit)
    patched = [(sqlite3, 'connect')]
    for name, module in list(sys.modules.items()):
        if module is not None and name.startswith('kp_agent_tooling') and getattr(module, 'connect', None) is REAL_CONNECT:
            patched.append((module, 'connect'))
    for module, attribute in patched:
        setattr(module, attribute, record.connect)
    record.active = True
    try:
        return call()
    finally:
        record.active = False
        for module, attribute in patched:
            setattr(module, attribute, REAL_CONNECT)


def counts(directory):
    store = Path(directory) / 'state' / 'episodes.sqlite3'
    index = Path(directory) / 'state' / INDEX
    result = {'outbox': None, 'indexed': None, 'sealed': None}
    with closing(REAL_CONNECT(store.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result['outbox'] = db.execute(f'SELECT count(*) FROM {OUTBOX}').fetchone()[0] if OUTBOX in names else None
        result['sealed'] = db.execute('SELECT (SELECT count(*) FROM episodes) + (SELECT count(*) FROM source_episodes)'
                                      ).fetchone()[0]
    if index.exists():
        with closing(REAL_CONNECT(index.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            result['indexed'] = db.execute('SELECT count(*) FROM indexed_episodes').fetchone()[0]
    return result


def store_of(directory):
    from kp_agent_tooling._impl.service.desk_memory_runtime import components
    return components(config(directory, 'probe-a'))[3]


def setup(directory):
    from kp_agent_tooling._impl.service.desk_memory_runtime import admit, initialize
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / 'state').mkdir(mode=0o700, exist_ok=True)
    rows = [{'binding_key': binding_key(role), 'tenant_id': TENANT, 'role': role, 'repo_key': REPO,
             'desk_label': role.capitalize() + ' desk', 'source': 'fixture:t12b', 'memory_write_allowed': True}
            for role in ('alpha', 'beta')]
    catalog = private(directory / 'catalog.json', {'schema_version': 'ops.imported-desk-catalog.v1',
                                                   'approval_ref': 'fixture:t12b', 'bindings': rows})
    for session in SESSIONS:
        private(config(directory, session), {
            'schema_version': 'ops.desk-memory.local.v1', 'state_root': str(directory / 'state'),
            'catalog_path': str(catalog), 'workspace_root': str(directory), 'provider_instance': 't12b',
            'provider_session_id': session})
    initialize(config(directory, 'probe-a'))
    for session, role in SESSIONS.items():
        admit(config(directory, session), desk_id=binding_key(role), provider_id='fixture', model_id='t12b-model')
    store = store_of(directory)
    store.capture('probe-a', source_ref='t12b:probe:setup', events=[{'event_id': 'e0', 'role': 'user',
                                                                     'text': 'Juniper probe setup.'}])
    from kp_agent_tooling._impl import leaf
    from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
    index = EpisodicSearchIndex(leaf.store_path(store.path, leaf.SEARCH_INDEX_DB, sibling=True), episode_store=store)
    if not index.path.exists():
        index.initialize()
    return {'phase': 'setup', **counts(directory)}


def _line(row):
    return (json.dumps(row, separators=(',', ':')) + '\n').encode()


def flows(directory):
    from kp_agent_tooling._impl.service.session_import_job import SessionImportJobs
    from kp_agent_tooling._impl.service.session_sources import SessionSources
    tag = str(time.time_ns())
    errors = []
    record = Record(directory)

    def run():
        store = store_of(directory)
        sources = SessionSources(store)
        events = lambda text: [{'event_id': 'e0', 'role': 'user', 'text': text}]  # noqa: E731
        store.capture('probe-a', source_ref=f't12b:probe:{tag}:capture', events=events('Juniper probe capture.'))
        session = sources.register(tenant_id=TENANT, runtime='claude', native_id=f'probe-{tag}')
        sources.import_episode(session_id=session, source_ref=f't12b:probe:{tag}:import',
                               events=events('Juniper probe import.'),
                               provenance={'source_system': 'local:t12b', 'row_id': tag, 'row_digest': 'd' * 64,
                                           'evidence_event_map': [], 'import_actor': 'operator:t12b'})
        sources.claim(session_id=session, predicate='session.owner', object={'kind': 'desk', 'id': binding_key('alpha')},
                      asserted_by='operator:t12b', recorded_at='2026-09-01T00:00:00Z', evidence=['fixture:t12b'])
        legacy = store.import_operator_episode(
            tenant_id=TENANT, role='alpha', repo_key=REPO, source_ref=f't12b:probe:{tag}:legacy',
            events=[{'event_id': 'import-000', 'role': 'tool', 'text': 'Juniper probe legacy.'}],
            source_provenance={'source_system': 'postgres:org_ops.kp_nodes', 'row_id': tag, 'row_digest': 'e' * 64,
                               'entity_type': 'Episode', 'source_actor': 'agent:legacy',
                               'source_observed_at': '2026-08-01T00:00:00Z', 'authored_at': '2026-08-01T00:00:00Z',
                               'legacy_host_session_id': 'legacy-' + tag, 'legacy_binding_provenance': 'fixture:t12b',
                               'evidence_event_map': []})
        sources.link(episode_id=legacy['episode_id'], session_id=session)
        try:
            jobs = SessionImportJobs(store, TENANT)
            source = directory / f'rollout-2026-09-24T12-00-00-{NATIVE[:-4]}{tag[-4:]}.jsonl'
            native = source.name[len('rollout-2026-09-24T12-00-00-'):-len('.jsonl')]
            source.write_bytes(_line({'type': 'session_meta', 'timestamp': '2026-09-24T12:00:00Z',
                                      'payload': {'id': native, 'cwd': '/synthetic/repo'}}) +
                               _line({'type': 'event_msg', 'timestamp': '2026-09-24T12:01:00Z',
                                      'payload': {'type': 'user_message', 'message': 'import juniper ' + tag}}))
            plan = jobs.preview({'schema_version': 'ops.session-import.request.v1', 'runtime': 'codex',
                                 'source_file': str(source), 'native_session_id': native, 'mode': 'full',
                                 'follow': False, 'import_actor': 'operator:t12b',
                                 'selected_desk_id': store._binding('probe-a'), 'max_batch_bytes': 4_000_000})
            jobs.apply(plan['plan_token'], consent=True)
            jobs.advance(plan['job_id'])
        except Exception as error:  # noqa: BLE001 - reported
            errors.append('session import: ' + repr(error)[:300])

    before = counts(directory)
    instrumented(record, run)
    after = counts(directory)
    return {'phase': 'flows', 'index_opens': record.index_opens, 'outbox_deletes': record.outbox_deletes,
            'store_connects': record.store_connects, 'index_statements': record.index_statements,
            'before': before, 'after': after, 'errors': errors}


def drain(directory, batch=500):
    import importlib
    record = Record(directory)
    module = importlib.import_module('kp_agent_tooling._impl.service.episodic_search')
    product = getattr(module, 'drain', None)
    if not callable(product):
        return {'phase': 'drain', 'error': 'no drain in kp_agent_tooling._impl.service.episodic_search'}

    class IndexArgument(module.EpisodicSearchIndex):
        def __fspath__(self):
            return os.fspath(self.path)

    def run():
        from kp_agent_tooling._impl import leaf
        store = store_of(directory)
        index = IndexArgument(leaf.store_path(store.path, leaf.SEARCH_INDEX_DB, sibling=True), episode_store=store)
        values = []
        for _ in range(20):
            values.append(repr(product(store, index, leaf.store_path(store.path, LEASE, sibling=True), batch))[:300])
            if not counts(directory)['outbox']:
                break
        return values
    before = counts(directory)
    values = instrumented(record, run)
    after = counts(directory)
    return {'phase': 'drain', 'index_opens': record.index_opens, 'outbox_deletes': record.outbox_deletes,
            'store_connects': record.store_connects, 'index_statements': record.index_statements,
            'before': before, 'after': after, 'values': values}


def seal(directory, count):
    store = store_of(directory)
    tag = str(time.time_ns())
    for number in range(count):
        store.import_operator_episode(
            tenant_id=TENANT, role='alpha', repo_key=REPO, source_ref=f't12b:seal:{tag}:{number}',
            events=[{'event_id': 'import-000', 'role': 'tool', 'text': f'Juniper backlog {tag} {number}.'}],
            source_provenance={'source_system': 'postgres:org_ops.kp_nodes', 'row_id': f'{tag}-{number}',
                               'row_digest': 'f' * 64, 'entity_type': 'Episode', 'source_actor': 'agent:legacy',
                               'source_observed_at': '2026-08-01T00:00:00Z', 'authored_at': '2026-08-01T00:00:00Z',
                               'legacy_host_session_id': 'legacy-' + tag, 'legacy_binding_provenance': 'fixture:t12b',
                               'evidence_event_map': []})
    return {'phase': 'seal', 'added': count, **counts(directory)}


def main(argv):
    phase, directory = argv[0], Path(argv[1])
    try:
        if phase == 'setup':
            result = setup(directory)
        elif phase == 'flows':
            result = flows(directory)
        elif phase == 'drain':
            result = drain(directory, int(argv[2]) if len(argv) > 2 else 500)
        elif phase == 'seal':
            result = seal(directory, int(argv[2]))
        elif phase == 'lag':
            result = {'phase': 'lag', **counts(directory)}
        else:
            result = {'error': f'unknown phase {phase}'}
    except BaseException as error:  # noqa: BLE001 - the outcome is the observation
        result = {'phase': phase, 'error': repr(error)[:500], 'traceback': traceback.format_exc()[-3000:]}
    result['volume'] = os.environ.get('AGENT_MEMORY_VOLUME')
    print(json.dumps(result))
    return 0 if 'error' not in result else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
