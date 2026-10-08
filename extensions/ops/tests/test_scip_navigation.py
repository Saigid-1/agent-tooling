import copy
import pytest
from ops_repo_fixture import repo
from kp_agent_tooling._impl.scip_navigation import build_index,lookup,definitions,digest,_span,write_partitioned_index,load_partitioned_index


def indexed(repo):
    root,rev=repo
    doc={'metadata':{'tool_info':{'name':'scip-python','version':'0.6.6'}},'documents':[
        {'relative_path':'server.py','occurrences':[
            {'symbol':'scip-python python demo 1 inspect().','symbol_roles':1,'range':[0,4,11]}]}]}
    return build_index(root,rev,'demo',doc,provenance={'fixture':True})


def test_definition_matches_committed_source(repo):
    root,rev=repo; index=indexed(repo)
    found=lookup(index,repo=root,revision=rev,path='server.py',line=1)
    symbol=found['occurrences'][0]['symbol']
    result=definitions(symbol,[{'index':index,'revision':rev,'repo':root}])
    assert result['status']=='source_candidates'
    assert result['definitions'][0]['byte_offset']==4
    assert result['runtime']=='not-assessed'
    assert result['package_build_equivalence']=='not-established'


def test_wrong_revision_or_digest_refused(repo):
    root,rev=repo; index=indexed(repo)
    with pytest.raises(ValueError,match='revision'):lookup(index,repo=root,revision='0'*40,path='server.py',line=1)
    index['data']['occurrences'][0]['blob_sha']='f'*40
    with pytest.raises(ValueError,match='digest'):lookup(index,repo=root,revision=rev,path='server.py',line=1)


def test_locals_never_cross_repository_and_missing_not_absence(repo):
    root,rev=repo
    with pytest.raises(ValueError):definitions('local 1',[])
    assert definitions('scip-python python missing 1 name().',[])['status']=='unresolved'
    assert lookup(indexed(repo),repo=root,revision=rev,path='server.py',line=2)['absence_verdict']=='not-established'


def test_duplicate_definitions_remain_ambiguous(repo):
    root,rev=repo; index=indexed(repo); row={'repo':root,'revision':rev,'index':index}
    result=definitions(index['data']['occurrences'][0]['symbol'],[row,row],limit=1)
    assert result['ambiguous'] and result['omitted']==1


def test_different_package_version_never_resolves(repo):
    root, rev = repo
    index = indexed(repo)
    result = definitions('scip-python python demo 2 inspect().',
        [{'repo': root, 'revision': rev, 'index': index}])
    assert result['status'] == 'unresolved'
    assert result['definitions'] == []


def test_worktree_drift_rejected_at_build_but_not_retroactively(repo):
    root,rev=repo; index=indexed(repo); (root/'server.py').write_text('changed')
    assert lookup(index,repo=root,revision=rev,path='server.py',line=1)['status']=='ok'
    with pytest.raises(ValueError,match='checkout'):indexed(repo)


def test_utf16_coordinates_and_invalid_ranges():
    assert _span('a😀name\n',[0,3,7])==(5,4)
    with pytest.raises(UnicodeError):_span('a😀name\n',[0,2,7])
    with pytest.raises(ValueError):_span('abc\n',[0,0,99])
    with pytest.raises(ValueError):_span('abc\n',[0,0,0])


def test_partitioned_scip_definitions_and_corrupt_part(repo, tmp_path, monkeypatch):
    import kp_agent_tooling._impl.scip_navigation as module
    root, rev = repo
    index = indexed(repo)
    index['data']['occurrences'] *= 3
    index['sha256'] = digest(index['data'])
    monkeypatch.setattr(module, 'MAX_PART_BYTES', 260)
    path = tmp_path / 'index.json'
    manifest = write_partitioned_index(path, index)
    assert len(manifest['data']['parts']) >= 2
    opened = load_partitioned_index(path, expected_sha256=manifest['sha256'], hydrate=False)
    assert 'occurrences' not in opened['data']
    located = lookup(opened, revision=rev, repo=root, path='server.py', line=1, limit=1)
    assert len(located['occurrences']) == 1 and located['omitted'] == 2
    symbol = index['data']['occurrences'][0]['symbol']
    found = definitions(symbol, [{'index': opened, 'revision': rev, 'repo': root}], limit=1)
    assert len(found['definitions']) == 1 and found['omitted'] == 2 and found['ambiguous']
    with pytest.raises(ValueError, match='configured index digest'):
        load_partitioned_index(path, expected_sha256='f' * 64)
    (tmp_path / manifest['data']['parts'][-1]['file']).write_text('[]')
    with pytest.raises(ValueError, match='partition'):
        load_partitioned_index(path, expected_sha256=manifest['sha256'], hydrate=False)


@pytest.mark.skip(reason='S5 excluded: needs OPS-only scripts/scip_cli.py')
def test_scip_cli_definitions_reads_catalog_and_partitioned_index(repo, tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path
    root, rev = repo
    index = indexed(repo)
    path = tmp_path / 'index.json'
    write_partitioned_index(path, index)
    catalog = tmp_path / 'catalog.json'
    catalog.write_text(json.dumps([{'repo': str(root), 'revision': rev, 'index': str(path)}]))
    symbol = index['data']['occurrences'][0]['symbol']
    output = subprocess.check_output([sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/scip_cli.py'),
                                      'definitions', '--catalog', str(catalog), '--symbol', symbol], text=True)
    assert json.loads(output)['definitions'][0]['path'] == 'server.py'


def configured_service(repo,tmp_path):
    import json
    from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeService
    root,rev=repo; index=indexed(repo); p=tmp_path/'index.json';p.write_text(json.dumps(index))
    config={'schema_version':'ops.knowledge-config.v1','repositories':{'demo':{
        'path':str(root),'ref':rev,'corpus_scope':'demo','tenant_ids':['tenant-a'],'capabilities':{}}},
        'platforms':{'demo':{'schema':'ops.platform-request.v1','owner':'OPS','profile':'test',
            'sources':{'demo':{'revision':rev,'artifacts':[{'path':'server.py','role':'registration'}]}}}},
        'scip_indexes':{'demo':[{'path':str(p),'revision':rev,'sha256':index['sha256']}]}}
    return config,KnowledgeService(config,lambda:None)


def test_shared_service_rejects_stale_target_and_configured_digest(repo,tmp_path):
    from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeService
    config,service=configured_service(repo,tmp_path)
    args={'repo_key':'demo','target_revision':repo[1],'path':'server.py','line':1}
    assert service.execute('symbol',args)['status']=='ok'
    assert service.execute('symbol',{**args,'target_revision':'f'*40})['status']=='partial'
    config['scip_indexes']['demo'][0]['sha256']='f'*64
    result=KnowledgeService(config,lambda:None).execute('symbol',args)
    assert result['status']=='partial' and not result['data']['results']


@pytest.mark.skip(reason='S5 excluded: needs the legacy gateway: fastapi, kp_ops.service.app, test_knowledge_mcp, test_mcp_gateway_compose')
def test_real_symbol_mcp_transport_and_tenant_refusal(repo,tmp_path):
    from fastapi.testclient import TestClient
    from test_knowledge_mcp import Verifier,call
    from test_mcp_gateway_compose import _graph
    from kp_agent_tooling._impl.service.app import create_app
    from kp_agent_tooling._impl.service.mutation import MemoryMutationContext
    config,service=configured_service(repo,tmp_path)
    grants=tmp_path/'grants.md'
    grants.write_text('| command | required scopes | boundary note |\n|---|---|---|\n| ops.knowledge.symbol | ops/knowledge:read | read |\n')
    app=create_app(_graph(),mutation_context=MemoryMutationContext(),mcp_verifier=Verifier(),
        mcp_grants_path=grants,compose_mcp_gateway=True,knowledge_service=service)
    args={'repo_key':'demo','target_revision':repo[1],'path':'server.py','line':1}
    with TestClient(app) as client:
        response=call(client,'tools/call',{'name':'knowledge.symbol','arguments':args})
        assert response.json()['result']['structuredContent']==service.execute('symbol',args)
        denied=call(client,'tools/call',{'name':'knowledge.symbol','arguments':args},'wrong-tenant')
        assert 'error' in denied.json()
