"""S1a falsifiers: the standing approval (P1).

Order: docs/work/orders/S1a-scheduled-summarizer.md, P1 (what the approval permits, revocation) and the
"Standing approval" falsifiers. Seams: tests/s1a_seams.py. Instruments: tests/s1a_harness.py (network guard,
loopback stub, queue reads) and tests/t10_instruments.py (statements and the sealed payload rows they return).

Each world has two desks, each with one queued job of one episode carrying a unique marker text; the role's
admission is made with the existing `admit`. The manual `work-once --approved-digests` path is guarded by its
existing tests, run unedited (tests/test_episodic_queue.py and
tests/test_assistant_memory_slice.py::test_public_queue_work_once_passes_policy_to_real_summarizer).

GREEN-IF, per test:
- `test_desk_absent_from_the_approval_is_never_read_claimed_or_sent`: desks `alpha` (approved) and `beta`
  (admitted, NOT approved). One tick: control, `alpha`'s job succeeded with one POST; `beta`'s job is
  untouched (queued, never leased, no attempt); no statement on the queue writes with `beta`'s binding; no
  statement on the episode store names `beta`'s binding or episode, and no `beta` episode payload row is
  returned (zero reads of its episodes); no request body carries `beta`'s marker.
- `test_admission_for_another_model_is_not_configured_for_that_desk`: desks `alpha` (approved, its role
  admission made for another model) and `gamma` (approved, admitted for the approved model). One tick: a
  `not_configured` status node names `alpha` (its binding key, desk id or the role's session id for it);
  `alpha`'s job is untouched and no request carries its marker; control, `gamma`'s job succeeded.
- `test_removing_a_desk_takes_effect_on_the_next_tick`: desk `alpha` approved; the watch loop runs three
  ticks. After tick 1 (alpha's first job succeeded) the harness enqueues a second job and removes alpha from
  the approval; tick 2 leaves that job untouched with no request; after tick 2 alpha is approved again and
  tick 3 sends it (one POST) and it succeeds (so tick 2 really ran with alpha removed).

Mutants that must be RED: approve every admitted desk (ignore the approval's desk list); ignore the
admission's model; read or claim before checking the approval; cache the approval across ticks.
"""
from __future__ import annotations

import s1a_seams as seams
from s1a_harness import assert_no_child_process, desk_id, make_world, run_role  # noqa: F401 - fixture


def _carries(run, marker):
    return [r.raw_path for r in run.requests if marker.encode() in r.body]


def test_desk_absent_from_the_approval_is_never_read_claimed_or_sent(make_world):
    world = make_world(('alpha', 'beta')).build(approved=('alpha',))
    run = run_role(world, record=True)
    assert_no_child_process(run)
    alpha, beta = world.job('alpha'), world.job('beta')
    assert alpha['state'] == 'succeeded' and len(run.posts()) == 1, (
        f'control: the approved desk was not summarized: {alpha}\n{run.describe()}')
    assert world.untouched(beta), f'the unapproved desk\'s job was touched: {beta} {world.attempts(beta["id"])}'
    binding, episode = world.bindings['beta'], world.episodes['beta'][0]
    recorder = run.recorder
    queue_writes = [s.sql for s in recorder.matching(r'^\s*(INSERT|UPDATE|DELETE|REPLACE)\b', db=world.queue_path)
                    if binding in s.sql]
    assert not queue_writes, f'queue writes for the unapproved desk: {queue_writes}'
    episode_reads = [s.sql for s in recorder.top(('call',)) if s.db == str(world.episodes_path.resolve())
                     and (binding in s.sql or episode in s.sql)]
    assert not episode_reads, f'statements on the unapproved desk\'s episodes: {episode_reads}'
    payloads = recorder.payloads('episode', phases=('call',))
    assert episode not in payloads, f'the unapproved desk\'s episode payload was read ({payloads[episode]} rows)'
    assert not _carries(run, world.markers['beta'][0]), 'a request carried the unapproved desk\'s source'


def test_admission_for_another_model_is_not_configured_for_that_desk(make_world):
    world = make_world(('alpha', 'gamma'))
    world.build(admitted=('gamma',))
    world.admit_role('alpha', model='s1a/other-model')
    run = run_role(world)
    assert_no_child_process(run)
    names = (world.bindings['alpha'], desk_id('alpha'), *world.role_sessions['alpha'])
    nodes = [n for n in run.status_nodes() if any(name in str(n) for name in names)]
    assert nodes, (f'no `{seams.NOT_CONFIGURED}` status names the desk admitted for another model:\n'
                   f'{run.describe()}')
    alpha, gamma = world.job('alpha'), world.job('gamma')
    assert world.untouched(alpha), f'the job of the desk admitted for another model was touched: {alpha}'
    assert not _carries(run, world.markers['alpha'][0]), 'a request carried that desk\'s source'
    assert gamma['state'] == 'succeeded', f'control: the correctly admitted desk was not summarized: {gamma}'


def test_removing_a_desk_takes_effect_on_the_next_tick(make_world):
    world = make_world(('alpha',)).build()
    jobs, posts_at = {}, {}

    def between(pause):
        posts_at[pause] = sum(1 for r in world.stub.requests if r.method == 'POST')
        if pause == 1:
            jobs['first'] = world.job('alpha', 0)
            jobs['second'] = world.enqueue('alpha', [world.seal('alpha')])
            world.write_approval(())
        elif pause == 2:
            row = next(j for j in world.jobs('alpha') if j['id'] == jobs['second'])
            jobs['after_tick_2'] = (row, world.attempts(row['id']), world.untouched(row))
            world.write_approval(('alpha',))

    run = run_role(world, watch_passes=3, between=between)
    assert_no_child_process(run)
    assert run.sleeps >= 2 and 'after_tick_2' in jobs, (
        f'the watch loop did not pause between three ticks through time.sleep (pauses: {run.sleeps}):\n'
        f'{run.describe()}')
    assert jobs['first']['state'] == 'succeeded' and posts_at[1] == 1, (
        f'tick 1 did not summarize the approved desk: {jobs["first"]}, POSTs {posts_at}')
    row, attempts, untouched = jobs['after_tick_2']
    assert untouched and posts_at[2] == posts_at[1], (
        f'tick 2 worked a desk removed from the approval: {row} attempts {attempts}, POSTs {posts_at}')
    second = next(j for j in world.jobs('alpha') if j['id'] == jobs['second'])
    assert second['state'] == 'succeeded' and len(run.posts()) == 2, (
        f'control: tick 3, with the desk approved again, did not send it: {second}\n{run.describe()}')

