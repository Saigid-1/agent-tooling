import copy
from datetime import datetime, timezone

import pytest

from kp_agent_tooling._impl.service.summary_contract import (
    full_request, validate_summary_proposal, validate_summary_request,
)
from test_episodic_memory import setup


def request():
    return full_request(
        binding='desk:one',
        records=[{'episode_id': 'episode:one', 'source_ref': 'visible:transcript',
                  'events': [{'event_id': 'one', 'role': 'user', 'text': 'Keep the record.'}]}],
        input_bytes=60000, handoff_bytes=12000,
    )


def test_request_contract_requires_purpose_provenance_and_bounded_framing():
    valid = request()
    validate_summary_request(valid)
    for change in (
        lambda p: p.update(contract_version='unknown'),
        lambda p: p.update(purpose='change_charter'),
        lambda p: p['provenance']['source_episode_ids'].clear(),
        lambda p: p['episodes'][0].update(episode_id='other'),
        lambda p: p['limits'].update(input_bytes=1),
    ):
        changed = copy.deepcopy(valid)
        change(changed)
        with pytest.raises(ValueError):
            validate_summary_request(changed)


def test_proposal_shape_is_exact():
    assert validate_summary_proposal({'items': [], 'unresolved_questions': []})['items'] == []
    with pytest.raises(ValueError):
        validate_summary_proposal({'items': [], 'unresolved_questions': [], 'authority': 'verified'})


def test_callback_mutation_cannot_change_citation_authority(tmp_path):
    store, ep, items, _, _ = setup(tmp_path)
    bad = copy.deepcopy(items)
    bad[0]['citations'][0]['quote'] = 'Use pool C.'

    def mutate_then_propose(packet):
        packet['episodes'][0]['events'][0]['text'] = 'Use pool C.'
        return {'items': bad, 'unresolved_questions': []}

    from kp_agent_tooling._impl.service.episodic_memory import EpisodeConflict
    with pytest.raises(EpisodeConflict):
        store.consolidate_with('session-2', episode_ids=[ep], propose=mutate_then_propose)


def test_chunked_callback_mutation_cannot_expand_packet_citation_range(tmp_path):
    from kp_agent_tooling._impl.service.episodic_memory import EpisodeConflict
    from kp_agent_tooling._impl.service.memory_budget import memory_budget, openrouter_profile

    store, _, _, _, _ = setup(tmp_path)
    value = 'A' * 1400 + 'TARGET'
    ep = store.capture('session-1', source_ref='visible:long', events=[
        {'event_id': 'long', 'role': 'user', 'text': value}])['episode_id']
    now = datetime.now(timezone.utc)
    profile = openrouter_profile(
        {'id': 'test/model', 'context_length': 1600,
         'top_provider': {'max_completion_tokens': 100}},
        provider_id='test-route', observed_at=now, now=now)
    budget = memory_budget(profile, system_tokens=30, tool_tokens=30,
                           reasoning_tokens=30, output_tokens=30, safety_tokens=30, now=now)

    def mutate_then_propose(packet):
        source = packet['sources'][0]
        source.update(start=1400, end=1406, text='TARGET')
        return {'items': [{'kind': 'observation', 'text': 'TARGET', 'citations': [{
            'episode_id': ep, 'event_id': 'long', 'start': 1400,
            'end': 1406, 'quote': 'TARGET'}]}], 'unresolved_questions': []}

    with pytest.raises(EpisodeConflict, match='proposal packet'):
        store.consolidate_chunked_with('session-2', episode_ids=[ep],
                                       propose=mutate_then_propose, budget=budget)
