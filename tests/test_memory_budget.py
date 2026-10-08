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


def test_profile_route_narrows_context_and_keeps_capability_unknown():
    p = profile(route={"provider_id": "route-a", "context_length": 50,
                       "max_completion_tokens": 12, "retention": "zero"})
    assert (p.context_tokens, p.max_output_tokens, p.tool_calling) == (50, 12, True)
    assert p.retention == "zero" and p.native_compaction == "unknown"
    assert len(p.metadata_sha256) == 64
    assert profile().retention == "unknown"


@pytest.mark.parametrize("entry", [{"id": "x"}, {"id": "x", "context_length": False},
                                   {"id": "x", "context_length": -1}])
def test_invalid_or_missing_metadata_rejected(entry):
    with pytest.raises(ValueError):
        openrouter_profile(entry, provider_id="route-a", observed_at=NOW, now=NOW)


def test_stale_or_mismatched_metadata_rejected():
    with pytest.raises(ValueError, match="stale"):
        openrouter_profile(MODEL, provider_id="route-a", observed_at=NOW,
                           now=NOW + timedelta(days=2))
    with pytest.raises(ValueError, match="identity"):
        profile(route={"provider_id": "route-b", "context_length": 20})


def test_reserves_and_lossless_unicode_slices():
    p = profile()
    budget = memory_budget(p, system_tokens=10, tool_tokens=10,
                           reasoning_tokens=10, output_tokens=10, safety_tokens=10, now=NOW)
    assert budget.input_tokens == 30
    value = "α😀é" * 30
    parts = chunk_events([{"event_id": "e", "role": "user", "text": value}],
                         episode_id="episode:1", budget=budget)
    assert len(parts) > 1
    assert "".join(s.text for s in parts) == value
    assert all(s.text == value[s.start:s.end] and s.estimated_tokens <= 30
               and s.counter == "utf8-byte-estimate" for s in parts)
    assert [(a.end, b.start) for a, b in zip(parts, parts[1:])] == [
        (part.end, part.end) for part in parts[:-1]]


def test_reserve_exhaustion_and_output_limit():
    with pytest.raises(ValueError, match="output reserve"):
        memory_budget(profile(), system_tokens=1, tool_tokens=1, reasoning_tokens=1,
                      output_tokens=21, safety_tokens=1, now=NOW)
    with pytest.raises(ValueError, match="no source context"):
        memory_budget(profile(), system_tokens=50, tool_tokens=10, reasoning_tokens=10,
                      output_tokens=10, safety_tokens=10, now=NOW)


def test_oversized_character_is_explicit_failure():
    budget = memory_budget(profile(), system_tokens=20, tool_tokens=20,
                           reasoning_tokens=20, output_tokens=10, safety_tokens=9, now=NOW)
    with pytest.raises(ValueError, match="one character"):
        chunk_events([{"event_id": "e", "role": "tool", "text": "😀"}],
                     episode_id="ep", budget=budget)


def test_chunked_consolidation_preserves_original_coordinates(tmp_path):
    store, _, _, _, _ = episode_setup(tmp_path)
    events = [{'event_id': 'long', 'role': 'user', 'text': 'abc😀' * 150}]
    episode_id = store.capture('session-1', source_ref='long-source', events=events)['episode_id']
    large = dict(MODEL, context_length=2400)
    large['top_provider'] = {'context_length': 2400, 'max_completion_tokens': 50}
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


def test_chunked_consolidation_rejects_expired_profile_before_proposal(tmp_path):
    store, episode_id, _, _, _ = episode_setup(tmp_path)
    budget = memory_budget(profile(), system_tokens=10, tool_tokens=10,
                           reasoning_tokens=10, output_tokens=10, safety_tokens=10, now=NOW)
    expired = replace(budget, profile=replace(budget.profile,
                      observed_at=NOW - timedelta(days=2)))
    calls = []
    with pytest.raises(ValueError, match='stale'):
        store.consolidate_chunked_with('session-2', episode_ids=[episode_id],
                                       propose=lambda packet: calls.append(packet), budget=expired)
    assert calls == []


def test_adjacent_events_share_packets_and_max_packets_preflights(tmp_path):
    store, _, _, _, _ = episode_setup(tmp_path)
    events = [{'event_id': f'e{i}', 'role': 'user', 'text': f'correction {i}'}
              for i in range(4)]
    episode_id = store.capture('session-1', source_ref='adjacent', events=events)['episode_id']
    large = dict(MODEL, context_length=2400)
    large['top_provider'] = {'context_length': 2400, 'max_completion_tokens': 50}
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


def test_receipt_revalidates_raw_metadata_and_budget():
    p = profile()
    b = memory_budget(p, system_tokens=10, tool_tokens=10, reasoning_tokens=10,
                      output_tokens=10, safety_tokens=10, now=NOW)
    saved_profile = asdict(p)
    saved_profile['observed_at'] = NOW.isoformat()
    receipt = {'schema_version': 'ops.memory-model-profile.v1',
               'raw_model_entry': MODEL, 'selected_route': None,
               'profile': saved_profile,
               'budget': {name: getattr(b, name) for name in (
                   'system_tokens', 'tool_tokens', 'reasoning_tokens',
                   'output_tokens', 'safety_tokens', 'input_tokens')}}
    assert load_memory_budget_receipt(receipt, now=NOW) == b
    changed = {**receipt, 'raw_model_entry': dict(MODEL, context_length=101)}
    with pytest.raises(ValueError, match='mismatch'):
        load_memory_budget_receipt(changed, now=NOW)
    changed = {**receipt, 'budget': dict(receipt['budget'], input_tokens=999)}
    with pytest.raises(ValueError, match='budget mismatch'):
        load_memory_budget_receipt(changed, now=NOW)
    with pytest.raises(ValueError, match='stale'):
        load_memory_budget_receipt(receipt, now=NOW + timedelta(days=2))


def test_cli_writes_reloadable_receipt_without_network(tmp_path):
    metadata = tmp_path / 'models.json'
    output = tmp_path / 'profile.json'
    metadata.write_text(json.dumps({'data': [dict(MODEL, pricing={
        'prompt': '0.000001', 'completion': '0.000002'})]}))
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'memory_model_profile.py'
    subprocess.run([sys.executable, str(script), '--metadata-json', str(metadata),
                    '--model-id', 'example/model', '--provider-id', 'route-a',
                    '--observed-at', NOW.isoformat(), '--output', str(output),
                    '--system', '10', '--tool', '10', '--reasoning', '10',
                    '--output-tokens', '10', '--safety', '10'], check=True)
    receipt = json.loads(output.read_text())
    assert load_memory_budget_receipt(receipt, now=NOW).input_tokens == 30


def test_cross_packet_citation_cannot_claim_unseen_source(tmp_path):
    store, _, _, _, _ = episode_setup(tmp_path)
    value = 'a' * 4500 + 'TARGET'
    episode_id = store.capture('session-1', source_ref='unseen-source', events=[
        {'event_id': 'long', 'role': 'user', 'text': value}])['episode_id']
    large = dict(MODEL, context_length=2400)
    large['top_provider'] = {'context_length': 2400, 'max_completion_tokens': 50}
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


def test_packet_framing_refuses_before_model_call(tmp_path):
    store, episode_id, _, _, _ = episode_setup(tmp_path)
    budget = memory_budget(profile(), system_tokens=10, tool_tokens=10,
        reasoning_tokens=10, output_tokens=10, safety_tokens=10, now=NOW)
    calls = []
    with pytest.raises(ValueError, match='framing exceeds budget'):
        store.consolidate_chunked_with('session-2', episode_ids=[episode_id],
            propose=lambda packet: calls.append(packet), budget=budget)
    assert calls == []
