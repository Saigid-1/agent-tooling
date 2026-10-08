"""Secondary-model proposal -> sealed capsule -> same-desk source recovery."""
import json
import pytest

from test_episodic_memory_tools import _setup
from test_episodic_summarizer import configured
from kp_agent_tooling._impl.service.episodic_memory import EpisodeStore, EpisodeConflict
from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools


def test_secondary_model_handoff_recovers_uncited_exception_after_restart(tmp_path):
    store, episode, event = _setup(tmp_path)
    # First selected span ends before the recovery exception.
    event = {'event_id':'choice','role':'user','text':'Choose pool B. '+('x'*585)+'; retain pool A for recovery.'}
    episode = store.capture('session-1',source_ref='integration:span-recovery',events=[event])['episode_id']
    calls = []
    def transport(key, raw, timeout):
        request = json.loads(raw)
        packet = json.loads(request['messages'][1]['content'])
        calls.append(packet)
        source = packet['sources'][0]
        assert source['text'] == event['text'][:600]
        quote = 'Choose pool B'
        proposal = {'items':[{'kind':'decision','text':quote,'source_ids':[source['source_id']]}],
            'unresolved_questions':['What are the recovery conditions?']}
        return {'id':'synthetic-receipt','usage':{'prompt_tokens':100,'completion_tokens':50},
            'choices':[{'finish_reason':'stop','message':{'content':json.dumps(proposal)}}]}
    summary = configured(transport)
    capsule = store.consolidate_chunked_with('session-1',episode_ids=[episode],
        propose=summary,budget=summary.budget)
    assert len(calls) == 1
    reopened = EpisodeStore(store.path,session_ledger=store.sessions,registry=store.registry)
    successor = EpisodicMemoryTools(reopened,'session-2')
    resumed = successor.call('memory.resume',{'capsule_id':capsule['capsule_id'],'budget_bytes':5000})
    assert resumed['items'][0]['item']['semantic_validation'] == 'not-assessed'
    directory = successor.call('memory.evidence_directory',{'capsule_id':capsule['capsule_id']})
    start,end = directory['entries'][0]['uncited_ranges'][0]
    original = successor.call('memory.read_event',
        {'episode_id':episode,'event_id':'choice','start':start,'length':end-start})
    assert original['text'] == '; retain pool A for recovery.'
    assert summary.last_receipt['generation_id'] == 'synthetic-receipt'


def test_real_quote_outside_supplied_packet_cannot_become_verified_memory(tmp_path):
    store, episode, event = _setup(tmp_path)
    # Fabricating source identity cannot be rescued by returning real source text.
    def proposal(packet):
        return {'items':[{'kind':'decision','text':event['text'],'citations':[{
            'episode_id':'episode:unseen','event_id':'choice','start':0,
            'end':len(event['text']),'quote':event['text']}]}],'unresolved_questions':[]}
    budget = configured(lambda *args:None).budget
    with pytest.raises(EpisodeConflict):
        store.consolidate_chunked_with('session-1',episode_ids=[episode],
                                      propose=proposal,budget=budget)
    assert EpisodicMemoryTools(store,'session-2').call('memory.list',{'kind':'capsules'})['total']==0
