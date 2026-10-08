"""T4 P2: fast, isolated hooks.

`kp-agent-host hook` finishes in under 1 s with Docker unavailable. It writes
only inside its spool, and cannot choose a desk. Ingestion refuses a foreign
payload (another session, workspace or transcript) and never double-captures
after a restart.
Falsifier: a hook that blocks or needs Docker, a write outside the spool, a
payload that picks a desk, or a duplicate capture.

Readings (reported under AMBIGUITY):
- the hook is run exactly as the harness runs it: the command the generated
  configuration names, through `/bin/sh -c`, with the harness's recorded
  environment; "Docker unavailable" is that environment with every PATH entry
  holding a `docker` removed; a `docker` that never answers (a stalled daemon)
  must not stall it either; the 1 s bound is wall-clock for the whole command,
  interpreter start included, for every run;
- "its spool" is the configured `spool_root`: every write the hook makes must be
  under it (the event itself lands in `<spool_root>/<launch_id>/events.jsonl`);
  the rest of the world (state root, transcripts, workspace, configuration, HOME,
  the hook's TMPDIR) must be byte-identical afterwards;
- a spool row carries the payload as a nested object or as its JSON text, and
  the launch id; the receipt reference's form is not asserted;
- the hook's payload bound is not stated: a 32 MiB payload must not be spooled
  whole, and the hook still works afterwards;
- Coordinator clarification (2026-10-01): a hook exits 0 or 1, never 2, so it
  can never block the harness (for example a Claude PreCompact); checked on every
  adapter hook run, including an oversized and a malformed payload;
- a payload that names a desk, binding, harness, source, parent, another
  launch or another receipt may be refused or ignored, but never followed;
- ingestion is a watcher; a refused event is passed over and a later valid event
  in the same spool file is still ingested, so the valid event is the proof that
  the refused ones before it were processed; a restart is a new watcher process.
"""
import os
import shutil

from host_harness import (CLAUDE_EVENTS, HostWorld, append_rows, assert_hooks_call_the_adapter, claude_payload,
                          claude_rows, claude_transcript, codex_meta, codex_rollout, new_session_id,
                          path_without_docker, row_carries, snapshot, snapshot_diff, write_docker)


def _transcript(hw, native, *texts):
    return append_rows(claude_transcript(hw.claude_root, hw.workspace, native), claude_rows(native, hw.workspace, *texts))


def test_hook_finishes_under_one_second_with_docker_unavailable(tmp_path):
    hw = HostWorld.create(tmp_path / 'w', mode='docker', profiles='absolute')
    desk = hw.save_desk()
    launched = hw.launch('claude', desk).ok()
    commands = assert_hooks_call_the_adapter(launched, CLAUDE_EVENTS)
    native = launched.native
    transcript = _transcript(hw, native, 'Remember the timing marker.', 'Recorded the timing marker.')

    env = dict(launched.record['env'])
    env['PATH'] = path_without_docker(env['PATH'])
    assert shutil.which('docker', path=env['PATH']) is None
    hang = hw.root / 'hang-bin'
    hang_log = hw.root / 'logs' / 'hang-docker.jsonl'
    write_docker(hang, mode='hang', log=hang_log)
    stalled = dict(env, PATH=os.pathsep.join([str(hang), env['PATH']]))

    timings, sent = [], []
    for condition, run_env, rounds in (('no docker', env, 5), ('stalled docker', stalled, 1)):
        for index in range(rounds):
            for event in ('Stop', 'PreCompact'):
                payload = claude_payload(native, transcript, hw.workspace, event, t4_probe=f'{condition}-{index}')
                for command in commands[event]:
                    code, _, err, seconds = hw.run_hook(command, payload, env=run_env, cwd=hw.workspace, timeout=90)
                    timings.append((condition, event, round(seconds, 4)))
                    assert code == 0, f'{event} hook failed ({condition}): exit {code}\n{err[-2000:]}'
                sent.append(payload)
    print('T4 P2 hook wall-clock seconds:', timings)
    slow = [t for t in timings if t[2] >= 1.0]
    assert not slow, f'hook took 1 s or more: {slow}; all runs: {timings}'
    assert not hang_log.exists(), 'the hook called docker'
    rows = hw.spool_rows(launched.launch_id)
    missing = [p['t4_probe'] for p in sent if not any(row_carries(row, p) for row in rows)]
    assert not missing, f'hook events missing from the spool: {missing}'


def test_hook_writes_only_inside_its_spool(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk()
    other_desk = hw.save_desk(role='Curator', name='Other desk')
    launched = hw.launch('claude', desk).ok()
    commands = assert_hooks_call_the_adapter(launched, CLAUDE_EVENTS)
    native, stranger = launched.native, new_session_id()
    transcript = _transcript(hw, native, 'Remember the isolation marker.', 'Recorded the isolation marker.')
    env = dict(launched.record['env'])
    assert env.get('TMPDIR') == str(hw.tmp)
    payloads = [claude_payload(native, transcript, hw.workspace, event) for event in CLAUDE_EVENTS]
    payloads.append(claude_payload(native, transcript, hw.workspace, 'Stop', desk_id=other_desk))
    payloads.append(claude_payload(stranger, claude_transcript(hw.claude_root, hw.workspace, stranger),
                                   hw.workspace, 'Stop'))

    before = snapshot(hw.root, exclude=(hw.spool,))
    for payload in payloads:
        for command in commands[payload['hook_event_name']]:
            code, _, err, _ = hw.run_hook(command, payload, env=env, cwd=hw.workspace)
            assert code == 0, err[-2000:]
    after = snapshot(hw.root, exclude=(hw.spool,))
    assert snapshot_diff(before, after) == [], 'the hook wrote outside its spool'
    rows = hw.spool_rows(launched.launch_id)
    for payload in payloads:
        assert any(row_carries(row, payload) and launched.launch_id in str(row) for row in rows), payload
    # The hook neither bound nor captured anything.
    assert [b['native_session_id'] for b in hw.bindings()] == [native]
    assert hw.episodes(native) == 0


def test_oversized_payload_is_not_spooled_whole(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk()
    launched = hw.launch('claude', desk).ok()
    command = assert_hooks_call_the_adapter(launched, CLAUDE_EVENTS)['Stop'][0]
    native = launched.native
    transcript = _transcript(hw, native, 'Remember the bound marker.', 'Recorded the bound marker.')
    env = dict(launched.record['env'])
    spool_file = hw.spool_file(launched.launch_id)
    size = spool_file.stat().st_size if spool_file.exists() else 0

    huge = 32 * 1024 * 1024
    payload = claude_payload(native, transcript, hw.workspace, 'PreCompact', custom_instructions='x' * huge)
    code, _, _, seconds = hw.run_hook(command, payload, env=env, cwd=hw.workspace, timeout=120)
    grown = (spool_file.stat().st_size if spool_file.exists() else 0) - size
    assert grown < huge, f'a {huge}-byte payload was spooled whole ({grown} bytes appended)'
    assert seconds < 1.0, f'the hook took {seconds:.3f} s on an oversized payload'
    assert code in (0, 1), f'the hook exited {code} on an oversized payload; 2 would block the harness'

    code, _, _, seconds = hw.run_hook(command, None, env=env, cwd=hw.workspace, raw=b'{"session_id": not json')
    assert code in (0, 1), f'the hook exited {code} on a malformed payload; 2 would block the harness'
    assert seconds < 1.0, f'the hook took {seconds:.3f} s on a malformed payload'

    normal = claude_payload(native, transcript, hw.workspace, 'Stop', t4_probe='after-oversized')
    code, _, err, _ = hw.run_hook(command, normal, env=env, cwd=hw.workspace)
    assert code == 0, err[-2000:]
    assert any(row_carries(row, normal) for row in hw.spool_rows(launched.launch_id))


def test_a_payload_cannot_choose_a_desk_or_another_launch(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk(name='Receipt desk')
    intruder = hw.save_desk(role='Curator', name='Intruder desk')
    # The receipt most dangerous to follow: an unbound Codex launch on another desk binds the first session it sees.
    other = hw.launch('codex', intruder, provider='openai').ok()
    # An operator-bound session that reads the intruder desk's memory (T2 registry `bind`).
    probe = new_session_id()
    hw.world.registry('bind', {'harness': 'probe', 'provider': 'probe', 'model': 'probe', 'native_session_id': probe,
                               'desk_id': intruder, 'source': 'operator', 'workspace': str(hw.workspace),
                               'parent_session_id': None}).ok()
    selectors = {'desk_id': intruder, 'desk': intruder, 'binding_key': hw.desk_key(intruder), 'harness': 'intruder',
                 'source': 'operator', 'provider': 'intruder', 'model': 'intruder', 'parent_session_id': probe,
                 'launch_id': other.launch_id, 'launch': other.launch_id, 'receipt': other.receipt,
                 'receipt_path': other.receipt}
    marker, sentinel = 'Holly selector marker', 'Ivy sentinel marker'
    claude = hw.launch('claude', desk, plan=[
        hw.claude_turn('selectors', f'Remember {marker}.', f'Recorded {marker}.', **selectors),
        hw.claude_turn('sentinel', f'Remember {sentinel}.', f'Recorded {sentinel}.')]).ok()
    session = new_session_id()
    codex = hw.launch('codex', desk, provider='openai', plan=[
        hw.codex_turn('selectors', session, f'Remember {marker}.', meta=codex_meta(session, hw.workspace),
                      **selectors),
        hw.codex_turn('sentinel', session, f'Recorded {sentinel}.')]).ok()
    native = claude.native
    before = hw.binding_for(native)

    hw.ingest(lambda: hw.hits_now(native, sentinel) >= 1 and hw.bound_now(session)
              and hw.hits_now(session, sentinel) >= 1)
    after = hw.binding_for(native)
    assert [(b['desk_id'], b['harness'], b['source'], b['parent_session_id'], b['recorded_at']) for b in after] == [
        (desk, 'claude', 'host', None, before[0]['recorded_at'])], after
    assert [(b['desk_id'], b['harness'], b['source'], b['parent_session_id']) for b in hw.binding_for(session)] == [
        (desk, 'codex', 'host', None)], hw.binding_for(session)
    assert [b['native_session_id'] for b in hw.bindings() if b['desk_id'] == intruder] == [probe]
    for text in (marker, sentinel):
        assert hw.hits(probe, text) == 0, f'{text!r} reached the desk the payload named'
    if hw.spool_file(other.launch_id).exists():
        foreign = [row for row in hw.spool_rows(other.launch_id)
                   if any(row_carries(row, run['payload']) for run in claude.record['runs'] + codex.record['runs'])]
        assert foreign == [], 'a payload was spooled under the launch it named'


def test_ingestion_refuses_foreign_claude_payloads(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk()
    other, own, foreign = new_session_id(), 'Linden own marker', 'Spruce foreign marker'
    elsewhere_root = hw.root / 'foreign-projects'
    other_workspace = hw.root / 'other-workspace'
    other_workspace.mkdir()
    launched = hw.launch('claude', desk, plan=[
        hw.claude_turn('other-session', f'Remember {foreign}.', 'Fine.', session=other),
        hw.claude_turn('other-workspace', f'Remember {foreign}.', 'Fine.', cwd=other_workspace),
        hw.claude_turn('other-transcript', f'Remember {foreign}.', 'Fine.', root=elsewhere_root),
        hw.claude_turn('own', f'Remember {own}.', f'Recorded {own}.'),
    ]).ok()
    native = launched.native
    for label in ('other-session', 'other-workspace', 'other-transcript', 'own'):
        assert launched.runs(label), f'no hook ran for {label}'

    hw.ingest(lambda: hw.hits_now(native, own) >= 1)
    assert hw.hits(native, foreign) == 0, 'a foreign payload was captured'
    assert hw.binding_for(other) == []
    assert [b['native_session_id'] for b in hw.bindings()] == [native]


def test_ingestion_refuses_foreign_codex_payloads(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk()
    session, other = new_session_id(), new_session_id()
    own, later, foreign = 'Poplar own codex marker', 'Hawthorn later codex marker', 'Yew foreign codex marker'
    other_workspace = hw.root / 'other-workspace'
    other_workspace.mkdir()
    outside = hw.root / 'foreign-sessions' / codex_rollout(hw.codex_root, session).name
    other_rollout = codex_rollout(hw.codex_root, other)
    launched = hw.launch('codex', desk, provider='openai', plan=[
        hw.codex_turn('other-workspace', session, f'Remember {own}.', meta=codex_meta(session, hw.workspace),
                      cwd=other_workspace),
        hw.codex_turn('outside-root', session, f'Remember {foreign}.', meta=codex_meta(session, hw.workspace),
                      transcript=outside),
        hw.codex_turn('other-session-transcript', session, f'Remember {foreign}.',
                      meta=codex_meta(other, hw.workspace), transcript=other_rollout),
        hw.codex_turn('own', session, f'Recorded {own}.'),
        hw.codex_turn('second-session', other, f'Recorded {foreign}.'),
        hw.codex_turn('own-later', session, f'Next {later}.', start_role='user'),
    ]).ok()
    for label in ('other-workspace', 'outside-root', 'other-session-transcript', 'own', 'second-session',
                  'own-later'):
        assert launched.runs(label), f'no hook ran for {label}'

    hw.ingest(lambda: hw.hits_now(session, later) >= 1)
    assert [(b['native_session_id'], b['desk_id']) for b in hw.bindings()] == [(session, desk)]
    assert hw.hits(session, own) >= 1
    assert hw.hits(session, foreign) == 0, 'a foreign payload was captured'
    assert not hw.ready(other)


def test_ingestion_never_double_captures_after_a_restart(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk()
    first, second, third = 'Maple first restart marker', 'Oak second restart marker', 'Elm third restart marker'
    launched = hw.launch('claude', desk, plan=[
        hw.claude_turn('stop', f'Remember {first}.', f'Recorded {first}.'),
        hw.claude_turn('stop-again'),  # the same Stop event fired twice, as a harness may
    ]).ok()
    native = launched.native
    command = launched.hook_commands()['Stop'][0]
    env = dict(launched.record['env'])
    transcript = claude_transcript(hw.claude_root, hw.workspace, native)

    hw.ingest(lambda: hw.hits_now(native, first) >= 1, label='first-run')
    episodes, first_hits = hw.episodes(native), hw.hits(native, first)

    # Restart with one new turn: only the new turn is added.
    append_rows(transcript, claude_rows(native, hw.workspace, f'Next {second}.', f'Kept {second}.'))
    code, _, err, _ = hw.run_hook(command, claude_payload(native, transcript, hw.workspace), env=env,
                                  cwd=hw.workspace)
    assert code == 0, err
    hw.ingest(lambda: hw.hits_now(native, second) >= 1, label='restart')
    assert hw.hits(native, first) == first_hits
    grown = hw.episodes(native)
    assert grown > episodes
    second_hits = hw.hits(native, second)

    # Restart again with a replayed event before the new turn: still nothing doubles.
    code, _, err, _ = hw.run_hook(command, claude_payload(native, transcript, hw.workspace), env=env,
                                  cwd=hw.workspace)
    assert code == 0, err
    append_rows(transcript, claude_rows(native, hw.workspace, f'Then {third}.', f'Kept {third}.'))
    code, _, err, _ = hw.run_hook(command, claude_payload(native, transcript, hw.workspace, 'PreCompact'),
                                  env=env, cwd=hw.workspace)
    assert code == 0, err
    hw.ingest(lambda: hw.hits_now(native, third) >= 1, label='second-restart')
    assert hw.hits(native, first) == first_hits
    assert hw.hits(native, second) == second_hits
    assert hw.episodes(native) > grown
