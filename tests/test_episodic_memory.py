import copy
import sqlite3
import pytest
from test_portable_desk_memory import episode_store
from kp_agent_tooling._impl.service.episodic_memory import EpisodeStore, EpisodeConflict, EpisodeUnavailable


def setup(tmp_path):
    store=episode_store(tmp_path)
    events=[{'event_id':'decision','role':'user','text':'Use pool B. Retain pool A only for offline recovery.'},
            {'event_id':'receipt','role':'tool','text':'Synthetic check: 17 passed. Release ref R7.'},
            {'event_id':'detail','role':'user','text':'Recovery rendezvous is room Cedar at 04:17 UTC.'}]
    ep=store.capture('session-1',source_ref='synthetic:visible-transcript:v1',events=events)['episode_id']
    quote='Use pool B.'
    items=[{'kind':'decision','text':'Use pool B.', 'citations':[{'episode_id':ep,'event_id':'decision','start':0,'end':len(quote),'quote':quote}]}]
    cap=store.consolidate('session-1',episode_ids=[ep],items=items,unresolved_questions=['What is the recovery rendezvous?'])
    return store,ep,items,cap,events


def test_handoff_recovers_omitted_detail_across_model_sessions(tmp_path):
    store,ep,items,cap,events=setup(tmp_path)
    handoff=store.handoff('session-2',cap['capsule_id'])
    assert 'Cedar' not in str(handoff)
    assert handoff['loss']['uncited_events']==2
    directory=store.evidence_directory('session-2',cap['capsule_id'],limit=1)
    assert directory['next_offset']==1 and directory['total']==3
    exact=store.read_event('session-2',episode_id=ep,event_id='detail')
    assert exact['text']==events[2]['text']
    assert exact['complete']


def test_wrong_desk_and_unadmitted_sessions_cannot_recall(tmp_path):
    store,ep,items,cap,events=setup(tmp_path)
    for session in ['session-3','missing']:
        with pytest.raises((EpisodeUnavailable,ValueError,RuntimeError)):
            store.handoff(session,cap['capsule_id'])
        with pytest.raises((EpisodeUnavailable,ValueError,RuntimeError)):
            store.read_event(session,episode_id=ep,event_id='detail')


def test_source_idempotency_and_no_rewrite(tmp_path):
    store,ep,items,cap,events=setup(tmp_path)
    assert store.capture('session-1',source_ref='synthetic:visible-transcript:v1',events=events)['episode_id']==ep
    changed=copy.deepcopy(events);changed[0]['text']='Use pool C.'
    with pytest.raises(EpisodeConflict):store.capture('session-1',source_ref='synthetic:visible-transcript:v1',events=changed)


def test_citation_verification_and_charter_separation(tmp_path):
    store,ep,items,cap,events=setup(tmp_path)
    changed=copy.deepcopy(items);changed[0]['citations'][0]['quote']='Use pool A.'
    with pytest.raises(EpisodeConflict):store.consolidate('session-1',episode_ids=[ep],items=changed,unresolved_questions=[])
    changed=copy.deepcopy(items);changed[0]['kind']='charter'
    with pytest.raises(ValueError):store.consolidate('session-1',episode_ids=[ep],items=changed,unresolved_questions=[])
    assert store.handoff('session-2',cap['capsule_id'])['items'][0]['semantic_validation']=='not-assessed'


def test_bounds_and_loss_do_not_silently_truncate(tmp_path):
    store,ep,items,cap,events=setup(tmp_path)
    changed=copy.deepcopy(items);changed[0]['text']='x'*1800
    with pytest.raises(ValueError,match='budget'):store.consolidate('session-1',episode_ids=[ep],items=changed,unresolved_questions=[],budget_bytes=1000)
    first=store.read_event('session-2',episode_id=ep,event_id='detail',length=8)
    rest=store.read_event('session-2',episode_id=ep,event_id='detail',start=first['next_start'])
    assert first['text']+rest['text']==events[2]['text']


def test_restart_and_tamper_detection(tmp_path):
    store,ep,items,cap,events=setup(tmp_path)
    restarted=EpisodeStore(store.path,session_ledger=store.sessions,registry=store.registry)
    assert restarted.handoff('session-2',cap['capsule_id'])==cap['handoff']
    with sqlite3.connect(store.path) as db:db.execute('UPDATE episodes SET payload=? WHERE id=?',(b'{}',ep))
    with pytest.raises(EpisodeUnavailable):store.read_event('session-2',episode_id=ep,event_id='detail')


def test_reflection_does_not_become_new_source_episode(tmp_path):
    store,ep,items,cap,events=setup(tmp_path)
    with pytest.raises(EpisodeUnavailable):store.consolidate('session-2',episode_ids=[cap['capsule_id']],items=[],unresolved_questions=[])


def test_replaceable_summarizer_is_bounded_and_validated(tmp_path):
    store,ep,items,cap,events=setup(tmp_path)
    calls=[]
    def summarizer(packet):
        calls.append(packet)
        return {'items':items,'unresolved_questions':['Recover the rendezvous if needed.']}
    result=store.consolidate_with('session-2',episode_ids=[ep],propose=summarizer)
    assert result['handoff']['loss']['uncited_events']==2
    assert calls[0]['episodes'][0]['events']==events
    large=store.capture('session-1',source_ref='large',events=[{'event_id':'large','role':'user','text':'x'*2000}])['episode_id']
    with pytest.raises(ValueError,match='input exceeds'):
        store.consolidate_with('session-2',episode_ids=[large],propose=summarizer,input_budget_bytes=1000)
    assert len(calls)==1
    wrong=copy.deepcopy(items);wrong[0]['citations'][0]['quote']='invented'
    with pytest.raises(EpisodeConflict):
        store.consolidate_with('session-2',episode_ids=[ep],propose=lambda p:{'items':wrong,'unresolved_questions':[]})
