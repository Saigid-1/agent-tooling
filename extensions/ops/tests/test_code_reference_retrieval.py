import json
import subprocess

import pytest

from kp_agent_tooling_ops._impl.code_references.adapters import (FrozenNavigationReadiness,
                                              RetainedNavigationReadiness)
from kp_agent_tooling_ops._impl.code_references.extraction import EXTRACTION_POLICY_VERSION
from kp_agent_tooling_ops._impl.code_references.manifest import FrozenReferenceRetrieval, ManifestShard
from kp_agent_tooling_ops._impl.code_references.resolution import RESOLUTION_POLICY_VERSION
from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeRequestError, KnowledgeService
from test_knowledge_service import Hit, Reader, setup


class Entities:
    def __init__(self, values): self.values = values
    def get(self, key): return self.values.get(key)


class Graph:
    def __init__(self, values): self.entities = Entities(values)


class Store(FrozenReferenceRetrieval):
    query_bounded = True
    def __init__(self, shard, edge): self.shard, self.edge, self.calls = shard, edge, []
    def select(self, **kwargs):
        self.calls.append(kwargs)
        return self.shard if kwargs['target_revision'] == self.shard.target_revision else None
    def retrieval_view(self, shard, graph):
        edge = dict(self.edge)
        edge.setdefault('_target_symbol', {})
        edge.setdefault('_target_change', {'file_path': 'scripts/find.py',
                                            'blob_sha': edge['attributes']['resolved_blob_sha']})
        return {'shard_id': shard.shard_id, 'state': shard.state,
                'candidate_total': shard.candidate_total,
                'retained_count': shard.retained_count,
                'overflow_count': shard.overflow_count,
                'omitted_by_kind': shard.omitted_by_kind,
                'unowned_count': shard.unowned_count,
                'outcomes': [dict(shard.outcomes[0], edge_ids=None)],
                'positive_edges': [edge]}


def revision(repo):
    return subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()


def blob(repo, path):
    return subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD:' + path], text=True).strip()


def test_disabled_path_ignores_maps_and_is_byte_shape_identical(setup):
    _, config = setup
    factory = lambda: Reader((Hit(),))
    service = KnowledgeService(config, factory)
    original = service.execute('retrieve', {'repo_key': 'repo', 'query': 'x'})
    disabled = service.execute('retrieve', {'repo_key': 'repo', 'query': 'x',
        'include_code_references': False, 'code_revisions': 'deliberately invalid',
        'reference_baseline_revisions': {'unknown': 'HEAD'}})
    assert disabled == original


def test_enabled_real_service_and_mcp_select_exact_manifest(setup, tmp_path):
    repo, config = setup
    config['repositories']['repo']['tenant_ids'] = ['tenant-a']
    target, target_blob = revision(repo), blob(repo, 'scripts/find.py')
    hit = Hit(blob_sha=blob(repo, 'docs/x.md'), byte_length=5)
    from kp_agent_tooling_ops._impl.code_references.retrieval import _chunk_id
    from dataclasses import asdict
    chunk = _chunk_id(asdict(hit))
    outcome = {'occurrence_id': 'occ', 'resolution_id': 'res', 'kind': 'path',
        'ref_byte_offset': 4, 'ref_byte_length': 15, 'literal_digest': 'a' * 64,
        'execution_state': 'complete', 'status': 'resolved', 'reason': None,
        'coverage': 'complete', 'match_count': 1, 'truncated': False,
        'edge_ids': ['edge']}
    shard = ManifestShard('shard', 'repo', hit.path, hit.blob_sha, chunk, 'repo', target,
        EXTRACTION_POLICY_VERSION, RESOLUTION_POLICY_VERSION, 'complete', 'inputs',
        1, 1, 0, {}, 0, True, True, (outcome,))
    edge = {'id': 'edge', 'to_id': 'change', 'attributes': {
        'occurrence_id': 'occ', 'resolution_id': 'res', 'resolved_blob_sha': target_blob,
        'target_start_line': 1, 'target_end_line': 1}}
    graph = Graph({'change': {'entity_type': 'Change', 'attributes': {
        'file_path': 'scripts/find.py', 'repo_key': 'repo', 'commit_sha': target,
        'blob_sha': target_blob}}})
    navigation = FrozenNavigationReadiness(snapshot_id='navigation-snapshot:sha256:' + 'b' * 64,
        revisions={'repo': target}, blobs={'repo': {'scripts/find.py': target_blob}})
    store = Store(shard, edge)
    service = KnowledgeService(config, lambda: Reader((hit,)), reference_store=store,
        reference_graph=graph, reference_navigation=navigation)
    report = service.execute('retrieve', {'repo_key': 'repo', 'query': 'x',
        'include_code_references': True, 'code_revisions': {'repo': target}})
    row = report['data']['results'][0]
    assert row['code_reference_coverage']['state'] == 'complete'
    assert row['code_reference_coverage']['status_counts']['resolved'] == 1
    reference = row['code_references'][0]
    assert reference['resolution_validity'] == 'established_at_requested_revision'
    assert reference['target_file_freshness'] == 'current'
    assert reference['continuation']['operation'] == 'navigation.source'
    assert reference['continuation']['arguments']['snapshot_id'] == navigation.snapshot_id
    assert store.calls[0]['target_revision'] == target

def test_enabled_maps_are_validated_before_backend(setup):
    _, config = setup
    service = KnowledgeService(config, lambda: pytest.fail('backend accessed'))
    with pytest.raises(KnowledgeRequestError):
        service.execute('retrieve', {'repo_key': 'repo', 'query': 'x',
            'include_code_references': True, 'code_revisions': {'repo': 'HEAD'}})


def test_readiness_is_derived_from_real_snapshot_registry(tmp_path):
    repo = tmp_path / 'source'; repo.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'Test'], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'test@example.invalid'], check=True)
    (repo / 'api.py').write_text('VALUE = 1\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'source'], check=True)
    target, target_blob = revision(repo), blob(repo, 'api.py')
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'schema_version': 'ops.navigation-profile.v1',
        'profile': 'fixture', 'published_at': 'fixture',
        'repos': {'repo': {'path': str(repo), 'revision': target,
                           'default_ref': 'refs/heads/main'}}}))
    config_model = {'schema_version': 'ops.agent-tooling.v1',
        'repos': {'repo': {'path': str(repo), 'revision': target}},
        'navigation_profile': str(profile)}
    config = tmp_path / 'tooling.json'; config.write_text(json.dumps(config_model))
    from kp_agent_tooling._impl.service.agent_tooling import AgentTooling
    tooling = AgentTooling(config)
    snapshot = tooling.call('navigation.snapshot', {'mode': 'capture'})
    readiness = FrozenNavigationReadiness.from_registry(config=config_model,
        registry=tooling.snapshot_registry, snapshot_id=snapshot['snapshot_id'])
    continuation = readiness.continuation(repo_key='repo', revision=target,
        path='api.py', blob_sha=target_blob, start_line=1, line_count=1)
    assert continuation['arguments']['snapshot_id'] == snapshot['snapshot_id']
    served = tooling.call('navigation.source', continuation['arguments'])
    assert served['status'] == 'ok' and served['blob_sha'] == target_blob


def test_retained_snapshot_routes_exact_old_revision_without_repinning_active(tmp_path):
    repo = tmp_path / 'source'; repo.mkdir()
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'Test'], check=True)
    subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'test@example.invalid'], check=True)
    registry = tmp_path / 'retained-snapshots'; registry.mkdir()
    profile = tmp_path / 'profile.json'
    config = tmp_path / 'tooling.json'

    def publish(value):
        (repo / 'api.py').write_text(f'VALUE = {value}\n')
        subprocess.run(['git', '-C', str(repo), 'add', 'api.py'], check=True)
        subprocess.run(['git', '-C', str(repo), 'commit', '-qm', f'value {value}'], check=True)
        revision_value, blob_value = revision(repo), blob(repo, 'api.py')
        profile.write_text(json.dumps({'schema_version': 'ops.navigation-profile.v1',
            'profile': 'fixture', 'published_at': 'fixture',
            'repos': {'repo': {'path': str(repo), 'revision': revision_value,
                               'default_ref': 'refs/heads/main'}}}))
        model = {'schema_version': 'ops.agent-tooling.v1',
            'repos': {'repo': {'path': str(repo), 'revision': revision_value}},
            'navigation_profile': str(profile), 'snapshot_registry': str(registry)}
        config.write_text(json.dumps(model))
        from kp_agent_tooling._impl.navigation_snapshot import capture
        snapshot = capture(model, registry)
        return model, snapshot['snapshot_id'], revision_value, blob_value

    _, old_snapshot, old_revision, old_blob = publish(1)
    model, active_snapshot, active_revision, _ = publish(2)
    (registry / ('selection-' + 'f' * 64 + '.json')).write_text('{}')
    readiness = RetainedNavigationReadiness.from_registry(config=model, registry=registry,
        active_snapshot_id=active_snapshot)

    assert readiness.active_revisions() == {'repo': active_revision}
    continuation = readiness.continuation(repo_key='repo', revision=old_revision,
        path='api.py', blob_sha=old_blob, start_line=1, line_count=1)
    assert continuation['arguments']['snapshot_id'] == old_snapshot
    from kp_agent_tooling._impl.navigation_snapshot import select
    from kp_agent_tooling._impl.navigation_workspace import read_source
    selected_config, _ = select(model, registry, continuation['arguments']['snapshot_id'])
    source_arguments = dict(continuation['arguments']); source_arguments.pop('snapshot_id')
    served = read_source(selected_config, **source_arguments)
    assert served['status'] == 'ok' and served['source_revision'] == old_revision
    assert readiness.continuation(repo_key='repo', revision=old_revision,
        path='api.py', blob_sha='f' * 40, start_line=1, line_count=1) is None
    assert readiness.continuation(repo_key='repo', revision='e' * 40,
        path='api.py', blob_sha=old_blob, start_line=1, line_count=1) is None


def test_agent_tooling_config_changes_reuse_explicit_snapshot_registry(tmp_path):
    from kp_agent_tooling._impl.service.agent_tooling import AgentTooling
    registry = tmp_path / 'stable-snapshots'; registry.mkdir()
    paths = []
    for marker in ('release-one', 'release-two'):
        config = tmp_path / f'{marker}.json'
        config.write_text(json.dumps({'schema_version': 'ops.agent-tooling.v1',
            'repos': {}, 'release_marker': marker,
            'snapshot_registry': str(registry)}))
        paths.append(AgentTooling(config).snapshot_registry)
    assert paths == [registry, registry]
