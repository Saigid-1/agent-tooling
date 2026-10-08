"""T11b Q1 (image-marked): in the Docker runtime, store paths outside the volume are refused at open.

Order: docs/work/orders/T11b-leaf-behaviour.md, Q1. "In the Docker runtime, store paths outside the
volume are refused, for reads and writes." The runtime is recognised by AGENT_MEMORY_VOLUME, which
every role receives from Compose `x-environment` and `docker exec`/`docker compose run` inherit. A
store path is derived from `state_root` (or `roster_path`, or a `/state` `config_template`); the
generic helpers refuse a marked path that does not resolve under /state/memory with
`store_outside_volume`, naming the path. Reads are refused as well as writes (N1). Unmarked paths
(model_gateway's ledger, `/state/refresh`, ...) are unaffected.

Falsifiers, each run inside the container under tests/t11b_audit.py's `sys.addaudithook` on `open`,
`os.mkdir` and `sqlite3.connect`, wrapped around the console script's own `main` through `python3 -c`:
- (a) `docker exec` of `kp-agent-desk` with a configuration whose `state_root` is outside
  /state/memory: `initialize` on an empty one (a write) and `context` on a populated one (a read);
- (b) the same through `docker compose run --rm -T tooling`;
- (c) `docker exec` of `kp-agent-launch --config <registry config> prepare` with an outside
  `state_root`: no directory is created.
In every refused case no store file is opened, for read or for write, and none is created.
Positive controls (N2), same wrapper:
- (d) model_gateway's ledger (`<artifact_root>/.model-gateway/ledger.sqlite3`, artifact_root
  /state/model-artifacts as docs/MODEL-GATEWAY.md names it) and a `/state/refresh` publication
  (`refresh_cli.atomic`) still open and write;
- (e) a `docker exec` that reads `/config/launch/desks.json` (`kp-agent-desk-registry --config
  /config/launch/registry.json list`, docs/DOCKER.md first run step 3) still works.
(f), a host install without the variable, is in-process: tests/test_t11b_q1_host.py.

Setup: one prepared project (plan, apply, prepare; `tooling` up). The outside state roots are
directories on the /state bind (`/state/t11b-q1/<case>`, 0700, the container user's), the operator
test files beside them (not under /config: the role preflight reads /config at start, and the order's
case is a tool started with `docker exec`, "written after start"). A populated outside root is a
`cp -a` of a store first created inside the volume (initialize, admit; the registry: initialize, one
desk) by the same console scripts.

Readings (repeated under AMBIGUITY): "refused" is a non-zero exit with no Python traceback whose
output contains `store_outside_volume` and the state_root; "a store file" is any path below the
state_root; "no directory is created" is no `os.mkdir` below the state_root and an unchanged tree.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid

import pytest

from t11b_audit import opened, path_of
from t11b_harness import (STORE, host_state, prepared_project, refusal_problems, shown, write_operator_json)

pytestmark = pytest.mark.image

TENANT = 't11b-q1'
BINDING = 'binding:' + hashlib.sha256(f'{TENANT}|implementation|repo'.encode()).hexdigest()
WORK = '/state/t11b-q1'
SOURCE_STORE = f'{STORE}/t11b-q1-source'
REGISTRY_STATE = f'{STORE}/registry'
OUTSIDE = {'a-initialize': 'empty', 'a-context': 'populated', 'b-initialize': 'empty', 'b-context': 'populated',
           'c-prepare': 'registry'}
DESK = 'desk:' + str(uuid.UUID(int=0x11b0_0000_0000_4000_8000_0000_0000_0001))


def _desk_config(state_root: str) -> dict:
    return {'schema_version': 'ops.desk-memory.local.v1', 'state_root': state_root,
            'catalog_path': f'{WORK}/catalog.json', 'workspace_root': f'{WORK}/workspace',
            'provider_instance': 't11b-q1', 'provider_session_id': 'session-q1'}


def _registry_config(state_root: str, catalog: str = '/config/launch/desks.json') -> dict:
    return {'schema_version': 'ops.desk-memory.local.v1', 'state_root': state_root, 'catalog_path': catalog,
            'workspace_root': f'{WORK}/workspace', 'provider_instance': 'agent-tooling',
            'provider_session_id': 'registry-operator'}


@pytest.fixture(scope='module')
def q1():
    world, project = prepared_project('q1')
    try:
        tooling = project.tooling
        work = host_state(world, 't11b-q1')
        work.mkdir(mode=0o700)
        (work / 'workspace').mkdir(mode=0o700)
        for name in OUTSIDE:
            (work / name).mkdir(mode=0o700)
        write_operator_json(work / 'catalog.json', {
            'schema_version': 'ops.imported-desk-catalog.v1', 'approval_ref': 'operator:t11b',
            'bindings': [{'binding_key': BINDING, 'tenant_id': TENANT, 'role': 'implementation', 'repo_key': 'repo',
                          'desk_label': 'T11b desk', 'source': 'operator:t11b', 'memory_write_allowed': True}]})
        write_operator_json(work / 'source.json', _desk_config(SOURCE_STORE))
        # docs/DOCKER.md first run, step 3, without harness_profiles_path (the packaged profiles: claude and
        # codex enabled), so that prepare reaches its launches/ directory at base.
        write_operator_json(world.root / 'config' / 'launch' / 'desks.json', {
            'schema_version': 'agent-tooling.desk-registry.v1', 'tenant_id': TENANT,
            'roster_path': f'{REGISTRY_STATE}/roles.json'})
        write_operator_json(world.root / 'config' / 'launch' / 'registry.json', _registry_config(REGISTRY_STATE))
        project.run_ok(tooling, 'sh', '-c', f'mkdir -m 700 {SOURCE_STORE} {REGISTRY_STATE}')
        project.run_ok(tooling, 'kp-agent-desk', '--config', f'{WORK}/source.json', 'initialize')
        project.run_ok(tooling, 'kp-agent-desk', '--config', f'{WORK}/source.json', 'admit', '--desk-id', BINDING,
                       '--provider-id', 'test', '--model-id', 'model-q1')
        registry = ['kp-agent-desk-registry', '--config', '/config/launch/registry.json']
        project.run_ok(tooling, *registry, 'initialize')
        project.run_ok(tooling, *registry, 'save', stdin=json.dumps({
            'desk_id': DESK, 'name': 'Product', 'description': 'T11b Q1 desk.', 'role': 'general',
            'repos': ['product'], 'capture': True, 'memory_write': True, 'expected_version': 0}))
        for name, kind in OUTSIDE.items():
            source = {'populated': SOURCE_STORE, 'registry': REGISTRY_STATE}.get(kind)
            if source:
                project.run_ok(tooling, 'cp', '-a', f'{source}/.', f'{WORK}/{name}/')
            config = _registry_config(f'{WORK}/{name}') if kind == 'registry' else _desk_config(f'{WORK}/{name}')
            write_operator_json(work / f'{name}.json', config)
        yield world, project
    finally:
        remaining = project.down()
        shutil.rmtree(world.base, ignore_errors=True)
    assert not remaining, f'left containers behind: {remaining}'


def _refused(project, run, root: str, what: str):
    before = project.listing(project.tooling, root)
    assert before['exists'], f'precondition: {root} exists'
    proc, record = run()
    after = project.listing(project.tooling, root)
    problems = refusal_problems(proc, record, root, before, after)
    assert not problems, f'{what}: not refused as Q1 states:\n- ' + '\n- '.join(problems) + '\n' + shown(proc, record)


@pytest.mark.parametrize('action', ['initialize', 'context'])
def test_q1a_docker_exec_kp_agent_desk_refuses_a_state_root_outside_the_volume(q1, action):
    """GREEN-IF `docker exec <tooling> kp-agent-desk --config <cfg> <action>` (state_root /state/t11b-q1/a-<action>,
    empty for initialize, a populated store for context) exits non-zero naming `store_outside_volume` and the
    state_root, with no open, SQLite connect or mkdir of any path below the state_root, and its tree unchanged."""
    world, project = q1
    root = f'{WORK}/a-{action}'
    _refused(project, lambda: project.audited_exec(project.tooling, 'kp_agent_tooling.desk_cli:main', 'kp-agent-desk',
                                                   ['--config', f'{root}.json', action]),
             root, f'docker exec kp-agent-desk {action}')


@pytest.mark.parametrize('action', ['initialize', 'context'])
def test_q1b_compose_run_tooling_kp_agent_desk_refuses_a_state_root_outside_the_volume(q1, action):
    """GREEN-IF `docker compose --project-directory "$root" run --rm -T tooling` of the same wrapped kp-agent-desk
    (state_root /state/t11b-q1/b-<action>) exits non-zero naming `store_outside_volume` and the state_root, with no
    open, SQLite connect or mkdir below the state_root, and its tree unchanged."""
    world, project = q1
    root = f'{WORK}/b-{action}'
    _refused(project, lambda: project.audited_run('tooling', 'kp_agent_tooling.desk_cli:main', 'kp-agent-desk',
                                                  ['--config', f'{root}.json', action]),
             root, f'docker compose run --rm tooling kp-agent-desk {action}')


def test_q1c_launch_prepare_with_an_outside_state_root_creates_no_directory(q1):
    """GREEN-IF `docker exec <tooling> kp-agent-launch --config <cfg> prepare` (a valid codex request for the saved desk;
    the registry state copied to /state/t11b-q1/c-prepare) exits non-zero naming `store_outside_volume` and the
    state_root, makes no directory (no os.mkdir below it: no launches/), opens no store file below it, and leaves its
    tree unchanged."""
    world, project = q1
    root = f'{WORK}/c-prepare'
    request = {'harness': 'codex', 'provider': 'openai', 'model': 'fixture-model', 'desk_id': DESK,
               'workspace': f'{WORK}/workspace', 'task_id': 't11b-q1c', 'source': 'host', 'parent_session_id': None}
    _refused(project, lambda: project.audited_exec(project.tooling, 'kp_agent_tooling.launch_cli:main',
                                                   'kp-agent-launch', ['--config', f'{root}.json', 'prepare'],
                                                   stdin=json.dumps(request)),
             root, 'docker exec kp-agent-launch prepare')


GATEWAY_AND_REFRESH = r'''
import json, sys, time
from decimal import Decimal
from pathlib import Path
from kp_agent_tooling._impl.service.model_gateway import Budget, BudgetLedger
from kp_agent_tooling import refresh_cli
root, publication = Path(sys.argv[1]), Path(sys.argv[2])
ledger = BudgetLedger(root)
budget = Budget(max_calls_per_hour=10, max_usd_per_day=Decimal("1"), confirm_over_usd=None,
                estimated_usd_per_call=Decimal("0.01"))
ledger.reserve(call_id="t11b-q1d", capability="text", provider="p", model="m", budget=budget, confirm=False,
               digest="d" * 64, now=time.time())
refresh_cli.atomic(publication, {"t11b": "q1d"})
print(json.dumps({"ledger": str(ledger.path), "ledger_is_file": ledger.path.is_file(),
                  "publication": json.loads(publication.read_text())}))
'''


def test_q1d_unmarked_gateway_ledger_and_refresh_publication_still_open_and_write(q1):
    """GREEN-IF, in the tooling role (AGENT_MEMORY_VOLUME set), the model gateway's budget ledger under
    /state/model-artifacts/.model-gateway is opened (SQLite connect seen) and written (a reservation), and a
    publication to /state/refresh/t11b-q1d.json is written and reads back; the snippet exits 0."""
    world, project = q1
    for name in ('model-artifacts', 'refresh'):
        host_state(world, name).mkdir(mode=0o700, exist_ok=True)
    proc, record = project.audited_exec(project.tooling, None, None,
                                        ['/state/model-artifacts', '/state/refresh/t11b-q1d.json'],
                                        code=GATEWAY_AND_REFRESH)
    assert proc.returncode == 0 and record is not None and not record['crashed'], (
        'the unmarked gateway ledger or /state/refresh publication was refused\n' + shown(proc, record))
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    ledger = '/state/model-artifacts/.model-gateway/ledger.sqlite3'
    assert result['ledger'] == ledger and result['ledger_is_file'], f'the ledger was not written: {result}'
    assert opened(record, ledger), f'no SQLite connect of {ledger} was seen\n' + shown(proc, record)
    assert result['publication'] == {'t11b': 'q1d'}, f'the /state/refresh publication did not read back: {result}'
    assert any(e[0] == 'open' and (path_of(e[1]) or '').startswith('/state/refresh/') for e in record['events']), (
        'no open under /state/refresh was seen\n' + shown(proc, record))


def test_q1e_a_docker_exec_reading_config_launch_desks_json_works(q1):
    """GREEN-IF `docker exec <tooling> kp-agent-desk-registry --config /config/launch/registry.json list` exits 0,
    lists the saved desk, and the audit shows /config/launch/desks.json opened."""
    world, project = q1
    proc, record = project.audited_exec(project.tooling, 'kp_agent_tooling.desk_registry_cli:main',
                                        'kp-agent-desk-registry',
                                        ['--config', '/config/launch/registry.json', 'list'])
    assert proc.returncode == 0 and record is not None and not record['crashed'], (
        'reading /config/launch/desks.json through the registry failed\n' + shown(proc, record))
    assert DESK in proc.stdout, f'the saved desk is not listed: {proc.stdout[-1500:]}'
    assert opened(record, '/config/launch/desks.json'), (
        '/config/launch/desks.json was not opened\n' + shown(proc, record))
