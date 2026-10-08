"""K1 P5: the provider is rebuilt on a new generation, not reloaded by error (F7)."""
import json

import pytest

from k1_feature_world import World, write_private


@pytest.fixture
def world(tmp_path):
    return World(tmp_path / 'world')


@pytest.fixture
def counted(monkeypatch):
    """Count services built and corpora opened by the provider, and active() reads."""
    from kp_agent_tooling._impl import navigation_workspace
    from kp_agent_tooling_ops._impl.service import portable_knowledge
    counts = {'service': 0, 'corpus': 0, 'active': 0}

    def counting(name, original):
        def wrapper(*args, **kwargs):
            counts[name] += 1
            return original(*args, **kwargs)
        return wrapper
    monkeypatch.setattr(portable_knowledge, 'KnowledgeService', counting('service', portable_knowledge.KnowledgeService))
    monkeypatch.setattr(portable_knowledge, 'DocumentCorpus', counting('corpus', portable_knowledge.DocumentCorpus))
    monkeypatch.setattr(navigation_workspace, 'active', counting('active', navigation_workspace.active))
    return counts


def _retrieve(world, name):
    return {'repo_key': 'core', 'query': 'portable context builder', 'target_revision': world.revisions[name]['core']}


def test_f7_a_profile_advance_between_two_calls_needs_no_reload(world):
    adapter = world.adapter()
    first = adapter.call('knowledge.symbol', world.symbol('A'))
    assert first['source_revision'] == world.revisions['A']['core']
    world.publish('B')
    second = adapter.call('knowledge.symbol', world.symbol('B'))  # no "reload required"
    assert second['status'] == 'ok' and second['source_revision'] == world.revisions['B']['core']
    assert second['navigation_profile']['published_at'] == 'generation-B'


def test_f7_pair_the_same_generation_rebuilds_nothing(world, counted):
    provider = world.provider()
    assert counted['service'] == 2  # the operator listing, then generation A
    provider.call('knowledge.retrieve', _retrieve(world, 'A'))
    service, corpus = provider.service, provider.reader()
    before = dict(counted)
    for _ in range(3):
        assert provider.call('knowledge.retrieve', _retrieve(world, 'A'))['status'] == 'ok'
        assert provider.call('knowledge.symbol', world.symbol('A'))['status'] == 'ok'
    assert provider.service is service and provider.reader() is corpus
    assert counted['service'] == before['service'] and counted['corpus'] == before['corpus'] == 1
    assert counted['active'] - before['active'] == 6  # one admission per call
    # A re-published profile over the same catalog is the same generation.
    world.publish('A', profile=dict(world.profiles['A'], published_at='re-stamped'))
    result = provider.call('knowledge.symbol', world.symbol('A'))
    assert result['navigation_profile']['published_at'] == 're-stamped'
    assert provider.service is service and counted['service'] == before['service']


def test_f7_a_new_generation_rebuilds_the_service_but_not_the_corpus(world, counted):
    provider = world.provider()
    provider.call('knowledge.retrieve', _retrieve(world, 'A'))
    service = provider.service
    world.publish('B')
    assert provider.call('knowledge.retrieve', _retrieve(world, 'B'))['status'] == 'ok'
    assert provider.service is not service
    assert counted['corpus'] == 1


def test_runtime_file_change_keeps_todays_refusal(world):
    provider = world.provider()
    runtime = json.loads(world.runtime.read_text())
    runtime['tenant_id'] = 'changed'
    write_private(world.runtime, runtime)
    with pytest.raises(ValueError, match='reload required'):
        provider.call('knowledge.symbol', world.symbol('A'))


def test_operator_catalog_edits_are_followed_per_call_as_today(world):
    adapter = world.adapter()
    world.publish('B')
    assert adapter.call('knowledge.retrieve', _retrieve(world, 'B'))['data']['results']
    operator = json.loads(world.operator_catalog.read_text())
    operator['repositories']['core']['artifacts']['docs/guide.md']['status'] = 'withdrawn'
    write_private(world.operator_catalog, operator)
    withdrawn = adapter.call('knowledge.retrieve', _retrieve(world, 'B'))
    assert withdrawn['data']['results'] == [] and withdrawn['data']['declared_artifacts'] == 0
    for row in operator['repositories'].values():  # platform members keep the anchor's tenants
        row['tenant_ids'] = ['someone-else']
    write_private(world.operator_catalog, operator)
    with pytest.raises(PermissionError):
        adapter.call('knowledge.capabilities', {'repo_key': 'core'})
    write_private(world.operator_catalog, {'schema_version': 'unsupported'})
    from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeRequestError
    with pytest.raises(KnowledgeRequestError):
        adapter.call('knowledge.capabilities', {'repo_key': 'core'})
