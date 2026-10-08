"""Explicit cross-desk reads preserve admission, attribution, and bound writes."""

from dataclasses import replace

import pytest
from jsonschema import ValidationError

from test_portable_desk_memory import episode_store
from kp_agent_tooling._impl.service.episodic_memory import EpisodeUnavailable, EpisodeUnknownBinding
from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools, tool_failure


def test_explicit_same_tenant_reads_and_bound_writes(tmp_path):
    store = episode_store(tmp_path)
    source_binding = store._binding('session-1')
    other_binding = store._binding('session-3')
    episode = store.capture('session-1', source_ref='visible:1', events=[
        {'event_id': '1', 'role': 'user', 'text': 'Observed evidence.'}])['episode_id']
    reader = EpisodicMemoryTools(store, 'session-3')
    choices = reader.call('memory.bindings', {})
    assert source_binding in [row['binding_key'] for row in choices['bindings']]
    assert choices['total'] == 2

    own = reader.call('memory.status', {})['episodes']
    assert own['binding_key'] == other_binding
    assert own['result_status'] == 'zero_results' and own['total'] == 0
    selected = reader.call('memory.status', {'binding_key': source_binding})['episodes']
    assert selected['total'] == 1 and selected['entries'][0]['episode_id'] == episode
    assert selected['entries'][0]['read_attribution']['source_sessions'] == ['session-1']
    assert selected['read_attribution']['read_scope'] == 'other_desk'
    assert selected['read_attribution']['source_observed_at'] == 'unknown'
    assert "another desk's claim" in selected['read_attribution']['authority']
    source = reader.call('memory.read_event', {'binding_key': source_binding,
        'episode_id': episode, 'event_id': '1'})
    assert source['text'] == 'Observed evidence.'
    assert source['read_attribution']['source_sessions'] == ['session-1']
    assert reader.call('memory.episode_directory', {'binding_key': source_binding,
        'episode_id': episode})['read_attribution']['read_scope'] == 'other_desk'

    with pytest.raises(ValidationError):
        reader.call('memory.propose', {'binding_key': source_binding,
            'episode_ids': [episode], 'items': [], 'unresolved_questions': []})
    with pytest.raises(EpisodeUnavailable):
        reader.call('memory.propose', {'episode_ids': [episode],
            'items': [], 'unresolved_questions': []})
    written = store.capture('session-3', source_ref='visible:2', events=[
        {'event_id': '2', 'role': 'user', 'text': 'Own desk.'}])
    assert written['binding_key'] == other_binding


def test_registered_empty_vs_invalid_and_other_tenant(tmp_path):
    store = episode_store(tmp_path)
    reader = EpisodicMemoryTools(store, 'session-3')
    same_tenant_empty = store._binding('session-1')
    result = reader.call('memory.list', {'kind': 'episodes', 'binding_key': same_tenant_empty})
    assert result['result_status'] == 'zero_results' and result['binding_key'] == same_tenant_empty
    with pytest.raises(EpisodeUnknownBinding, match='not registered'):
        reader.call('memory.list', {'kind': 'episodes', 'binding_key': 'binding:' + '0'*64})
    assert tool_failure('memory.list', EpisodeUnknownBinding('private')) == {
        'status': 'error', 'category': 'unknown_binding',
        'guidance': 'Use memory.bindings to select a registered binding in this tenant.'}

    original = store.registry
    other_tenant = replace(original.list_bindings()[0],
        binding_key='binding:' + '1'*64, tenant_id='another-tenant')
    class WithForeignBinding:
        def resolve(self, **coordinates):
            return original.resolve(**coordinates)
        def list_bindings(self):
            return (*original.list_bindings(), other_tenant)
    store.registry = WithForeignBinding()
    assert other_tenant.binding_key not in [row['binding_key']
        for row in reader.call('memory.bindings', {})['bindings']]
    with pytest.raises(EpisodeUnavailable, match='tenant'):
        reader.call('memory.status', {'binding_key': other_tenant.binding_key})


def test_other_desk_capsule_recovery_stays_explicit(tmp_path):
    store = episode_store(tmp_path)
    binding = store._binding('session-1')
    quote = 'Observed evidence.'
    episode = store.capture('session-1', source_ref='visible:1', events=[
        {'event_id': '1', 'role': 'user', 'text': quote}])['episode_id']
    capsule = store.consolidate('session-1', episode_ids=[episode],
        items=[{'kind': 'observation', 'text': 'An observation was recorded.',
                'citations': [{'episode_id': episode, 'event_id': '1', 'start': 0,
                               'end': len(quote), 'quote': quote}]}],
        unresolved_questions=[])['capsule_id']
    reader = EpisodicMemoryTools(store, 'session-3')
    selected = {'capsule_id': capsule, 'binding_key': binding}
    handoff = reader.call('memory.handoff', selected)
    assert handoff['read_attribution']['source_sessions'] == ['session-1']
    assert handoff['read_attribution']['source_actor'] == 'unknown'
    directory = reader.call('memory.evidence_directory', selected)
    assert directory['read_attribution']['binding_key'] == binding
    resume = reader.call('memory.resume', selected)
    assert resume['read_attribution']['read_scope'] == 'other_desk'
    assert resume['source_recovery']['arguments']['binding_key'] == binding
    assert resume['handoff_recovery']['arguments']['binding_key'] == binding
    page = reader.call('memory.handoff_page', selected)
    assert page['entries'][0]['kind'] == 'item'
    assert page['read_attribution']['source_observed_at'] == 'unknown'
