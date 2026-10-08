from copy import deepcopy
import pytest
from kp_agent_tooling_ops._impl.verification_finding import validate_finding

CONFIG = {'repos': {'ats': {'revision': 'a'*40}}, 'evidence_revision': 'b'*40}
REF = {'kind':'packet','slice_id': 'ats-pool-lifecycle', 'id': 'runtime','packet_identity':'d'*64}


def finding():
    return {'schema_version':'ops.verification-finding.v5', 'claim':{'subject':'ats::route',
        'target':'candidate','predicate':'ready','expected_value':True}, 'observation_assessment':None,
        'outcome':'unverified', 'scope':{'scenario':'success','sources':{'ats':'a'*40},
        'execution_evidence':{'profile':'worker','execution_revision':'b'*40,'candidate_sha256':'c'*64}},
        'packets':[{'slice_id':'ats-pool-lifecycle','packet_identity':'d'*64}],
        'evidence':[{'reference':dict(REF),'establishes':'one observed request','limitation':'controlled'}],
        'delivery':{'complete':[dict(REF)],'gaps':[]},
        'unresolved':[{'question':'production?','classification':'observation_required',
            'basis':'retained scope excludes production','references':[dict(REF)],'next_action':'observe canary'}],
        'maintenance_trigger':['source changes'],'memory_status':'not-promoted'}


def resolver(config, **args):
    if args['expected_packet_identity'] != 'd'*64:
        return {'status':'unverified'}
    return {'status':'ready','packet_identity':'d'*64,'evidence':{
        'runtime':{'candidate_sha256':'c'*64,'profile':'worker'},'entry':{'evidence_class':'source'}}}


def validate(f):
    return validate_finding(CONFIG, f, resolver)


def test_valid_does_not_certify_semantics_or_delivery():
    r=validate(finding())
    assert r['status']=='valid'
    assert r['semantic_verdict']=='not-assessed'
    assert 'self-reported' in r['delivery_verification']


@pytest.mark.parametrize('change,code', [
    (lambda f:f['packets'][0].update(packet_identity='e'*64),'packet_unavailable_or_identity_changed'),
    (lambda f:f['scope']['execution_evidence'].update(execution_revision='e'*40),'execution_revision_mismatch'),
    (lambda f:f['scope']['sources'].update(ats='e'*40),'configured_source_mismatch'),
    (lambda f:f['scope']['execution_evidence'].update(candidate_sha256='e'*64),'candidate_profile_not_resolved'),
    (lambda f:f['evidence'][0]['reference'].update(id='made-up'),'unresolved_evidence_reference'),
    (lambda f:f['delivery'].update(complete=[]),'cited_evidence_not_delivered_complete'),
    (lambda f:f['unresolved'][0].update(classification='registered_but_unread'),'unread_classification_conflict'),
    (lambda f:f['unresolved'][0].update(references=[]),'reviewed_basis_requires_cited_evidence'),
])
def test_rejects_false_handoff(change, code):
    f=finding(); change(f)
    assert code in {e['code'] for e in validate(f)['errors']}


def test_truncation_requires_recovery_before_citation():
    f=finding()
    f['delivery']['gaps']=[{'reference':dict(REF),'reason':'host truncated','recovered':False}]
    assert validate(f)['status']=='invalid'
    f['delivery']['gaps'][0]['recovered']=True
    assert validate(f)['status']=='valid'


def test_unread_adjacent_item_can_remain_unread():
    f=finding()
    f['unresolved']=[{'question':'entry?','classification':'registered_but_unread',
        'basis':'catalog only','references':[{'kind':'packet','slice_id':'ats-pool-lifecycle','id':'entry','packet_identity':'d'*64}],
        'next_action':'read entry'}]
    assert validate(f)['status']=='valid'


def test_missing_or_abbreviated_identity_rejected_before_resolution():
    for f in [None, {}, finding()]:
        if f and 'scope' in f:f['scope']['execution_evidence']['candidate_sha256']='c...'
        assert validate(f)['status']=='invalid'


def test_resolver_failure_is_not_validation_success():
    def unavailable(*a, **kw):raise OSError('offline')
    assert validate_finding(CONFIG,finding(),unavailable)['status']=='invalid'


def test_profile_error_returns_exact_registered_pair_and_reference():
    f=finding();f['scope']['execution_evidence']['profile']='thread-8'
    r=validate(f)
    error=next(e for e in r['errors'] if e['code']=='candidate_profile_not_resolved')
    assert error['path']=='/scope/execution_evidence/profile'
    assert error['supported_pairs']==[{'candidate_sha256':'c'*64,'profile':'worker',
        'reference':{'slice_id':'ats-pool-lifecycle','id':'runtime','pointer':''}}]


def test_digest_binds_exact_handoff_including_invalid_findings():
    from kp_agent_tooling_ops._impl.verification_finding import finding_digest
    f=finding();r=validate(f)
    assert r['finding_sha256']==finding_digest(f)
    assert validate_finding(CONFIG,f,resolver,expected_finding_sha256=r['finding_sha256'])['status']=='valid'
    f['claim']='edited after validation'
    r2=validate_finding(CONFIG,f,resolver,expected_finding_sha256=r['finding_sha256'])
    assert r2['status']=='invalid' and any(e['code']=='finding_identity_changed' for e in r2['errors'])
    assert validate({})['finding_sha256']==finding_digest({})


def test_one_target_and_predicate_per_finding():
    f=finding()
    f['claim']='already pooled and ready to deploy'
    assert validate(f)['status']=='invalid'
    f=finding();f['claim']['predicate']=['pooled','ready']
    assert validate(f)['status']=='invalid'
    f=finding();f['claim']['target']=['baseline','deployment']
    assert validate(f)['status']=='invalid'
    # The two independent questions can carry different verdicts.
    f=finding();f['claim'].update(target='baseline',predicate='uses_pool',expected_value=True)
    f['outcome']='contradicted'
    assert validate(f)['status']=='valid'
    f['claim'].update(target='deployment',predicate='ready',expected_value=True)
    f['outcome']='unverified'
    assert validate(f)['status']=='valid'


def test_observation_join_recomputed_and_incompatible_context_preserved(tmp_path):
    from kp_agent_tooling_ops._impl.observations import ObservationStore, make_record
    store=ObservationStore(tmp_path)
    scope={'sources':{'ats':'a'*40},'binding_sha256':'1'*64}
    def save(binding='1'*64, subject='ats::route', value=True):
        return store.append(make_record('test','invoke',subject,'controlled-test',
            dict(scope,binding_sha256=binding),{'result':value},
            [{'predicate':'ready','value':value}],[],['controlled fixture']))
    a=save();b=save('2'*64)
    config=dict(CONFIG,observation_registry=str(tmp_path))
    f=finding();f['observation_assessment']={'ids':[a,b],'expected_scope':scope,
        'declared_status':'compatible','use':'joint_support'}
    def check():return validate_finding(config,f,resolver)
    r=check();assert r['status']=='invalid'
    assert r['checked_observations']['incompatible']==[b]
    f['observation_assessment']['declared_status']='incompatible'
    assert check()['status']=='invalid'  # Honest status cannot authorize a join.
    f['observation_assessment']['use']='separate_context'
    assert check()['status']=='valid'
    f['observation_assessment'].update(ids=[a],declared_status='compatible',use='joint_support')
    assert check()['status']=='valid'
    # Conflicting values, different subjects and corrupted originals fail closed.
    f['observation_assessment']['ids']=[a,save(value=False)]
    assert check()['status']=='invalid'
    f['observation_assessment']['ids']=[save(subject='ats::other')]
    assert any(e['code']=='observation_subject_mismatch' for e in check()['errors'])
    f['observation_assessment']['ids']=[a]
    original=store.read(a)['record']['original_sha256']
    (tmp_path/'originals'/f'{original}.json').write_text('{}')
    assert any(e['code']=='observation_assessment_unavailable' for e in check()['errors'])


def test_declared_assessment_requires_available_registry():
    f=finding();f['observation_assessment']={'ids':['a'*64],
        'expected_scope':{'sources':{'ats':'a'*40},'binding_sha256':'b'*64},
        'declared_status':'compatible','use':'joint_support'}
    assert any(e['code']=='observation_assessment_unavailable' for e in validate(f)['errors'])


def test_runtime_observation_applies_only_to_exact_candidate_and_scenario(tmp_path):
    from kp_agent_tooling_ops._impl.observations import ObservationStore, make_record
    store=ObservationStore(tmp_path)
    scope={'sources':{'ats':'a'*40},'binding_sha256':'1'*64}
    original={'runtime':{'candidate_sha256':'c'*64,'profile':'worker-thread-candidate',
        'selector':{'phase':'success'},'spans':[{'name':'ats.graph.request',
        'resource':{'attributes':{'service.name':'ops-lifecycle-workload'}}}]}}
    key=store.append(make_record('ops.lifecycle.retained','invoke','ats::route','runtime',scope,original,
        [{'predicate':'ready','value':True}],[],['controlled experiment']))
    config=dict(CONFIG,observation_registry=str(tmp_path))
    f=finding();f['scope']['execution_evidence'].update(profile='worker-thread-candidate',
        runtime_service='ops-lifecycle-workload')
    f['observation_assessment']={'ids':[key],'expected_scope':scope,
        'declared_status':'compatible','use':'joint_support'}
    def local_resolver(config, **args):
        value=resolver(config, **args)
        value['evidence']['runtime']['profile']='worker-thread-candidate'
        return value
    def codes():return {e['code'] for e in validate_finding(config,f,local_resolver)['errors']}
    assert not codes()
    f['claim']['target']='baseline'
    assert 'observation_target_mismatch' in codes()
    f['claim']['target']='deployment'
    assert 'observation_target_mismatch' in codes()
    f['claim']['target']='candidate';f['scope']['scenario']='other'
    assert 'observation_scenario_mismatch' in codes()
    f['scope']['scenario']='success';f['scope']['execution_evidence']['profile']='other'
    assert 'observation_transformation_mismatch' in codes()
    f['scope']['execution_evidence']['profile']='worker-thread-candidate'
    f['scope']['execution_evidence']['runtime_service']='other-service'
    assert 'observation_runtime_scope_mismatch' in codes()
    f['observation_assessment']['use']='separate_context'
    assert 'observation_transformation_mismatch' not in codes()


def test_runtime_observation_without_provenance_cannot_be_joint_support(tmp_path):
    from kp_agent_tooling_ops._impl.observations import ObservationStore, make_record
    store=ObservationStore(tmp_path)
    scope={'sources':{'ats':'a'*40},'binding_sha256':'1'*64}
    key=store.append(make_record('test','invoke','ats::route','runtime',scope,
        {'result':True},[{'predicate':'ready','value':True}],[],['legacy fixture']))
    f=finding();f['observation_assessment']={'ids':[key],'expected_scope':scope,
        'declared_status':'compatible','use':'joint_support'}
    config=dict(CONFIG,observation_registry=str(tmp_path))
    assert 'observation_applicability_missing' in {
        e['code'] for e in validate_finding(config,f,resolver)['errors']}


def test_baseline_runtime_provenance_can_match_baseline_but_not_deployment(tmp_path):
    from kp_agent_tooling_ops._impl.observations import ObservationStore, make_record
    store=ObservationStore(tmp_path)
    scope={'sources':{'ats':'a'*40},'binding_sha256':'1'*64}
    original={'runtime':{'candidate_sha256':'c'*64,'profile':'baseline',
        'selector':{'scenario':'success'},'spans':[{'name':'ats.graph.request',
        'resource':{'attributes':{'service.name':'ops-lifecycle-workload'}}}]}}
    key=store.append(make_record('ops.lifecycle.retained','invoke','ats::route','runtime',scope,original,
        [{'predicate':'ready','value':True}],[],['controlled experiment']))
    f=finding();f['claim']['target']='baseline'
    f['scope']['execution_evidence'].update(profile='baseline',runtime_service='ops-lifecycle-workload')
    f['observation_assessment']={'ids':[key],'expected_scope':scope,
        'declared_status':'compatible','use':'joint_support'}
    config=dict(CONFIG,observation_registry=str(tmp_path))
    def codes():return {e['code'] for e in validate_finding(config,f,resolver)['errors']}
    assert not any(c.startswith('observation_') for c in codes())
    f['claim']['target']='deployment'
    assert 'observation_target_mismatch' in codes()


def test_source_only_baseline_needs_no_candidate_execution_identity():
    f=finding();f['claim'].update(target='baseline',predicate='uses_pool')
    f['outcome']='contradicted';f['scope']['execution_evidence']=None
    ref={'kind':'packet','slice_id':'ats-pool-lifecycle','id':'entry','packet_identity':'d'*64}
    f['evidence']=[{'reference':ref,'establishes':'direct connection in source','limitation':'not execution'}]
    f['delivery']['complete']=[ref];f['unresolved']=[]
    assert validate(f)['status']=='valid'
    f['scope']['profile']='worker'
    assert validate(f)['status']=='invalid'  # No ambiguous legacy fields.


def test_runtime_citation_cannot_omit_execution_identity():
    f=finding();f['scope']['execution_evidence']=None
    assert any(e['code']=='execution_evidence_identity_required' for e in validate(f)['errors'])


def test_execution_identity_cannot_come_from_unread_neighbor():
    f=finding()
    entry={**REF,'id':'entry'}
    f['evidence']=[{'reference':entry,'establishes':'entry source','limitation':'not execution'}]
    f['delivery']['complete']=[entry]
    f['delivery']['gaps']=[{'reference':dict(REF),'reason':'not read','recovered':False}]
    f['unresolved']=[]
    assert any(e['code']=='candidate_profile_not_resolved' for e in validate(f)['errors'])
