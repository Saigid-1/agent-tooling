"""T3 P4: capture follows the desk.

Claude Stop/PreCompact and Codex Stop hooks capture that session's visible
events into the desk's memory. Replay adds nothing. A desk with
`capture: false` captures nothing, but the binding and memory still work. A
payload for another session, workspace or transcript is refused.
Falsifier: missing or duplicate capture, capture on a `capture: false` desk, or a
foreign payload accepted.

Readings (reported under AMBIGUITY): these tests use the shipped `claude` and
`codex` profiles with HOME pointing into the test root, so transcripts live in
the CLIs' native layouts under `$HOME/.claude/projects` and `$HOME/.codex/sessions`;
"captured into the desk's memory" is
observed through the desk-scoped `memory.search` and `memory.list` of a memory
config for that exact session (as the sidebar's capture is); a refused hook exits
non-zero; a replayed hook succeeds (exit 0) and adds nothing; hooks run through
the fake executable, which runs every command the generated configuration names
for the event.
"""
import json
import shutil
from pathlib import Path

from t12b_seams import drain  # T12b B4: the one drain helper
from t3_harness import (World, append_rows, claude_rows, claude_transcript, codex_rollout, codex_rows, describe_runs,
                        episodes_total, flag_values, json_files_in, mcp_session, memory, new_session_id, runs_for,
                        search_hits, server_env, step)


def _claude_world(tmp_path):
    world = World.create(tmp_path / 'world')
    return world, world.fake('claude')


def _codex_world(tmp_path):
    world = World.create(tmp_path / 'world')
    return world, world.fake('codex')


def _claude_payload(session, transcript, cwd, event='Stop', **extra):
    body = {'session_id': session, 'transcript_path': str(transcript), 'cwd': str(cwd), 'hook_event_name': event,
            'permission_mode': 'default'}
    body.update({'stop_hook_active': False} if event == 'Stop' else {'trigger': 'auto', 'custom_instructions': ''})
    body.update(extra)
    return body


def _codex_payload(session, transcript, cwd, event='Stop', **extra):
    body = {'session_id': session, 'transcript_path': str(transcript), 'cwd': str(cwd), 'hook_event_name': event,
            'model': 'fixture-model', 'turn_id': 'turn-1', 'stop_hook_active': False}
    body.update(extra)
    return body


def _all_ok(record, label):
    runs = runs_for(record, label)
    assert runs, f'no hook ran for {label}: {describe_runs(record["runs"])}'
    assert all(r['code'] == 0 for r in runs), describe_runs(runs)


def _all_refused(record, label):
    runs = runs_for(record, label)
    assert runs, f'no hook ran for {label}: {describe_runs(record["runs"])}'
    assert all(r['code'] != 0 for r in runs), f'{label} was accepted: {describe_runs(runs)}'


def _hits(world, session, marker):
    drain(world.operator)  # T12b B4: the indexer, not the hook, indexes
    _, replies = memory(world, session, calls=[('memory.search', {'query': marker})])
    return len(search_hits(replies[0], marker))


def test_claude_stop_and_precompact_capture_visible_events_once(tmp_path):
    world, fake = _claude_world(tmp_path)
    desk = world.save_desk()
    prepared = world.prepared(world.request(harness='claude', desk_id=desk))
    native = prepared['native_session_id']
    first, second = 'Cedar stop capture marker', 'Birch precompact capture marker'
    transcript = append_rows(claude_transcript(world.claude_root, world.workspace, native),
                             claude_rows(native, world.workspace, f'Remember {first}.', f'Recorded {first}.'))

    stop = _claude_payload(native, transcript, world.workspace)
    _all_ok(world.launch(fake, prepared, plan=[step('Stop', stop)]), 'Stop')
    captured = episodes_total(world, native)
    assert captured >= 1 and _hits(world, native, first) >= 1

    # Replay adds nothing.
    _all_ok(world.launch(fake, prepared, plan=[step('Stop', stop)]), 'Stop')
    assert episodes_total(world, native) == captured
    hits = _hits(world, native, first)

    # New visible events are captured on PreCompact; earlier ones are not duplicated.
    append_rows(transcript, claude_rows(native, world.workspace, f'Next {second}.', f'Kept {second}.'))
    record = world.launch(fake, prepared,
                          plan=[step('PreCompact', _claude_payload(native, transcript, world.workspace, 'PreCompact'))])
    _all_ok(record, 'PreCompact')
    assert episodes_total(world, native) > captured
    assert _hits(world, native, second) >= 1
    assert _hits(world, native, first) == hits


def test_codex_stop_captures_rollout_events_once(tmp_path):
    world, fake = _codex_world(tmp_path)
    desk = world.save_desk()
    prepared = world.prepared(world.request(harness='codex', desk_id=desk, provider='openai'))
    session = new_session_id()
    first, second = 'Alder codex capture marker', 'Rowan codex second marker'
    rollout = append_rows(codex_rollout(world.codex_root, session),
                          codex_rows(session, world.workspace, f'Remember {first}.', f'Recorded {first}.'))

    stop = _codex_payload(session, rollout, world.workspace)
    _all_ok(world.launch(fake, prepared, plan=[step('Stop', stop)]), 'Stop')
    assert [b['desk_id'] for b in world.binding_for(session)] == [desk]
    captured = episodes_total(world, session)
    assert captured >= 1 and _hits(world, session, first) >= 1

    _all_ok(world.launch(fake, prepared, plan=[step('Stop', stop)]), 'Stop')
    assert episodes_total(world, session) == captured
    hits = _hits(world, session, first)

    append_rows(rollout, codex_rows(session, world.workspace, f'Next {second}.', f'Kept {second}.')[1:])
    _all_ok(world.launch(fake, prepared, plan=[step('Stop', stop)]), 'Stop')
    assert episodes_total(world, session) > captured
    assert _hits(world, session, second) >= 1
    assert _hits(world, session, first) == hits


def test_capture_false_desk_binds_and_serves_memory_but_captures_nothing(tmp_path):
    world, fake = _claude_world(tmp_path)
    desk = world.save_desk(name='Quiet desk', capture=False)
    prepared = world.prepared(world.request(harness='claude', desk_id=desk))
    native = prepared['native_session_id']
    assert [(b['desk_id'], b['source']) for b in world.binding_for(native)] == [(desk, 'board')]
    marker = 'Hazel uncaptured marker'
    transcript = append_rows(claude_transcript(world.claude_root, world.workspace, native),
                             claude_rows(native, world.workspace, f'Remember {marker}.', f'Recorded {marker}.'))
    world.launch(fake, prepared, plan=[
        step('Stop', _claude_payload(native, transcript, world.workspace)),
        step('PreCompact', _claude_payload(native, transcript, world.workspace, 'PreCompact'))])

    assert episodes_total(world, native) == 0
    assert _hits(world, native, marker) == 0
    # Memory still works through the generated MCP configuration.
    spawn = world.env()
    spawn.update(prepared['env_additions'])
    servers = {}
    for value in flag_values(prepared['argv_additions'], '--mcp-config'):
        servers.update(json.loads(value if value.lstrip().startswith('{') else Path(value).read_text())['mcpServers'])
    served = []
    drain(world.operator)  # T12b B4: the indexer, not the hook, indexes
    for name, server in servers.items():
        env = server_env(spawn, server)
        if not any('provider_session_id' in v for _, v in json_files_in([*server.get('args', []), *env.values()])
                   if isinstance(v, dict)):
            continue
        _, replies = mcp_session(server['command'], server.get('args', []), env, list_tools=False,
                                 calls=[('memory.connection_status', {}), ('memory.search', {'query': marker})])
        assert replies[0].value['status'] == 'ready' and not replies[1].is_error, (name, replies)
        served.append(name)
    assert served, servers


def test_foreign_claude_payloads_are_refused(tmp_path):
    world, fake = _claude_world(tmp_path)
    desk = world.save_desk()
    prepared = world.prepared(world.request(harness='claude', desk_id=desk))
    native, other = prepared['native_session_id'], new_session_id()
    own, foreign = 'Linden own marker', 'Spruce foreign marker'
    transcript = append_rows(claude_transcript(world.claude_root, world.workspace, native),
                             claude_rows(native, world.workspace, f'Remember {own}.', f'Recorded {own}.'))
    other_transcript = append_rows(claude_transcript(world.claude_root, world.workspace, other),
                                   claude_rows(other, world.workspace, f'Remember {foreign}.', 'Fine.'))
    elsewhere = world.root / 'foreign-projects' / transcript.parent.name / transcript.name
    elsewhere.parent.mkdir(parents=True)
    shutil.copyfile(transcript, elsewhere)
    append_rows(elsewhere, claude_rows(native, world.workspace, f'Remember {foreign}.', 'Fine.'))
    other_workspace = world.root / 'other-workspace'
    other_workspace.mkdir()

    record = world.launch(fake, prepared, plan=[
        step('Stop', _claude_payload(other, other_transcript, world.workspace), 'other-session'),
        step('Stop', _claude_payload(native, transcript, other_workspace), 'other-workspace'),
        step('Stop', _claude_payload(native, elsewhere, world.workspace), 'other-transcript'),
        step('PreCompact', _claude_payload(native, other_transcript, world.workspace, 'PreCompact'),
             'other-session-transcript'),
    ])
    for label in ('other-session', 'other-workspace', 'other-transcript', 'other-session-transcript'):
        _all_refused(record, label)
    assert episodes_total(world, native) == 0
    assert world.binding_for(other) == []

    # Control: the exact session's own payload is accepted, and nothing foreign came with it.
    _all_ok(world.launch(fake, prepared, plan=[step('Stop', _claude_payload(native, transcript, world.workspace))]),
            'Stop')
    assert _hits(world, native, own) >= 1
    assert _hits(world, native, foreign) == 0


def test_foreign_codex_first_payloads_are_refused_without_binding(tmp_path):
    world, fake = _codex_world(tmp_path)
    desk = world.save_desk()
    prepared = world.prepared(world.request(harness='codex', desk_id=desk, provider='openai'))
    session, other = new_session_id(), new_session_id()
    own, foreign = 'Poplar own codex marker', 'Yew foreign codex marker'
    rollout = append_rows(codex_rollout(world.codex_root, session),
                          codex_rows(session, world.workspace, f'Remember {own}.', f'Recorded {own}.'))
    outside = world.root / 'foreign-sessions' / rollout.name
    outside.parent.mkdir(parents=True)
    append_rows(outside, codex_rows(session, world.workspace, f'Remember {foreign}.', 'Fine.'))
    other_rollout = append_rows(codex_rollout(world.codex_root, other),
                                codex_rows(other, world.workspace, f'Remember {foreign}.', 'Fine.'))
    other_workspace = world.root / 'other-workspace'
    other_workspace.mkdir()

    record = world.launch(fake, prepared, plan=[
        step('Stop', _codex_payload(session, rollout, other_workspace), 'other-workspace'),
        step('Stop', _codex_payload(session, outside, world.workspace), 'outside-root'),
        step('Stop', _codex_payload(session, other_rollout, world.workspace), 'other-session-transcript'),
    ])
    for label in ('other-workspace', 'outside-root', 'other-session-transcript'):
        _all_refused(record, label)
    assert world.bindings() == [], 'a refused first hook bound a session'

    _all_ok(world.launch(fake, prepared, plan=[step('Stop', _codex_payload(session, rollout, world.workspace))]),
            'Stop')
    assert [b['desk_id'] for b in world.bindings()] == [desk]
    assert _hits(world, session, own) >= 1
    assert _hits(world, session, foreign) == 0
