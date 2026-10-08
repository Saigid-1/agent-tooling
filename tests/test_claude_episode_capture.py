import json
import sqlite3
from contextlib import closing

import pytest

from kp_agent_tooling._impl.service.claude_episode_capture import ClaudeEpisodeCapture, ClaudeCaptureConflict


class Store:
    def __init__(self):
        self.episodes = {}
        self.fail = False
        self.binding = 'desk'

    def _binding(self, session):
        return self.binding

    def capture(self, session, *, source_ref, events):
        if self.fail:
            raise RuntimeError('simulated store outage')
        old = self.episodes.get(source_ref)
        if old is not None:
            assert old == events
        self.episodes[source_ref] = events
        return {'episode_id': source_ref}


class Queue:
    def __init__(self):
        self.jobs = {}
        self.fail = False

    def enqueue(self, session, *, episode_ids, reason):
        if self.fail:
            raise RuntimeError('simulated queue outage')
        self.jobs.setdefault(episode_ids[0], reason)
        return {'job_id':episode_ids[0]}


def append(path, *rows):
    with path.open('ab') as out:
        for row in rows:
            out.write(json.dumps(row,ensure_ascii=False).encode()+b'\n')


def row(kind, content):
    return {'type':kind,'sessionId':'s','message':{'role':kind,'content':content}}


def setup(tmp_path):
    source = tmp_path/'s.jsonl'
    source.touch()
    capture = ClaudeEpisodeCapture(tmp_path/'cursor.sqlite')
    capture.initialize()
    return source,capture,Store(),Queue()


def test_exact_parts_omissions_append_and_no_duplicate(tmp_path):
    source,capture,store,queue = setup(tmp_path)
    append(source,row('user',[{'type':'text','text':'é user'}, {'type':'image','source':'hidden'}]),
           row('assistant',[{'type':'thinking','thinking':'secret'}, {'type':'text','text':'visible'}]),
           row('user',[{'type':'tool_result','content':[{'type':'text','text':'tool one'},
                                                       {'type':'text','text':'tool two'}]}]),
           {'type':'summary','summary':'native summary'})
    result = capture.capture('s',store=store,queue=queue,transcript_path=source)
    assert result['event_count'] == 4
    texts = [event['text'] for event in next(iter(store.episodes.values()))]
    assert texts == ['é user','visible','tool one','tool two']
    assert len(result['omissions']) == 3
    assert any(x['reason']=='native_summary' for x in result['omissions'])
    assert any(x['reason']=='native_summary' for x in capture.receipts('s',store=store)[0]['omissions'])
    assert capture.capture('s',store=store,queue=queue,transcript_path=source)['state']=='idle'
    assert len(queue.jobs)==1
    assert capture.status('s',store=store,transcript_path=source)['pending_source_bytes']==0
    append(source,row('assistant',[{'type':'text','text':'later'}]))
    second = capture.capture('s',store=store,queue=queue,transcript_path=source)
    assert second['state']=='captured'
    assert len(queue.jobs)==2


def test_pending_replay_and_incomplete_tail(tmp_path):
    source,capture,store,queue=setup(tmp_path)
    append(source,row('user','first'))
    queue.fail=True
    with pytest.raises(RuntimeError):
        capture.capture('s',store=store,queue=queue,transcript_path=source)
    assert capture.status('s',store=store,transcript_path=source)['has_pending_batch']
    queue.fail=False
    result=capture.capture('s',store=store,queue=queue,transcript_path=source)
    assert result['state']=='captured'
    assert len(store.episodes)==len(queue.jobs)==1
    with source.open('ab') as out:
        out.write(b'{"type":"user"')
    assert capture.capture('s',store=store,queue=queue,transcript_path=source)['incomplete_tail']
    with source.open('ab') as out:
        out.write(b',"sessionId":"s","message":{"role":"user","content":"tail"}}\n')
    assert capture.capture('s',store=store,queue=queue,transcript_path=source)['state']=='captured'


def test_rotation_rewrite_bad_utf8_and_session_mismatch(tmp_path):
    source,capture,store,queue=setup(tmp_path)
    append(source,row('user','same'))
    capture.capture('s',store=store,queue=queue,transcript_path=source)
    raw=source.read_bytes()
    source.write_bytes(raw.replace(b'same',b'edit'))
    with pytest.raises(ClaudeCaptureConflict,match='rewritten'):
        capture.capture('s',store=store,queue=queue,transcript_path=source)
    # Keep the old inode allocated: Linux may reuse an immediately unlinked
    # inode, which does not exercise the changed-file-identity branch.
    original_inode = source.stat().st_ino
    source.rename(source.with_suffix('.rotated'))
    source.write_bytes(raw)
    assert source.stat().st_ino != original_inode
    with pytest.raises(ClaudeCaptureConflict,match='replaced'):
        capture.status('s',store=store,transcript_path=source)


def test_repeated_assistant_snapshots_remain_distinct(tmp_path):
    source,capture,store,queue=setup(tmp_path)
    first=row('assistant',[{'type':'text','text':'first snapshot'}])
    second=row('assistant',[{'type':'text','text':'revised snapshot'}])
    first['message']['id']=second['message']['id']='msg-one'
    first['uuid']='one'; second['uuid']='two'
    append(source,first,second)
    capture.capture('s',store=store,queue=queue,transcript_path=source)
    events=next(iter(store.episodes.values()))
    assert [event['text'] for event in events]==['first snapshot','revised snapshot']
    assert all(event['event_id'].startswith('snapshot-byte-') for event in events)
    assert events[0]['event_id'] != events[1]['event_id']


def test_malformed_source_rejected(tmp_path):
    source,capture,store,queue=setup(tmp_path)
    source.write_bytes(b'\xff\n')
    with pytest.raises(ClaudeCaptureConflict,match='UTF-8'):
        capture.capture('s',store=store,queue=queue,transcript_path=source)
    source.write_bytes(json.dumps({'type':'user','sessionId':'other','message':{'role':'user','content':'x'}}).encode()+b'\n')
    with pytest.raises(ClaudeCaptureConflict,match='session differs'):
        capture.capture('s',store=store,queue=queue,transcript_path=source)


def test_bounded_multicall_verification_detects_earlier_rewrite(tmp_path):
    source,capture,store,queue=setup(tmp_path)
    append(source,*[row('user','x'*400+str(i)) for i in range(6)])
    capture.capture('s',store=store,queue=queue,transcript_path=source,max_bytes=10000)
    append(source,row('user','new'))
    first=capture.capture('s',store=store,queue=queue,transcript_path=source,max_bytes=1024)
    assert first['state']=='verifying'
    raw=source.read_bytes(); source.write_bytes(b'Y'+raw[1:])
    with pytest.raises(ClaudeCaptureConflict,match='rewritten'):
        for _ in range(10):
            capture.capture('s',store=store,queue=queue,transcript_path=source,max_bytes=1024)


def test_line_larger_than_page_fails_without_stalling(tmp_path):
    source,capture,store,queue=setup(tmp_path)
    append(source,row('user','x'*2000))
    with pytest.raises(ClaudeCaptureConflict,match='exceeds capture page'):
        capture.capture('s',store=store,queue=queue,transcript_path=source,max_bytes=1024)


def test_changed_desk_binding_cannot_read_or_continue_session(tmp_path):
    source,capture,store,queue=setup(tmp_path)
    append(source,row('user','desk-private'))
    capture.capture('s',store=store,queue=queue,transcript_path=source)
    store.binding='different-desk'
    with pytest.raises(ClaudeCaptureConflict,match='different desk binding'):
        capture.status('s',store=store,transcript_path=source)
    with pytest.raises(ClaudeCaptureConflict,match='different desk binding'):
        capture.receipts('s',store=store)
    append(source,row('assistant','must not capture'))
    with pytest.raises(ClaudeCaptureConflict,match='different desk binding'):
        capture.capture('s',store=store,queue=queue,transcript_path=source)


def mapped(tmp_path):
    source,capture,store,queue=setup(tmp_path)
    capture.native_session_id='native'
    capture.hook_transcript_path='/host/native.jsonl'
    capture.bind('host',store=store,transcript_path=source)
    return source,capture,store,queue


def test_immutable_native_identity_and_wrong_row(tmp_path):
    source,capture,store,queue=mapped(tmp_path)
    append(source,{'type':'user','sessionId':'native','message':{'role':'user','content':'mapped'}})
    assert capture.capture('host',store=store,queue=queue,transcript_path=source)['state']=='captured'
    for attribute,value in [('native_session_id','other'),('hook_transcript_path','/wrong')]:
        before=getattr(capture,attribute);setattr(capture,attribute,value)
        with pytest.raises(ClaudeCaptureConflict):
            capture.capture('host',store=store,queue=queue,transcript_path=source)
        with pytest.raises(ClaudeCaptureConflict):
            capture.bind('host',store=store,transcript_path=source)
        setattr(capture,attribute,before)
    store.binding='other'
    with pytest.raises(ClaudeCaptureConflict):
        capture.capture('host',store=store,queue=queue,transcript_path=source)
    store.binding='desk'
    append(source,row('user','wrong native'))
    with pytest.raises(ClaudeCaptureConflict):
        capture.capture('host',store=store,queue=queue,transcript_path=source)
    assert len(store.episodes)==len(queue.jobs)==1


def test_index_failure_replays_without_duplicate_capture(tmp_path):
    """T12b R3 replacement (ruled exception, Verification, 2026-10-04; tests/t12b_ruled_exceptions.py): a replayed
    capture seals once and is indexed by the next drain. The capture is interrupted after its seal (the queue fails,
    so the batch stays pending) and replayed, with the capture's index configured (as the hook CLIs configure it),
    under the Compose marker (tests/t12b_seams.py `compose_marker`), so the seal is the only thing the capture can do
    with it. GREEN-IF the replay captures the same episode, the store holds it once, one job is queued, exactly one
    outbox `seal` row waits for it, the capture never connected to the index (the T10 P4 honesty instrument), and the
    next drain indexes exactly that episode, which a search then finds."""
    from test_claude_capture_integration import _row, _setup, _write_rows
    from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
    from t10_instruments import recording
    from t12b_seams import compose_marker, drain, outbox_rows
    store, queue, capture = _setup(tmp_path)
    index_path = store.path.with_name('episode-search.sqlite3')
    capture.index = EpisodicSearchIndex(index_path, episode_store=store)
    capture.index.initialize()
    transcript = tmp_path / 'replay.jsonl'
    _write_rows(transcript, [_row('user', 'Search me after the replay.')])

    class FailOnce:
        fail = True

        def enqueue(self, session, **kwargs):
            if self.fail:
                raise RuntimeError('simulated queue outage')
            return queue.enqueue(session, **kwargs)

        def __getattr__(self, name):
            return getattr(queue, name)
    flaky = FailOnce()
    with compose_marker(tmp_path), recording() as recorder:
        recorder.phase = 'call'
        with pytest.raises(RuntimeError):
            capture.capture('session-1', store=store, queue=flaky, transcript_path=transcript)
        assert capture.status('session-1', store=store, transcript_path=transcript)['has_pending_batch']
        flaky.fail = False
        result = capture.capture('session-1', store=store, queue=flaky, transcript_path=transcript)
    assert result['state'] == 'captured'
    assert recorder.connected(store.path, phases=('call',)) >= 1, 'positive control: the capture reached the store'
    assert recorder.connected(index_path, phases=('call',)) == 0, 'the capture opened the index'
    with closing(store._connect()) as db:
        assert db.execute('SELECT count(*) FROM episodes').fetchone()[0] == 1, 'the replay sealed twice'
    assert len(queue.list('session-1')['entries']) == 1
    seals = [row for row in outbox_rows(store.path) if row['reason'] == 'seal']
    assert [row['episode_id'] for row in seals] == [result['episode_id']]
    drain(store)
    with closing(sqlite3.connect(index_path)) as db:
        assert [row[0] for row in db.execute('SELECT episode_id FROM indexed_episodes')] == [result['episode_id']]
    assert len(capture.index.search('session-2', query='Search me after the replay')['results']) == 1


def test_verified_seed_large_prefix_and_append_liveness(tmp_path):
    import hashlib
    source,capture,store,queue=mapped(tmp_path)
    append(source,*[{'type':'user','sessionId':'native','message':{'role':'user','content':'x'*10000}} for _ in range(110)])
    offset=source.stat().st_size
    sha=hashlib.sha256(source.read_bytes()).hexdigest()
    receipt={'session':'host','native_session_id':'native','binding':'desk',
             'transcript_path':str(source.resolve()),'offset':offset,'prefix_sha256':sha,
             'verified':True,'provenance':'operator verified import receipt 123'}
    for changed in ({'offset':offset-1},{'native_session_id':'wrong'},{'verified':False}):
        with pytest.raises(ClaudeCaptureConflict):
            capture.seed('host',store=store,transcript_path=source,offset=offset,prefix_sha256=sha,provenance={**receipt,**changed})
    with pytest.raises(ClaudeCaptureConflict,match='prefix differs'):
        capture.seed('host',store=store,transcript_path=source,offset=offset,prefix_sha256='0'*64,provenance={**receipt,'prefix_sha256':'0'*64})
    capture.seed('host',store=store,transcript_path=source,offset=offset,prefix_sha256=sha,provenance=receipt)
    capture.seed('host',store=store,transcript_path=source,offset=offset,prefix_sha256=sha,provenance=receipt)
    assert not store.episodes
    for i in range(3):
        append(source,{'type':'user','sessionId':'native','message':{'role':'user','content':f'new {i}'}})
        assert capture.capture('host',store=store,queue=queue,transcript_path=source,max_bytes=1024)['state']=='captured'
    data=source.read_bytes().replace(b'xxxx',b'yyyy',1);source.write_bytes(data)
    with pytest.raises(ClaudeCaptureConflict,match='rewritten'):
        capture.capture('host',store=store,queue=queue,transcript_path=source)


def test_large_visible_row_is_split_losslessly_and_replayable(tmp_path):
    source,capture,store,queue=mapped(tmp_path)
    text='😀' * 75000
    append(source,{'type':'user','sessionId':'native','message':{'role':'user','content':text}})
    result=capture.capture('host',store=store,queue=queue,transcript_path=source)
    record={'events':store.episodes[result['episode_id']]}
    assert ''.join(e['text'] for e in record['events'])==text
    assert all(len(e['text'].encode())<=128000 for e in record['events'])
    assert len({e['event_id'] for e in record['events']})==len(record['events'])
    assert capture.capture('host',store=store,queue=queue,transcript_path=source)['state']=='idle'
