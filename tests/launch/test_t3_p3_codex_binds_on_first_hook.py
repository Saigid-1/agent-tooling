"""T3 P3: Codex tasks bind on the first hook (launcher side).

The spawned argv carries the MCP and hook `-c` overrides, and the environment
carries the receipt. The first hook event binds that Codex session id to the
desk (`source: "board"`), and later payloads with a different session id are
refused. (The merge with Kanban's own Codex hook overrides is tested in
apps/kanban/test/runtime/launch.)
Falsifier: no binding, or a second session accepted.

Readings (reported under AMBIGUITY): these tests use the shipped `codex`
profile with HOME pointing into the test root, so rollouts live under
`$HOME/.codex/sessions`; the fake `codex` applies `-c key=value` overrides in
order, a later override replacing the value at its key path, and runs each
command configured under `hooks.<Event>`; the first event fired is the earliest
configured of SessionStart, UserPromptSubmit and Stop; "the binding and memory
still work" (P4) is read to include the Codex session's injected MCP memory
server once the first hook has bound it.
"""
from t12b_seams import drain  # T12b B4: the one drain helper
from t3_harness import (World, append_rows, codex_override_keys, codex_rollout, codex_rows, desk_binding_key,
                        describe_runs, mcp_session, new_session_id, ready, runs_for, server_env, step)


def _payload(world, session, event, transcript, **extra):
    body = {'session_id': session, 'transcript_path': str(transcript), 'cwd': str(world.workspace),
            'hook_event_name': event, 'model': 'fixture-model', 'turn_id': 'turn-1'}
    if event == 'Stop':
        body['stop_hook_active'] = False
    elif event == 'UserPromptSubmit':
        body['prompt'] = 'Begin.'
    elif event == 'SessionStart':
        body['source'] = 'startup'
    body.update(extra)
    return body


def _configured_events(world, fake, prepared):
    """The hook events the fake `codex` reads from the overrides (a launch with no events fired)."""
    hooks = world.launch(fake, prepared, plan=[], label='probe')['config']['hooks']
    return {event for event, groups in hooks.items() if isinstance(groups, list)}


def _first_event(world, fake, prepared):
    configured = _configured_events(world, fake, prepared)
    assert 'Stop' in configured, configured
    return next(event for event in ('SessionStart', 'UserPromptSubmit', 'Stop') if event in configured)


def test_codex_first_hook_binds_that_session_and_refuses_another(tmp_path):
    world = World.create(tmp_path / 'world')
    fake = world.fake('codex')
    desk = world.save_desk()
    prepared = world.prepared(world.request(harness='codex', desk_id=desk, provider='openai', model='fixture-model',
                                            task_id='task-p3'))
    assert prepared['native_session_id'] is None, prepared
    keys = codex_override_keys(prepared['argv_additions'])
    assert any(k == 'mcp_servers' or k.startswith('mcp_servers.') for k in keys), keys
    assert any(k == 'hooks' or k.startswith('hooks.') for k in keys), keys
    assert prepared['receipt_path'] in prepared['env_additions'].values(), prepared
    assert world.bindings() == [], 'a hook-strategy session was bound before its first hook'

    first, other = new_session_id(), new_session_id()
    rollout = append_rows(codex_rollout(world.codex_root, first), codex_rows(first, world.workspace))
    other_rollout = append_rows(codex_rollout(world.codex_root, other), codex_rows(other, world.workspace))
    event = _first_event(world, fake, prepared)
    record = world.launch(fake, prepared, plan=[
        step(event, _payload(world, first, event, rollout), 'first'),
        step('Stop', _payload(world, other, 'Stop', other_rollout), 'other'),
        step(event, _payload(world, first, event, rollout), 'replay'),
    ])
    for label, expected_ok in (('first', True), ('other', False), ('replay', True)):
        runs = runs_for(record, label)
        assert runs, f'no hook ran for {label}: {describe_runs(record["runs"])}'
        assert all((r['code'] == 0) == expected_ok for r in runs), (label, describe_runs(runs))

    recorded = world.bindings()
    assert [(b['native_session_id'], b['desk_id'], b['harness'], b['source'], b['workspace'],
             b['parent_session_id']) for b in recorded] == [
        (first, desk, 'codex', 'board', str(world.workspace), None)], recorded
    assert ready(world, first)
    assert not ready(world, other)


def test_codex_memory_server_answers_once_the_first_hook_binds(tmp_path):
    world = World.create(tmp_path / 'world')
    fake = world.fake('codex')
    desk = world.save_desk()
    prepared = world.prepared(world.request(harness='codex', desk_id=desk, provider='openai', model='fixture-model'))
    session = new_session_id()
    rollout = append_rows(codex_rollout(world.codex_root, session), codex_rows(session, world.workspace))
    event = _first_event(world, fake, prepared)
    record = world.launch(fake, prepared, plan=[step(event, _payload(world, session, event, rollout), 'first')])
    assert all(r['code'] == 0 for r in runs_for(record, 'first')), describe_runs(record['runs'])
    assert [b['native_session_id'] for b in world.bindings()] == [session]

    servers = record['config']['mcp_servers']
    assert servers, record['config']
    spawn = world.env()
    spawn.update(prepared['env_additions'])
    answered = []
    drain(world.operator)  # T12b B4: the indexer, not the hook, indexes
    for name, server in servers.items():
        _, replies = mcp_session(server['command'], server.get('args', []), server_env(spawn, server),
                                 list_tools=False, calls=[('memory.connection_status', {}),
                                                          ('memory.search', {'query': 'Lantern'}),
                                                          ('memory.bindings', {})])
        status, search, bindings = replies
        if not status.is_error and status.value.get('status') == 'ready':
            assert not search.is_error, (name, search.value)
            assert [b['binding_key'] for b in bindings.value['bindings'] if b['own']] == [
                desk_binding_key(world, desk)], (name, bindings.value)
            answered.append(name)
    assert answered, f'no injected MCP server serves the bound Codex session: {servers!r}'
