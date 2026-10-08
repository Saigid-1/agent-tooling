"""K1 P4: without a navigation profile nothing changes (F6, a guard GREEN at base)."""
import json

import pytest

from k1_feature_world import World


@pytest.fixture
def world(tmp_path):
    value = World(tmp_path / 'world')
    tooling = dict(value.tooling_value)
    tooling.pop('navigation_profile')
    value.write_tooling(tooling)
    value.publish('B')  # published, but not configured: it must not be followed
    return value


def test_f6_configured_pins_read_the_operator_catalog(world):
    adapter = world.adapter()
    pinned = adapter.call('knowledge.symbol', world.symbol('A'))
    assert pinned['status'] == 'ok' and pinned['source_revision'] == world.revisions['A']['core']
    assert 'navigation_profile' not in pinned
    later = adapter.call('knowledge.symbol', world.symbol('B'))
    assert later['status'] == 'partial' and later['data']['gaps'] == ['target_not_in_platform_snapshot']
    listed = adapter.call('knowledge.capabilities', {'repo_key': 'core'})
    assert [row['capability_id'] for row in listed['data']['capabilities']] == ['cap-a']
    operator = json.loads(world.operator_catalog.read_text())
    assert adapter.knowledge_provider.service._config_path is not None
    assert adapter.knowledge_provider.service._config == operator


def test_f6_without_a_profile_the_provider_never_reads_one(world, monkeypatch):
    from kp_agent_tooling._impl import navigation_workspace
    def refuse(*_, **__):
        raise AssertionError('active() read without a configured profile')
    monkeypatch.setattr(navigation_workspace, 'active', refuse)
    provider = world.provider()
    assert provider.call('knowledge.symbol', world.symbol('A'))['status'] == 'ok'
    assert [t['name'] for t in provider.tools()] == [t['name'] for t in world.provider(navigation=False).tools()]


def test_f6_operator_edits_are_followed_per_call_as_before(world):
    provider = world.provider()
    operator = json.loads(world.operator_catalog.read_text())
    for row in operator['repositories'].values():  # platform members keep the anchor's tenants
        row['tenant_ids'] = ['someone-else']
    from k1_feature_world import write_private
    write_private(world.operator_catalog, operator)
    with pytest.raises(PermissionError):
        provider.call('knowledge.capabilities', {'repo_key': 'core'})
