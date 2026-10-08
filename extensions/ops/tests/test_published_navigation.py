import hashlib
import json
from pathlib import Path
import pytest
from test_scip_navigation import repo, indexed
from kp_agent_tooling._impl.scip_navigation import write_partitioned_index
from kp_agent_tooling_ops._impl.service.published_navigation import PublishedScipNavigationProvider


@pytest.fixture
def published(repo):
    root, revision = repo
    index = root / 'index.json'
    envelope = write_partitioned_index(index, indexed(repo))
    member = {'path': str(root), 'revision': revision}
    catalog = {'repositories': {'demo': {'path': str(root), 'ref': revision}},
               'scip_indexes': {'demo': [{'path': str(index), 'revision': revision, 'sha256': envelope['sha256']}]}}
    catalog_path = root / 'catalog.json'
    catalog_path.write_text(json.dumps(catalog))
    profile = {'schema_version': 'ops.navigation-profile.v1', 'repos': {'demo': member},
               'knowledge_config': str(catalog_path),
               'knowledge_config_sha256': hashlib.sha256(catalog_path.read_bytes()).hexdigest()}
    path = root / 'profile.json'
    path.write_text(json.dumps(profile))
    config = {'provider':'published_scip','profile_path':str(path),
              'published_root':str(root),'local_root':str(root)}
    return PublishedScipNavigationProvider(config, {'demo': {'path':str(root)}}), root, revision, path


def test_published_entry_is_source_verified_despite_worktree_changes(published):
    provider, root, revision, _ = published
    (root/'server.py').write_text('def unrelated(): pass\n')
    result = provider.inspect(root, revision, 'server.inspect')
    assert result['status'] == 'ok'
    assert result['report']['citation']['excerpt'] == 'def inspect():'
    assert result['report']['coverage'] == 'entry_definition_only'
    assert result['report']['next_call']['arguments']['target_revision'] == revision
    assert provider.inspect(root, revision, 'server.py::inspect')['status'] == 'ok'


def test_wrong_revision_is_not_silently_repinned(published):
    provider, root, revision, _ = published
    result = provider.inspect(root, '0'*40, 'server.inspect')
    assert result['reason'] == 'target_not_in_published_profile'
    assert result['source_revision'] == '0'*40 and result['published_revision'] == revision


@pytest.mark.parametrize('damage', ['catalog', 'partition', 'escape', 'symbol'])
def test_invalid_published_evidence_fails_closed(published, damage):
    provider, root, revision, path = published
    symbol = 'server.inspect'
    if damage == 'catalog':
        (root/'catalog.json').write_text('{}')
    elif damage == 'partition':
        next(root.glob('*.part.json')).write_text('[]')
    elif damage == 'escape':
        profile = json.loads(path.read_text());profile['knowledge_config']='/outside/catalog.json'
        path.write_text(json.dumps(profile))
    else:
        symbol = 'server.absent'
    result = provider.inspect(root, revision, symbol)
    assert result['status'] == 'unavailable' and result.get('reason')
    assert 'report' not in result


def test_service_configuration_selects_published_provider(published):
    from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeService
    provider, root, revision, path = published
    config = {'schema_version':'ops.knowledge-config.v1','repositories':{'demo':{
        'path':str(root),'ref':revision,'corpus_scope':'demo','tenant_ids':['demo'],'capabilities':{}}},
        'navigation':{'provider':'published_scip','profile_path':str(path),
                      'published_root':str(root),'local_root':str(root)}}
    service = KnowledgeService(config, lambda: None)
    result = service._navigation_provider.inspect(root, revision, 'server.inspect')
    assert result['status'] == 'ok'
    assert result['report']['source_call']['arguments']['target_revision'] == revision
