"""T3 P2: Claude tasks bind at launch (launcher side).

For a desk task, `prepare` mints the session id and binds it at once. The
spawned argv carries the minted id, an MCP config whose memory server config is
exactly that session's, and a settings file with the capture hooks. The binding
is recorded with `source: "board"`, and MCP `initialize` plus `memory.search`
over the generated config succeed. (The merge with Kanban's own hooks is tested
in apps/kanban/test/runtime/launch.)
Falsifier: missing injection, an unbound session, or a memory config for another
session.

Readings (reported under AMBIGUITY): the generated MCP servers are started
exactly as Claude would start them (command, args, the spawn environment plus the
server's `env`); "that session's memory config" is checked on every absolute JSON
file a server names that carries `provider_session_id`, and at least one must;
`--strict-mcp-config` would drop the user's other MCP servers, so it must not be
added.
"""
import json
from pathlib import Path

from t12b_seams import drain  # T12b B4: the one drain helper
from t3_harness import (INSTANCE, World, desk_binding_key, flag_values, json_files_in, mcp_session,
                        server_env)


def _servers(prepared):
    servers = {}
    for value in flag_values(prepared['argv_additions'], '--mcp-config'):
        document = json.loads(value if value.lstrip().startswith('{') else Path(value).read_text())
        servers.update(document.get('mcpServers') or {})
    return servers


def _world(tmp_path):
    return World.create(tmp_path / 'world')


def _memory_servers(world, prepared):
    """Generated servers that name a memory config; each config must be this session's."""
    native = prepared['native_session_id']
    spawn = world.env()
    spawn.update(prepared['env_additions'])
    found = []
    for name, server in _servers(prepared).items():
        env = server_env(spawn, server)
        configs = [value for _, value in json_files_in([*server.get('args', []), *env.values()])
                   if isinstance(value, dict) and 'provider_session_id' in value]
        for config in configs:
            assert config['provider_session_id'] == native, (name, config)
            assert config['provider_instance'] == INSTANCE, (name, config)
            assert Path(config['state_root']).resolve() == world.state.resolve(), (name, config)
        if configs:
            found.append((name, server, env))
    assert found, f'no generated MCP server names a memory config: {_servers(prepared)!r}'
    return found


def test_claude_launch_binds_the_minted_session_and_serves_its_memory(tmp_path):
    world = _world(tmp_path)
    desk = world.save_desk()
    request = world.request(harness='claude', desk_id=desk, task_id='task-p2', parent='parent-session-0',
                            provider='anthropic', model='fixture-model')
    prepared = world.prepared(request)
    native = prepared['native_session_id']
    argv = prepared['argv_additions']

    assert flag_values(argv, '--session-id') == [native], argv
    assert flag_values(argv, '--settings'), argv
    assert '--strict-mcp-config' not in argv, argv
    recorded = world.binding_for(native)
    assert len(recorded) == 1, recorded
    assert {k: recorded[0][k] for k in ('harness', 'provider', 'model', 'native_session_id', 'desk_id',
                                         'source', 'workspace', 'parent_session_id')} == {
        'harness': 'claude', 'provider': 'anthropic', 'model': 'fixture-model', 'native_session_id': native,
        'desk_id': desk, 'source': 'board', 'workspace': str(world.workspace),
        'parent_session_id': 'parent-session-0'}

    key = desk_binding_key(world, desk)
    drain(world.operator)  # T12b B4: the indexer, not the hook, indexes
    for name, server, env in _memory_servers(world, prepared):
        _, replies = mcp_session(server['command'], server.get('args', []), env, list_tools=False, calls=[
            ('memory.connection_status', {}), ('memory.search', {'query': 'Lantern'}), ('memory.bindings', {})])
        status, search, bindings = replies
        assert not status.is_error and status.value['status'] == 'ready', (name, status.value)
        assert not search.is_error, (name, search.value)
        assert [b['binding_key'] for b in bindings.value['bindings'] if b['own']] == [key], (name, bindings.value)


def test_each_launch_gets_its_own_session_on_its_own_desk(tmp_path):
    world = _world(tmp_path)
    first = world.save_desk(name='First desk')
    second = world.save_desk(role='Curator', name='Second desk')
    launches = {desk: world.prepared(world.request(harness='claude', desk_id=desk, task_id=f'task-{n}'))
                for n, desk in enumerate((first, second))}
    natives = {desk: launch['native_session_id'] for desk, launch in launches.items()}
    assert len(set(natives.values())) == 2, natives
    assert {b['native_session_id']: b['desk_id'] for b in world.bindings()} == {
        natives[first]: first, natives[second]: second}

    for desk, prepared in launches.items():
        for name, server, env in _memory_servers(world, prepared):
            _, replies = mcp_session(server['command'], server.get('args', []), env, list_tools=False,
                                     calls=[('memory.bindings', {})])
            own = [b['binding_key'] for b in replies[0].value['bindings'] if b['own']]
            assert own == [desk_binding_key(world, desk)], (desk, name, replies[0].value)
