import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeRequestError, KnowledgeService


@dataclass
class Hit:
    repo_key: str = 'docs'
    path: str = 'docs/x.md'
    blob_sha: str = 'a' * 40
    byte_offset: int = 0
    byte_length: int = 12
    heading: str = 'Title'
    score: float = 0.8
    text: str = 'hello'


class Reader:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def recall(self, **kwargs):
        self.calls.append(kwargs)
        return self.hits


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / 'repo'; repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / 'scripts').mkdir()
    (repo / 'scripts' / 'find.py').write_text('"""Find widgets."""\n')
    (repo / 'docs').mkdir()
    (repo / 'docs' / 'x.md').write_text('hello')
    (repo / 'manifest.json').write_text(json.dumps({
        'schema_version': 'ops.capability-map.v1', 'capability_id': 'cap',
        'summary': 'sample', 'source_revision': '0' * 40, 'references': [{
            'id': 'entry', 'role': 'entrypoint', 'path': 'scripts/find.py',
            'blob_sha': '0' * 40}]}))
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'first'], check=True)
    config = {'schema_version': 'ops.knowledge-config.v1', 'repositories': {
        'repo': {'path': str(repo), 'ref': 'HEAD',
                 'default_branch_ref': subprocess.check_output(
                     ['git', '-C', str(repo), 'symbolic-ref', 'HEAD'], text=True).strip(),
                 'corpus_scope': 'docs',
                 'tenant_ids': ['tenant'], 'capabilities': {},
                 'artifacts': {'docs/x.md': {'status': 'maintained', 'owner': 'OPS'}}}}}
    return repo, config


def test_validation_and_lazy_backend(setup):
    _, config = setup
    blob = subprocess.check_output(['git', '-C', str(setup[0]), 'rev-parse', 'HEAD:docs/x.md'], text=True).strip()
    reader = Reader((Hit(blob_sha=blob),))
    calls = []
    def factory():
        calls.append(1); return reader
    service = KnowledgeService(config, factory)
    assert service.repository_keys == ('repo',)
    assert service.permits_tenant('repo', 'tenant')
    assert not service.permits_tenant('repo', 'other')
    with pytest.raises(KnowledgeRequestError):
        service.execute('retrieve', {'repo_key': 'repo', 'query': 'x', 'top_k': True})
    with pytest.raises(KnowledgeRequestError):
        service.execute('discover', {'repo_key': 'repo', 'query': 'x', 'tenant_id': 'tenant'})
    absent = service.execute('check_references', {'repo_key': 'repo', 'capability_id': 'absent'})
    assert absent['status'] == 'error'
    assert absent['data']['error']['code'] == 'no_capabilities_declared'
    assert calls == []
    discovered = service.execute('discover', {'repo_key': 'repo', 'query': 'find'})
    assert discovered['status'] == 'ok'
    assert discovered['data']['results'][0]['tool'] == 'find.py'
    assert calls == []
    retrieved = service.execute('retrieve', {'repo_key': 'repo', 'query': 'hello'})
    assert retrieved['status'] == 'ok'
    assert retrieved['data']['results'][0]['blob_sha'] == blob
    assert reader.calls == [{'query': 'hello', 'top_k': 5, 'repo_keys': ('docs',), 'paths': ('docs/x.md',)}]
    assert retrieved['data']['repo_keys'] == ['docs']
    assert 'selected committed source' in retrieved['limitations'][0]
    assert len(calls) == 1


def test_retrieve_revision_proof_and_historical_separation(setup):
    repo, config = setup
    blob = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD:docs/x.md'], text=True).strip()
    baseline = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    config['repositories']['repo']['ref'] = baseline
    reader = Reader((Hit(blob_sha=blob),))
    service = KnowledgeService(config, lambda: reader)
    (repo / 'scripts' / 'find.py').write_text('"""Find more widgets."""\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
                    'commit', '-qm', 'code only'], check=True)
    unchanged = service.execute('retrieve', {'repo_key': 'repo', 'query': 'hello'})
    assert unchanged['status'] == 'ok'
    assert unchanged['source_revision'] == unchanged['target_revision']
    assert unchanged['data']['target_revision'] == unchanged['target_revision']
    assert unchanged['data']['default_revision'] == unchanged['target_revision']
    assert unchanged['data']['configured_catalog_revision'] == baseline
    assert unchanged['data']['configured_catalog_lineage'] == 'ancestor'
    assert unchanged['data']['corpus_revision_status'] == 'unknown'
    assert unchanged['data']['results'][0]['revision_proof'] == 'exact_path_blob_match'
    assert unchanged['data']['results'][0]['document_revision'] == unchanged['target_revision']
    assert unchanged['data']['results'][0]['target_blob_sha'] == blob
    (repo / 'docs' / 'x.md').write_text('new guidance')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
                    'commit', '-qm', 'guide change'], check=True)
    stale = service.execute('retrieve', {'repo_key': 'repo', 'query': 'hello'})
    assert stale['status'] == 'no_results'
    assert stale['data']['results'] == []
    assert stale['data']['reason'] == 'indexed_bytes_not_at_target'
    assert stale['data']['source_fallbacks'][0]['arguments'] == {
        'repo_key': 'repo', 'target_revision': stale['target_revision'],
        'path': 'docs/x.md', 'start_line': 1, 'line_count': 80}
    assert stale['data']['source_fallbacks'][0]['availability'] == 'not-verified'
    historical = service.execute('retrieve', {'repo_key': 'repo', 'query': 'hello',
                                               'include_historical': True})
    assert historical['data']['results'] == []
    assert historical['data']['historical_results'][0]['guidance_scope'] == 'historical_only'
    assert historical['data']['historical_results'][0]['document_revision'] is None


def test_new_document_not_indexed_offers_exact_source_fallback(setup):
    repo, config = setup
    config['repositories']['repo']['ref'] = subprocess.check_output(
        ['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    (repo / 'docs/new.md').write_text('new unindexed guidance')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
                    'commit', '-qm', 'new document'], check=True)
    config['repositories']['repo']['artifacts']['docs/new.md'] = {
        'status': 'maintained', 'owner': 'OPS'}
    report = KnowledgeService(config, lambda: Reader(())).execute(
        'retrieve', {'repo_key': 'repo', 'query': 'new guidance'})
    assert report['status'] == 'no_results'
    assert report['data']['indexed_artifacts'] == 'not-assessed'
    assert 'exact requested revision' in report['data']['next_action']
    assert any(item['arguments']['path'] == 'docs/new.md'
               for item in report['data']['source_fallbacks'])
    assert report['data']['configured_catalog_lineage'] == 'ancestor'


def test_default_branch_missing_and_explicit_target_unavailable_are_named(setup):
    repo, config = setup
    config['repositories']['repo']['default_branch_ref'] = 'refs/heads/no-such-branch'
    service = KnowledgeService(config, lambda: Reader(()))
    missing_default = service.execute('retrieve', {'repo_key': 'repo', 'query': 'x'})
    assert missing_default['data']['error']['code'] == 'default_branch_unavailable'
    missing_target = service.execute('retrieve', {'repo_key': 'repo', 'query': 'x',
                                                   'target_revision': '0' * 40})
    assert missing_target['data']['error']['code'] == 'target_unavailable'


def test_divergent_catalog_cannot_supply_current_guidance(setup):
    repo, config = setup
    main_ref = config['repositories']['repo']['default_branch_ref']
    main_name = main_ref.removeprefix('refs/heads/')
    blob = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD:docs/x.md'], text=True).strip()
    subprocess.run(['git', '-C', str(repo), 'checkout', '-qb', 'catalog'], check=True)
    (repo / 'scripts/find.py').write_text('"""Catalog branch."""\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
                    'commit', '-qm', 'catalog fork'], check=True)
    catalog = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    subprocess.run(['git', '-C', str(repo), 'checkout', '-q', main_name], check=True)
    (repo / 'scripts/find.py').write_text('"""Main branch."""\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
                    'commit', '-qm', 'main fork'], check=True)
    config['repositories']['repo']['ref'] = catalog
    service = KnowledgeService(config, lambda: Reader((Hit(blob_sha=blob),)))
    current = service.execute('retrieve', {'repo_key': 'repo', 'query': 'hello'})
    assert current['data']['configured_catalog_lineage'] == 'divergent'
    assert current['data']['configured_catalog_default_lineage'] == 'divergent'
    assert current['data']['results'] == []
    assert current['data']['reason'] == 'catalog_lineage_untrusted'
    historical_target = service.execute('retrieve', {'repo_key': 'repo', 'query': 'hello',
                                                       'target_revision': catalog})
    assert historical_target['data']['results'][0]['revision_proof'] == 'exact_path_blob_match'
    assert historical_target['data']['guidance_scope'] == 'requested_revision_only'
    assert historical_target['data']['configured_catalog_default_lineage'] == 'divergent'


def test_lineage_distinguishes_descendant_and_git_failure(setup, monkeypatch):
    from types import SimpleNamespace
    import kp_agent_tooling_ops._impl.service.knowledge as module
    repo, _ = setup
    original = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                                       text=True).strip()
    (repo / 'scripts/find.py').write_text('"""Later."""\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
                    'commit', '-qm', 'later'], check=True)
    later = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'],
                                    text=True).strip()
    catalog = SimpleNamespace(repository=repo, revision=later)
    target = SimpleNamespace(repository=repo, revision=original)
    assert KnowledgeService._configured_lineage(catalog, target) == 'descendant'
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 10)
    monkeypatch.setattr(module.subprocess, 'run', timeout)
    assert KnowledgeService._configured_lineage(catalog, target) == 'unknown'


def test_capability_inventory_is_repository_specific(setup):
    _, config = setup
    config['repositories']['repo']['capabilities'] = {'docs': 'manifest.json'}
    config['repositories']['other'] = {**config['repositories']['repo'], 'capabilities': {}}
    service = KnowledgeService(config, lambda: None)
    assert service.execute('capabilities', {'repo_key': 'repo'})['data']['capabilities'] == [
        {'capability_id': 'docs', 'manifest_path': 'manifest.json'}]
    assert service.execute('capabilities', {'repo_key': 'other'})['data']['capabilities'] == []


def test_bad_config_and_bounded_rows(setup):
    _, config = setup
    bad = json.loads(json.dumps(config)); bad['extra'] = True
    with pytest.raises(KnowledgeRequestError): KnowledgeService(bad, lambda: None)
    config['repositories']['repo']['artifacts'] = {f'docs/{i}.md': {'status':'maintained','owner':'OPS'} for i in range(50)}
    repo = setup[0]
    for i in range(50):
        (repo / 'docs' / f'{i}.md').write_text('x' * 1500)
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
                    'commit', '-qm', 'documents'], check=True)
    blob = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD:docs/0.md'], text=True).strip()
    reader = Reader(tuple(Hit(text='x' * 1500, path=f'docs/{i}.md', blob_sha=blob) for i in range(50)))
    service = KnowledgeService(config, lambda: reader)
    report = service.execute('retrieve', {'repo_key': 'repo', 'query': 'x', 'top_k': 50})
    assert report['omitted'] > 0
    assert report['status'] == 'ok'
    assert len((json.dumps(report, ensure_ascii=True) + '\n').encode()) <= 32768
    assert report['data']['returned'] == len(report['data']['results'])
    assert report['omitted'] == 50 - len(report['data']['results'])
    assert all(row['blob_sha'] == blob for row in report['data']['results'])


def test_backend_failure_safe(setup):
    _, config = setup
    def fail(): raise RuntimeError('secret credential')
    report = KnowledgeService(config, fail).execute('retrieve', {'repo_key': 'repo', 'query': 'x'})
    assert report['status'] == 'error'
    assert 'secret credential' not in json.dumps(report)




def test_query_and_source_errors(setup):
    _, config = setup
    service = KnowledgeService(config, lambda: pytest.fail('backend constructed'))
    with pytest.raises(KnowledgeRequestError):
        service.execute('discover', {'repo_key': 'repo', 'query': 'é' * 257})
    config['repositories']['repo']['ref'] = 'missing'
    report = KnowledgeService(config, lambda: None).execute('discover', {'repo_key': 'repo', 'query': 'x'})
    assert report['status'] == 'error'
    assert report['source_revision'] is None




@pytest.mark.parametrize('mutation', [
    lambda c: c.update(schema_version='wrong'),
    lambda c: c['repositories']['repo'].update(extra=True),
    lambda c: c['repositories']['repo'].update(path='relative'),
    lambda c: c['repositories']['repo'].update(tenant_ids=[]),
    lambda c: c['repositories']['repo'].update(tenant_ids=['']),
    lambda c: c['repositories']['repo'].update(capabilities={'cap': '../escape'}),
    lambda c: c['repositories']['repo'].update(capabilities={'cap': '/absolute'}),
])
def test_config_rejects_malformed_fields(setup, mutation):
    _, config = setup
    mutation(config)
    with pytest.raises(KnowledgeRequestError):
        KnowledgeService(config, lambda: None)


@pytest.mark.parametrize('operation,args', [
    ('unknown', {'repo_key': 'repo'}),
    ('discover', {'repo_key': 'repo', 'query': 'x', 'limit': 0}),
    ('discover', {'repo_key': 'repo', 'query': 'x', 'limit': True}),
    ('discover', {'repo_key': 'repo', 'query': 'x', 'limit': 51}),
    ('discover', {'repo_key': 'repo', 'query': 'é' * 257}),
    ('retrieve', {'repo_key': 'repo', 'query': 'x', 'top_k': False}),
    ('retrieve', {'repo_key': 'repo', 'query': 'x', 'path': 'secret'}),
    ('retrieve', {'repo_key': 'unknown', 'query': 'x'}),
    ('retrieve', {'repo_key': 'repo', 'query': '  '}),
])
def test_closed_requests_refuse_before_backend(setup, operation, args):
    _, config = setup
    service = KnowledgeService(config, lambda: pytest.fail('backend constructed'))
    with pytest.raises(KnowledgeRequestError):
        service.execute(operation, args)


def test_undeclared_capability_is_reported_before_backend(setup):
    _, config = setup
    service = KnowledgeService(config, lambda: pytest.fail('backend constructed'))
    report = service.execute('check_references', {'repo_key': 'repo', 'capability_id': 'unknown'})
    assert report['status'] == 'error'
    assert report['data']['error']['code'] == 'no_capabilities_declared'
    assert report['data']['error']['declared_capabilities'] == []


def test_tenant_and_interrupt_behavior(setup):
    _, config = setup
    def interrupt(): raise KeyboardInterrupt
    service = KnowledgeService(config, interrupt)
    assert not service.permits_tenant([], 'tenant')
    assert not service.permits_tenant('repo', [])
    with pytest.raises(KeyboardInterrupt):
        service.execute('retrieve', {'repo_key': 'repo', 'query': 'x'})




def test_mapping_arguments_and_config_isolation(setup):
    from types import MappingProxyType
    _, config = setup
    service = KnowledgeService(config, lambda: Reader(()))
    config['repositories']['repo']['tenant_ids'].append('injected')
    assert not service.permits_tenant('repo', 'injected')
    result = service.execute('retrieve', MappingProxyType({'repo_key': 'repo', 'query': 'x'}))
    assert result['status'] == 'no_results'
    assert result['data']['repo_keys'] == ['docs']


def test_discovery_limit_counts_omissions(setup):
    repo, config = setup
    (repo / 'scripts' / 'find_two.py').write_text('"""Find another widget."""\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'second'], check=True)
    report = KnowledgeService(config, lambda: pytest.fail('backend constructed')).execute(
        'discover', {'repo_key': 'repo', 'query': 'find', 'limit': 1})
    assert report['status'] == 'ok'
    assert report['data']['available'] >= 2
    assert report['omitted'] == report['data']['available'] - 1


def test_git_source_bounded_read(setup):
    from kp_agent_tooling_ops._impl.tool_discovery import DiscoveryError, GitSource
    repo, _ = setup
    source = GitSource(repo, 'HEAD')
    _, blob = source.entry('scripts/find.py')
    with pytest.raises(DiscoveryError):
        source.read_bounded(blob, 2)
    assert source.read_bounded(blob, 100) == b'"""Find widgets."""\n'

from knowledge_lifecycle_fixture import isolated_lifecycle
