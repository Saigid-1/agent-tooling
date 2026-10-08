"""Synthetic Claude capture through durable queue and desk-bound recovery."""

import json
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from kp_agent_tooling._impl.service.claude_episode_capture import ClaudeCaptureConflict, ClaudeEpisodeCapture
from kp_agent_tooling._impl.service.episodic_memory import EpisodeUnavailable
from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools
from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue
from kp_agent_tooling._impl.service.desk_memory_runtime import initialize, components
from test_portable_desk_memory import config, bind, episode_store


def _write_rows(path, rows):
    with path.open('ab') as stream:
        for row in rows:
            stream.write((json.dumps(row, ensure_ascii=False) + '\n').encode())


def _row(role, content):
    return {'type': role, 'message': {'role': role, 'content': content}}


def _setup(tmp_path):
    store = episode_store(tmp_path)
    queue = ConsolidationQueue(tmp_path / 'state/queue.sqlite3', store=store)
    queue.initialize()
    capture = ClaudeEpisodeCapture(tmp_path / 'state/claude-cursor.sqlite3')
    capture.initialize()
    return store, queue, capture


def test_capture_queue_summary_successor_recovers_omitted_source(tmp_path):
    store, queue, capture = _setup(tmp_path)
    transcript = tmp_path / 'fixture.jsonl'
    _write_rows(transcript, [
        _row('user', [{'type': 'text', 'text': 'Choose pool B; keep pool A for recovery.'}]),
        _row('assistant', [{'type': 'text', 'text': 'I will record the choice.'}]),
        _row('user', [{'type': 'tool_result', 'content': 'Recovery rendezvous: Cedar, 04:17 UTC.'}]),
    ])
    receipt = capture.capture('session-1', store=store, queue=queue,
                              transcript_path=transcript, reason='batch')
    assert receipt['event_count'] == 3
    assert receipt['job_id'] and receipt['episode_id']
    episode = store._read(store._binding('session-1'), receipt['episode_id'],
                          'episodes', 'episode')
    assert 'Cedar' in str(episode['events'])

    calls = []

    def secondary_summary(packet):
        calls.append(packet)
        source = next(s for s in packet['sources'] if 'Choose pool B' in s['text'])
        quote = 'Choose pool B'
        start = source['start'] + source['text'].index(quote)
        return {'items': [{'kind': 'decision', 'text': quote, 'citations': [{
            'episode_id': source['episode_id'], 'event_id': source['event_id'],
            'start': start, 'end': start + len(quote), 'quote': quote}]}],
            'unresolved_questions': ['Where is the recovery rendezvous?']}

    from test_episodic_summarizer import configured
    budget = configured(lambda *args: None).budget
    done = queue.run_once('session-2', 'synthetic-secondary', propose=secondary_summary,
                          budget=budget, approve_sources=lambda digests: len(digests) == 1)
    assert done['state'] == 'succeeded' and len(calls) == 1
    successor = EpisodicMemoryTools(store, 'session-2')
    resume = successor.call('memory.resume', {'capsule_id': done['capsule_id']})
    assert resume['complete_handoff']
    assert 'Cedar' not in str(resume['items'])
    assert resume['source_recovery']['name'] == 'memory.evidence_directory'
    directory = successor.call('memory.evidence_directory', {'capsule_id': done['capsule_id']})
    omitted = next(e for e in directory['entries'] if not e['cited'] and e['role'] == 'tool')
    exact = successor.call('memory.read_event', {'episode_id': receipt['episode_id'],
                                                  'event_id': omitted['event_id']})
    assert 'Cedar, 04:17 UTC' in exact['text']
    with pytest.raises(EpisodeUnavailable):
        EpisodicMemoryTools(store, 'session-3').call('memory.read_event', {
            'episode_id': receipt['episode_id'], 'event_id': omitted['event_id']})

    # A repeated complete transcript is a no-op; a restart sees the same cursor.
    restarted = ClaudeEpisodeCapture(capture.path)
    again = restarted.capture('session-1', store=store, queue=queue,
                              transcript_path=transcript, reason='batch')
    assert again['state'] == 'idle' and again['offset'] == receipt['offset']
    assert queue.list('session-2')['entries'][0]['capsule_id'] == done['capsule_id']


def test_pending_replay_verifies_exact_source_and_avoids_duplicate_job(tmp_path):
    store, queue, capture = _setup(tmp_path)
    transcript = tmp_path / 'pending.jsonl'
    _write_rows(transcript, [_row('user', [{'type': 'text', 'text': 'Alpha choice'}])])

    class FailOnce:
        def enqueue(self, *args, **kwargs):
            raise RuntimeError('simulated crash after episode seal')

    with pytest.raises(RuntimeError, match='simulated crash'):
        capture.capture('session-1', store=store, queue=FailOnce(), transcript_path=transcript)
    assert capture.status('session-1', store=store, transcript_path=transcript)['has_pending_batch']
    resumed = ClaudeEpisodeCapture(capture.path).capture(
        'session-1', store=store, queue=queue, transcript_path=transcript)
    assert resumed['state'] == 'captured'
    assert len(queue.list('session-2')['entries']) == 1

    # Same-length mutation cannot replay the pending batch as if it were original.
    second = tmp_path / 'second.jsonl'
    _write_rows(second, [_row('user', [{'type': 'text', 'text': 'Alpha choice'}])])
    second_capture = ClaudeEpisodeCapture(tmp_path / 'second-cursor.sqlite3')
    second_capture.initialize()
    with pytest.raises(RuntimeError):
        second_capture.capture('session-1', store=store, queue=FailOnce(),
                               transcript_path=second)
    original = second.read_bytes()
    second.write_bytes(original.replace(b'Alpha', b'Bravo'))
    with pytest.raises(ClaudeCaptureConflict, match='pending source rewritten'):
        ClaudeEpisodeCapture(second_capture.path).capture(
            'session-1', store=store, queue=queue, transcript_path=second)


def test_native_summary_and_control_rows_are_not_source_events(tmp_path):
    store, queue, capture = _setup(tmp_path)
    transcript = tmp_path / 'summaries.jsonl'
    summary = _row('assistant', [{'type': 'text', 'text': 'Native summary invented conclusion'}])
    summary['isCompactSummary'] = True
    _write_rows(transcript, [summary,
        {'type': 'summary', 'summary': 'Native control summary'},
        _row('user', [{'type': 'text', 'text': 'Original visible evidence'}])])
    receipt = capture.capture('session-1', store=store, queue=queue,
                              transcript_path=transcript)
    assert receipt['event_count'] == 1
    record = store._read(store._binding('session-1'), receipt['episode_id'],
                         'episodes', 'episode')
    assert [e['text'] for e in record['events']] == ['Original visible evidence']
    assert len(receipt['omissions']) == 2


def test_session_identity_mismatch_fails_before_capture(tmp_path):
    store, queue, capture = _setup(tmp_path)
    transcript = tmp_path / 'wrong-session.jsonl'
    row = _row('user', [{'type': 'text', 'text': 'Other session content'}])
    row['sessionId'] = 'session-3'
    _write_rows(transcript, [row])
    with pytest.raises(ClaudeCaptureConflict, match='row session differs'):
        capture.capture('session-1', store=store, queue=queue,
                        transcript_path=transcript)
    assert queue.list('session-1')['entries'] == []


def test_concurrent_capture_of_same_append_enqueues_once(tmp_path):
    store, queue, capture = _setup(tmp_path)
    transcript = tmp_path / 'concurrent.jsonl'
    _write_rows(transcript, [_row('user', [{'type': 'text', 'text': 'One event'}])])
    barrier = threading.Barrier(2)

    def run():
        barrier.wait()
        local = ClaudeEpisodeCapture(capture.path)
        return local.capture('session-1', store=store, queue=queue,
                             transcript_path=transcript)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: run(), range(2)))
    assert sorted(receipt['state'] for receipt in receipts) == ['captured', 'idle']
    assert len(queue.list('session-2')['entries']) == 1
