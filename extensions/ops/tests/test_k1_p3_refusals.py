"""K1 P3: refuse, never mix and never fall back (F5).

Each case gives a knowledge result (status ``error``, code ``knowledge_unavailable``,
a reason) and carries no answer from generation A, generation B or the operator pin.
"""
import copy
import hashlib
import json
import subprocess

import pytest

from k1_feature_world import World, write_private


@pytest.fixture
def world(tmp_path):
    return World(tmp_path / 'world')


def _assert_refused(world, result, refusal, reason=None):
    assert result['status'] == 'error', result
    error = result['data']['error']
    assert error['code'] == 'knowledge_unavailable' and error['refusal'] == refusal
    assert result['reason'] == (refusal if reason is None else reason)
    assert result['source_revision'] is None and 'navigation_profile' not in result
    text = json.dumps(result)
    for name in ('A', 'B'):
        for revision in world.revisions[name].values():
            assert revision not in text  # no answer from A, B or the operator pin (A)


def _every_tool(world, adapter):
    calls = [('knowledge.symbol', world.symbol('B')), ('knowledge.symbol', world.symbol('A')),
             ('knowledge.capabilities', {'repo_key': 'core'}),
             ('knowledge.retrieve', {'repo_key': 'core', 'query': 'guide'})]
    return [adapter.call(name, args) for name, args in calls]


def test_f5_a_catalog_whose_digest_differs_from_the_profile(world):
    adapter = world.adapter()
    profile = dict(world.profiles['B'], knowledge_config_sha256='0' * 64)
    world.publish('B', profile=profile)
    for result in _every_tool(world, adapter):
        _assert_refused(world, result, 'navigation_profile_refused', 'navigation catalog identity mismatch')


def test_f5_a_profile_active_refuses_passes_its_words_through(world):
    adapter = world.adapter()
    catalog = copy.deepcopy(world.catalogs['B'])
    catalog['repositories']['core']['ref'] = world.revisions['A']['core']  # catalog/profile pairing broken
    world.publish('B', catalog=catalog)
    profile = json.loads(world.current.read_text())
    profile['repos']['core']['revision'] = world.revisions['B']['core']
    world.publish('B', profile=profile)
    for result in _every_tool(world, adapter):
        _assert_refused(world, result, 'navigation_profile_refused', 'catalog source differs from active profile')
    world.publish('B', profile={'schema_version': 'ops.navigation-profile.v1', 'repos': {}})
    _assert_refused(world, adapter.call('knowledge.symbol', world.symbol('B')), 'navigation_profile_refused',
                    'profile repository membership changed; host refresh required')
    world.current.write_text('{')
    result = adapter.call('knowledge.symbol', world.symbol('B'))
    assert result['data']['error']['refusal'] == 'navigation_profile_refused'


def test_f5_a_served_repository_missing_from_the_published_catalog(world):
    adapter = world.adapter()
    operator = json.loads(world.operator_catalog.read_text())
    legacy = world.base / 'state' / 'repositories' / 'legacy'
    subprocess.check_call(['git', 'clone', '-q', str(world.src / 'ops'), str(legacy)], stderr=subprocess.DEVNULL)
    operator['repositories']['legacy'] = dict(operator['repositories']['ops'], path=str(legacy), corpus_scope='legacy')
    write_private(world.operator_catalog, operator)
    world.publish('B')
    for result in _every_tool(world, adapter):
        _assert_refused(world, result, 'served_repository_not_published')
        assert 'legacy' in result['data']['error']['detail']
        assert result['refused_navigation_profile']['published_at'] == 'generation-B'


@pytest.mark.parametrize('drop', ['scip_indexes.ops', 'scip_indexes.core', 'platforms.core', 'platforms',
                                  'platforms.core.sources.ops'])
def test_f5_a_platform_or_index_entry_a_served_repository_needs(world, drop):
    adapter = world.adapter()
    catalog = copy.deepcopy(world.catalogs['B'])
    *parents, last = drop.split('.')
    owner = catalog
    for part in parents:
        owner = owner[part]
    if parents:
        del owner[last]
    else:
        catalog[last] = {}
    world.publish('B', catalog=catalog)
    for result in _every_tool(world, adapter):
        _assert_refused(world, result, 'published_entry_missing')


def test_a_profile_that_publishes_no_catalog_serves_nothing(world):
    adapter = world.adapter()
    profile = {k: v for k, v in world.profiles['B'].items() if not k.startswith('knowledge_config')}
    world.publish('B', profile=profile)
    for result in _every_tool(world, adapter):
        _assert_refused(world, result, 'served_repository_not_published')


def test_merge_that_fails_validation_is_refused_not_mixed(world):
    adapter = world.adapter()
    catalog = copy.deepcopy(world.catalogs['B'])
    # A served repository's published index entry that active() does not check and the
    # catalog's own validation refuses (capabilities are the operator's: amendment 1).
    catalog['scip_indexes']['ops'][0]['sha256'] = 'not-a-digest'
    world.publish('B', catalog=catalog)
    for result in _every_tool(world, adapter):
        _assert_refused(world, result, 'merged_catalog_invalid')
        assert result['data']['error']['detail'] == 'index digest required'


def test_a_refusal_never_falls_back_to_the_last_good_generation(world):
    adapter = world.adapter()
    assert adapter.call('knowledge.symbol', world.symbol('A'))['status'] == 'ok'
    world.publish('B', profile=dict(world.profiles['B'], knowledge_config_sha256='1' * 64))
    _assert_refused(world, adapter.call('knowledge.symbol', world.symbol('A')), 'navigation_profile_refused',
                    'navigation catalog identity mismatch')
    world.publish('B')  # the generation is admitted again: answers resume, at B
    result = adapter.call('knowledge.symbol', world.symbol('B'))
    assert result['status'] == 'ok' and result['source_revision'] == world.revisions['B']['core']


def test_a_catalog_swapped_after_admission_is_refused(world, monkeypatch):
    from kp_agent_tooling._impl import navigation_workspace
    provider = world.provider()
    world.publish('B')
    catalog = world.generations / 'B' / 'knowledge.json'
    admitted = navigation_workspace.active

    def admit_then_swap(config, **kwargs):
        profile = admitted(config, **kwargs)
        catalog.write_text(catalog.read_text() + ' ')
        return profile
    monkeypatch.setattr(navigation_workspace, 'active', admit_then_swap)
    result = provider.call('knowledge.symbol', world.symbol('B'))
    _assert_refused(world, result, 'navigation_profile_refused', 'navigation catalog changed after admission')
    assert hashlib.sha256(catalog.read_bytes()).hexdigest() != world.profiles['B']['knowledge_config_sha256']
