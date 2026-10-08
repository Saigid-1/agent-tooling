"""Question planning is metadata; evidence must still be delivered by packet reads."""
import json

import pytest

from kp_agent_tooling_ops._impl import verification_plan
from kp_agent_tooling_ops._impl.evidence_references import packet_reference


IDENTITIES={'ats-pool-lifecycle':'a'*64,'ats-worker-performance':'b'*64}
CONFIG={'repos':{'ats':{'revision':'c'*40}}}


@pytest.fixture
def fake_packet(monkeypatch):
    calls=[]

    def packet(config,slice_id,mode='plan',evidence_ids=None,budget=12000,
               expected_packet_identity=None):
        assert config is CONFIG
        assert mode in {'plan','read'}
        calls.append({'slice_id':slice_id,'mode':mode,'evidence_ids':evidence_ids,
                      'budget':budget,'expected_packet_identity':expected_packet_identity})
        identity=IDENTITIES[slice_id]
        if mode=='plan':return {'status':'ready','packet_identity':identity}
        assert expected_packet_identity==identity
        assert evidence_ids
        return {'status':'ready','packet_identity':identity}

    monkeypatch.setattr(verification_plan,'packet',packet)
    return calls


def test_multiple_questions_dedupe_obligations_and_pin_reads(fake_packet):
    result=verification_plan.plan(CONFIG,['baseline','deployment'])
    assert result['status']=='ready'
    row=result['coverage'][0]
    assert row['slice_id']=='ats-pool-lifecycle'
    assert [ref['id'] for ref in row['required_references']]==[
        'entry','entry-import','tenant','connect','candidate','runtime']
    assert result['remaining_references']==row['required_references']
    assert result['next_calls']==[{'tool':'verification.packet','arguments':{
        'slice_id':'ats-pool-lifecycle','mode':'read',
        'evidence_ids':['entry','entry-import','tenant','connect','candidate','runtime'],
        'budget':12000,'expected_packet_identity':IDENTITIES['ats-pool-lifecycle']}}]
    assert len(fake_packet)==2 and result['evidence_reads_planned']==1
    assert result['recovery_reserve']==2 and result['deployment_gaps']
    assert 'evidence' not in result and 'original' not in result


def test_only_identity_valid_supplied_reference_is_skipped(fake_packet,monkeypatch):
    valid=packet_reference('ats-pool-lifecycle','entry',IDENTITIES['ats-pool-lifecycle'])
    stale=packet_reference('ats-pool-lifecycle','tenant','d'*64)

    def resolve(config,ref):
        if ref==stale:raise ValueError('packet_identity_changed')
        if ref!=valid:raise ValueError('unexpected reference')
        return {'reference':ref}

    monkeypatch.setattr(verification_plan,'resolve_reference',resolve)
    result=verification_plan.plan(CONFIG,['baseline'],supplied=[valid,stale])
    row=result['coverage'][0]
    assert row['already_supplied']==[valid]
    assert [ref['id'] for ref in row['unread']]==['entry-import','tenant','connect']
    assert result['invalid_supplied']==[{'reference':stale,'reason':'packet_identity_changed'}]
    assert result['next_calls'][0]['arguments']['evidence_ids']==['entry-import','tenant','connect']


def test_split_continuations_are_retained_with_pinned_identity(fake_packet,monkeypatch):
    original=verification_plan.packet

    def split_packet(config,slice_id,mode='plan',evidence_ids=None,budget=12000,
                     expected_packet_identity=None):
        if mode=='plan':return original(config,slice_id,mode=mode,budget=budget)
        assert expected_packet_identity==IDENTITIES[slice_id]
        return {'status':'split_required','next_calls':[{'tool':'verification.packet','arguments':{
            'slice_id':slice_id,'mode':'read','evidence_ids':evidence_ids[:2],
            'budget':budget,'expected_packet_identity':expected_packet_identity}}],
            'oversized':[{'id':evidence_ids[-1],'next_call':{'tool':'verification.packet',
                'arguments':{'slice_id':slice_id,'mode':'read',
                             'evidence_ids':[evidence_ids[-1]],'budget':50000,
                             'expected_packet_identity':expected_packet_identity}}}]}

    monkeypatch.setattr(verification_plan,'packet',split_packet)
    result=verification_plan.plan(CONFIG,['baseline'])
    assert result['status']=='ready' and len(result['next_calls'])==2
    assert all(call['arguments']['expected_packet_identity']==IDENTITIES['ats-pool-lifecycle']
               for call in result['next_calls'])
    assert result['next_calls'][1]['arguments']['budget']==50000
    assert result['recovery_reserve']==2


def test_limits_and_metadata_overflow_have_recovery_without_evidence(fake_packet,monkeypatch):
    for questions in ([],['baseline','baseline'],['unknown']):
        with pytest.raises(ValueError,match='unique registered'):verification_plan.plan(CONFIG,questions)
    for budget in (3999,12001):
        with pytest.raises(ValueError,match='plan budget'):verification_plan.plan(CONFIG,['baseline'],budget=budget)
    with pytest.raises(ValueError,match='too many supplied'):
        verification_plan.plan(CONFIG,['baseline'],supplied=[{}]*61)

    monkeypatch.setattr(verification_plan,'QUESTIONS',{'large':{
        'ats-pool-lifecycle':[f'item-{i:03d}' for i in range(100)]}})
    result=verification_plan.plan(CONFIG,['large'],budget=4000)
    assert result=={'status':'split_required','reason':'Plan metadata exceeds bound; request fewer questions',
                    'next_calls':[{'tool':'verification.plan','arguments':{'questions':['large'],'budget':4000}}]}
    assert 'evidence' not in json.dumps(result)
