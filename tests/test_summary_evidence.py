"""Adversarial summary inputs at the sealed-capsule boundary."""
import pytest
from test_episodic_memory import setup
from kp_agent_tooling._impl.service.episodic_handoff import resume, handoff_page


def episode(store, text, role='assistant'):
    return store.capture('session-1', source_ref='guard:'+text,
                         events=[{'event_id':'claim','role':role,'text':text}])['episode_id']


def item(ep, source, text, kind='observation'):
    return {'kind':kind,'text':text,'citations':[{'episode_id':ep,'event_id':'claim',
            'start':0,'end':len(source),'quote':source}]}


def test_attribution_is_from_sealed_source_not_successor_or_proposal(tmp_path):
    store,*_=setup(tmp_path)
    source='I plan to change the pool. Not implemented or deployed.'
    ep=episode(store,source)
    proposal=item(ep,source,'Pool change planned.')
    result=store.consolidate('session-2',episode_ids=[ep],items=[proposal],unresolved_questions=[])
    h=result['handoff']
    assert h['binding_key']==store._binding('session-1')
    assert h['items'][0]['attribution']==[{'citation_index':0,'role':'assistant','source_session':'session-1'}]
    assert h['items'][0]['semantic_validation']=='not-assessed'
    assert 'Not implemented or deployed.' in [r['citation']['quote'] for r in h['source_qualifiers']]
    assert h['source_qualifiers'][0]['role']=='assistant'
    tampered=dict(proposal,attribution=[{'role':'user'}])
    with pytest.raises(ValueError):
        store.consolidate('session-2',episode_ids=[ep],items=[tampered],unresolved_questions=[])


def test_uncited_negative_source_survives_empty_summary_and_paging(tmp_path):
    store,*_=setup(tmp_path)
    source='No production workload was tested. Neither timeout was approved.'
    ep=episode(store,source,'tool')
    cap=store.consolidate('session-2',episode_ids=[ep],items=[],unresolved_questions=[])
    h=cap['handoff']; assert len(h['source_qualifiers'])==2
    packet=resume(store,'session-2',capsule_id=cap['capsule_id'],budget_bytes=1500)
    assert len(packet['source_qualifiers'])+len(packet['omissions']['qualifier_indices'])==2
    assert packet['evidence_review']['semantic_entailment']=='not-assessed'
    page=handoff_page(store,'session-2',capsule_id=cap['capsule_id'],budget_bytes=8000)
    assert [r['value'] for r in page['entries']]==h['source_qualifiers']


@pytest.mark.parametrize('claim',[
    'The timeout must be selected after the load test.',
    'Therefore production is safe.',
    'This proves that the implementation is correct.',
    'Deployment succeeded.',
])
def test_invented_relationship_or_execution_is_review_required(tmp_path,claim):
    store,*_=setup(tmp_path)
    source='Production enablement is blocked until the load test passes.'
    ep=episode(store,source,'user')
    cap=store.consolidate('session-1',episode_ids=[ep],items=[item(ep,source,claim)],unresolved_questions=[])
    check=cap['handoff']['items'][0]['evidence_check']
    assert check['status']=='review_required'
    assert check['semantic_entailment']=='not-assessed'
    assert check['next_step']=='read_cited_source_then_verify_claim'


def test_verbatim_prerequisite_remains_source_assertion_not_execution_proof(tmp_path):
    store,*_=setup(tmp_path)
    source='Production enablement is blocked until the load test passes.'
    ep=episode(store,source,'user')
    cap=store.consolidate('session-1',episode_ids=[ep],items=[item(ep,source,source)],unresolved_questions=[])
    check=cap['handoff']['items'][0]['evidence_check']
    assert check['status']=='source_text_matched'
    assert check['semantic_entailment']=='not-assessed'


def test_question_presupposition_is_flagged_too(tmp_path):
    store,*_=setup(tmp_path)
    ep=episode(store,'Timeout remains unresolved.','user')
    cap=store.consolidate('session-1',episode_ids=[ep],items=[],
                         unresolved_questions=['Which timeout do we select after the load test?'])
    assert cap['handoff']['evidence_review']['unlinked_question_indices']==[0]


def test_truncated_positive_quote_does_not_hide_negative_event(tmp_path):
    store,*_=setup(tmp_path)
    source='Not deployed.'
    ep=episode(store,source)
    forged=item(ep,source,'deployed.')
    forged['citations'][0].update(start=4,quote='deployed.')
    h=store.consolidate('session-1',episode_ids=[ep],items=[forged],unresolved_questions=[])['handoff']
    assert h['items'][0]['evidence_check']['status']=='review_required'
    assert h['source_qualifiers'][0]['citation']['quote']==source


def test_multiline_unicode_negation_and_qualifier_budget(tmp_path):
    store,*_=setup(tmp_path)
    ep=episode(store,'😀 Not deployed\nNo tests run.')
    h=store.consolidate('session-1',episode_ids=[ep],items=[],unresolved_questions=[])['handoff']
    assert [q['citation']['quote'] for q in h['source_qualifiers']]==['😀 Not deployed','No tests run.']
    large=episode(store,'Not verified. '*65)
    with pytest.raises(ValueError,match='qualifier budget'):
        store.consolidate('session-1',episode_ids=[large],items=[],unresolved_questions=[])


def test_legacy_capsule_remains_readable_without_new_claims(tmp_path):
    import json,sqlite3
    from kp_agent_tooling._impl.service.episodic_memory import _id, _bytes
    store,_,_,cap,_=setup(tmp_path)
    binding=store._binding('session-1')
    old=store._read(binding,cap['capsule_id'],'capsules','episode-capsule')
    for key in ['binding_key','source_qualifiers','evidence_review']:
        old['handoff'].pop(key)
    for entry in old['handoff']['items']:
        entry.pop('attribution');entry.pop('evidence_check')
    identity=_id('episode-capsule',old)
    with sqlite3.connect(store.path) as db:
        db.execute('INSERT INTO capsules VALUES (?,?,?)',(identity,binding,_bytes(old)))
    packet=resume(store,'session-2',capsule_id=identity,budget_bytes=24000)
    assert packet['evidence_review']['version']=='legacy'
    assert packet['source_qualifiers']==[]


def test_excess_qualifiers_fail_before_paid_model_call(tmp_path):
    from test_episodic_summarizer import configured
    store,*_=setup(tmp_path)
    ep=episode(store,'Not verified. '*65)
    calls=[]
    for chunked in [False, True]:
        with pytest.raises(ValueError,match='qualifier budget'):
            if chunked:
                store.consolidate_chunked_with('session-1',episode_ids=[ep],
                    propose=lambda packet:calls.append(packet),budget=configured(None).budget)
            else:
                store.consolidate_with('session-1',episode_ids=[ep],propose=lambda packet:calls.append(packet))
    assert calls==[]


def test_successful_frozen_trial_replays_with_preserved_nondeployment(tmp_path):
    import json
    from pathlib import Path
    root=Path(__file__).resolve().parents[1]
    events=json.loads((root/'tests/fixtures/summary-diagnostics-20260922/source.json').read_text())
    old=json.loads((root/'tests/fixtures/summary-0731-fallback/handoff.json').read_text())
    store,*_=setup(tmp_path)
    ep=store.capture('session-1',source_ref='frozen:six-events',events=events)['episode_id']
    items=[]
    for old_item in old['items']:
        items.append({'kind':old_item['kind'],'text':old_item['text'],
                      'citations':[dict(c,episode_id=ep) for c in old_item['citations']]})
    cap=store.consolidate('session-2',episode_ids=[ep],items=items,
                         unresolved_questions=old['unresolved_questions'])
    assert cap['bytes']<=12000
    assert any('deployed' in q['citation']['quote'] and q['role']=='assistant'
               for q in cap['handoff']['source_qualifiers'])
    assert len(cap['handoff']['items'])==7
    assert all(i['attribution'] for i in cap['handoff']['items'])
