"""T3 P1: swap by configuration.

An operator profile file (named by `harness_profiles_path` in the registry
descriptor) adds `claude-alt`, which reuses the claude strategies and parser and
points at a different executable path. A desk task launched with it is
prepared, bound with `harness` = `claude-alt`, and captured, with no code
change. Disabling a profile makes `prepare` refuse it.
Falsifier: a code change is needed, or a disabled profile is still prepared.

Readings (reported under AMBIGUITY): the operator file is
`{schema_version, profiles: [...]}` with the id in each profile's `harness`;
these are the only tests that write an operator profile file (every other test
uses the shipped defaults); the hook commands are exercised by a fake executable
that runs whatever the generated settings file configures for the event.
"""
from t12b_seams import drain  # T12b B4: the one drain helper
from t3_harness import (FakeHarness, World, append_rows, claude_profile, claude_rows, claude_transcript,
                        describe_runs, memory, runs_for, search_hits, step)


def test_operator_profile_adds_a_harness_without_code_change(tmp_path):
    root = (tmp_path / 'world').resolve()
    projects = root / 'alt-projects'
    alternate = FakeHarness.create(root.parent / 'alt-bin' / 'nested', 'claude', name='claude-alt')
    world = World.create(root, profiles=[claude_profile('claude-alt', executable=alternate.executable,
                                                        root=projects)])
    projects.mkdir()
    desk = world.save_desk(name='Alternate desk')

    prepared = world.prepared(world.request(harness='claude-alt', desk_id=desk, provider='anthropic',
                                            model='alt-model', task_id='task-alt'))
    native = prepared['native_session_id']
    assert [(b['harness'], b['desk_id'], b['source'], b['model']) for b in world.binding_for(native)] == [
        ('claude-alt', desk, 'board', 'alt-model')]

    marker = 'Juniper alternate harness marker'
    transcript = append_rows(claude_transcript(projects, world.workspace, native),
                             claude_rows(native, world.workspace, f'Please remember: {marker}.',
                                         f'Noted, {marker} recorded.'))
    record = world.launch(alternate, prepared, plan=[step('Stop', {
        'session_id': native, 'transcript_path': str(transcript), 'cwd': str(world.workspace),
        'hook_event_name': 'Stop', 'stop_hook_active': False})])
    runs = runs_for(record, 'Stop')
    assert runs and all(r['code'] == 0 for r in runs), describe_runs(record['runs'])

    drain(world.operator)  # T12b B4: the indexer, not the hook, indexes
    _, replies = memory(world, native, calls=[('memory.search', {'query': marker})])
    assert search_hits(replies[0], marker), replies[0].value


def test_a_disabled_profile_is_refused(tmp_path):
    root = (tmp_path / 'world').resolve()
    fake = FakeHarness.create(root.parent / 'bin', 'claude')
    alternate = FakeHarness.create(root.parent / 'alt-bin', 'claude', name='claude-alt')
    projects = root / 'alt-projects'
    world = World.create(root, profiles=[
        claude_profile('claude', executable=fake.executable, root=projects, enabled=False),
        claude_profile('claude-alt', executable=alternate.executable, root=projects, enabled=False)])
    desk = world.save_desk()

    for harness in ('claude', 'claude-alt'):
        run = world.prepare(world.request(harness=harness, desk_id=desk))
        assert run.code != 0, f'disabled profile {harness} was prepared\n{run.describe()}'
    assert world.bindings() == []

    # Enabling it again is a configuration change only.
    world.write_profiles([
        claude_profile('claude', executable=fake.executable, root=projects, enabled=False),
        claude_profile('claude-alt', executable=alternate.executable, root=projects, enabled=True)])
    prepared = world.prepared(world.request(harness='claude-alt', desk_id=desk))
    assert [b['harness'] for b in world.binding_for(prepared['native_session_id'])] == ['claude-alt']
    assert world.prepare(world.request(harness='claude', desk_id=desk)).code != 0
