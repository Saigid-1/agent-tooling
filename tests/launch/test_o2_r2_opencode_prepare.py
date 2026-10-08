"""O2 R2 (Python side) and F2: what `kp-agent-launch prepare` returns for an OpenCode board launch.

Order: docs/work/orders/O2-opencode-third-harness.md. The board half (the adapter applies these
additions, under O1's policy) is apps/kanban/test/runtime/terminal/o2-opencode-launch-binding.test.ts.

GREEN-IF an `opencode` board launch is prepared with no argv additions and exactly three environment
additions: the receipt, OPENCODE_CONFIG_CONTENT holding ONLY this launch's `kp_desk_memory` server in
OpenCode's local shape (no permission, agent, tool or plugin key), and the hook command as a JSON
argv naming this launch's receipt; each fits the board's 4096-character bound; two launches bound to
two desks name two different memory configurations; and a host launch is refused before anything is
written or bound. RED at base: the harness has no profile (exit 3, harness_unavailable).
Mutants (RED here): the server put in argv; a permission key in the content; the host refusal removed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from t3_harness import World  # noqa: E402

CONTENT = 'OPENCODE_CONFIG_CONTENT'
HOOK = 'KP_AGENT_LAUNCH_HOOK_COMMAND'
RECEIPT = 'KP_AGENT_LAUNCH_RECEIPT'
BOARD_ENV_BOUND = 4096  # apps/kanban launchBindingResultSchema: env_additions values


def _prepared(world, desk, **extra):
    return world.prepared(world.request(harness='opencode', desk_id=desk, provider='harness-default',
                                        model='harness-default', **extra))


def test_an_opencode_launch_carries_its_server_and_hook_in_three_environment_additions(tmp_path):
    world = World.create(tmp_path / 'w')
    desk = world.save_desk()
    prepared = _prepared(world, desk)
    receipt = Path(prepared['receipt_path'])
    assert prepared['native_session_id'] is None, 'OpenCode cannot take a chosen id: the first hook binds'
    assert prepared['argv_additions'] == []
    env = prepared['env_additions']
    assert set(env) == {RECEIPT, CONTENT, HOOK}, sorted(env)
    assert env[RECEIPT] == str(receipt)
    assert all(len(value) <= BOARD_ENV_BOUND for value in env.values()), {k: len(v) for k, v in env.items()}

    content = json.loads(env[CONTENT])
    assert list(content) == ['mcp'], 'the per-launch document carries the server and nothing that could loosen the policy'
    assert list(content['mcp']) == ['kp_desk_memory']
    server = content['mcp']['kp_desk_memory']
    assert set(server) == {'type', 'command', 'environment', 'enabled'} and server['type'] == 'local'
    assert server['enabled'] is True
    memory = receipt.parent / 'memory.json'
    assert server['command'][1:] == ['-m', 'kp_agent_tooling.memory_cli', '--config', str(memory), 'serve']
    assert prepared.get('deferred_files') == {'memory_config': str(memory)}, 'the first hook writes it at bind'
    assert not memory.exists()

    hook = json.loads(env[HOOK])
    assert hook[1:] == ['-m', 'kp_agent_tooling.launch_cli', '--receipt', str(receipt), 'hook']
    assert Path(hook[0]).is_absolute() and Path(hook[0]) == Path(server['command'][0])
    assert world.bindings() == [], 'nothing is bound before the first hook'


def test_two_launches_on_two_desks_each_name_only_their_own_server(tmp_path):
    world = World.create(tmp_path / 'w')
    first, second = world.save_desk(name='First desk'), world.save_desk(name='Second desk')
    a, b = _prepared(world, first), _prepared(world, second)
    servers = [json.loads(p['env_additions'][CONTENT])['mcp'] for p in (a, b)]
    assert all(list(s) == ['kp_desk_memory'] for s in servers)
    configs = [s['kp_desk_memory']['command'][4] for s in servers]
    assert configs[0] != configs[1]
    for prepared, config in zip((a, b), configs):
        assert Path(config).parent == Path(prepared['receipt_path']).parent
        receipt = json.loads(Path(prepared['receipt_path']).read_text())
        assert receipt['desk_id'] == (first if prepared is a else second)
        assert receipt['profile']['mcp'] == {'strategy': 'config_content_env', 'env': CONTENT, 'key': 'mcp'}


def test_a_host_launch_of_opencode_is_refused_before_anything_is_written(tmp_path):
    world = World.create(tmp_path / 'w')
    desk = world.save_desk()
    launches = world.state / 'launches'
    before = sorted(p.name for p in launches.iterdir()) if launches.exists() else []
    run = world.prepare(world.request(harness='opencode', desk_id=desk, source='host')).refused()
    assert run.code == 1 and 'board' in run.stdout, run.describe()
    after = sorted(p.name for p in launches.iterdir()) if launches.exists() else []
    assert after == before and world.bindings() == []
