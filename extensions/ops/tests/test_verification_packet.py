import copy
import gzip
import json
from pathlib import Path
import pytest
from kp_agent_tooling_ops._impl.verification_packet import validate_join, packet, encoded, excerpt

@pytest.fixture
def receipts():
    root=Path('records/lifecycle-30')
    gate=json.load(gzip.open(root/'lifecycle-gate.json.gz'))
    host=json.load(gzip.open(root/'thread-8-a.json.gz'))
    audit=json.loads((root/'collector-audit.json').read_text())
    sources=copy.deepcopy(gate['source'])
    return gate,host,audit,sources,Path('scripts/lifecycle_workload_server.py').read_text(),Path('kp_ops/lifecycle_matrix.py').read_text()


@pytest.mark.skip(reason='S5 excluded: receipts fixture reads OPS-only records/lifecycle-30 and scripts/lifecycle_workload_server.py')
def test_retained_crossrepo_join(receipts):
    candidate=validate_join(*receipts)
    assert '_acquire(user)' in candidate and '_release(db)' in candidate
    assert 'async def graph_query' not in candidate


@pytest.mark.skip(reason='S5 excluded: receipts fixture reads OPS-only records/lifecycle-30 and scripts/lifecycle_workload_server.py')
@pytest.mark.parametrize('mutation', ['source','candidate','profile','collector','trace','model','runner'])
def test_incompatible_evidence_is_rejected(receipts,mutation):
    gate,host,audit,sources,runner,transform=receipts
    if mutation=='source': sources['core']['revision']='0'*40
    if mutation=='candidate': host['candidate_sha256']='0'*64
    if mutation=='profile': host['profile']='baseline'
    if mutation=='collector': audit['receipts_sha256']['thread-8-a']='0'*64
    if mutation=='trace': gate['telemetry']['spans']=[]
    if mutation=='model': gate['model_digest']='0'*64
    if mutation=='runner': runner+='\n'
    with pytest.raises(ValueError): validate_join(gate,host,audit,sources,runner,transform)


def test_budget_overflow_returns_no_partial_evidence(monkeypatch):
    monkeypatch.setattr('kp_agent_tooling_ops._impl.verification_packet.assemble',lambda c:{'large':{'evidence_class':'source','text':'x'*5000},'small':{'evidence_class':'source','text':'actual'}})
    result=packet({},'ats-pool-lifecycle','read',budget=4000)
    assert result['status']=='split_required' and 'evidence' not in result
    assert len(encoded(result))<=4000
    small=packet({},'ats-pool-lifecycle','read',['small'],4000)
    assert small['status']=='ready' and small['omitted']==['large']
    assert result['packet_identity']==small['packet_identity']


def test_missing_evidence_stays_unverified(monkeypatch):
    def unavailable(c): raise ValueError('runtime_trace_missing')
    monkeypatch.setattr('kp_agent_tooling_ops._impl.verification_packet.assemble',unavailable)
    result=packet({},'ats-pool-lifecycle')
    assert result['status']=='unverified' and result['evidence']==[]
    assert result['semantic_verdict']=='requires_evidence_review'


def test_excerpt_preserves_guard_and_finally():
    text='def f():\n    if denied: raise ValueError()\n    try:\n        return run()\n    finally:\n        close()\n'
    result=excerpt(text,'f')
    assert result['text']==text.rstrip() and result['end_line']==6


def test_identity_changes_with_evidence(monkeypatch):
    data={'one':{'evidence_class':'source','text':'first'}}
    monkeypatch.setattr('kp_agent_tooling_ops._impl.verification_packet.assemble',lambda c:data)
    before=packet({},'ats-pool-lifecycle')['packet_identity']
    data['one']['text']='changed'
    assert packet({},'ats-pool-lifecycle')['packet_identity']!=before


def test_wrong_tool_evidence_ids_return_recovery_catalog(monkeypatch):
    monkeypatch.setattr('kp_agent_tooling_ops._impl.verification_packet.assemble',lambda c:{'entry':{'evidence_class':'source','text':'actual'}})
    result=packet({},'ats-pool-lifecycle','read',['worker-thread-summary'])
    assert result['status']=='invalid_selection' and result['evidence']==[]
    assert result['catalog'][0]['id']=='entry'
    assert result['next_call']['arguments']['mode']=='plan'


def test_split_continuations_fit_and_preserve_every_selected_item(monkeypatch):
    items={str(i):{'evidence_class':'source','text':'x'*1200} for i in range(8)}
    monkeypatch.setattr('kp_agent_tooling_ops._impl.verification_packet.assemble',lambda c:items)
    split=packet({},'ats-pool-lifecycle','read',budget=4000)
    assert split['status']=='split_required' and split['oversized']==[]
    seen=[]
    for continuation in split['next_calls']:
        page=packet({},**continuation['arguments'])
        assert page['status']=='ready' and len(encoded(page))<=4000
        seen.extend(page['evidence'])
    assert seen==list(items)


def test_oversized_item_has_explicit_executable_larger_budget(monkeypatch):
    monkeypatch.setattr('kp_agent_tooling_ops._impl.verification_packet.assemble',lambda c:{'trace':{'evidence_class':'runtime','text':'x'*6000}})
    split=packet({},'ats-pool-lifecycle','read',budget=4000)
    option=split['oversized'][0]
    assert option['minimum_budget']>4000
    page=packet({},**option['next_call']['arguments'])
    assert page['status']=='ready' and len(encoded(page))<=option['minimum_budget']


def test_stale_continuation_refused(monkeypatch):
    items={'entry':{'evidence_class':'source','text':'before'}}
    monkeypatch.setattr('kp_agent_tooling_ops._impl.verification_packet.assemble',lambda c:items)
    identity=packet({},'ats-pool-lifecycle')['packet_identity']
    items['entry']['text']='after'
    result=packet({},'ats-pool-lifecycle','read',expected_packet_identity=identity)
    assert result['status']=='unverified' and result['gap']['reason']=='packet_identity_changed'


def test_budget_units_error_returns_recovery():
    result=packet({},'ats-pool-lifecycle','read',budget=6)
    assert result['status']=='invalid_budget' and result['evidence']==[]
    assert result['next_call']['arguments']['budget']==12000


def test_roles_do_not_invent_runtime_graph_depth(monkeypatch):
    monkeypatch.setattr('kp_agent_tooling_ops._impl.verification_packet.assemble',lambda c:{'entry':{'evidence_class':'source','text':'entry'},'binding':{'evidence_class':'source','symbol':'main.acquire','text':'nested'}})
    result=packet({},'ats-pool-lifecycle')
    assert result['catalog'][0]['role']=='target'
    assert result['catalog'][1]['symbol_containment_depth']==1
    assert all(link['graph_hops'] is None for link in result['adjacency'])
    missing=next(link for link in result['adjacency'] if link['status']=='observation_required')
    assert 'next_call' not in missing


def test_plan_overflow_provides_identity_pinned_recovery(monkeypatch):
    monkeypatch.setattr('kp_agent_tooling_ops._impl.verification_packet.assemble',lambda c:{'artifact-'+str(i):{'evidence_class':'source','text':'x'} for i in range(35)})
    split=packet({},'ats-pool-lifecycle','plan',budget=4000)
    assert split['status']=='split_required' and 'catalog' not in split
    plan=packet({},**split['next_calls'][0]['arguments'])
    assert plan['status']=='ready' and len(plan['catalog'])==35


def test_large_requested_budget_cannot_bypass_multi_item_delivery_limit(monkeypatch):
    items={str(i):{'evidence_class':'source','text':'x'*5000} for i in range(5)}
    monkeypatch.setattr('kp_agent_tooling_ops._impl.verification_packet.assemble',lambda c:items)
    split=packet({},'ats-pool-lifecycle','read',budget=100000)
    assert split['status']=='split_required'
    assert split['budget']['ceiling']==12000
    seen=[]
    for c in split['next_calls']:
        page=packet({},**c['arguments'])
        assert page['status']=='ready' and len(encoded(page))<=12000
        seen.extend(page['evidence'])
    assert seen==list(items)
    # Internal validation can inspect full content without returning it to an agent.
    assert packet({},'ats-pool-lifecycle','read',budget=100000,_internal=True)['status']=='ready'


def test_default_single_item_overflow_requires_explicit_larger_budget(monkeypatch):
    monkeypatch.setattr('kp_agent_tooling_ops._impl.verification_packet.assemble',lambda c:{'trace':{'evidence_class':'runtime','text':'x'*14000}})
    split=packet({},'ats-pool-lifecycle','read',['trace'])
    assert split['status']=='split_required'
    page=packet({},**split['oversized'][0]['next_call']['arguments'])
    assert page['status']=='ready' and page['evidence']['trace']['text']=='x'*14000


def test_plan_supplies_bounded_identity_pinned_groups(monkeypatch):
    items={str(i):{'evidence_class':'source','text':'x'*5000} for i in range(5)}
    monkeypatch.setattr('kp_agent_tooling_ops._impl.verification_packet.assemble',lambda c:items)
    plan=packet({},'ats-pool-lifecycle')
    seen=[]
    for call in plan['read_groups']:
        result=packet({},**call['arguments'])
        assert result['packet_identity']==plan['packet_identity']
        assert len(encoded(result))<=12000
        seen.extend(result['evidence'])
    assert seen==list(items)
