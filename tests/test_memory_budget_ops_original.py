# S5 port (subset) of OPS at the extraction commit tests/test_memory_budget.py: only the tests whose OPS form is not
# already covered by tests/test_memory_budget.py. See tests/EXTRACTED-TEST-LEDGER.md.
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

from kp_agent_tooling._impl.service.episodic_memory import EpisodeConflict
from kp_agent_tooling._impl.service.memory_budget import (chunk_events, load_memory_budget_receipt,
                                          memory_budget, openrouter_profile)
from test_episodic_memory import setup as episode_setup


NOW = datetime.now(timezone.utc)
MODEL = {"id": "example/model", "context_length": 100,
         "top_provider": {"context_length": 80, "max_completion_tokens": 20},
         "supported_parameters": ["tools", "max_tokens"]}


def profile(**kwargs):
    return openrouter_profile(MODEL, provider_id="route-a", observed_at=NOW,
                              now=NOW, **kwargs)


@pytest.mark.xfail(strict=True, reason="S5 divergence: consolidate_chunked_with raises 'packet framing exceeds budget' at context_length=1000 (also red at OPS at the extraction commit; tests/test_memory_budget.py uses context_length=2400)")
def test_chunked_consolidation_preserves_original_coordinates(tmp_path):
    store, _, _, _, _ = episode_setup(tmp_path)
    events = [{'event_id': 'long', 'role': 'user', 'text': 'abc😀' * 150}]
    episode_id = store.capture('session-1', source_ref='long-source', events=events)['episode_id']
    large = dict(MODEL, context_length=1000)
    large['top_provider'] = {'context_length': 1000, 'max_completion_tokens': 50}
    p = openrouter_profile(large, provider_id='route-a', observed_at=NOW, now=NOW)
    budget = memory_budget(p, system_tokens=15, tool_tokens=10,
                           reasoning_tokens=10, output_tokens=10, safety_tokens=10, now=NOW)
    seen = []

    def propose(packet):
        seen.extend(packet['sources'])
        source = packet['sources'][0]
        quote = source['text'][:3]
        return {'items': [{'kind': 'observation', 'text': 'Source span retained',
                           'citations': [{'episode_id': source['episode_id'],
                                          'event_id': source['event_id'],
                                          'start': source['start'],
                                          'end': source['start'] + len(quote),
                                          'quote': quote}]}],
                'unresolved_questions': []}

    result = store.consolidate_chunked_with('session-2', episode_ids=[episode_id],
                                            propose=propose, budget=budget)
    assert result['source_span_count'] == len(seen) > len(events)
    assert result['packet_count'] <= len(seen)
    assert any('Cross-packet corrections' in q for q in result['handoff']['unresolved_questions'])
    assert all(source['text'] == next(e['text'] for e in events
                                     if e['event_id'] == source['event_id'])[
                                         source['start']:source['end']] for source in seen)


@pytest.mark.xfail(strict=True, reason='S5 divergence: packet_count < 4 fails (4 < 4) at context_length=1500 (also red at OPS at the extraction commit; tests/test_memory_budget.py uses context_length=2400)')
def test_adjacent_events_share_packets_and_max_packets_preflights(tmp_path):
    store, _, _, _, _ = episode_setup(tmp_path)
    events = [{'event_id': f'e{i}', 'role': 'user', 'text': f'correction {i}'}
              for i in range(4)]
    episode_id = store.capture('session-1', source_ref='adjacent', events=events)['episode_id']
    large = dict(MODEL, context_length=1500)
    large['top_provider'] = {'context_length': 1500, 'max_completion_tokens': 50}
    p = openrouter_profile(large, provider_id='route-a', observed_at=NOW, now=NOW)
    budget = memory_budget(p, system_tokens=30, tool_tokens=30,
                           reasoning_tokens=30, output_tokens=30, safety_tokens=30, now=NOW)
    seen = []

    def propose(packet):
        seen.append(packet)
        return {'items': [], 'unresolved_questions': []}

    result = store.consolidate_chunked_with('session-2', episode_ids=[episode_id],
                                            propose=propose, budget=budget)
    assert result['source_span_count'] == 4
    assert result['packet_count'] < 4
    assert [s['event_id'] for packet in seen for s in packet['sources']] == [e['event_id'] for e in events]

    long_id = store.capture('session-1', source_ref='longer', events=[
        {'event_id': 'long', 'role': 'user', 'text': 'x' * 5000}])['episode_id']
    seen.clear()
    with pytest.raises(ValueError, match='max_packets'):
        store.consolidate_chunked_with('session-2', episode_ids=[long_id],
                                       propose=propose, budget=budget, max_packets=1)
    assert seen == []


@pytest.mark.xfail(strict=True, reason='S5 divergence: framing-budget ValueError raised instead of EpisodeConflict at context_length=1200 (also red at OPS at the extraction commit; tests/test_memory_budget.py uses context_length=2400)')
def test_cross_packet_citation_cannot_claim_unseen_source(tmp_path):
    store, _, _, _, _ = episode_setup(tmp_path)
    value = 'a' * 4500 + 'TARGET'
    episode_id = store.capture('session-1', source_ref='unseen-source', events=[
        {'event_id': 'long', 'role': 'user', 'text': value}])['episode_id']
    large = dict(MODEL, context_length=1200)
    large['top_provider'] = {'context_length': 1200, 'max_completion_tokens': 50}
    p = openrouter_profile(large, provider_id='route-a', observed_at=NOW, now=NOW)
    budget = memory_budget(p, system_tokens=30, tool_tokens=30,
                           reasoning_tokens=30, output_tokens=30, safety_tokens=30, now=NOW)
    calls = []

    def propose(packet):
        calls.append(packet)
        return {'items': [{'kind': 'observation', 'text': 'False claim', 'citations': [{
            'episode_id': episode_id, 'event_id': 'long', 'start': len(value)-6,
            'end': len(value), 'quote': 'TARGET'}]}], 'unresolved_questions': []}

    with pytest.raises(EpisodeConflict, match='proposal packet'):
        store.consolidate_chunked_with('session-2', episode_ids=[episode_id],
                                       propose=propose, budget=budget)
    assert len(calls) == 1
