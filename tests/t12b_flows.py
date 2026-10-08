"""T12b in-process flows: the sealing flows that index inline at base, each set up in its own root.

Every flow is driven through the public entry point its role or host CLI uses (the same drivers as the T11a P4
golden generator and the existing reliers): the library seals (capture, register, import, claim, link, operator
import), workspace capture `once` (the capture role's command, W5), a session-import job's `advance` (the
board's `kp-agent-session-import`, W6), a registry launch's Codex Stop hook (`kp-agent-launch hook`, W1(b)/W2),
`kp-agent-desk ... index-history` (W9) and `upgrade-sources` (W10).

Each setup returns a Flow: `run()` performs the flow once; `store`, `store_path` and `index_path` name its
state. The index exists before `run()` (created empty), so a flow that writes only an existing index (W4, W10)
is exercised too.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import t12b_seams as seams

HERE = Path(__file__).resolve().parent
if str(HERE / 'launch') not in sys.path:
    sys.path.insert(0, str(HERE / 'launch'))


@dataclass
class Flow:
    run: Callable
    store: object
    extra: dict = field(default_factory=dict)

    @property
    def store_path(self) -> Path:
        return Path(os.fspath(self.store.path))

    @property
    def index_path(self) -> Path:
        return seams.index_path_of(self.store)


@contextlib.contextmanager
def environment(**values):
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


def ensure_index(store) -> None:
    """An empty index beside the store (the operator's `initialize`), when none exists yet."""
    if seams.index_path_of(store).exists():
        return
    from kp_agent_tooling._impl import leaf
    from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
    index = EpisodicSearchIndex(leaf.store_path(store.path, leaf.SEARCH_INDEX_DB, sibling=True), episode_store=store)
    if hasattr(index, 'initialize'):
        index.initialize()
    else:
        seams.drain(store)


# ------------------------------------------------------------------------------------------- the flows

def library_seals(root: Path) -> Flow:
    import t10_corpus as corpus
    import t10_world as w
    world = w.World(root, corpus.DESKS, corpus.SESSIONS)
    world.initialize()
    store = world.store()
    ensure_index(store)

    def run():
        sources = world.sources()
        captured = world.store().capture(corpus.A, source_ref='t12b:flow:capture', events=w.events('Juniper flow.'))
        session = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='t12b-flow')
        sources.import_episode(session_id=session, source_ref='t12b:flow:import', events=w.events('Juniper import.'),
                               provenance=w.provenance('t12b-flow-import'))
        sources.claim(**w.claim_args(session, corpus.ALPHA))
        legacy = world.store().import_operator_episode(
            tenant_id=w.TENANT_ONE, role='alpha', repo_key=w.REPO, source_ref='t12b:flow:legacy',
            events=w.imported_events('Juniper legacy flow.'), source_provenance=w.legacy_provenance('t12b-flow'))
        sources.link(episode_id=legacy['episode_id'], session_id=session)
        return captured
    return Flow(run, store, {'world': world})


def _git(*args):
    subprocess.run(['git', *map(str, args)], check=True, capture_output=True)


def workspace_capture_once(root: Path) -> Flow:
    from test_portable_desk_memory import config, episode_store
    from test_workspace_capture_adversarial import NATIVE_INCLUDED, _repo, _rollout, _worker
    from kp_agent_tooling.workspace_capture_cli import main as capture_cli
    root.mkdir(parents=True)
    store = episode_store(root)
    ensure_index(store)
    main = _repo(root / 'main')
    codex_root = root / 'codex'
    _rollout(codex_root, NATIVE_INCLUDED, main, 'selected workspace juniper')
    worker = _worker(root, store, main, codex_root)
    policy = root / 'capture-policy.json'
    policy.write_text(json.dumps(worker.policy))
    policy.chmod(0o600)
    session_config = config(root, session='session-1', instance='fixture')

    def run():
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = capture_cli(['--config', str(session_config), '--policy', str(policy), 'once'])
        assert code == 0, out.getvalue()
        return json.loads(out.getvalue())
    return Flow(run, store)


NATIVE = 'a55869b4-6ab4-4e5c-b028-8d4b31d27b71'


def _codex_line(row):
    return json.dumps(row, separators=(',', ':')).encode() + b'\n'


def session_import_advance(root: Path) -> Flow:
    from test_portable_desk_memory import bind, config
    from kp_agent_tooling._impl.service.desk_memory_runtime import components, initialize
    from kp_agent_tooling._impl.service.session_import_job import SessionImportJobs
    from kp_agent_tooling._impl.service.session_sources import SessionSources
    root.mkdir(parents=True)
    first = config(root, 'session-1', 'fixture')
    initialize(first)
    bind(first, model='model-one')
    store = components(first)[3]
    SessionSources(store).upgrade()
    ensure_index(store)
    tenant = store.sessions.resolve('session-1', store.registry).tenant_id
    jobs = SessionImportJobs(store, tenant)
    source = root / f'rollout-2026-09-24T12-00-00-{NATIVE}.jsonl'
    source.write_bytes(_codex_line({'type': 'session_meta', 'timestamp': '2026-09-24T12:00:00Z',
                                    'payload': {'id': NATIVE, 'cwd': '/synthetic/repo'}}) +
                       _codex_line({'type': 'event_msg', 'timestamp': '2026-09-24T12:01:00Z',
                                    'payload': {'type': 'user_message', 'message': 'import juniper'}}))
    request = {'schema_version': 'ops.session-import.request.v1', 'runtime': 'codex', 'source_file': str(source),
               'native_session_id': NATIVE, 'mode': 'full', 'follow': False, 'import_actor': 'operator:synthetic',
               'selected_desk_id': store._binding('session-1'), 'max_batch_bytes': 4_000_000}
    plan = jobs.preview(request)
    jobs.apply(plan['plan_token'], consent=True)

    def run():
        return jobs.advance(plan['job_id'])
    return Flow(run, store)


CODEX_SESSION = '019a0000-0000-7000-8000-0000000c0dec'


def codex_launch_hook(root: Path) -> Flow:
    from t3_harness import World, append_rows, codex_rollout, codex_rows
    from kp_agent_tooling._impl.service import launch_binding
    from kp_agent_tooling._impl.service.desk_memory_runtime import components
    world = World.create(root)
    desk = world.save_desk()
    env = dict(HOME=str(world.root / 'home'), CLAUDE_CONFIG_DIR=None, CODEX_HOME=None)
    with environment(**env):
        prepared = launch_binding.prepare(world.operator, world.request(
            harness='codex', desk_id=desk, provider='openai', model='fixture-model', task_id='task-t12b'))
    store = components(world.operator)[3]
    ensure_index(store)
    rollout = append_rows(codex_rollout(world.codex_root, CODEX_SESSION),
                          codex_rows(CODEX_SESSION, world.workspace, 'hello juniper', 'answer'))
    payload = {'session_id': CODEX_SESSION, 'transcript_path': str(rollout), 'cwd': str(world.workspace),
               'hook_event_name': 'Stop', 'model': 'fixture-model', 'turn_id': 'turn-1', 'stop_hook_active': False}

    def run():
        with environment(**env):
            return launch_binding.hook(prepared['receipt_path'], payload)
    return Flow(run, store, {'world': world})


def _desk_cli(config, action):
    from kp_agent_tooling import desk_cli
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
        code = desk_cli.main(['--config', str(config), action])
    assert code == 0, f'{action}: exit {code}: {out.getvalue()} {err.getvalue()}'
    return json.loads(out.getvalue())


def index_history(root: Path) -> Flow:
    flow = library_seals(root)
    flow.run()
    world = flow.extra['world']
    config = next(iter(world.configs.values()))
    return Flow(lambda: _desk_cli(config, 'index-history'), flow.store, flow.extra)


def upgrade_sources(root: Path) -> Flow:
    flow = library_seals(root)
    flow.run()
    world = flow.extra['world']
    config = next(iter(world.configs.values()))
    return Flow(lambda: _desk_cli(config, 'upgrade-sources'), flow.store, flow.extra)


SETUPS = {
    'library seals': library_seals,
    'workspace capture once': workspace_capture_once,
    'session import advance': session_import_advance,
    'codex launch hook': codex_launch_hook,
    'index-history': index_history,
    'upgrade-sources': upgrade_sources,
}
