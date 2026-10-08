"""T3 P6: no model path to a desk.

Neither hook payloads nor MCP tools can choose or change a desk, harness or
admission; the receipt fixes them.
Falsifier: a payload or tool call that changes the bound desk.

Readings (reported under AMBIGUITY): these tests use the shipped profiles with
HOME pointing into the test root; a hook payload that names a desk, harness,
source or binding may be refused or ignored, but never followed; the generated
MCP servers are inspected through `tools/list` exactly as Claude would start
them; a second launch receipt for another desk cannot take over a session that
is already bound.
"""
import json
import re
from pathlib import Path

from t3_harness import (BINDING_INPUTS, World, append_rows, claude_rows, claude_transcript, codex_rollout, codex_rows,
                        desk_binding_key, flag_values, mcp_session, memory, new_session_id, server_env, step)


def _world(tmp_path):
    world = World.create(tmp_path / 'world')
    claude, codex = world.fake('claude'), world.fake('codex')
    first = world.save_desk(name='Receipt desk')
    second = world.save_desk(role='Curator', name='Intruder target desk')
    return world, claude, codex, first, second


def _properties(schema, found):
    if isinstance(schema, dict):
        for name, value in (schema.get('properties') or {}).items():
            found.add(name)
            _properties(value, found)
        for key in ('items', 'anyOf', 'oneOf', 'allOf', 'additionalProperties'):
            value = schema.get(key)
            for child in (value if isinstance(value, list) else [value]):
                _properties(child, found)
    return found


def _other_desk_episodes(world, session, other_key):
    _, replies = memory(world, session, calls=[('memory.list', {'kind': 'episodes', 'binding_key': other_key})])
    return 0 if replies[0].is_error else replies[0].value['total']


def _selectors(world, desk, session):
    return {'desk_id': desk, 'harness': 'intruder', 'source': 'operator', 'provider': 'intruder',
            'model': 'intruder', 'native_session_id': session, 'parent_session_id': 'intruder-parent',
            'binding_key': desk_binding_key(world, desk), 'receipt_path': str(world.root / 'nowhere.json')}


def test_hook_payloads_cannot_choose_desk_harness_or_source(tmp_path):
    world, claude, codex, first, second = _world(tmp_path)
    second_key = desk_binding_key(world, second)
    marker = 'Holly payload selector marker'

    # Codex: the payload of the binding (first) hook names another desk, harness and source.
    prepared = world.prepared(world.request(harness='codex', desk_id=first, provider='openai'))
    session = new_session_id()
    rollout = append_rows(codex_rollout(world.codex_root, session),
                          codex_rows(session, world.workspace, f'Remember {marker}.', 'Recorded.'))
    payload = {'session_id': session, 'transcript_path': str(rollout), 'cwd': str(world.workspace),
               'hook_event_name': 'Stop', 'stop_hook_active': False, **_selectors(world, second, session)}
    world.launch(codex, prepared, plan=[step('Stop', payload)])
    recorded = world.binding_for(session)
    assert all((b['desk_id'], b['harness'], b['source'], b['parent_session_id']) == (first, 'codex', 'board', None)
               for b in recorded), recorded
    assert len(recorded) <= 1, recorded
    if recorded:
        assert _other_desk_episodes(world, session, second_key) == 0

    # Claude: a capture payload names another desk; the minted binding is unchanged.
    prepared = world.prepared(world.request(harness='claude', desk_id=first))
    native = prepared['native_session_id']
    before = world.binding_for(native)
    transcript = append_rows(claude_transcript(world.claude_root, world.workspace, native),
                             claude_rows(native, world.workspace, f'Remember {marker}.', 'Recorded.'))
    payload = {'session_id': native, 'transcript_path': str(transcript), 'cwd': str(world.workspace),
               'hook_event_name': 'Stop', 'stop_hook_active': False, **_selectors(world, second, native)}
    world.launch(claude, prepared, plan=[step('Stop', payload)])
    after = world.binding_for(native)
    assert [(b['desk_id'], b['harness'], b['source']) for b in after] == [(first, 'claude', 'board')], after
    assert [b['recorded_at'] for b in after] == [b['recorded_at'] for b in before]
    assert _other_desk_episodes(world, native, second_key) == 0
    # No session was ever bound to the desk the payloads named.
    assert [b for b in world.bindings() if b['desk_id'] == second] == []


def test_generated_mcp_tools_cannot_bind_or_choose_a_desk(tmp_path):
    world, _, _, first, second = _world(tmp_path)
    prepared = world.prepared(world.request(harness='claude', desk_id=first))
    spawn = world.env()
    spawn.update(prepared['env_additions'])
    servers = {}
    for value in flag_values(prepared['argv_additions'], '--mcp-config'):
        servers.update(json.loads(value if value.lstrip().startswith('{') else Path(value).read_text())['mcpServers'])
    assert servers, prepared
    for name, server in servers.items():
        tools, _ = mcp_session(server['command'], server.get('args', []), server_env(spawn, server))
        for tool in tools:
            if tool['name'] != 'memory.bindings':  # read-only listing of registered bindings
                assert not re.search(r'bind|admi(t|ssion)|enrol', tool['name'], re.I), (
                    name, tool['name'])
            inputs = _properties(tool.get('inputSchema') or {}, set())
            assert not inputs & BINDING_INPUTS, (name, tool['name'], sorted(inputs & BINDING_INPUTS))
    assert [b['desk_id'] for b in world.bindings()] == [first]
    assert world.binding_for(prepared['native_session_id'])[0]['desk_id'] == first
    assert second not in {b['desk_id'] for b in world.bindings()}


def test_a_second_receipt_cannot_move_a_bound_session(tmp_path):
    world, _, codex, first, second = _world(tmp_path)
    receipt_first = world.prepared(world.request(harness='codex', desk_id=first, provider='openai'))
    receipt_second = world.prepared(world.request(harness='codex', desk_id=second, provider='openai',
                                                  task_id='task-2'))
    session = new_session_id()
    rollout = append_rows(codex_rollout(world.codex_root, session),
                          codex_rows(session, world.workspace, 'Remember the rowan.', 'Recorded.'))
    payload = {'session_id': session, 'transcript_path': str(rollout), 'cwd': str(world.workspace),
               'hook_event_name': 'Stop', 'stop_hook_active': False}
    world.hook(receipt_first['receipt_path'], payload).ok()
    assert [b['desk_id'] for b in world.binding_for(session)] == [first]

    world.hook(receipt_second['receipt_path'], payload).refused()
    assert [b['desk_id'] for b in world.binding_for(session)] == [first]
    _, replies = memory(world, session, calls=[('memory.bindings', {})])
    assert [b['binding_key'] for b in replies[0].value['bindings'] if b['own']] == [desk_binding_key(world, first)]
