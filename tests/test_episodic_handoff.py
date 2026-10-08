import copy,json
import pytest
from kp_agent_tooling._impl.service.episodic_memory import EpisodeStore,EpisodeUnavailable
from kp_agent_tooling._impl.service.episodic_handoff import coverage,encoded_size
from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools
from test_episodic_memory import setup


def test_interval_union_accounts_for_every_unicode_character():
    rows=[{'episode_id':'e','event_id':'v','characters':10}]
    refs=[{'episode_id':'e','event_id':'v','start':a,'end':b} for a,b in [(2,5),(4,7),(7,8)]]
    row=coverage(rows,[{'citations':refs}])[0]
    assert row['cited_ranges']==[[2,8]] and row['uncited_ranges']==[[0,2],[8,10]]
    assert row['cited_characters']==6 and row['meaning_preservation']=='not-assessed'


def test_successor_recovers_omitted_source_with_exact_ranges_after_restart(tmp_path):
    store,ep,items,cap,events=setup(tmp_path)
    restarted=EpisodeStore(store.path,session_ledger=store.sessions,registry=store.registry)
    tools=EpisodicMemoryTools(restarted,'session-2')
    before=store.path.read_bytes()
    resume=tools.call('memory.resume',{'capsule_id':cap['capsule_id'],'budget_bytes':1800})
    assert encoded_size(resume)<=1800
    assert resume['omissions']['source_citation_coverage']['uncited_characters']>0
    directory=tools.call('memory.evidence_directory',{'capsule_id':cap['capsule_id']})
    detail=next(row for row in directory['entries'] if row['event_id']=='detail')
    assert detail['uncited_ranges']==[[0,len(events[2]['text'])]]
    a,b=detail['uncited_ranges'][0]
    recovered=tools.call('memory.read_event',{'episode_id':ep,'event_id':'detail','start':a,'length':b-a})
    assert recovered['text']==events[2]['text']
    assert store.path.read_bytes()==before
    with pytest.raises(EpisodeUnavailable):EpisodicMemoryTools(restarted,'session-3').call('memory.resume',{'capsule_id':cap['capsule_id']})


def test_bounded_handoff_exclusions_are_all_recoverable(tmp_path):
    store,ep,items,_,_=setup(tmp_path)
    items=[dict(copy.deepcopy(items[0]),text=str(i)+' '+('long decision '*80)) for i in range(5)]
    cap=store.consolidate('session-1',episode_ids=[ep],items=items,unresolved_questions=['Check receipt.'])
    tools=EpisodicMemoryTools(store,'session-2')
    packet=tools.call('memory.resume',{'capsule_id':cap['capsule_id'],'budget_bytes':1800})
    assert encoded_size(packet)<=1800 and not packet['complete_handoff']
    returned={row['index'] for row in packet['items']}
    excluded=set(packet['omissions']['handoff_item_indices'])
    assert not returned&excluded and returned|excluded==set(range(5))
    offset=0;recovered=[]
    while offset is not None:
        page=tools.call('memory.handoff_page',{'capsule_id':cap['capsule_id'],'offset':offset,'budget_bytes':2400})
        assert encoded_size(page)<=2400 and page['status']!='item_exceeds_budget'
        recovered.extend(row for row in page['entries'] if row['kind']=='item')
        offset=page['next_offset']
    assert [row['value'] for row in recovered]==store.handoff('session-2',cap['capsule_id'])['items']


def test_oversized_page_item_is_explicit_not_a_false_empty_result(tmp_path):
    store,ep,items,_,_=setup(tmp_path)
    items[0]['text']='x'*1900
    cap=store.consolidate('session-1',episode_ids=[ep],items=items,unresolved_questions=[])
    packet=EpisodicMemoryTools(store,'session-2').call('memory.handoff_page',{'capsule_id':cap['capsule_id'],'budget_bytes':1500})
    assert packet['status']=='item_exceeds_budget' and packet['next_offset']==0 and packet['total']==1
