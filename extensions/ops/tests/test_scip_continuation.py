import copy
import json
import pytest
from test_scip_navigation import repo, configured_service
from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeService, KnowledgeRequestError
from kp_agent_tooling._impl.scip_navigation import digest


def member_service(repo, tmp_path):
    config, _ = configured_service(repo, tmp_path)
    config['repositories']['member'] = copy.deepcopy(config['repositories']['demo'])
    config['platforms']['demo']['sources']['member'] = copy.deepcopy(config['platforms']['demo']['sources']['demo'])
    original = json.loads((tmp_path/'index.json').read_text())
    original['data']['repo_key'] = 'member'
    original['sha256'] = digest(original['data'])
    path = tmp_path/'member.json'
    path.write_text(json.dumps(original))
    config['scip_indexes']['member'] = [{**config['scip_indexes']['demo'][0], 'path':str(path), 'sha256':original['sha256']}]
    return config, KnowledgeService(config, lambda:None)


def test_member_continuation_and_coverage(repo, tmp_path):
    config, service = member_service(repo, tmp_path)
    result = service.execute('symbol', {'repo_key':'member','target_revision':repo[1], 'path':'server.py','line':1})
    assert result['status'] == 'ok'
    assert result['repo_key'] == 'member'
    assert result['data']['platform_key'] == 'demo'
    assert result['data']['platform_key_role'] == 'configured_platform_membership'
    assert result['data']['requested_repo_key'] == 'member'
    follow = result['data']['results'][0]['resolution']['definitions'][0]['next_call']
    assert follow['arguments']['line'] == 1
    assert service.execute('symbol', follow['arguments'])['status'] == 'ok'
    report = service.execute('platform', {'repo_key':'member'})
    coverage = report['data']['tool_coverage']['member']
    assert coverage['context']['capability_ids'] == []
    assert coverage['symbol']['indexes'][0]['path_scopes'] == ['server.py']
    assert coverage['retrieve']['corpus_status'] == 'not-assessed'
    assert coverage['discover']['scope'] == 'scripts/*.py only; not general code search'


def test_unindexed_path_is_explicit(repo, tmp_path):
    """GREEN-IF path_not_indexed names every configured repository/path scope."""
    _, service = member_service(repo, tmp_path)
    result = service.execute('symbol', {'repo_key':'member','target_revision':repo[1], 'path':'unknown.py','line':1})
    assert result['status'] == 'partial'
    assert 'path_not_indexed' in result['data']['gaps']
    assert len(result['data']['indexed_scope']) == 2
    assert {scope['repo_key'] for scope in result['data']['indexed_scope']} == {'demo', 'member'}
    for scope in result['data']['indexed_scope']:
        assert scope['repo_key'] in {'demo', 'member'}
        assert scope['revision'] == repo[1]
        assert scope['indexed_paths'] == 1
        assert scope['paths_sha256'] == __import__('hashlib').sha256(json.dumps(['server.py']).encode()).hexdigest()
        assert scope['covers_requested_path'] is False
        assert scope['top_level_dirs'] == ['server.py']


def test_ambiguous_member_refuses_selection(repo, tmp_path):
    config, _ = member_service(repo, tmp_path)
    config['repositories']['other'] = copy.deepcopy(config['repositories']['demo'])
    config['platforms']['other'] = copy.deepcopy(config['platforms']['demo'])
    config['platforms']['other']['sources']['other'] = config['platforms']['other']['sources'].pop('demo')
    service = KnowledgeService(config, lambda:None)
    with pytest.raises(KnowledgeRequestError):
        service.execute('symbol', {'repo_key':'member','target_revision':repo[1], 'path':'server.py','line':1})


@pytest.mark.skip(reason='S5 excluded: needs the legacy gateway: fastapi, kp_ops.service.app, test_knowledge_mcp, test_mcp_gateway_compose')
def test_member_transport_checks_anchor_tenant(repo, tmp_path):
    from fastapi.testclient import TestClient
    from test_knowledge_mcp import Verifier, call
    from test_mcp_gateway_compose import _graph
    from kp_agent_tooling._impl.service.app import create_app
    from kp_agent_tooling._impl.service.mutation import MemoryMutationContext
    config, _ = member_service(repo, tmp_path)
    config['repositories']['member']['tenant_ids'].append('tenant-b')
    service = KnowledgeService(config, lambda:None)
    grants = tmp_path/'grants.md'
    grants.write_text('| command | required scopes | boundary note |\n|---|---|---|\n| ops.knowledge.symbol | ops/knowledge:read | read |\n')
    app = create_app(_graph(), mutation_context=MemoryMutationContext(), mcp_verifier=Verifier(),
        mcp_grants_path=grants, compose_mcp_gateway=True, knowledge_service=service)
    args = {'repo_key':'member','target_revision':repo[1],'path':'server.py','line':1}
    with TestClient(app) as client:
        allowed = call(client,'tools/call',{'name':'knowledge.symbol','arguments':args})
        assert allowed.json()['result']['structuredContent']['status']=='ok'
        denied = call(client,'tools/call',{'name':'knowledge.symbol','arguments':args},'wrong-tenant')
        assert 'error' in denied.json()


def test_corrupt_coverage_does_not_claim_verified(repo, tmp_path):
    config, _ = member_service(repo, tmp_path)
    config['scip_indexes']['member'][0]['sha256'] = 'f'*64
    result = KnowledgeService(config, lambda:None).execute('platform',{'repo_key':'demo'})
    assert result['data']['tool_coverage']['member']['symbol']['indexes'][0]['status']=='unverified'
