"""Synthetic native rollouts exercise bounded capture and durable ownership."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from unittest.mock import patch

from test_portable_desk_memory import episode_store
from test_portable_desk_memory import config
from kp_agent_tooling._impl.service.session_import_job import ImportJobError, SessionImportJobs
from kp_agent_tooling._impl.service.session_sources import SessionSources
from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
from kp_agent_tooling.session_import_cli import main as cli_main


NATIVE = 'a55869b4-6ab4-4e5c-b028-8d4b31d27b71'


def line(value):
    return json.dumps(value, separators=(',', ':')).encode() + b'\n'


def meta():
    return line({'type': 'session_meta', 'timestamp': '2026-09-24T12:00:00Z',
                 'payload': {'id': NATIVE, 'cwd': '/synthetic/repo'}})


def user(text, *, kind='event_msg'):
    if kind == 'event_msg':
        return line({'type': kind, 'timestamp': '2026-09-24T12:01:00Z',
                     'payload': {'type': 'user_message', 'message': text}})
    return line({'type': 'response_item', 'timestamp': '2026-09-24T12:02:00Z',
                 'payload': {'type': 'message', 'role': 'user',
                             'content': [{'type': 'input_text', 'text': text}]}})


def assistant(text):
    return line({'type': 'response_item', 'timestamp': '2026-09-24T12:03:00Z',
                 'payload': {'type': 'message', 'role': 'assistant',
                             'content': [{'type': 'output_text', 'text': text}]}})


def setup(tmp_path, *, mode='full', follow=False, batch_bytes=4_000_000):
    store = episode_store(tmp_path)
    SessionSources(store).upgrade()
    tenant = store.sessions.resolve('session-1', store.registry).tenant_id
    jobs = SessionImportJobs(store, tenant)
    source = tmp_path / f'rollout-2026-09-24T12-00-00-{NATIVE}.jsonl'
    selected = {'schema_version': 'ops.session-import.request.v1', 'runtime': 'codex',
                'source_file': str(source), 'native_session_id': NATIVE,
                'mode': mode, 'follow': follow, 'import_actor': 'operator:synthetic',
                'selected_desk_id': store._binding('session-1'),
                'max_batch_bytes': batch_bytes}
    return store, jobs, source, selected


def drain(jobs, identity, limit=20):
    for _ in range(limit):
        status = jobs.advance(identity)
        if status['phase'] in ('complete', 'waiting_for_complete_row') or (
                status['phase'] == 'following' and
                status['coverage']['next_offset'] == status['coverage']['observed_size']):
            return status
    raise AssertionError('bounded import failed to finish')


def test_large_control_row_over_batch_bound_and_latest_mixed_anchor(tmp_path):
    store, jobs, source, request = setup(tmp_path, mode='current-turn-and-forward',
                                         follow=True, batch_bytes=512_000)
    early = user('early')
    later = user('later latest', kind='response_item')
    oversized = line({'type': 'compacted', 'payload': {'opaque': 'x' * 1_000_000}})
    source.write_bytes(meta() + early + later + oversized + assistant('visible after compaction'))
    preview = jobs.preview(request)
    assert preview['current_turn_anchor']['user_row_offset'] == len(meta()) + len(early)
    assert preview['coverage']['pre_anchor_excluded_bytes'] == len(meta()) + len(early)
    status = jobs.apply(preview['plan_token'], consent=True)
    assert status['attribution']['status'] == 'proposed_unasserted'
    status = drain(jobs, preview['job_id'])
    assert status['counts']['oversized_skip_budget_overrun_rows'] == 1
    assert status['counts']['quarantined_rows'] == 1
    assert status['counts']['compaction_markers'] == 1
    assert status['counts']['imported'] == 2
    assert status['cursor']['offset'] == source.stat().st_size
    asserted = jobs.assert_owner(job_id=preview['job_id'], selected_desk_id=request['selected_desk_id'],
        asserted_by='operator:synthetic', recorded_at='2026-09-24T12:05:00Z',
        evidence=['operator_confirmed_exact_session_preview'])
    assert asserted['attribution']['source_range']['start_offset'] == len(meta()) + len(early)
    assert jobs.status(preview['job_id'])['attribution']['claim_id'] == asserted['claim_id']
    with store._connect() as db:
        rows = list(db.execute('SELECT payload FROM source_episodes'))
    assert len(rows) == 2
    assert all('early' not in row[0].decode() for row in rows)


def test_append_retry_and_overlapping_scope_reuse_sealed_row(tmp_path):
    store, jobs, source, request = setup(tmp_path, follow=True)
    source.write_bytes(meta() + user('first visible'))
    first = jobs.preview(request)
    jobs.apply(first['plan_token'], consent=True)
    drain(jobs, first['job_id'])
    source.write_bytes(source.read_bytes() + assistant('second visible'))
    status = drain(jobs, first['job_id'])
    assert status['counts']['imported'] == 2
    # A second job begins at the same user row but sees a larger artifact.
    scoped = dict(request, mode='current-turn-and-forward', import_actor='operator:other')
    second = jobs.preview(scoped)
    jobs.apply(second['plan_token'], consent=True)
    replay = drain(jobs, second['job_id'])
    assert replay['counts']['already_present'] == 2
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM source_episodes').fetchone()[0] == 2


def test_durable_prefix_failure_and_partial_trailing_wait(tmp_path):
    _, jobs, source, request = setup(tmp_path, follow=True)
    body = meta() + user('stable')
    source.write_bytes(body + b'{"type":"response_item"')
    preview = jobs.preview(request)
    assert preview['coverage']['partial_trailing_bytes'] > 0
    jobs.apply(preview['plan_token'], consent=True)
    status = drain(jobs, preview['job_id'])
    assert status['phase'] == 'waiting_for_complete_row'
    assert jobs.status(preview['job_id'])['phase'] == 'waiting_for_complete_row'
    source.write_bytes(body.replace(b'stable', b'broken') + b'{"type":"response_item"')
    with pytest.raises(ImportJobError, match='rewritten'):
        jobs.advance(preview['job_id'])
    status = jobs.status(preview['job_id'])
    assert status['phase'] == 'error'
    assert status['errors'][-1]['code'] == 'source_prefix_changed'


def test_preview_is_read_only_and_anchor_required(tmp_path):
    store, jobs, source, request = setup(tmp_path, mode='current-turn-and-forward')
    source.write_bytes(meta() + assistant('no user boundary'))
    with pytest.raises(ImportJobError) as error:
        jobs.preview(request)
    assert error.value.code == 'anchor_unavailable'
    assert not jobs.path.exists()
    source.write_bytes(meta() + user('one user boundary'))
    preview = jobs.preview(request)
    assert not jobs.path.exists()
    with pytest.raises(ImportJobError) as error:
        jobs.apply(preview['plan_token'], consent=False)
    assert error.value.code == 'consent_required'
    assert not jobs.path.exists()


def test_more_than_eight_megabytes_is_bounded_and_index_repair_is_durable(tmp_path):
    """T12b R3 replacement (ruled exception, Verification, 2026-10-04; tests/t12b_ruled_exceptions.py): the source
    stays bounded, and the index repair is durable through the outbox, not through `index_pending`: the job's
    batches are sealed under the Compose marker (tests/t12b_seams.py `compose_marker`: the board role, where a sealer
    never drains), a drainer process is killed at its first index write (tests/t12b_drain_process.py
    --crash-at-first-write), and the next drain applies both seals. GREEN-IF the job completes with the old bounds
    (2 imported, 9 quarantined, 9 over the skip budget), `index_pending_episodes` stays 0, after the killed drainer
    the index holds neither episode and both seals still wait in the outbox, and after the next drain the index
    holds both."""
    import subprocess
    import sys as _sys
    from t12b_seams import compose_marker, drain as drain_outbox, outbox_rows
    store, jobs, source, request = setup(tmp_path, batch_bytes=512_000)
    source.write_bytes(meta() + user('searchable cedar') +
                       b''.join(line({'type': 'compacted', 'payload': {'opaque': 'x' * 1_000_000}})
                                for _ in range(9)) + assistant('searchable oak'))
    assert source.stat().st_size > 8_000_000
    preview = jobs.preview(request)
    assert preview['counts']['complete_rows'] <= request.get('max_batch_rows', 2_000)
    jobs.apply(preview['plan_token'], consent=True)
    index = EpisodicSearchIndex(store.path.with_name('episode-search.sqlite3'), episode_store=store)
    index.initialize()
    with compose_marker(tmp_path):
        final = drain(jobs, preview['job_id'])
    assert final['phase'] == 'complete'
    assert final['counts']['imported'] == 2
    assert final['counts']['quarantined_rows'] == 9
    assert final['counts']['oversized_skip_budget_overrun_rows'] == 9
    assert final['index_pending_episodes'] == 0
    with store._connect() as db:
        sealed = {row[0] for row in db.execute('SELECT id FROM source_episodes')}
    assert len(sealed) == 2

    def indexed():
        with closing(sqlite3.connect(index.path)) as db:
            return {row[0] for row in db.execute('SELECT episode_id FROM indexed_episodes')}
    tests = Path(__file__).resolve().parent
    killed = subprocess.run([_sys.executable, str(tests / 't12b_drain_process.py'), '--config',
                             str(tmp_path / 'session-1.json'), '--out', str(tmp_path / 'killed.json'),
                             '--crash-at-first-write'], capture_output=True, text=True, timeout=180,
                            env={**__import__('os').environ, 'PYTHONPATH': __import__('os').pathsep.join([
                                str(tests), str(tests.parent / 'packages' / 'tooling' / 'src'),
                                __import__('os').environ.get('PYTHONPATH', '')])})
    assert killed.returncode == 17, f'precondition: the drainer was killed at its first index write: {killed.stderr[-2000:]}'
    assert not (indexed() & sealed), 'the killed drainer applied a seal'
    assert {row['episode_id'] for row in outbox_rows(store.path) if row['reason'] == 'seal'} >= sealed, (
        'a seal no longer waits in the outbox after the killed drainer')
    drain_outbox(store)
    assert sealed <= indexed(), f'the next drain did not apply {sorted(sealed - indexed())}'


def test_worker_lease_stop_resume_and_full_snapshot_refresh(tmp_path):
    _, jobs, source, request = setup(tmp_path, follow=True)
    source.write_bytes(meta() + user('first'))
    preview = jobs.preview(request)
    jobs.apply(preview['plan_token'], consent=True)
    with jobs._db(writable=True) as db:
        db.execute('UPDATE jobs SET lease_owner=?,lease_until=? WHERE id=?',
                   ('other-worker', 9999999999, preview['job_id']))
    with pytest.raises(ImportJobError) as error:
        jobs.advance(preview['job_id'])
    assert error.value.code == 'worker_active'
    with jobs._db(writable=True) as db:
        db.execute('UPDATE jobs SET lease_owner=NULL,lease_until=NULL WHERE id=?', (preview['job_id'],))
    assert jobs.stop(preview['job_id'])['phase'] == 'paused'
    assert jobs.follow(preview['job_id'])['phase'] == 'paused'
    assert jobs.resume(preview['job_id'])['phase'] == 'ready'
    assert drain(jobs, preview['job_id'])['phase'] == 'following'
    full = dict(request, follow=False)
    first = jobs.preview(full)
    jobs.apply(first['plan_token'], consent=True)
    source.write_bytes(source.read_bytes() + assistant('second'))
    newer = jobs.preview(full)
    assert newer['job_id'] != first['job_id']
    jobs.apply(newer['plan_token'], consent=True)
    assert drain(jobs, newer['job_id'])['counts']['already_present'] == 1


def test_cli_has_structured_admission_and_source_errors(tmp_path, capsys):
    bad = config(tmp_path, session='unadmitted', instance='fixture')
    code = cli_main(['--config', str(bad), 'desks'])
    output = json.loads(capsys.readouterr().out)
    assert code == 1
    assert output['status'] == 'error'
    assert output['code'] in ('invalid_configuration', 'admission_unavailable')
    assert 'unadmitted' not in output['message']


def test_current_turn_owner_routes_only_later_native_evidence(tmp_path):
    store, jobs, source, request = setup(tmp_path)
    source.write_bytes(meta() + user('early private cedar') + user('later private oak', kind='response_item'))
    whole = jobs.preview(request)
    jobs.apply(whole['plan_token'], consent=True)
    drain(jobs, whole['job_id'])
    scoped = dict(request, mode='current-turn-and-forward',
                  selected_desk_id=store._binding('session-3'))
    latest = jobs.preview(scoped)
    assert latest['current_turn_anchor']['user_row_offset'] > 0
    jobs.apply(latest['plan_token'], consent=True)
    drain(jobs, latest['job_id'])
    jobs.assert_owner(job_id=latest['job_id'], selected_desk_id=scoped['selected_desk_id'],
                      asserted_by='operator:test', recorded_at='2026-09-24T13:00:00Z',
                      evidence=['operator_confirmed_exact_session_preview'])
    selected, _ = SessionSources(store).select('session-1', scope='desk',
                                                binding_key=scoped['selected_desk_id'])
    texts = [SessionSources(store).read(identity)['events'][0]['text'] for identity in selected]
    assert texts == ['later private oak']


def test_public_cli_preview_apply_continue_and_status(tmp_path, capsys):
    store, _, source, request = setup(tmp_path)
    source.write_bytes(meta() + user('public CLI visible'))
    cfg = config(tmp_path, session='session-1', instance='fixture')
    payload = tmp_path / 'request.json'
    payload.write_text(json.dumps(request))
    assert cli_main(['--config', str(cfg), 'preview', '--input', str(payload)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview['status'] == 'preview'
    payload.write_text(json.dumps({'schema_version': 'ops.session-import.apply.v1',
                                   'plan_token': preview['plan_token'], 'consent': True}))
    assert cli_main(['--config', str(cfg), 'apply', '--input', str(payload)]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied['job_id'] == preview['job_id']
    payload.write_text(json.dumps({'schema_version': 'ops.session-import.job-ref.v1',
                                   'job_id': preview['job_id']}))
    assert cli_main(['--config', str(cfg), 'continue', '--input', str(payload)]) == 0
    continued = json.loads(capsys.readouterr().out)
    assert continued['counts']['imported'] == 1
    assert cli_main(['--config', str(cfg), 'continue', '--input', str(payload)]) == 0
    capsys.readouterr()
    assert cli_main(['--config', str(cfg), 'status', '--input', str(payload)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['phase'] == 'complete'


def test_reasoning_omitted_and_partial_full_snapshot_can_be_refreshed(tmp_path):
    store, jobs, source, request = setup(tmp_path)
    reasoning = line({'type': 'response_item', 'timestamp': '2026-09-24T12:01:00Z',
                      'payload': {'type': 'message', 'role': 'assistant', 'phase': 'analysis',
                                  'content': [{'type': 'output_text', 'text': 'private reasoning'}]}})
    complete = meta() + user('visible answer') + reasoning
    source.write_bytes(complete + b'{"type":"response_item"')
    first = jobs.preview(request)
    assert first['coverage']['reviewed_partial_trailing_bytes'] > 0
    jobs.apply(first['plan_token'], consent=True)
    done = drain(jobs, first['job_id'])
    assert done['phase'] == 'complete'
    assert done['coverage']['reviewed_complete_end'] == len(complete)
    assert done['counts']['imported'] == 1
    assert done['counts']['omitted_control_rows'] >= 2
    source.write_bytes(complete + assistant('later visible'))
    second = jobs.preview(request)
    assert second['job_id'] != first['job_id']
    jobs.apply(second['plan_token'], consent=True)
    refreshed = drain(jobs, second['job_id'])
    assert refreshed['counts']['already_present'] == 1
    assert refreshed['counts']['imported'] == 1
    with store._connect() as db:
        payloads = [row[0].decode() for row in db.execute('SELECT payload FROM source_episodes')]
    assert all('private reasoning' not in payload for payload in payloads)


def test_reviewed_uncaptured_bytes_cannot_change_after_apply(tmp_path):
    store, jobs, source, request = setup(tmp_path)
    source.write_bytes(meta() + user('reviewed original'))
    preview = jobs.preview(request)
    jobs.apply(preview['plan_token'], consent=True)
    source.write_bytes(source.read_bytes().replace(b'reviewed original', b'unreviewed edits'))
    with pytest.raises(ImportJobError) as error:
        jobs.advance(preview['job_id'])
    assert error.value.code == 'reviewed_source_changed'
    with store._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM source_episodes').fetchone()[0] == 0
    assert jobs.status(preview['job_id'])['phase'] == 'error'
