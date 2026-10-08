"""Catalog states are reported as such, never as search outcomes or malformed requests."""
import json
import subprocess

import pytest

from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeService, KnowledgeRequestError


class _Hit:
    def __init__(self, path):
        self.repo_key = 'docs'; self.path = path; self.blob_sha = '0' * 40
        self.byte_offset = 0; self.byte_length = 1; self.heading = 'h'; self.score = 0.5; self.text = 't'


class _Reader:
    def __init__(self, hits=()):
        self.hits = tuple(hits); self.calls = 0
    def recall(self, *, query, top_k, repo_keys, paths):
        self.calls += 1
        return self.hits


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'; root.mkdir(); (root / 'docs').mkdir()
    (root / 'docs/x.md').write_text('# x\n')
    subprocess.run(['git', '-C', str(root), 'init', '-q'], check=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'first'], check=True)
    return root


def _config(root, *, artifacts, capabilities):
    return {'schema_version': 'ops.knowledge-config.v1', 'repositories': {
        'repo': {'path': str(root), 'ref': 'HEAD',
                 'default_branch_ref': subprocess.check_output(
                     ['git', '-C', str(root), 'symbolic-ref', 'HEAD'], text=True).strip(),
                 'corpus_scope': 'docs', 'tenant_ids': ['tenant'],
                 'capabilities': capabilities, 'artifacts': artifacts}}}


def test_retrieve_with_no_declared_artifacts_reports_corpus_empty_without_consulting_reader(repo):
    reader = _Reader()
    service = KnowledgeService(_config(repo, artifacts={}, capabilities={}), lambda: reader)
    report = service.execute('retrieve', {'repo_key': 'repo', 'query': 'anything'})
    assert report['status'] == 'corpus_empty'
    assert report['data']['reason'] == 'no_maintained_artifacts_declared'
    assert report['data']['declared_artifacts'] == 0
    assert report['data']['absence_verdict'] == 'not-established'
    assert reader.calls == 0


def test_retrieve_with_declared_but_unmatched_artifacts_says_so(repo):
    reader = _Reader()
    service = KnowledgeService(_config(repo, artifacts={'docs/x.md': {'status': 'maintained', 'owner': 'OPS'}},
                                       capabilities={}), lambda: reader)
    report = service.execute('retrieve', {'repo_key': 'repo', 'query': 'anything'})
    assert report['status'] == 'no_results'
    assert report['data']['declared_artifacts'] == 1
    assert report['data']['reason'] == 'no_matching_chunks_or_not_indexed'
    assert reader.calls == 1


def test_context_for_repository_without_capabilities_is_a_named_catalog_error(repo):
    service = KnowledgeService(_config(repo, artifacts={}, capabilities={}), lambda: _Reader())
    target = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    report = service.execute('context', {'repo_key': 'repo', 'capability_id': 'code-navigation', 'target_revision': target})
    assert report['status'] == 'error'
    assert report['data']['error']['code'] == 'no_capabilities_declared'
    assert report['data']['error']['declared_capabilities'] == []
    check = service.execute('check_references', {'repo_key': 'repo', 'capability_id': 'code-navigation'})
    assert check['data']['error']['code'] == 'no_capabilities_declared'


def test_context_for_undeclared_capability_lists_the_declared_ones(repo):
    (repo / 'docs/m.map.json').write_text(json.dumps({'schema_version': 'ops.capability-map.v1', 'capability_id': 'declared',
        'summary': 's', 'source_revision': '0' * 40, 'references': [{'id': 'a', 'role': 'architecture', 'path': 'docs/x.md', 'blob_sha': '0' * 40}]}))
    service = KnowledgeService(_config(repo, artifacts={}, capabilities={'declared': 'docs/m.map.json'}), lambda: _Reader())
    target = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    report = service.execute('context', {'repo_key': 'repo', 'capability_id': 'other', 'target_revision': target})
    assert report['data']['error']['code'] == 'capability_not_declared'
    assert report['data']['error']['declared_capabilities'] == ['declared']


def test_unknown_repository_still_raises(repo):
    service = KnowledgeService(_config(repo, artifacts={}, capabilities={}), lambda: _Reader())
    with pytest.raises(KnowledgeRequestError, match='unknown repository'):
        service.execute('context', {'repo_key': 'nope', 'capability_id': 'x', 'target_revision': '0' * 40})


def test_gateway_schema_lets_a_capability_less_repository_reach_the_service():
    """`{'not': {}}` used to make every context/check_references request for such a repo malformed."""
    pytest.importorskip('fastapi')
    pytest.importorskip('kp_core.mcp')
    import importlib
    import test_knowledge_mcp as mcp_fixture
    cls = importlib.import_module('kp_agent_tooling._impl.service.knowledge_mcp').KnowledgeSkillAdapter

    class Service(mcp_fixture.Service):
        repository_keys = ('ops', 'bare')
        capability_ids = ('document-retrieval',)
        capabilities_by_repository = {'ops': ('document-retrieval',), 'bare': ()}
        def permits_tenant(self, repo, tenant): return tenant == 'tenant-a'

    service = Service()
    adapter = cls(service, verifier=mcp_fixture.Verifier(), resolver=mcp_fixture.Resolver(),
                  on_scope_refusal=lambda *args: None, validate_handler_identity=False)
    context = next(d for d in adapter.descriptors if d.name == 'knowledge.context')
    branches = context.input_schema['allOf']
    assert all(branch.get('then') != {'not': {}} for branch in branches)
    assert not any(branch.get('if', {}).get('properties', {}).get('repo_key', {}).get('const') == 'bare'
                   for branch in branches)
    import jsonschema
    from kp_agent_tooling._impl.service.mcp_transport import _thaw
    jsonschema.validate({'repo_key': 'bare', 'capability_id': 'document-retrieval',
                         'target_revision': '0' * 40}, _thaw(context.input_schema))
