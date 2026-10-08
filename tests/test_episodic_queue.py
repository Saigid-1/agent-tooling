import json
import sqlite3
import pytest

from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue, ConsolidationConflict, _digest
from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools
from test_episodic_memory_tools import _setup
from test_episodic_summarizer import configured


def setup(tmp_path):
    store,episode,event=_setup(tmp_path)
    now=[1000.]
    q=ConsolidationQueue(tmp_path/'queue.sqlite3',store=store,clock=lambda:now[0])
    q.initialize()
    return q,episode,event,now


def test_enqueue_is_durable_deduplicated_and_desk_bound(tmp_path):
    q,ep,_,_=setup(tmp_path)
    first=q.enqueue('session-1',episode_ids=[ep])
    new=ConsolidationQueue(q.path,store=q.store)
    assert new.enqueue('session-2',episode_ids=[ep],reason='session_end')==first
    assert new.get('session-2',first['job_id'])==first
    with pytest.raises(ConsolidationConflict):
        new.get('session-3',first['job_id'])
    assert new.claim('session-3','worker') is None
    other=q.store.capture('session-1',source_ref='new',events=[
        {'event_id':'new','role':'user','text':'New information'}])['episode_id']
    with pytest.raises(ConsolidationConflict,match='only new'):
        q.enqueue('session-1',episode_ids=[ep,other])
    assert q.enqueue('session-1',episode_ids=[other])['job_id']!=first['job_id']


def test_expired_pre_call_lease_is_reclaimed_and_old_worker_fenced(tmp_path):
    q,ep,_,now=setup(tmp_path)
    q.enqueue('session-1',episode_ids=[ep])
    first=q.claim('session-1','one',lease_seconds=10)
    assert q.claim('session-2','two') is None
    now[0]+=11
    second=q.claim('session-2','two')
    assert second['generation']==first['generation']+1
    with pytest.raises(ConsolidationConflict,match='lease'):
        q._intent('session-1',first,1,{'source':'a'})
    q._intent('session-2',second,1,{'source':'a'})


def test_crash_after_intent_never_reissues_proposal(tmp_path):
    q,ep,_,now=setup(tmp_path)
    job=q.enqueue('session-1',episode_ids=[ep])
    lease=q.claim('session-1','crashed',lease_seconds=10)
    q._intent('session-1',lease,1,{'source':'a'})
    now[0]+=11
    restarted=ConsolidationQueue(q.path,store=q.store,clock=lambda:now[0])
    assert restarted.claim('session-2','replacement') is None
    assert restarted.get('session-2',job['job_id'])['state']=='needs_review'
    with pytest.raises(ConsolidationConflict):
        q._finish('session-1',lease,capsule_id='late')


def test_worker_validates_secondary_summary_and_successor_recovers_source(tmp_path):
    q,ep,event,_=setup(tmp_path)
    job=q.enqueue('session-1',episode_ids=[ep],reason='context_threshold')
    calls=[]
    def transport(key,body,timeout):
        calls.append(json.loads(body))
        quote='Choose pool B'
        wire=json.loads(calls[-1]['messages'][1]['content'])
        proposal={'items':[{'kind':'decision','text':quote,'source_ids':[wire['sources'][0]['source_id']]}],
            'unresolved_questions':[]}
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(proposal)}}],
                'usage':{'prompt_tokens':50}}
    proposer=configured(transport)
    packets=[]
    def capture(packet):
        packets.append(packet)
        return proposer(packet)
    result=q.run_once('session-2','worker',propose=capture,budget=proposer.budget,
                      approve_sources=lambda digests:digests==(_digest([event]),))
    assert result['state']=='succeeded' and len(calls)==1
    assert result['attempts'][0]['state']=='citations_validated'
    packet=json.loads(calls[0]['messages'][1]['content'])
    assert packet['schema_version']=='ops.summary-source-spans.v1'
    assert result['attempts'][0]['packet_sha256']==_digest(packets[0])
    assert q.run_once('session-2','worker',propose=proposer,budget=proposer.budget,
                      approve_sources=lambda _:True) is None
    tools=EpisodicMemoryTools(q.store,'session-2')
    assert tools.call('memory.resume',{'capsule_id':result['capsule_id']})['complete_handoff']
    assert tools.call('memory.read_event',{'episode_id':ep,'event_id':'choice','start':13})['text']=='; retain pool A for recovery.'


def test_failed_request_persists_review_state_and_no_retry(tmp_path):
    q,ep,_,_=setup(tmp_path)
    job=q.enqueue('session-1',episode_ids=[ep])
    calls=[]
    def failing(*args):
        calls.append(1)
        raise RuntimeError('private provider text')
    proposer=configured(failing)
    with pytest.raises(Exception):
        q.run_once('session-1','worker',propose=proposer,budget=proposer.budget,
                   approve_sources=lambda _:True)
    result=q.get('session-2',job['job_id'])
    assert result['state']=='needs_review'
    assert result['attempts'][0]['state']=='failed_or_uncertain'
    assert 'private provider' not in str(result)
    assert q.run_once('session-1','worker',propose=proposer,budget=proposer.budget,
                      approve_sources=lambda _:True) is None
    assert len(calls)==1


def test_source_denial_precedes_provider_call(tmp_path):
    q,ep,_,_=setup(tmp_path)
    job=q.enqueue('session-1',episode_ids=[ep])
    calls=[]
    proposer=configured(lambda *args:calls.append(1))
    with pytest.raises(ConsolidationConflict,match='source policy'):
        q.run_once('session-1','worker',propose=proposer,budget=proposer.budget,
                   approve_sources=lambda _:False)
    assert calls==[] and q.get('session-1',job['job_id'])['attempts']==[]


def test_queue_loss_does_not_reinitialize_on_worker_start(tmp_path):
    q,_,_,_=setup(tmp_path)
    q.path.unlink()
    with pytest.raises(ConsolidationConflict):
        q.claim('session-1','worker')
    assert not q.path.exists()


def test_concurrent_workers_only_one_claims_job(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    q,ep,_,now=setup(tmp_path)
    q.enqueue('session-1',episode_ids=[ep])
    barrier=threading.Barrier(2)
    def claim(worker):
        local=ConsolidationQueue(q.path,store=q.store,clock=lambda:now[0])
        barrier.wait()
        return local.claim('session-2',worker)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(claim,['a','b']))
    assert sum(r is not None for r in results)==1


def test_invalid_quote_is_not_marked_validated(tmp_path):
    q,ep,_,_=setup(tmp_path)
    job=q.enqueue('session-1',episode_ids=[ep])
    budget=configured(lambda *a:None).budget
    def bad(packet):
        return {'items':[{'kind':'decision','text':'bad','citations':[{
            'episode_id':ep,'event_id':'choice','start':0,'end':3,'quote':'BAD'}]}],
                'unresolved_questions':[]}
    with pytest.raises(Exception):
        q.run_once('session-1','worker',propose=bad,budget=budget,approve_sources=lambda _:True)
    result=q.get('session-2',job['job_id'])
    assert result['state']=='needs_review' and result['capsule_id'] is None
    assert result['attempts'][0]['state']=='proposal_received'


def test_operator_cli_initializes_enqueues_and_reads_status(tmp_path,monkeypatch,capsys):
    import sys
    import kp_agent_tooling.queue_cli as cli
    store,ep,_=_setup(tmp_path)
    adapter=EpisodicMemoryTools(store,'session-1')
    monkeypatch.setattr(cli,'from_config',lambda _:adapter)
    path=tmp_path/'state/cli-queue.sqlite3'
    prefix=['episodic_queue_cli','--config',str(tmp_path/'session-1.json'),'--queue',str(path)]
    monkeypatch.setattr(sys,'argv',prefix+['initialize'])
    assert cli.main()==0
    assert json.loads(capsys.readouterr().out)['model_requests']==0
    monkeypatch.setattr(sys,'argv',prefix+['enqueue','--episode',ep])
    assert cli.main()==0
    job=json.loads(capsys.readouterr().out)
    monkeypatch.setattr(sys,'argv',prefix+['status','--job',job['job_id']])
    assert cli.main()==0
    assert json.loads(capsys.readouterr().out)['state']=='queued'


def test_queue_retains_privacy_policy_without_private_content(tmp_path):
    store,ep,_=_setup(tmp_path)
    q=ConsolidationQueue(tmp_path/'privacy-queue.sqlite3',store=store);q.initialize()
    q.enqueue('session-1',episode_ids=[ep],reason='manual')
    class Proposal:
        last_receipt=None
        def __call__(self, packet):
            self.last_receipt={'data_collection':'deny','zdr_required':True,'allow_fallbacks':True,'prompt':'private'}
            return {'items':[],'unresolved_questions':[]}
    from test_episodic_summarizer import configured
    result=q.run_once('session-1','worker',propose=Proposal(),budget=configured(None).budget,approve_sources=lambda _:True)
    receipt=result['attempts'][0]['receipt']
    assert receipt['data_collection']=='deny' and receipt['zdr_required'] is True
    assert 'prompt' not in receipt


def test_summary_baseline_preserves_explicit_overrides_and_other_models():
    from kp_agent_tooling.queue_cli import summary_options
    from kp_agent_tooling._impl.service.memory_budget import DEFAULT_SUMMARY_MODEL
    assert summary_options(DEFAULT_SUMMARY_MODEL) == {'json_output': True, 'reasoning_effort': 'low'}
    assert summary_options('other/model') == {'json_output': False, 'reasoning_effort': None}
    assert summary_options(DEFAULT_SUMMARY_MODEL, json_output=False, reasoning_effort='high') == {
        'json_output': False, 'reasoning_effort': 'high'}
    assert summary_options(DEFAULT_SUMMARY_MODEL, disable_reasoning=True)['reasoning_effort'] is None
