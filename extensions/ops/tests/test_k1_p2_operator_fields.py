"""K1 P2: the operator's fields and served set survive following a generation.

F3 (operator fields survive), F4 (served set) and F8 (no widening).
"""
import copy
import json

import jsonschema
import pytest

from k1_feature_world import OPERATOR_TENANT, REFRESH_TENANT, World, write_private


@pytest.fixture
def world(tmp_path):
    return World(tmp_path / 'world')


def _retrieve(world, name='B'):
    return {'repo_key': 'core', 'query': 'portable context builder', 'target_revision': world.revisions[name]['core']}


def test_f3_operator_tenant_answers_and_the_published_tenant_is_refused(world):
    adapter = world.adapter()
    world.publish('B')
    result = adapter.call('knowledge.symbol', world.symbol('B'))
    assert result['status'] == 'ok' and result['source_revision'] == world.revisions['B']['core']
    service = adapter.knowledge_provider.service
    assert service.permits_tenant('core', OPERATOR_TENANT)
    assert not service.permits_tenant('core', REFRESH_TENANT)
    with pytest.raises(PermissionError):
        service.execute_for_tenant('symbol', world.symbol('B'), REFRESH_TENANT)
    # A runtime configured for the published tenant is refused the same way.
    runtime = json.loads(world.runtime.read_text())
    runtime['tenant_id'] = REFRESH_TENANT
    write_private(world.runtime, runtime)
    with pytest.raises(PermissionError):
        world.provider().call('knowledge.symbol', world.symbol('B'))


def test_f3_entry_symbols_artifacts_and_default_branch_are_the_operators(world):
    operator = json.loads(world.operator_catalog.read_text())
    world.publish('B')
    provider = world.provider()
    retrieved = provider.call('knowledge.retrieve', _retrieve(world))
    assert retrieved['status'] == 'ok' and retrieved['data']['declared_artifacts'] == 1
    assert retrieved['data']['results'][0]['path'] == 'docs/guide.md'
    served = provider.service._config['repositories']
    for key in ('core', 'ops'):
        for field in ('entry_symbols', 'artifacts', 'default_branch_ref', 'corpus_scope', 'tenant_ids'):
            assert served[key][field] == operator['repositories'][key][field]
    assert provider.service._config['navigation'] == operator['navigation']


def test_f4_a_published_only_repository_is_not_answerable(world):
    adapter = world.adapter()
    world.publish('B')
    schema = next(t for t in adapter.tools(include_gateway=False) if t['name'] == 'knowledge.symbol')['inputSchema']
    assert schema['properties']['repo_key']['enum'] == ['core', 'ops']
    refused = adapter.call('knowledge.symbol', world.symbol('B', 'extra'))
    assert refused['status'] == 'error' and refused.get('reason') == 'gateway_request_invalid_for_catalog'
    with pytest.raises(jsonschema.ValidationError):
        world.provider().call('knowledge.capabilities', {'repo_key': 'extra'})
    assert 'extra' not in world.provider().service._config['repositories']


def test_f8_published_tenants_repositories_and_corpus_scope_never_widen(world):
    widened = copy.deepcopy(world.catalogs['B'])
    for row in widened['repositories'].values():
        row['tenant_ids'] = [REFRESH_TENANT, 'published-only-tenant']
        row['corpus_scope'] = 'elsewhere'
    # A published platform anchored at a served repository that reaches the unserved one.
    widened['platforms']['ops'] = {**copy.deepcopy(widened['platforms']['core']),
                                   'sources': {key: widened['platforms'][anchor]['sources'][key]
                                               for anchor, key in (('core', 'ops'), ('extra', 'extra'))}}
    world.publish('B', catalog=widened)
    provider = world.provider()
    retrieved = provider.call('knowledge.retrieve', _retrieve(world))
    # The operator's corpus scope is searched, not the published one.
    assert retrieved['status'] == 'ok' and retrieved['data']['results'], retrieved
    assert retrieved['data']['repo_keys'] == ['core']
    service = provider.service
    assert sorted(service._config['repositories']) == ['core', 'ops']
    assert all(row['tenant_ids'] == [OPERATOR_TENANT] for row in service._config['repositories'].values())
    assert not service.permits_tenant('core', 'published-only-tenant')
    with pytest.raises(PermissionError):
        service.execute_for_tenant('capabilities', {'repo_key': 'core'}, 'published-only-tenant')
    # A published platform that reaches an unserved repository is not served.
    assert sorted(service._config['platforms']) == ['core']
    symbol = provider.call('knowledge.symbol', world.symbol('B', 'ops'))
    assert symbol['status'] == 'ok' and symbol['data']['platform_key'] == 'core'
