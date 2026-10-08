"""S1a P1 (a standing approval per desk) and the P3 schedule.

Order: docs/work/orders/S1a-scheduled-summarizer.md, P1 and the falsifier "Standing approval":
- a desk not in the approval: its jobs are never claimed, there are zero reads of its episodes
  and zero requests;
- an admission whose `model_id` is not the approved model gives `not_configured` for that desk;
- removing a desk stops it on the next tick.
P3's schedule: per desk, jobs one at a time until the queue is idle, the gateway refuses or the
per-tick cap is reached; the interval's default and its environment variable.

"Zero reads of its episodes" is measured with the T10 instrument (tests/t10_instruments.py):
no sealed episode payload of the unapproved desk is returned by any statement of the tick.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from s1a_world import IMPL, MODEL, OTHER_MODEL, PROVIDER, VERIF, NetworkGuard, World
from t10_instruments import recording


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _payload_identity(world, episode):
    db = sqlite3.connect(f'file:{world.state / "episodes.sqlite3"}?mode=ro', uri=True)
    try:
        payload = db.execute('SELECT payload FROM episodes WHERE id=?', (episode,)).fetchone()[0]
    finally:
        db.close()
    return 'episode:sha256:' + hashlib.sha256(bytes(payload)).hexdigest()


def test_a_desk_not_in_the_approval_is_never_claimed_read_or_sent(world):
    approved, _ = world.enqueue(IMPL)
    other, other_episode = world.enqueue(VERIF)
    with recording() as recorder:
        status = world.role().tick()
    assert status['status'] == 'ok', status
    assert world.job(IMPL, approved)['state'] == 'succeeded'
    result = world.job(VERIF, other)
    assert result['state'] == 'queued' and result['attempts'] == [], result
    assert _payload_identity(world, other_episode) not in recorder.payloads('episode'), (
        'the tick read a sealed episode of a desk that is not in the approval')
    assert len(world.provider.posts()) == 1
    sources = json.loads(world.provider.posts()[0].body['messages'][1]['content'])['sources']
    assert all(source['text'] in 'Choose pool B; retain pool A for recovery.' for source in sources)
    assert [desk['binding_key'] for desk in status['desks']] == [world.binding[IMPL]]


def test_an_admission_for_another_model_reads_not_configured_for_that_desk(world):
    job, _ = world.enqueue(IMPL)
    stale = world.admit_summarizer(IMPL, 'summarizer-impl-other-model', model=OTHER_MODEL)
    document = world.approval_document()
    document['desks'][0]['memory_config'] = str(stale)
    world.write_approval(document)
    status = world.role().tick()
    assert status['status'] == 'not_configured', status
    desk = status['desks'][0]
    assert desk['status'] == 'not_configured' and 'admission' in desk['missing'], desk
    assert desk['reason'] == 'admission_model_differs', desk
    assert world.provider.calls == []
    assert world.job(IMPL, job)['state'] == 'queued'


def test_an_admission_of_another_provider_instance_is_not_the_roles(world):
    job, _ = world.enqueue(IMPL)
    document = world.approval_document()
    document['desks'][0]['memory_config'] = str(world.capture[IMPL])
    world.write_approval(document)
    status = world.role().tick()
    assert status['status'] == 'not_configured' and status['desks'][0]['reason'] == 'not_the_role_instance', status
    assert world.provider.calls == [] and world.job(IMPL, job)['state'] == 'queued'


def test_an_admission_of_another_desk_is_refused(world):
    job, _ = world.enqueue(IMPL)
    document = world.approval_document()
    document['desks'][0]['memory_config'] = str(world.summarizer[VERIF])
    world.write_approval(document)
    status = world.role().tick()
    assert status['desks'][0]['reason'] == 'admission_desk_differs', status
    assert world.provider.calls == [] and world.job(IMPL, job)['state'] == 'queued'


def test_one_desk_without_admission_leaves_the_other_working(world):
    world.write_approval(desks=(IMPL, VERIF))
    unadmitted = world.admit_summarizer(VERIF, 'summarizer-verif-unadmitted', admit_it=False)
    document = world.approval_document(desks=(IMPL, VERIF))
    document['desks'][1]['memory_config'] = str(unadmitted)
    world.write_approval(document)
    impl_job, _ = world.enqueue(IMPL)
    verif_job, _ = world.enqueue(VERIF)
    status = world.role().tick()
    assert world.job(IMPL, impl_job)['state'] == 'succeeded'
    assert world.job(VERIF, verif_job)['state'] == 'queued'
    by_desk = {desk['binding_key']: desk for desk in status['desks']}
    assert by_desk[world.binding[VERIF]]['status'] == 'not_configured'


def test_removing_a_desk_stops_it_on_the_next_tick(world):
    world.write_approval(desks=(IMPL, VERIF))
    first, _ = world.enqueue(VERIF, ref='a')
    second, _ = world.enqueue(VERIF, ref='b')
    world.role(max_jobs_per_tick=1).tick()
    assert world.job(VERIF, first)['state'] == 'succeeded'
    world.write_approval(desks=(IMPL,))
    world.role().tick()
    result = world.job(VERIF, second)
    assert result['state'] == 'queued' and result['attempts'] == [], result
    assert len(world.provider.posts()) == 1


def test_no_per_digest_list_is_needed_for_an_approved_desk(world):
    jobs = [world.enqueue(IMPL, ref=str(index), text=f'Decision {index}: choose pool B.')[0] for index in range(2)]
    world.role().tick()
    assert [world.job(IMPL, job)['state'] for job in jobs] == ['succeeded', 'succeeded']


def test_the_per_tick_cap_bounds_the_jobs_of_a_desk(world):
    jobs = [world.enqueue(IMPL, ref=str(index))[0] for index in range(3)]
    status = world.role(max_jobs_per_tick=2).tick()
    assert [world.job(IMPL, job)['state'] for job in jobs] == ['succeeded', 'succeeded', 'queued']
    assert status['desks'][0]['jobs'] == {'succeeded': 2} and status['desks'][0]['stopped'] == 'per_tick_cap'


def test_an_idle_queue_ends_the_desk_without_a_request(world):
    status = world.role().tick()
    assert status['status'] == 'ok' and status['desks'][0]['stopped'] == 'idle', status
    assert world.provider.posts() == []


def test_a_failed_job_goes_to_review_and_ends_the_desk_for_this_tick(world):
    first, _ = world.enqueue(IMPL, ref='a')
    second, _ = world.enqueue(IMPL, ref='b')
    world.provider.reported_model = OTHER_MODEL
    status = world.role().tick()
    assert world.job(IMPL, first)['state'] == 'needs_review'
    assert world.job(IMPL, second)['state'] == 'queued'
    assert status['desks'][0]['stopped'] == 'job_failed', status


def test_the_role_never_admits(world):
    def launches():
        db = sqlite3.connect(f'file:{world.state / "sessions.sqlite3"}?mode=ro', uri=True)
        try:
            return db.execute('SELECT provider_instance, provider_session_id, model_id FROM desk_session_launches '
                              'ORDER BY 1, 2').fetchall()
        finally:
            db.close()
    before = launches()
    unadmitted = world.admit_summarizer(IMPL, 'summarizer-never-admitted', admit_it=False)
    document = world.approval_document()
    document['desks'][0]['memory_config'] = str(unadmitted)
    world.write_approval(document)
    world.enqueue(IMPL)
    world.role().tick()
    assert launches() == before


def test_interval_default_and_environment_variable(monkeypatch):
    from kp_agent_tooling._impl.service import summarizer_role as role
    assert role.INTERVAL_VARIABLE == 'AGENT_SUMMARIZER_INTERVAL_SECONDS'
    monkeypatch.delenv(role.INTERVAL_VARIABLE, raising=False)
    assert role.interval_seconds(None) == role.DEFAULT_INTERVAL_SECONDS == 900
    monkeypatch.setenv(role.INTERVAL_VARIABLE, '120')
    assert role.interval_seconds(None) == 120
    assert role.interval_seconds(60) == 60
    for invalid in ('0', 'soon', '86401'):
        monkeypatch.setenv(role.INTERVAL_VARIABLE, invalid)
        with pytest.raises(ValueError):
            role.interval_seconds(None)


def test_the_console_entry_watches_and_reports_not_configured_without_network(tmp_path, monkeypatch, capsys):
    from kp_agent_tooling._impl.service import summarizer_role as role
    guard = NetworkGuard(monkeypatch)
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    code = role.main(['--approval', str(tmp_path / 'absent.json'), '--gateway-config', str(tmp_path / 'g.json'),
                      '--state', str(state), '--watch', '--interval', '1', '--max-ticks', '2'])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert code == 0 and len(lines) == 2, lines
    assert all(line['status'] == 'not_configured' and 'approval' in line['missing'] for line in lines)
    assert guard.attempts == []
