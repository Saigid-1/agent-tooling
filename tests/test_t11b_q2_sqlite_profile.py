"""T11b Q2: one SQLite profile (docs/work/orders/T11b-leaf-behaviour.md, Q2, L2).

Order: "Every connection uses a resolved URI, ruled here. The 13 sites that do not resolve today
change how a symlinked state_root behaves" ... "The default stays at sqlite3's 5.0. The sites with 10
and 30 keep theirs, and isolation_level=None and check_same_thread=False stay where they are."
Falsifier: "a connection opened through a symlinked parent that reaches a file other than the
resolved target; a timeout that differs from its T11a value (measured with the T10 instrument)."

Instrument: T11a's P4 recorder (tests/fixtures/t11a/generate_connection_profiles.py, which also runs
every step under the T10 recorder), unchanged, driving the same public steps as the P4 golden: the
portable desk-memory state lives behind a symlinked directory (``<link>`` -> ``<real>``). Two
additions here, neither of which changes the recorder:
- each recorded ``sqlite3.connect`` also notes, at the moment of the call, whether its database is a
  ``file:`` URI (``uri=True``) and whether that URI's path is its own ``os.path.realpath`` (resolved);
- the launch steps run a second time with the registry world reached through a symlinked parent
  (the P4 golden's launch world is resolved when it is created, so a site that does not resolve is
  invisible there): its operator file, descriptor and paths name ``<link>/…``.

Readings (repeated under AMBIGUITY): "reaches a file other than the resolved target" is read as "its
database argument names a path other than its resolved target": through a symlinked parent the
open itself lands on the same inode either way, but SQLite derives its -journal/-wal names from
the name it is given, and Q1's check needs the resolved path. ``:memory:`` is not a file and is
excluded. "Its T11a value" is the P4 golden's profile at the same step and position.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / 'launch'))

from t11a_golden import FIXTURES, load_generator, scratch  # noqa: E402
from t11b_audit import path_of  # noqa: E402
from test_t11b_regression_set import b2_step  # noqa: E402

COMPARED = ('timeout', 'isolation_level', 'check_same_thread')


def resolving_recorder(generator):
    class Resolving(generator.Recorder):
        def record(self, args, kwargs):
            super().record(args, kwargs)
            bound = dict(zip(generator.PARAMETERS, args))
            bound.update(kwargs)
            database = bound['database']
            text = os.fsdecode(database) if isinstance(database, bytes) else os.fspath(database)
            uri = bool(bound.get('uri', False))
            path = None if text == ':memory:' else (path_of(text) if uri else os.path.abspath(text))
            self.steps[self.current][-1]['q2'] = {
                'site': self._site(), 'uri': uri,
                'resolved': path is not None and path == os.path.realpath(path)}
    return Resolving


def linked_launch_steps(generator, recorder, real, link):
    """P4's launch steps, with the registry world's operator file, descriptor and paths named through ``link``."""
    from t3_harness import World, append_rows, codex_rollout, codex_rows
    from kp_agent_tooling._impl.service import launch_binding
    from kp_agent_tooling._impl.service.desk_memory_runtime import components
    from kp_agent_tooling._impl.service.session_bindings import list_records
    world = World.create(real / 'launch-world')
    desk = world.save_desk()
    base = link / 'launch-world'

    def rewrite(path, **values):
        document = json.loads(path.read_text())
        document.update(values)
        path.write_text(json.dumps(document))
        path.chmod(0o600)
    rewrite(world.operator, state_root=str(base / 'state'), catalog_path=str(base / world.descriptor.name),
            workspace_root=str(base / 'workspace'))
    rewrite(world.descriptor, roster_path=str(base / world.roster.name))
    operator = base / world.operator.name
    workspace = base / 'workspace'
    session = '019a0000-0000-7000-8000-00000000b11b'
    prepared = {}
    with generator.environment(HOME=str(base / 'home'), CLAUDE_CONFIG_DIR=None, CODEX_HOME=None):
        recorder.step('linked launch: prepare codex', lambda: prepared.update(codex=launch_binding.prepare(
            operator, world.request(harness='codex', desk_id=desk, provider='openai', model='fixture-model',
                                    task_id='task-q2', workspace=workspace))))
        rollout = append_rows(codex_rollout(base / 'home' / '.codex' / 'sessions', session),
                              codex_rows(session, world.workspace, 'hello é', 'answer'))
        payload = {'session_id': session, 'transcript_path': str(rollout), 'cwd': str(workspace),
                   'hook_event_name': 'Stop', 'model': 'fixture-model', 'turn_id': 'turn-1',
                   'stop_hook_active': False}
        recorder.step('linked launch: first codex hook',
                      lambda: launch_binding.hook(prepared['codex']['receipt_path'], payload))
        recorder.step('linked launch: prepare claude', lambda: prepared.update(claude=launch_binding.prepare(
            operator, world.request(harness='claude', desk_id=desk, task_id='task-q2-claude', workspace=workspace))))
        recorder.step('linked launch: session bindings', lambda: list_records(components(operator)[2]))


def recorded(part):
    generator = load_generator('generate_connection_profiles')
    with scratch('t11b-q2-') as root:
        real = root / 'real'
        real.mkdir()
        link = root / 'link'
        link.symlink_to(real, target_is_directory=True)
        recorder = resolving_recorder(generator)(generator.normaliser(root, link, real))
        if part == 'core':
            generator.desk_memory_steps(recorder, root, link)
            generator.gateway_steps(recorder, link)
            generator.launch_steps(recorder, root)
            linked_launch_steps(generator, recorder, real, link)
        else:
            generator.ops_steps(recorder, link)
        steps = json.loads(json.dumps(recorder.steps))
        shutil.rmtree(root / 'launch-world', ignore_errors=True)
    return steps


def unresolved(steps):
    found = []
    for step, profiles in steps.items():
        for index, profile in enumerate(profiles):
            if 'step_error' in profile:
                found.append(f'{step}: the step failed: {profile["step_error"]}')
                continue
            if profile['database']['value'] == ':memory:':
                continue
            q2 = profile['q2']
            if not (q2['uri'] and q2['resolved']):
                why = 'not a URI' if not q2['uri'] else 'a URI of an unresolved path'
                found.append(f'{step} #{index}: {why}: {profile["database"]["value"]}  [{q2["site"]}]')
    return found


@pytest.mark.parametrize('part', ['core', 'ops'])
def test_q2_every_connection_reaches_its_resolved_target(part):
    """GREEN-IF every recorded product connection (``:memory:`` aside), over P4's steps plus the launch steps
    through a symlinked parent, is opened by a ``file:`` URI (uri=True) whose path is its own realpath at the
    moment of the open."""
    if part == 'ops':
        pytest.importorskip('kp_agent_tooling_ops', reason='the ops stores need extensions/ops installed')
    found = unresolved(recorded(part))
    sites = sorted({line.rsplit('[', 1)[-1].rstrip(']') for line in found if '[' in line})
    assert not found, (f'{len(found)} connections ({len(sites)} distinct sites) do not reach their resolved '
                       'target by a resolved URI:\n' + '\n'.join(found) + '\nsites:\n  ' + '\n  '.join(sites))


@pytest.mark.parametrize('part', ['core', 'ops'])
def test_q2_timeouts_and_profile_options_equal_t11a(part):
    """GREEN-IF, for every P4 step, the same number of connections is opened as in T11a's golden, and each
    connection's timeout, isolation_level and check_same_thread equal the golden's at the same position.

    T12b (Amendment 1, meet): a step that reads as T12b's B2 kind (``test_t11b_regression_set.b2_step``: its index
    runs replaced by the drain or the build-and-swap reindex) changes its connection count by design. There the
    positional comparison gives way to the B2 reading, which already holds Q2's property: every kept connection is
    identical but for B1's first statement, and every drain or reindex connection carries the open profile
    (timeout, isolation_level, check_same_thread among it) of a base connection of the same kind."""
    if part == 'ops':
        pytest.importorskip('kp_agent_tooling_ops', reason='the ops stores need extensions/ops installed')
    generator = load_generator('generate_connection_profiles')
    golden = json.loads((FIXTURES / generator.GOLDENS[part]).read_text())['steps']
    text, _ = generator.build(part)
    here = json.loads(text)['steps']
    problems = []
    for step in sorted(set(golden) | set(here)):
        base = [p for p in golden.get(step, []) if 'database' in p]
        head = [p for p in here.get(step, []) if 'database' in p]
        if b2_step(golden.get(step, []), here.get(step, [])) is not None:
            continue  # a T12b B2 step: the B2 reading holds Q2's property (docstring)
        if len(base) != len(head):
            problems.append(f'{step}: {len(head)} connections, T11a opened {len(base)}')
        for index, (old, new) in enumerate(zip(base, head)):
            for name in COMPARED:
                if old[name] != new[name]:
                    problems.append(f'{step} #{index} {old["database"]["value"]}: {name} {new[name]!r}, '
                                    f'T11a {old[name]!r}')
    assert not problems, 'connection profiles differ from T11a beyond the resolved URI:\n' + '\n'.join(problems)
