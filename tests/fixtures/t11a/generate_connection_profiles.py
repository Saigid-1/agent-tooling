"""P4 golden: the connection profile of every product SQLite open (T11a order, P4, M2).

Generated at base by:

    python tests/fixtures/t11a/generate_connection_profiles.py

which rewrites tests/fixtures/t11a/connection_profiles.json (compared byte for byte by
tests/test_t11a_p4_connection_profiles.py) and connection_profile_sites.txt (which base
connect sites each step reached; documentation, not compared).

Instrument (M2): the T10 recorder realpaths the database argument and records no kwargs.
Each step also runs under the T10 recorder, and every connection's first top-level statement
(literal-free) is recorded beside its profile: an open that runs a statement of its own (an
implicit pragma) changes it (Verification U2).
This one wraps ``sqlite3.connect`` (on ``sqlite3``, ``sqlite3.dbapi2`` and any product module
attribute bound to it, as T10 does) and records, per call and in order: the raw ``database``
argument's type and text, and the effective ``uri``, ``timeout``, ``isolation_level``,
``check_same_thread``, ``detect_types`` and ``autocommit`` (positional or keyword, defaults
filled in). It passes neither ``factory`` nor ``cached_statements``.

The sites are driven through the public entry points that open those stores: the portable
desk-memory lifecycle (initialize, admit, capture, read_context), the write scope, the
consolidation queue, the search index, Claude/Codex/child capture ledgers, hook telemetry,
session import jobs, workspace capture, the spool cursors, the model-gateway budget ledger,
the knowledge lifecycle ledger, the reference manifest store, and a registry launch
(prepare and first hook). The portable state lives behind a symlinked directory, so a
URI built with ``.resolve()`` and one built without it read differently.

Normalised: the scratch root (``<root>``), its symlinked state directory (``<link>``) and the
directory it points at (``<real>``), each also in ``file:`` URI form.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import time
import traceback
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2]))  # tests/
sys.path.insert(0, str(HERE.parents[2] / 'launch'))

from t11a_golden import golden, patched, scratch, write_golden  # noqa: E402

# The core part runs with the core alone (the CI core job); the ops part needs extensions/ops.
GOLDENS = {'core': 'connection_profiles.json', 'ops': 'connection_profiles_ops.json'}
SITES = 'connection_profile_sites.txt'
SESSION = 'session-1'
NATIVE = 'a55869b4-6ab4-4e5c-b028-8d4b31d27b71'
CODEX_SESSION = '019a0000-0000-7000-8000-0000000c0dec'
PARAMETERS = ('database', 'timeout', 'detect_types', 'isolation_level', 'check_same_thread', 'factory',
              'cached_statements', 'uri', 'autocommit')
DEFAULTS = {'timeout': 5.0, 'detect_types': 0, 'isolation_level': '', 'check_same_thread': True, 'uri': False,
            'autocommit': getattr(sqlite3, 'LEGACY_TRANSACTION_CONTROL', -1)}
LEAF = 'kp_agent_tooling/_impl/leaf.py'


class Recorder:
    def __init__(self, normalise):
        self.normalise = normalise
        self.steps, self.sites, self.current = {}, {}, None
        self.connections, self.traced = [], None

    def record(self, args, kwargs):
        bound = dict(zip(PARAMETERS, args))
        bound.update(kwargs)
        database = bound['database']
        text = os.fspath(database) if not isinstance(database, (bytes, str)) else database
        if isinstance(text, bytes):
            text = os.fsdecode(text)
        profile = {'database': {'type': type(database).__name__, 'value': self.normalise(text)}}
        for name, default in DEFAULTS.items():
            value = bound.get(name, default)
            profile[name] = float(value) if name == 'timeout' else value
        extra = sorted(set(bound) - set(DEFAULTS) - {'database'})
        if extra:
            profile['other_arguments'] = extra
        self.steps[self.current].append(profile)
        self.sites.setdefault(self.current, []).append(self._site())

    @staticmethod
    def _site():
        for frame in reversed(traceback.extract_stack()[:-3]):
            name = frame.filename.replace(os.sep, '/')
            if ('/kp_agent_tooling' in name and not name.endswith(LEAF)) and '/tests/' not in name:
                short = name.split('/src/', 1)[-1]
                return f'{short}:{frame.lineno} {frame.name}'
        return '<outside the product>'

    @contextlib.contextmanager
    def active(self):
        real = sqlite3.connect

        def connect(*args, **kwargs):
            self.record(args, kwargs)
            start = len(self.traced.statements) if self.traced is not None else 0
            try:
                connection = real(*args, **kwargs)
            except BaseException:
                self.connections.append((None, start))
                raise
            self.connections.append((connection, start))  # held: no id reuse within the step
            return connection

        targets = [(sqlite3, 'connect'), (sqlite3.dbapi2, 'connect')]
        for name, module in list(sys.modules.items()):
            if module is not None and name.startswith('kp_agent_tooling') and getattr(module, 'connect', None) is real:
                targets.append((module, 'connect'))
        for target, attribute in targets:
            setattr(target, attribute, connect)
        try:
            yield
        finally:
            for target, attribute in targets:
                setattr(target, attribute, real)

    def step(self, name, call):
        """Run one step under the T10 recorder (tests/t10_instruments.py) as well: each connection's
        first top-level statement is recorded beside its profile, so a statement the open itself
        runs (an implicit pragma) reads as a difference (Verification U2)."""
        from t10_instruments import recording, strip_literals
        self.current = name
        self.steps[name] = []
        self.connections = []
        with recording() as traced:
            self.traced = traced
            with self.active():
                try:
                    call()
                except Exception as error:  # the step's own outcome is not the subject; keep it visible
                    self.steps[name].append({'step_error': f'{type(error).__name__}: {self.normalise(str(error))}'})
            self.traced = None
        profiles = [p for p in self.steps[name] if 'database' in p]
        for profile, (connection, start) in zip(profiles, self.connections):
            first = None if connection is None else next(
                (st for st in traced.statements[start:] if st.conn == id(connection) and not st.nested), None)
            profile['first_statement'] = None if first is None else ' '.join(strip_literals(first.sql).split())
        self.connections = []
        self.current = None


def normaliser(root, link, real):
    forms = []
    for path, token in ((real, '<real>'), (link, '<link>'), (root, '<root>')):
        forms += [(str(path), token), (path.as_uri(), 'file://' + token)]
    forms.sort(key=lambda pair: len(pair[0]), reverse=True)

    def normalise(text):
        for form, token in forms:
            text = text.replace(form, token)
        return text
    return normalise


def codex_line(row):
    return json.dumps(row, separators=(',', ':')).encode() + b'\n'


def desk_memory_steps(r, root, link):
    from test_portable_desk_memory import config, bind
    from kp_agent_tooling._impl.service.desk_memory_runtime import components, initialize, read_context
    from kp_agent_tooling._impl.service.desk_write_scope import record_scope, scope_enabled
    from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue
    from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
    from kp_agent_tooling._impl.service.claude_episode_capture import ClaudeEpisodeCapture
    from kp_agent_tooling._impl.service.claude_memory_hook import HookTelemetry
    from kp_agent_tooling._impl.service.launch_binding import RolloutCapture
    from kp_agent_tooling._impl.service.spool_ingest import ChildRolloutCapture, Cursors
    from kp_agent_tooling._impl.service.session_sources import SessionSources
    from kp_agent_tooling._impl.service.session_import_job import SessionImportJobs
    from kp_agent_tooling._impl.service import workspace_capture as wc

    first = config(link, SESSION, 'fixture')
    r.step('desk memory: initialize', lambda: initialize(first))
    r.step('desk memory: admit', lambda: bind(first, model='model-one'))
    _, _, ledger, store = components(first)
    events = [{'event_id': '1', 'role': 'user', 'text': 'Retain exact evidence é.'}]
    captured = {}
    r.step('episode store: capture', lambda: captured.update(
        store.capture(SESSION, source_ref='transcript:1', events=events)))
    r.step('episode store: read_event', lambda: store.read_event(
        SESSION, episode_id=captured['episode_id'], event_id='1'))
    r.step('desk memory: read_context', lambda: read_context(first))
    r.step('write scope: record and read', lambda: (
        record_scope(store, SESSION, enabled=True, approval_ref='approval:é'), scope_enabled(store, SESSION)))
    state = Path(json.loads(Path(first).read_text())['state_root'])
    queue = ConsolidationQueue(state / 'queue.sqlite3', store=store)
    r.step('queue: initialize', queue.initialize)
    r.step('queue: enqueue', lambda: queue.enqueue(SESSION, episode_ids=[captured['episode_id']], reason='batch'))
    index = EpisodicSearchIndex(store.path.with_name('episode-search.sqlite3'), episode_store=store)
    r.step('search index: rebuild', index.rebuild)
    r.step('search index: search', lambda: index.search(SESSION, query='retain evidence'))

    claude = ClaudeEpisodeCapture(state / 'capture.sqlite3')
    transcript = link / 'claude.jsonl'
    transcript.write_bytes(json.dumps({'type': 'user', 'sessionId': SESSION,
                                       'message': {'role': 'user', 'content': 'claude é'}}).encode() + b'\n')
    r.step('claude capture: initialize', claude.initialize)
    r.step('claude capture: capture', lambda: claude.capture(SESSION, store=store, queue=queue,
                                                            transcript_path=transcript))
    telemetry = HookTelemetry(state / 'telemetry.sqlite3')
    r.step('hook telemetry: initialize', telemetry.initialize)
    r.step('hook telemetry: record', lambda: telemetry.record(store, SESSION, {'hook_event_name': 'Stop'}))

    workspace = (root / 'workspace')
    workspace.mkdir()
    rollout = link / 'rollout.jsonl'
    rollout.write_bytes(codex_line({'type': 'session_meta', 'payload': {'id': SESSION, 'cwd': str(workspace)}}) +
                        codex_line({'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'codex é'}}))
    codex = RolloutCapture(state / 'rollout-capture.sqlite3')
    r.step('rollout capture: initialize', codex.initialize)
    r.step('rollout capture: capture', lambda: codex.capture(SESSION, store=store, queue=queue, transcript_path=rollout,
                                                             workspace=workspace))
    child = ChildRolloutCapture(state / 'child-capture.sqlite3', parent='019a0000-0000-7000-8000-00000000aaaa')
    r.step('child rollout capture: initialize', child.initialize)
    r.step('spool cursors: get', lambda: Cursors(state).get('launch-1'))

    r.step('session sources: upgrade', lambda: SessionSources(store).upgrade())
    tenant = store.sessions.resolve(SESSION, store.registry).tenant_id
    jobs = {}
    r.step('import jobs: construct', lambda: jobs.update(jobs=SessionImportJobs(store, tenant)))
    source = link / f'rollout-2026-09-24T12-00-00-{NATIVE}.jsonl'
    source.write_bytes(codex_line({'type': 'session_meta', 'timestamp': '2026-09-24T12:00:00Z',
                                   'payload': {'id': NATIVE, 'cwd': '/synthetic/repo'}}) +
                       codex_line({'type': 'event_msg', 'timestamp': '2026-09-24T12:01:00Z',
                                   'payload': {'type': 'user_message', 'message': 'import é'}}))
    request = {'schema_version': 'ops.session-import.request.v1', 'runtime': 'codex', 'source_file': str(source),
               'native_session_id': NATIVE, 'mode': 'full', 'follow': False, 'import_actor': 'operator:synthetic',
               'selected_desk_id': store._binding(SESSION), 'max_batch_bytes': 4_000_000}
    plan = {}
    r.step('import jobs: preview', lambda: plan.update(jobs['jobs'].preview(request)))
    r.step('import jobs: apply', lambda: jobs['jobs'].apply(plan['plan_token'], consent=True))
    r.step('import jobs: advance', lambda: jobs['jobs'].advance(plan['job_id']))
    r.step('import jobs: status', lambda: jobs['jobs'].status(plan['job_id']))

    native = {'claude': link / 'native-claude', 'codex': link / 'native-codex'}
    for folder in native.values():
        folder.mkdir()
    with patched(wc, 'validate_policy', lambda policy: {}):
        capture = wc.WorkspaceCapture(store, {'native_roots': {k: str(v) for k, v in native.items()}, 'repos': {},
                                             'max_candidates': 8, 'max_batch_bytes': 512_000,
                                             'max_batch_rows': 100, 'excluded_sessions': [], 'tenant_id': 'ténant'})
        r.step('workspace capture: preview before initialize', capture.preview)
        r.step('workspace capture: initialize', capture.initialize)
        r.step('workspace capture: preview', capture.preview)


def gateway_steps(r, link):
    from kp_agent_tooling._impl.service.model_gateway import Budget, BudgetLedger
    (link / 'gateway').mkdir()
    ledger = BudgetLedger(link / 'gateway')
    budget = Budget(max_calls_per_hour=10, max_usd_per_day=Decimal('1'), confirm_over_usd=None,
                    estimated_usd_per_call=Decimal('0.01'))
    now = 1_790_000_000.0
    r.step('budget ledger: state before any call', lambda: ledger.state('text', 'provider', 'model', budget, now))
    r.step('budget ledger: reserve', lambda: ledger.reserve(call_id='call-1', capability='text', provider='provider',
                                                           model='model', budget=budget, confirm=False,
                                                           digest='d' * 64, now=now))
    r.step('budget ledger: state', lambda: ledger.state('text', 'provider', 'model', budget, now))


def ops_steps(r, link):
    from kp_agent_tooling_ops._impl.service.knowledge_lifecycle import LifecycleLedger
    from kp_agent_tooling_ops._impl.code_references.manifest import SQLiteReferenceManifestStore
    ledgers = {}
    r.step('lifecycle ledger: initialize', lambda: ledgers.update(
        ledger=LifecycleLedger.initialize(link / 'lifecycle' / 'withdrawals.sqlite3')))
    r.step('lifecycle ledger: withdraw', lambda: ledgers['ledger'].withdraw('répo', 'docs/a.md', blob_sha='b' * 40))
    r.step('lifecycle ledger: permits', lambda: ledgers['ledger'].permits('répo', 'docs/a.md', 'b' * 40))

    def manifest():
        store = SQLiteReferenceManifestStore(link / 'references.sqlite3')
        close = getattr(store, 'close', None)
        if close:
            close()
        else:
            store._connection.close()
    r.step('reference manifest: open', manifest)


def launch_steps(r, root):
    from t3_harness import World, append_rows, codex_rollout, codex_rows
    from kp_agent_tooling._impl.service import launch_binding
    from kp_agent_tooling._impl.service.desk_memory_runtime import components
    from kp_agent_tooling._impl.service.session_bindings import list_records
    world = World.create(root / 'world')
    desk = world.save_desk()
    prepared = {}
    with environment(HOME=str(world.root / 'home'), CLAUDE_CONFIG_DIR=None, CODEX_HOME=None):
        r.step('launch: prepare codex', lambda: prepared.update(codex=launch_binding.prepare(
            world.operator, world.request(harness='codex', desk_id=desk, provider='openai', model='fixture-model',
                                          task_id='task-p4'))))
        rollout = append_rows(codex_rollout(world.codex_root, CODEX_SESSION),
                              codex_rows(CODEX_SESSION, world.workspace, 'hello é', 'answer'))
        payload = {'session_id': CODEX_SESSION, 'transcript_path': str(rollout), 'cwd': str(world.workspace),
                   'hook_event_name': 'Stop', 'model': 'fixture-model', 'turn_id': 'turn-1',
                   'stop_hook_active': False}
        r.step('launch: first codex hook', lambda: launch_binding.hook(prepared['codex']['receipt_path'], payload))
        r.step('launch: prepare claude', lambda: prepared.update(claude=launch_binding.prepare(
            world.operator, world.request(harness='claude', desk_id=desk, task_id='task-p4-claude'))))
        r.step('session bindings: list', lambda: list_records(components(world.operator)[2]))


@contextlib.contextmanager
def environment(**values):
    """Set (or, with None, unset) environment variables for the duration; always restored."""
    saved = {name: os.environ.get(name) for name in values}
    try:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def build(part='core'):
    with scratch('t11a-p4-') as root:
        real = root / 'real'
        real.mkdir()
        link = root / 'link'
        link.symlink_to(real, target_is_directory=True)
        recorder = Recorder(normaliser(root, link, real))
        if part == 'core':
            desk_memory_steps(recorder, root, link)
            gateway_steps(recorder, link)
            launch_steps(recorder, root)
        else:
            ops_steps(recorder, link)
        profiles, sites = recorder.steps, recorder.sites
    return golden({'steps': profiles}), sites


def main():
    sites = {}
    for part, name in GOLDENS.items():
        text, found = build(part)
        write_golden(text, name)
        sites.update(found)
        print(f'wrote tests/fixtures/t11a/{name} ({len(text)} bytes)')
    reached = sorted({s for step in sites.values() for s in step})
    lines = ['# Base connect sites each P4 step reached (first product frame below the leaf).',
             '# Generated with the golden; documentation only, not compared.', '']
    for step, rows in sites.items():
        lines.append(step)
        lines += [f'    {row}' for row in rows]
    lines += ['', f'# distinct sites: {len(reached)}'] + [f'#   {s}' for s in reached]
    (HERE.parent / SITES).write_text('\n'.join(lines) + '\n')
    print(f'{len(reached)} distinct connect sites')


if __name__ == '__main__':
    main()
