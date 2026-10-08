"""First assistant slice: stable identity, isolated portable state and policy wiring."""
import asyncio
from dataclasses import replace
import json
import os
from datetime import timedelta
from pathlib import Path
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from kp_agent_tooling._impl.service.assistant_memory_policy import load_policy
from kp_agent_tooling._impl.service.desk_identity import binding_key
from kp_agent_tooling._impl.service.desk_memory_runtime import admit, components, initialize
from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue
from kp_agent_tooling._impl.service.episodic_queue import _digest
from kp_agent_tooling._impl.service.episodic_summarizer import OpenRouterEpisodeSummarizer
from kp_agent_tooling._impl.service.summary_contract import full_request
from test_episodic_summarizer import configured, response
from test_portable_desk_memory import config as desk_config


def _private(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def _desk(tmp_path, name):
    home = tmp_path / name
    home.mkdir(mode=0o700)
    state = home / 'state'
    state.mkdir(mode=0o700)
    # Both fixtures deliberately use the same tenant and binding coordinates.
    # Distinct results therefore prove physical store isolation, not tenant filtering.
    key = binding_key(tenant_id='shared-workspace', role='assistant', repo_key='personal')
    catalog = _private(home / 'catalog.json', {
        'schema_version': 'ops.imported-desk-catalog.v1', 'approval_ref': 'operator:fixture',
        'bindings': [dict(binding_key=key, tenant_id='shared-workspace', role='assistant', repo_key='personal',
            desk_label='Workspace Assistant', source='operator:fixture', memory_write_allowed=True)]})
    policy = _private(home / 'policy.json', {'schema_version': 'ops.assistant-summary-policy.v1',
        'focus_questions': ['Which observed outcome supports the decision?']})

    def session(identifier, instance):
        return _private(home / (identifier + '.json'), {
            'schema_version': 'ops.assistant-memory.local.v1', 'state_root': str(state),
            'catalog_path': str(catalog), 'workspace_root': str(home),
            'provider_instance': instance, 'provider_session_id': identifier,
            'assistant_policy_path': str(policy)})

    first = session('session-a', 'codex')
    initialize(first)
    assert admit(first, desk_id=key, provider_id='provider-a', model_id='model-a')['binding_key'] == key
    return home, state, key, session


def _call(config, tool, arguments):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': str(root / 'packages/tooling/src') + ':' + str(root)}
    result = subprocess.run([sys.executable, '-m', 'kp_agent_tooling.memory_cli',
        '--config', str(config), 'call', '--tool', tool, '--arguments', json.dumps(arguments)],
        capture_output=True, text=True, env=env, check=False)
    return result.returncode, json.loads(result.stdout)


def test_public_cli_and_mcp_keep_assistant_history_isolated_across_provider_change(tmp_path):
    home, state, key, session = _desk(tmp_path, 'assistant')
    other_home, other_state, _, other_session = _desk(tmp_path, 'unrelated')
    first = session('session-a', 'codex')
    store = components(first)[3]
    original = store.capture('session-a', source_ref='visible:decision', events=[
        {'event_id': 'choice', 'role': 'user', 'text': 'Use option B for the pilot.'}])['episode_id']
    correction = store.capture('session-a', source_ref='visible:correction', events=[
        {'event_id': 'correction', 'role': 'user',
         'text': 'Correction: option B failed the pilot; use option A next time.'}])['episode_id']
    foreign = components(other_session('session-a', 'codex'))[3]
    foreign.capture('session-a', source_ref='visible:private', events=[
        {'event_id': 'private', 'role': 'user', 'text': 'Private unrelated marker.'}])
    successor = session('session-b', 'claude')
    assert admit(successor, desk_id=key, provider_id='provider-b', model_id='model-b')['binding_key'] == key
    assert components(successor)[2].resolve('session-b', components(successor)[1]).provider_id == 'provider-b'
    index = EpisodicSearchIndex(state / 'episode-search.sqlite3', episode_store=store)
    index.rebuild()
    other_index = EpisodicSearchIndex(other_state / 'episode-search.sqlite3', episode_store=foreign)
    other_index.rebuild()
    queue = ConsolidationQueue(state / 'queue.sqlite3', store=store)
    queue.initialize()
    queue.enqueue('session-a', episode_ids=[original], reason='manual')
    assert not (other_state / 'queue.sqlite3').exists()

    code, listed = _call(successor, 'memory.list', {'kind': 'episodes'})
    assert code == 0 and {row['episode_id'] for row in listed['entries']} == {original, correction}
    assert _call(successor, 'memory.search', {'query': 'Private unrelated'})[1]['results'] == []
    assert _call(successor, 'memory.search', {'query': 'Correction'})[1]['results'][0]['episode_id'] == correction
    args = {'episode_ids': [original, correction], 'items': [
        {'kind': 'decision', 'text': 'Use option B for the pilot.', 'citations': [
            {'episode_id': original, 'event_id': 'choice', 'start': 0, 'end': 27,
             'quote': 'Use option B for the pilot.'}]},
        {'kind': 'observation', 'text': 'Correction: option B failed the pilot; use option A next time.',
         'citations': [{'episode_id': correction, 'event_id': 'correction', 'start': 0,
                        'end': 61, 'quote': 'Correction: option B failed the pilot; use option A next time.'}]}],
        'unresolved_questions': ['Was option A tested after this correction?']}
    # Derive exact character bounds from source, not a paraphrase.
    for item in args['items']:
        item['citations'][0]['end'] = len(item['citations'][0]['quote'])
    code, proposed = _call(successor, 'memory.propose', args)
    assert code == 0 and proposed['handoff']['items'][1]['evidence_check']['semantic_entailment'] == 'not-assessed'

    async def mcp_roundtrip():
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, 'PYTHONPATH': str(root / 'packages/tooling/src') + ':' + str(root)}
        params = StdioServerParameters(command=sys.executable,
            args=['-m', 'kp_agent_tooling.memory_cli', '--config', str(successor), 'serve'], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=20)) as client:
                await client.initialize()
                history = await client.call_tool('memory.handoff', {'capsule_id': proposed['capsule_id']})
                assert not history.isError and len(history.structuredContent['items']) == 2
                event = await client.call_tool('memory.read_event',
                    {'episode_id': correction, 'event_id': 'correction'})
                assert event.structuredContent['text'].startswith('Correction:')
                denied = await client.call_tool('memory.search', {'query': 'Private unrelated'})
                assert denied.structuredContent['results'] == []
    asyncio.run(mcp_roundtrip())
    assert (state / 'sessions.sqlite3').exists() and (state / 'episodes.sqlite3').exists()
    assert (state / 'episode-search.sqlite3').exists() and (state / 'queue.sqlite3').exists()
    assert (other_state / 'episodes.sqlite3').exists()


def test_assistant_policy_reaches_trusted_system_request_without_external_call(tmp_path):
    home, state, key, session = _desk(tmp_path, 'assistant')
    policy = load_policy(home / 'policy.json')
    wire = []
    base = configured(lambda _key, body, _timeout: (wire.append(json.loads(body)) or response()))
    summarizer = OpenRouterEpisodeSummarizer(api_key='fixture', budget=base.budget,
        transport=lambda _key, body, _timeout: (wire.append(json.loads(body)) or response()),
        trusted_policy=policy)
    summarizer.budget = replace(base.budget, system_tokens=4096)
    packet = full_request(binding=key, records=[{'episode_id': 'episode',
        'events': [{'event_id': 'source', 'role': 'user', 'text': 'Use option A after the failed pilot.'}]}],
        input_bytes=60000, handoff_bytes=12000)
    summarizer(packet)
    system, user = wire[0]['messages']
    assert 'structural soundness' in system['content']
    assert 'Which observed outcome supports the decision?' in system['content']
    assert 'infer competence from questions' in system['content']
    assert 'focus_questions' not in user['content']
    assert summarizer.last_receipt['prompt_version'].endswith('+assistant-policy-v1')
    assert 'option A' not in str(summarizer.last_receipt)


def test_public_queue_work_once_passes_policy_to_real_summarizer(tmp_path, monkeypatch, capsys):
    import kp_agent_tooling.queue_cli as cli
    home, state, _, session = _desk(tmp_path, 'assistant')
    config = session('session-a', 'codex')
    store = components(config)[3]
    episode = store.capture('session-a', source_ref='visible:decision', events=[
        {'event_id': 'source', 'role': 'user', 'text': 'The pilot used option B.'}])['episode_id']
    queue = state / 'queue.sqlite3'
    prefix = ['queue', '--config', str(config), '--queue', str(queue)]
    monkeypatch.setattr(sys, 'argv', prefix + ['initialize'])
    assert cli.main() == 0
    capsys.readouterr()
    monkeypatch.setattr(sys, 'argv', prefix + ['enqueue', '--episode', episode])
    assert cli.main() == 0
    capsys.readouterr()
    approved = _private(home / 'approved.json', [_digest(store.citation_records('session-a', [episode])[episode]['events'])])
    profile = _private(home / 'profile.json', {'raw_model_entry': {'supported_parameters': []}})
    base = configured(None).budget
    budget = replace(base, system_tokens=4096, input_tokens=base.input_tokens - 2048)
    monkeypatch.setattr(cli, 'load_memory_budget_receipt', lambda _: budget)
    monkeypatch.setenv('ASSISTANT_TEST_KEY', 'fixture-only')
    wire = []
    real_summarizer = OpenRouterEpisodeSummarizer

    def local_summarizer(**kwargs):
        return real_summarizer(**kwargs,
            transport=lambda _key, body, _timeout: (wire.append(json.loads(body)) or response()))

    monkeypatch.setattr(cli, 'OpenRouterEpisodeSummarizer', local_summarizer)
    monkeypatch.setattr(sys, 'argv', prefix + ['work-once', '--profile', str(profile),
        '--approved-digests', str(approved), '--key-env', 'ASSISTANT_TEST_KEY'])
    assert cli.main() == 0
    receipt = json.loads(capsys.readouterr().out)['attempts'][0]['receipt']
    assert 'structural soundness' in wire[0]['messages'][0]['content']
    assert receipt['prompt_version'].endswith('+assistant-policy-v1')
    assert len(receipt['prompt_sha256']) == 64


def test_assistant_configuration_fails_closed_on_invalid_policy_or_catalog(tmp_path):
    home, state, _, session = _desk(tmp_path, 'assistant')
    path = session('session-b', 'other')
    _private(home / 'policy.json', {'schema_version': 'ops.assistant-summary-policy.v1',
        'focus_questions': ['']})
    with pytest.raises(ValueError, match='policy'):
        components(path)
    _private(home / 'policy.json', {'schema_version': 'ops.assistant-summary-policy.v1',
        'focus_questions': []})
    global_home = tmp_path / 'global'
    global_home.mkdir()
    global_config = desk_config(global_home, session='global-session', instance='global')
    initialize(global_config)
    assistant_config = json.loads(path.read_text())
    assistant_config['state_root'] = str(global_home / 'state')
    _private(path, assistant_config)
    with pytest.raises(ValueError, match='unowned data'):
        components(path)
    _private(path, {**assistant_config, 'state_root': str(state)})
    legacy = desk_config(home, session='legacy-session', instance='legacy')
    body = json.loads(legacy.read_text())
    body['state_root'] = str(state)
    _private(legacy, body)
    with pytest.raises(ValueError, match='assistant state'):
        components(legacy)
