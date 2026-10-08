"""KG-01 admission: untrusted candidates cannot override source lifecycle."""
import json
from dataclasses import replace
import pytest
from test_knowledge_service import setup, Reader
from test_knowledge_context import case, Nav
from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeService

@pytest.mark.parametrize('state', ['historical', 'superseded', 'withdrawn', None])
def test_direct_retrieve_excludes_ineligible_candidate(case, state):
    config = case[1]
    artifacts = config['repositories']['repo']['artifacts']
    if state is None:
        artifacts.clear()
    else:
        artifacts['docs/guide.md']['status'] = state
    service = KnowledgeService(config, lambda: Reader([case[4]]))
    result = service.execute('retrieve', {'repo_key': 'repo', 'query': 'guidance'})
    assert result['status'] == 'corpus_empty'
    assert result['data']['results'] == []
    assert case[4].text not in json.dumps(result)

def test_reader_cannot_leak_other_scope_or_unlisted_path(case):
    hit = case[4]
    reader = Reader([replace(hit, repo_key='private'), replace(hit, path='docs/old.md'), hit])
    result = KnowledgeService(case[1], lambda: reader).execute('retrieve', {'repo_key': 'repo', 'query': 'guidance'})
    assert result['data']['returned'] == 1
    assert reader.calls[0]['paths'] == ('docs/guide.md',)

@pytest.mark.parametrize('operation', ['retrieve', 'context'])
def test_long_lived_service_observes_catalog_change_and_restart(case, tmp_path, operation):
    config = case[1]
    path = tmp_path / 'catalog.json'
    path.write_text(json.dumps(config))
    factory = lambda: Reader([case[4]])
    service = KnowledgeService(path, factory, navigation_provider=Nav())
    args = {'repo_key': 'repo', 'query': 'guidance'}
    if operation == 'context':
        args.update(capability_id='cap', target_revision=case[2])
    assert case[4].text in json.dumps(service.execute(operation, args))
    config['repositories']['repo']['artifacts']['docs/guide.md']['status'] = 'superseded'
    path.write_text(json.dumps(config))
    for candidate in (service, KnowledgeService(path, factory, navigation_provider=Nav())):
        assert case[4].text not in json.dumps(candidate.execute(operation, args))

def test_catalog_change_during_recall_discards_response(case, tmp_path):
    config = case[1]; path = tmp_path / 'catalog.json'; path.write_text(json.dumps(config))
    class ChangingReader:
        def recall(self, **kwargs):
            config['repositories']['repo']['artifacts']['docs/guide.md']['status'] = 'historical'
            path.write_text(json.dumps(config))
            return [case[4]]
    result = KnowledgeService(path, ChangingReader).execute('retrieve', {'repo_key':'repo','query':'guidance'})
    assert result['status'] == 'error'
    assert case[4].text not in json.dumps(result)

@pytest.mark.skip(reason='S5 excluded: needs OPS test_document_retrieval_report over OPS-only scripts/desk_memory_cli.py and kp_core')
@pytest.mark.parametrize('format', ['json', 'jsonl'])
def test_document_cli_lifecycle_changes_without_reindex(tmp_path, monkeypatch, capsys, format):
    from test_document_retrieval_report import _cli_with_pilot
    import os
    cli, repo, reader = _cli_with_pilot(tmp_path, monkeypatch)
    args = ['--repo', repo.repo_key, 'docs', 'violet sextant', '--format', format]
    assert cli.main(args) == 0
    assert 'preserves immutable blob citations' in capsys.readouterr().out
    from pathlib import Path
    file = Path(os.environ['KP_KNOWLEDGE_CONFIG'])
    config = json.loads(file.read_text())
    config['repositories'][repo.repo_key]['artifacts']['docs/pilot.md']['status'] = 'superseded'
    file.write_text(json.dumps(config))
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    assert 'preserves immutable blob citations' not in output
    if format == 'json':
        # scripts/desk_memory_cli.py `docs` builds its own report outside KnowledgeService,
        # so an emptied admission set still prints no_results there (a known inconsistency).
        assert json.loads(output)['status'] == 'no_results'

@pytest.mark.skip(reason='S5 excluded: needs the legacy gateway: fastapi, kp_ops.service.app, test_knowledge_mcp, test_mcp_gateway_compose')
def test_mcp_uses_lifecycle_policy_after_catalog_change(case, tmp_path, isolated_lifecycle):
    import test_knowledge_mcp as mcp
    import test_mcp_gateway_compose as composed
    from kp_agent_tooling._impl.service.app import create_app
    from kp_agent_tooling._impl.service.mutation import MemoryMutationContext
    from fastapi.testclient import TestClient
    config = case[1]; config['repositories']['repo']['tenant_ids'] = ['tenant-a']
    path = tmp_path / 'catalog.json'; path.write_text(json.dumps(config))
    service = KnowledgeService(path, lambda: Reader([case[4]]), navigation_provider=Nav(), lifecycle=isolated_lifecycle)
    grants = tmp_path / 'grants.md'
    grants.write_text('| command | required scopes | boundary note |\n|---|---|---|\n| ops.knowledge.retrieve | ops/knowledge:read | read |\n| ops.knowledge.context | ops/knowledge:read | read |\n')
    app = create_app(composed._graph(), mutation_context=MemoryMutationContext(),
        mcp_verifier=mcp.Verifier(), mcp_grants_path=grants,
        compose_mcp_gateway=True, knowledge_service=service)
    client = TestClient(app)
    for state in ['maintained', 'superseded', 'withdrawn']:
        config['repositories']['repo']['artifacts']['docs/guide.md']['status'] = state
        path.write_text(json.dumps(config))
        for op in ['retrieve', 'context']:
            args = {'repo_key':'repo', 'query':'guidance'}
            if op == 'context': args.update(capability_id='cap', target_revision=case[2])
            response = mcp.call(client, 'tools/call', {'name':'knowledge.'+op,'arguments':args})
            assert response.status_code == 200
            assert (case[4].text in response.text) == (state == 'maintained')
    config['repositories']['repo']['artifacts']['docs/guide.md']['status'] = 'maintained'
    path.write_text(json.dumps(config))
    isolated_lifecycle.withdraw('docs','docs/guide.md',reason='superseded')
    response=mcp.call(client,'tools/call',{'name':'knowledge.retrieve','arguments':{'repo_key':'repo','query':'guidance'}})
    assert response.json()['result']['structuredContent']['status']=='corpus_empty'
    assert response.json()['result']['structuredContent']['data']['reason']=='all_declared_artifacts_withdrawn'
    assert case[4].text not in response.text
    config['repositories']['repo']['tenant_ids'] = ['other']
    path.write_text(json.dumps(config))
    assert 'error' in mcp.call(client,'tools/call',{'name':'knowledge.retrieve','arguments':{'repo_key':'repo','query':'guidance'}}).json()

from knowledge_lifecycle_fixture import isolated_lifecycle
