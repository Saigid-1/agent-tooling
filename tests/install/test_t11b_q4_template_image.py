"""T11b Q4 (image-marked): the assistant template leaves the volume.

Order: docs/work/orders/T11b-leaf-behaviour.md, Q4 (T9b Amendment 7 J2, L7). The assistant `memory.json`
template is operator-written under `$root/config/`. `launches/` is `state_root/launches` of the template's
own `state_root` (`/state/memory/assistant/store/launches`), never `template.parent`, never
`state_root.parent`. The T9b exemption is removed together (container.py, runtime_install's
`required_store_path`, test_t9b_p7). Operator step for an existing runtime: (1) a one-off container prints
the template, `docker compose --project-directory "$root" run --rm -T --entrypoint cat tooling
/state/memory/assistant/memory.json` (meet note, Coordinator 2026-10-04: the `--entrypoint` form bypasses the
role preflight, as docs/DOCKER.md's upgrade step 7 does; the order's first text, without it, is refused by the
preflight once the exemption is gone); (2) the operator writes it under `$root/config/`; (3) the operator
edits `config_template` in the binding; (4) roles refuse until then, and `verify` names the operator file.
Falsifiers:
- a role that writes under /config;
- a template under /state/memory that a role accepts, or that `verify` does not name;
- after the documented step, the old template left in the volume is read (audit hook);
- an existing assistant binding stops resolving after the step.

Setup (both projects): a prepared project (plan, apply, prepare; `tooling` up) holding an existing
assistant runtime exactly as docs/DOCKER.md "Sidebar assistant memory" writes it before T11b: the template
`/state/memory/assistant/memory.json` (state_root `/state/memory/assistant/store`), its catalog and policy
under `$root/config/board/`, the binding `$root/config/board/assistant-binding.json` naming the template;
the store initialized (sessions, episodes, the owner marker of the one assistant binding) by `kp-agent-desk
initialize` with the same configuration.
- Project "old": the binding still names the template in the volume (falsifier 2, and the control on step 1,
  the meet note's `--entrypoint cat` form).
- Project "stepped": the operator step applied (the template written to `$root/config/board/assistant-memory.json`,
  the binding's `config_template` edited to `/config/board/assistant-memory.json`), the role recreated, then the
  board's assistant command (`python3 -m kp_agent_tooling.assistant_host_cli --binding
  /config/board/assistant-binding.json ... prepare`, its `main` under tests/t11b_audit.py's hook) run with
  `docker exec` in the role (falsifiers 1, 3, 4).

Every role refusal is asserted through t9b_harness.assert_role_refused (Coordinator rule, Verification C2).

Readings (repeated under AMBIGUITY):
- "the path the docs name" for the new template: this test writes `$root/config/board/assistant-memory.json`
  (beside the binding, catalog and policy) rather than reading a path from the head's docs;
- the role refusal's reason is `operator_file_names_outside_store`, the preflight's existing reason for an
  operator file naming a store path it must not;
- "verify names the operator file": some object in `verify`'s JSON carries both the binding's root-relative
  path and the key `config_template`;
- step (1) in both projects is the meet note's form, `run --rm -T --entrypoint cat tooling <template>`; in the old
  project it is a control of its own;
- "an existing binding still resolves": `prepare` exits 0 for the same binding key (the store's owner marker
  matches), the role keeps running, and the receipt is under `state_root/launches`.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess

import pytest

from s3_harness import explain
from t9b_harness import assert_role_refused, verify, wait_exit
from t11b_audit import opened, writes_under
from t11b_harness import host_state, prepared_project, shown, write_operator_json

pytestmark = pytest.mark.image

TENANT = 't11b-q4'
ASSISTANT_KEY = 'binding:' + hashlib.sha256(f'{TENANT}|assistant|personal'.encode()).hexdigest()
OLD_TEMPLATE = '/state/memory/assistant/memory.json'
STORE_ROOT = '/state/memory/assistant/store'
NEW_TEMPLATE = '/config/board/assistant-memory.json'
BINDING = '/config/board/assistant-binding.json'
BINDING_RELATIVE = 'config/board/assistant-binding.json'
WORKSPACE = '/state/t11b-q4/workspace'
TASK = '__home_agent__:t11b-q4:claude'
REFUSAL_REASON = 'operator_file_names_outside_store'


def _template() -> dict:
    return {'schema_version': 'ops.assistant-memory.local.v1', 'state_root': STORE_ROOT,
            'catalog_path': '/config/board/assistant-desk.json', 'workspace_root': WORKSPACE,
            'provider_instance': 'kanban-claude-assistant', 'provider_session_id': 'assistant-template',
            'assistant_policy_path': '/config/board/assistant-policy.json'}


def _binding(template: str) -> dict:
    return {'schema_version': 'agent.assistant-host.v1', 'config_template': template,
            'transcript_root': '/state/.claude/projects'}


def _existing_assistant_runtime(world, project) -> None:
    """docs/DOCKER.md "Sidebar assistant memory", as written before T11b, plus the store initialized."""
    host_state(world, 't11b-q4').mkdir(mode=0o700)
    host_state(world, 't11b-q4/workspace').mkdir(mode=0o700)
    board = world.root / 'config' / 'board'
    write_operator_json(board / 'assistant-desk.json', {
        'schema_version': 'ops.imported-desk-catalog.v1', 'approval_ref': 'operator:assistant',
        'bindings': [{'binding_key': ASSISTANT_KEY, 'tenant_id': TENANT, 'role': 'assistant', 'repo_key': 'personal',
                      'desk_label': 'Workspace Assistant', 'source': 'operator', 'memory_write_allowed': True}]})
    write_operator_json(board / 'assistant-policy.json',
                        {'schema_version': 'ops.assistant-summary-policy.v1', 'focus_questions': []})
    project.run_ok(project.tooling, 'sh', '-c', 'umask 077 && mkdir -m 700 /state/memory/assistant '
                   f'/state/memory/assistant/store && cat > {OLD_TEMPLATE}', stdin=json.dumps(_template()) + '\n')
    # The store as a first sidebar launch leaves it: initialized, owned by the one assistant binding. The same
    # configuration, read from a copy on the bind, so that this setup step does not depend on how the head
    # treats a configuration file inside the volume.
    write_operator_json(host_state(world, 't11b-q4/initialize.json'), _template())
    project.run_ok(project.tooling, 'kp-agent-desk', '--config', '/state/t11b-q4/initialize.json', 'initialize')
    write_operator_json(board / 'assistant-binding.json', _binding(OLD_TEMPLATE))


def _teardown(world, project) -> None:
    remaining = project.down()
    shutil.rmtree(world.base, ignore_errors=True)
    assert not remaining, f'left containers behind: {remaining}'


@pytest.fixture(scope='module')
def old():
    world, project = prepared_project('q4-old')
    try:
        _existing_assistant_runtime(world, project)
        yield world, project
    finally:
        _teardown(world, project)


def _find(value, predicate):
    found = []
    if isinstance(value, dict):
        if predicate(value):
            found.append(value)
        for child in value.values():
            found += _find(child, predicate)
    elif isinstance(value, list):
        for child in value:
            found += _find(child, predicate)
    return found


def test_q4_verify_names_the_binding_of_a_template_under_state_memory(old):
    """GREEN-IF `kp-agent-install verify --runtime-root "$root"` (Docker reachable), with the binding naming the
    template /state/memory/assistant/memory.json, exits 0 and its JSON holds an object naming both
    `config/board/assistant-binding.json` and `config_template`."""
    world, project = old
    proc = verify(world)
    assert proc.returncode == 0, 'verify failed\n' + explain(proc)
    report = json.loads(proc.stdout)
    named = _find(report, lambda d: any(isinstance(v, str) and v.endswith(BINDING_RELATIVE) for v in d.values())
                  and 'config_template' in d.values())
    assert named, ('verify does not name the binding whose config_template is under /state/memory: '
                   f'{json.dumps((report.get("readiness") or {}).get("memory_store"))[:2000]}')


def test_q4_a_role_refuses_a_template_under_state_memory(old):
    """GREEN-IF the tooling role, recreated while the binding names the template in the volume, is refused by its
    store preflight (t9b_harness.assert_role_refused, reason `operator_file_names_outside_store`)."""
    world, project = old
    up = project.compose('up', '-d', '--force-recreate', 'tooling', timeout=600)
    assert project.container('tooling'), 'no tooling container\n' + explain(up)
    try:
        assert_role_refused(project, 'tooling', REFUSAL_REASON)
    finally:
        project.compose('rm', '-s', '-f', 'tooling', timeout=300)


def test_q4_control_the_orders_step_1_prints_the_template(old):
    """Positive control (the order's step 1 as the meet note writes it): `docker compose --project-directory "$root"
    run --rm -T --entrypoint cat tooling /state/memory/assistant/memory.json` exits 0 and prints the template while the
    binding still names it ("roles refuse until then" must not make the documented first step impossible)."""
    world, project = old
    proc = project.compose('run', '--rm', '-T', '--entrypoint', 'cat', 'tooling', OLD_TEMPLATE, timeout=600)
    assert proc.returncode == 0, "the order's step 1 does not print the template\n" + explain(proc)
    assert json.loads(proc.stdout) == _template(), f'step 1 printed something else: {proc.stdout[-1000:]}'


ASSISTANT = 'kp_agent_tooling.assistant_host_cli:main'


@pytest.fixture(scope='module')
def stepped():
    world, project = prepared_project('q4-step')
    try:
        _existing_assistant_runtime(world, project)
        # (1) print the template with a one-off container (the meet note's step 1: --entrypoint bypasses the preflight).
        printed = project.compose('run', '--rm', '-T', '--entrypoint', 'cat', 'tooling', OLD_TEMPLATE, timeout=600)
        assert printed.returncode == 0, 'could not print the template\n' + explain(printed)
        # (2) the operator writes it under $root/config/, (3) and edits config_template in the binding.
        write_operator_json(world.root / NEW_TEMPLATE.lstrip('/'), json.loads(printed.stdout))
        write_operator_json(world.root / BINDING.lstrip('/'), _binding(NEW_TEMPLATE))
        up = project.compose('up', '-d', '--force-recreate', 'tooling', timeout=600)
        cid = project.container('tooling')
        state = wait_exit(project, cid, 8.0) if cid else {}
        running = bool(state.get('Running'))
        proc, record = (project.audited_exec(project.tooling, ASSISTANT, 'assistant_host_cli',
                                             ['--binding', BINDING, '--workspace', WORKSPACE, '--task', TASK,
                                              'prepare'])
                        if running else (subprocess.CompletedProcess(['<tooling not running>'], 1, '', ''), None))
        role = {'running': running, 'state': state, 'log': project.logs(cid)[-2000:] if cid else '', 'up': explain(up)}
        yield world, project, proc, record, role
    finally:
        _teardown(world, project)


def test_q4_no_role_writes_under_config(stepped):
    """GREEN-IF, after the step, the board's assistant `prepare` in the tooling role makes no directory and opens no
    file for writing (and connects no database) under /config."""
    world, project, proc, record, role = stepped
    assert role['running'], f'precondition: the tooling role runs after the step: {role}'
    assert record is not None, 'the audit wrapper left no record\n' + shown(proc, record)
    found = writes_under(record, '/config')
    assert not found, 'the assistant prepare writes under /config:\n  ' + '\n  '.join(found) + '\n' + shown(proc, record)


def test_q4_after_the_step_the_old_template_in_the_volume_is_not_read(stepped):
    """GREEN-IF, after the step, the board's assistant `prepare` never opens /state/memory/assistant/memory.json."""
    world, project, proc, record, role = stepped
    assert role['running'], f'precondition: the tooling role runs after the step: {role}'
    assert record is not None, 'the audit wrapper left no record\n' + shown(proc, record)
    found = opened(record, OLD_TEMPLATE)
    assert not found, f'the old template left in the volume was read after the step: {found}\n' + shown(proc, record)


def test_q4_the_existing_binding_still_resolves_after_the_step(stepped):
    """GREEN-IF, after the step, the tooling role runs, and the board's assistant `prepare` exits 0 for the same
    binding key with its launch receipt under the template's state_root/launches
    (/state/memory/assistant/store/launches/<id>/launch.json)."""
    world, project, proc, record, role = stepped
    assert role['running'], f'the tooling role does not run after the step: {role}'
    assert proc.returncode == 0, 'the existing assistant binding no longer resolves after the step\n' + shown(proc, record)
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result.get('binding_key') == ASSISTANT_KEY, f'prepare resolved another binding: {result}'
    receipt = str(result.get('launch_receipt', ''))
    assert receipt.startswith(f'{STORE_ROOT}/launches/') and receipt.endswith('/launch.json'), (
        f'the launch is not under state_root/launches ({STORE_ROOT}/launches): {receipt}')
