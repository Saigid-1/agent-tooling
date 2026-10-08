import pytest
from kp_agent_tooling_ops._impl.observations import ObservationStore, make_record, encoded

SCOPE={'sources':{'ats':'a'*40},'binding_sha256':'b'*64}


def save(store,value=True,kind='runtime',scope=None):
    return store.append(make_record('fixture-provider','invoke-1','ats::route',kind,scope or SCOPE,
        {'value':value},[{'predicate':'returned','value':value}],[],['fixture only']))


def test_conflicts_preserved_and_missing_runtime_unknown(tmp_path):
    store=ObservationStore(tmp_path)
    a=save(store,True,'static');b=save(store,False,'static')
    assert a!=b
    r=store.assess([a,b],SCOPE)
    assert r['status']=='conflicting' and r['execution']=='unknown'
    assert len(store.list()['records'])==2


def test_changed_binding_or_revision_invalidates_join(tmp_path):
    store=ObservationStore(tmp_path);a=save(store)
    changed={**SCOPE,'binding_sha256':'c'*64}
    assert store.assess([a],changed)['status']=='incompatible'
    assert store.assess([a],{'sources':{'ats':'d'*40},'binding_sha256':'b'*64})['status']=='incompatible'


def test_independent_retrieval_tamper_detection_and_idempotence(tmp_path):
    store=ObservationStore(tmp_path);a=save(store)
    assert save(store)==a
    record=store.read(a)
    assert record['original']['value'] is True
    path=tmp_path/'records'/f'{a}.json'
    path.write_text('{}')
    with pytest.raises(ValueError,match='integrity'):store.read(a)


def test_no_authorization_and_static_never_execution(tmp_path):
    store=ObservationStore(tmp_path);a=save(store,kind='static')
    r=store.assess([a],SCOPE)
    assert r['execution']=='unknown' and r['authorization']=='not-assessed'
    assert store.read(a)['record']['authorization']=='not-granted'


def test_references_resolve_and_empty_assessment_unknown(tmp_path):
    store=ObservationStore(tmp_path)
    assert store.assess([],SCOPE)['status']=='unknown'
    r=make_record('fixture','id','route','static',SCOPE,{},[],['f'*64],['test'])
    with pytest.raises(ValueError,match='reference'):store.append(r)


def test_limits_do_not_truncate_evidence(tmp_path):
    store=ObservationStore(tmp_path);a=save(store)
    assert store.read_bounded(a,10)['status']=='split_required'
    assert 'record' not in store.read_bounded(a,10)
    listed=store.list()['records'][0]
    needed=listed['original_read_bytes']
    assert listed['next_call']=={'tool':'verification.observations',
        'arguments':{'mode':'read','id':a,'budget':max(4000,needed)}}
    refusal=store.read_bounded(a,10)
    assert refusal['required_bytes']==needed
    assert refusal['next_call']=={'tool':'verification.observations',
        'arguments':{'mode':'read','id':a,'budget':max(4000,needed)}}
    ready=store.read_bounded(a,refusal['next_call']['arguments']['budget'])
    assert ready['status']=='ready' and len(encoded(ready))==needed


def test_original_too_large_for_public_read_has_no_false_continuation(tmp_path):
    store=ObservationStore(tmp_path)
    a=store.append(make_record('fixture','id','route','static',SCOPE,
        {'payload':'x'*110000},[],[],['test']))
    refusal=store.read_bounded(a,100000)
    assert refusal['status']=='split_required' and refusal['required_bytes']>100000
    assert 'next_call' not in refusal
    row=store.list()['records'][0]
    assert row['original_read_bytes']==refusal['required_bytes']
    assert row['next_call'] is None and 'maximum public read budget' in row['read_unavailable_reason']


def test_metadata_list_paginates_with_byte_ceiling_and_checks_original(tmp_path):
    store=ObservationStore(tmp_path)
    ids=[store.append(make_record('fixture',str(i),'route','static',SCOPE,
        {'payload':'x'*300},[],[],['test'])) for i in range(30)]
    seen=[];after=None
    while True:
        page=store.list(limit=100,after=after,budget=4000)
        assert len(encoded(page))<=4000
        seen.extend(row['id'] for row in page['records'])
        if not page['has_more']:break
        assert page['next_after']==seen[-1]
        after=page['next_after']
    assert seen==sorted(ids)
    for row in store.list(limit=1)['records']:
        assert store.read_bounded(row['id'],row['next_call']['arguments']['budget'])['status']=='ready'
    original=store.read(ids[0])['record']['original_sha256']
    (tmp_path/'originals'/f'{original}.json').write_text('{}')
    with pytest.raises(ValueError,match='integrity'):store.list()


def test_cannot_smuggle_authority_or_mutate_original(tmp_path):
    store=ObservationStore(tmp_path)
    r=make_record('fixture','id','route','static',SCOPE,{},[],[],['test'])
    r['authorization']='trusted'
    with pytest.raises(ValueError,match='noncanonical'):store.append(r)
    a=save(store);record=store.read(a)['record']
    (tmp_path/'originals'/f"{record['original_sha256']}.json").write_text('{"value":false}')
    with pytest.raises(ValueError,match='integrity'):store.read(a)


def test_provider_adapter_retains_original_and_rejects_missing_invocation():
    from kp_agent_tooling_ops._impl.observation_adapters import serena_observation
    original={'status':'ok','invocation':{'provider_response_received':True},
        'report':{'source_snapshot':{'ats':{'revision':'a'*40}},'provider_identity':{'name':'serena'}}}
    r=serena_observation(original,'ats::route')
    assert r['original']==original and r['kind']=='static'
    assert r['assertions']==[]
    original['invocation']['provider_response_received']=False
    with pytest.raises(ValueError,match='invocation'):serena_observation(original,'ats::route')


def test_runtime_adapter_rejects_wrong_sources():
    from kp_agent_tooling_ops._impl.observation_adapters import runtime_observation
    p={'status':'ready','join_status':'identity_checks_passed','evidence':{'runtime':{
        'evidence_class':'observed-runtime','spans':[{'name':'ats.graph.request','attributes':{'source.ats.revision':'a'*40}}]}}}
    with pytest.raises(ValueError,match='source mismatch'):runtime_observation(p,{'ats':'b'*40})


@pytest.mark.skip(reason='S5 excluded: reads OPS-only records/claude-review-baseline/register.py and its retained receipts')
def test_retained_baseline_and_candidate_receipts_have_distinct_provenance():
    from pathlib import Path
    from runpy import run_path
    # The registry builder reads historical receipts; it does not run the workload.
    module=run_path(str(Path(__file__).resolve().parents[1]/'records/claude-review-baseline/register.py'))
    selected=list(module['selected_receipts']())
    assert [(label, scenario) for label, scenario, _ in selected] == [
        ('baseline-a','success-c1-r0'), ('baseline-a','duplicate-input-422'),
        ('thread-8-a','success-c1-r0'), ('thread-8-a','duplicate-input-422')]
    assert [r['assertions'][0]['value'] for _,_,r in selected] == [200,422,200,422]
    assert {r['original']['runtime']['profile'] for _,_,r in selected} == {
        'baseline','worker-thread-candidate'}
    assert len({r['scope']['binding_sha256'] for _,_,r in selected}) == 2
    assert all(r['scope']['sources'] == module['SOURCE'] for _,_,r in selected)


@pytest.mark.parametrize('change', [
    lambda original: original['runtime'].update(profile='unrecognized'),
    lambda original: original['runtime'].update(spans=None),
    lambda original: original['runtime']['spans'][0]['resource'].update(attributes=None),
    lambda original: original['runtime'].pop('candidate_sha256'),
])
def test_lifecycle_applicability_fails_closed_on_malformed_original(change):
    from kp_agent_tooling_ops._impl.observation_adapters import lifecycle_applicability
    record={'provider':'ops.lifecycle.retained'}
    original={'runtime':{'candidate_sha256':'c'*64,'profile':'baseline',
        'selector':{'scenario':'success'},'spans':[{'name':'ats.graph.request',
        'resource':{'attributes':{'service.name':'ops-lifecycle-workload'}}}]}}
    change(original)
    assert lifecycle_applicability(record,original) is None
    assert lifecycle_applicability({'provider':'unknown'},original) is None
