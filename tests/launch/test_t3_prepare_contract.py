"""T3 interface: `kp-agent-launch --config <registry operator config> prepare`.

`prepare` refuses unknown harnesses, unknown desks and any extra field, and it
writes a private per-launch directory. (That the shipped `claude` and `codex`
profiles are enabled is exercised by every P2-P6 test, which use them.)

Readings (reported under AMBIGUITY):
- stdout carries at least the five fields the order lists; extra fields are not
  asserted either way;
- `files` may be a list or a mapping; every absolute path inside it is checked;
- a refusal is a non-zero exit, and a refused request records no binding;
- the default profiles' transcript roots live under the launching user's home,
  so these tests point HOME at a scratch directory and never touch a real home.
"""
from pathlib import Path

from t3_harness import PREPARE_FIELDS, World, flag_values, is_private_dir, is_private_file, paths_in


def _world(tmp_path):
    return World.create(tmp_path / 'world')


def test_prepare_refuses_unknown_harness_unknown_desk_and_extra_fields(tmp_path):
    world = _world(tmp_path)
    desk = world.save_desk()
    good = world.request(harness='claude', desk_id=desk)
    assert set(good) == set(PREPARE_FIELDS)

    refused = {
        'unknown harness': dict(good, harness='harness-nobody-configured'),
        'unknown desk': dict(good, desk_id='desk:00000000-0000-4000-8000-000000000000'),
        'extra field': dict(good, binding_key='binding:' + '0' * 64),
        'extra desk selector': dict(good, desks=[desk]),
    }
    for reason, body in refused.items():
        run = world.prepare(body)
        assert run.code != 0, f'{reason} was prepared\n{run.describe()}'
    assert world.bindings() == [], 'a refused prepare recorded a binding'

    # Control: the same registry prepares the well-formed request.
    prepared = world.prepared(good)
    assert [b['native_session_id'] for b in world.bindings()] == [prepared['native_session_id']]


def test_prepare_writes_a_private_per_launch_directory(tmp_path):
    world = _world(tmp_path)
    desk = world.save_desk()
    launches = [world.prepared(world.request(harness=h, desk_id=desk, task_id=f'task-{i}'))
                for i, h in enumerate(('claude', 'claude', 'codex'))]

    directories = set()
    for output in launches:
        receipt = Path(output['receipt_path'])
        assert receipt.is_absolute() and is_private_file(receipt), output
        assert is_private_dir(receipt.parent), f'launch directory is not private: {receipt.parent}'
        directories.add(receipt.parent)
        files = paths_in(output['files'])
        assert files, f'prepare listed no files: {output!r}'
        for path in files:
            assert is_private_file(path), f'launch file is missing or not private: {path}'
            assert path.resolve().is_relative_to(receipt.parent.resolve()), (path, receipt.parent)
    for output in launches[:2]:
        for flag in ('--mcp-config', '--settings'):
            for value in flag_values(output['argv_additions'], flag):
                assert is_private_file(Path(value)), f'{flag} {value} is not a private file'
    # One directory per launch; minted sessions are distinct.
    assert len(directories) == len(launches), directories
    assert launches[0]['native_session_id'] != launches[1]['native_session_id']
    # Every launch binds for the registry config's harness instance.
    assert {b['native_session_id'] for b in world.bindings()} == {
        launches[0]['native_session_id'], launches[1]['native_session_id']}
