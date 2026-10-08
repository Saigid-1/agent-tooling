"""K1 P1: under a navigation profile, the generation fields follow the active profile.

F1 (a new generation is followed) and F2 (the stale operator path), on the served
surface (AgentTooling with the OPS provider). F9 is inverted by amendment 1:
``capabilities`` is an operator field, so a generation's map is never taken.
"""
import copy
import json
import subprocess

import pytest

from k1_feature_world import World


@pytest.fixture
def world(tmp_path):
    return World(tmp_path / 'world')


def test_f1_symbol_answers_at_the_published_generation(world):
    adapter = world.adapter()
    first = adapter.call('knowledge.symbol', world.symbol('A'))
    assert first['status'] == 'ok' and first['source_revision'] == world.revisions['A']['core']
    assert first['navigation_profile']['profile_sha256'] == world.profile_sha256()
    profile_b = world.publish('B')  # no operator edit, no restart
    result = adapter.call('knowledge.symbol', world.symbol('B'))
    assert result['status'] == 'ok', result
    assert result['source_revision'] == world.revisions['B']['core']
    scope = {row['repo_key']: row['revision'] for row in result['data']['indexed_scope']}
    assert scope['core'] == world.revisions['B']['core']
    assert result['data']['results'] and result['data']['gaps'] == []
    assert result['navigation_profile']['profile_sha256'] == profile_b
    assert result['navigation_profile']['published_at'] == 'generation-B'


def test_f2_the_operator_clone_without_b_does_not_decide(world):
    operator_core = json.loads(world.operator_catalog.read_text())['repositories']['core']['path']
    missing = subprocess.run(['git', '-C', operator_core, 'cat-file', '-e', world.revisions['B']['core'] + '^{commit}'],
                             capture_output=True)
    assert missing.returncode != 0  # the 2026-10-07 attempt-1 world
    adapter = world.adapter()
    profile_b = world.publish('B')
    result = adapter.call('knowledge.symbol', world.symbol('B'))
    assert result['status'] == 'ok' and result['source_revision'] == world.revisions['B']['core']
    assert result['navigation_profile']['profile_sha256'] == profile_b
    assert 'knowledge_unavailable' not in json.dumps(result)


# Shape a: the empty map refresh publishes today. Shape b: a non-empty map that differs
# from the operator's in its keys and in the manifest of a shared key.
F9_PUBLISHED_MAPS = {'empty': {},
                     'different': {'cap-a': 'docs/advanced.md', 'cap-published': 'docs/advanced.md'}}


@pytest.mark.parametrize('shape', sorted(F9_PUBLISHED_MAPS))
def test_f9_capabilities_are_the_operators_whatever_the_generation_publishes(world, shape):
    catalog = copy.deepcopy(world.catalogs['B'])
    for row in catalog['repositories'].values():
        row['capabilities'] = dict(F9_PUBLISHED_MAPS[shape])
    adapter = world.adapter()
    profile_b = world.publish('B', catalog=catalog)
    operator = json.loads(world.operator_catalog.read_text())['repositories']
    listed = adapter.call('knowledge.capabilities', {'repo_key': 'core'})
    assert listed['status'] == 'ok' and listed['navigation_profile']['profile_sha256'] == profile_b
    assert ({row['capability_id']: row['manifest_path'] for row in listed['data']['capabilities']}
            == operator['core']['capabilities'] == {'cap-a': 'docs/guide.md'})
    served = adapter.knowledge_provider.service._config['repositories']
    for key in ('core', 'ops'):
        assert served[key]['capabilities'] == operator[key]['capabilities']
    # The advertised schema is the operator's map too.
    schema = next(t for t in adapter.tools(include_gateway=False) if t['name'] == 'knowledge.context')['inputSchema']
    enums = [branch['then']['properties']['capability_id']['enum'] for branch in schema['allOf']
             if branch.get('if', {}).get('properties', {}).get('repo_key', {}).get('const') == 'core']
    assert enums == [['cap-a']]
    # The generation is still followed for its own fields.
    symbol = adapter.call('knowledge.symbol', world.symbol('B'))
    assert symbol['status'] == 'ok' and symbol['source_revision'] == world.revisions['B']['core']


def test_generation_values_come_from_the_profile_catalog(world):
    world.publish('B')
    provider = world.provider()
    result = provider.call('knowledge.symbol', world.symbol('B', 'ops'))
    assert result['status'] == 'ok' and result['navigation_profile']['published_at'] == 'generation-B'
    published = world.catalogs['B']
    served = provider.service._config
    operator = json.loads(world.operator_catalog.read_text())['repositories']
    for key in ('core', 'ops'):
        for field in ('ref', 'path'):
            assert served['repositories'][key][field] == published['repositories'][key][field]
        assert served['repositories'][key]['capabilities'] == operator[key]['capabilities']
    assert served['platforms'] == {'core': published['platforms']['core']}
    assert served['scip_indexes'] == {key: published['scip_indexes'][key] for key in ('core', 'ops')}
