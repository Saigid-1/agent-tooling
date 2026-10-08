"""L3 acceptance for textual candidates alongside Serena references.

GREEN-IF: a uniquely resolved symbol with requested references also receives a
separate, bounded textual candidate pass through the public adapter. Duck calls,
comments/strings and zero-reference cases remain visible; disabled references and
unresolved declarations do not manufacture candidates. Incomplete scans return an
exact navigation.search_page continuation.
"""
import json
import subprocess

import pytest

from kp_agent_tooling._impl.service.agent_tooling import AgentTooling


def _git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


@pytest.fixture
def public_adapter(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'config', 'user.name', 'Test')
    _git(repo, 'config', 'user.email', 'test@example.invalid')
    (repo / 'connector.py').write_text(
        'class Connector:\n    def discover(self):\n        return 1\n')
    (repo / 'calls.py').write_text(
        'connector.discover()\nother.discover()\n# fake.discover()\ntext = "discover("\n')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'fixture')
    revision = _git(repo, 'rev-parse', 'HEAD')
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'schema_version': 'ops.navigation-profile.v1',
        'profile': 'fixture', 'repos': {'repo': {'path': str(repo), 'revision': revision}}}))
    registry = tmp_path / 'registry'
    registry.mkdir()
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'schema_version': 'ops.agent-tooling.v1',
        'repos': {'repo': {'path': str(repo), 'revision': revision}},
        'navigation_profile': str(profile), 'navigation_registry_path': str(registry),
        'serena': {'command': '/provider/serena', 'python': '/provider/python',
                   'runtime_home': str(tmp_path / 'runtime')}}))
    adapter = AgentTooling(config)
    monkeypatch.setattr('kp_agent_tooling._impl.service.agent_tooling.os.environ', {})
    citation = {'path': 'connector.py', 'revision': revision, 'start_line': 1,
                'end_line': 3, 'blob_sha': 'b' * 40}
    def resolved(*args, **kwargs):
        return {'status': 'ok', 'diagnostic': {'symbol_state': 'resolved',
            'reference_state': 'zero_results'}, 'report': {'status': 'ok',
            'citations': [citation], 'traversal': {'edges': [], 'completeness': 'not-established'}}}
    monkeypatch.setattr('kp_agent_tooling._impl.service.agent_tooling.SerenaNavigationProvider.inspect', resolved)
    return adapter, repo, revision


def test_public_inspect_adds_separate_deduplicated_textual_candidates(public_adapter):
    adapter, _, revision = public_adapter
    result = adapter.call('serena.inspect', {'repo_key': 'repo', 'path': 'connector.py',
        'symbol': 'Connector/discover', 'include_references': True, 'include_imports': False})
    textual = result['report']['textual_candidates']
    assert result['report']['traversal']['edges'] == []
    assert result['diagnostic']['reference_state'] == 'zero_results'
    assert textual['evidence_kind'] == 'textual'
    assert textual['source_revision'] == revision
    coordinates = [(row['path'], row['line']) for row in textual['results']]
    assert coordinates == [('calls.py', 1), ('calls.py', 2), ('calls.py', 3),
                           ('calls.py', 4), ('connector.py', 2)]
    assert len(coordinates) == len(set(coordinates))
    assert textual['limitations'] == ['syntactic matches may be comments, strings, declarations, or unrelated names; they are not references or proven callers']


def test_references_disabled_and_unresolved_symbols_do_not_add_candidates(public_adapter, monkeypatch):
    adapter, _, _ = public_adapter
    disabled = adapter.call('serena.inspect', {'repo_key': 'repo', 'path': 'connector.py',
        'symbol': 'Connector/discover', 'include_references': False, 'include_imports': False})
    assert 'textual_candidates' not in disabled['report']
    monkeypatch.setattr('kp_agent_tooling._impl.service.agent_tooling.SerenaNavigationProvider.inspect',
        lambda *a, **kw: {'status': 'ok', 'diagnostic': {'symbol_state': 'zero_results',
            'reference_state': 'requires_unique_symbol'}, 'report': {'status': 'no_results',
            'citations': [], 'traversal': {'edges': []}}})
    unresolved = adapter.call('serena.inspect', {'repo_key': 'repo', 'path': 'connector.py',
        'symbol': 'missing', 'include_references': True, 'include_imports': False})
    assert 'textual_candidates' not in unresolved['report']


def test_incomplete_textual_pass_returns_exact_public_continuation(public_adapter, monkeypatch):
    adapter, _, revision = public_adapter
    monkeypatch.setattr('kp_agent_tooling._impl.navigation_search_pages.MAX_FILES', 1)
    snapshot = adapter.call('navigation.snapshot', {'mode': 'capture'})['snapshot_id']
    result = adapter.call('serena.inspect', {'repo_key': 'repo', 'path': 'connector.py',
        'symbol': 'Connector/discover', 'include_references': True, 'include_imports': False,
        'target_revision': revision, 'snapshot_id': snapshot})
    textual = result['report']['textual_candidates']
    assert not textual['traversal_complete']
    next_call = textual['next_call']
    assert next_call['name'] == 'navigation.search_page'
    assert next_call['arguments']['target_revision'] == revision
    assert next_call['arguments']['snapshot_id'] == snapshot
    resumed = adapter.call(next_call['name'], next_call['arguments'])
    assert resumed['source_revision'] == revision


def test_textual_failure_preserves_actionable_search_reason(public_adapter):
    adapter, _, _ = public_adapter
    adapter.config.pop('navigation_registry_path')
    result = adapter.call('serena.inspect', {'repo_key': 'repo', 'path': 'connector.py',
        'symbol': 'Connector/discover', 'include_references': True, 'include_imports': False})
    textual = result['report']['textual_candidates']
    assert textual['status'] == 'error'
    assert textual['reason'] == 'navigation_registry_unconfigured'
    assert 'navigation_registry_path' in textual['message']


def test_textual_query_contract_normalizes_name_and_defines_whitespace(public_adapter):
    adapter, _, _ = public_adapter
    result = adapter.call('serena.inspect', {'repo_key': 'repo', 'path': 'connector.py',
        'symbol': 'Connector/discover', 'include_references': True,
        'include_imports': False})
    textual = result['report']['textual_candidates']
    assert textual['normalized_name'] == 'discover'
    assert textual['query'] == 'discover('
    assert textual['query_strategy'] == 'single_literal_superset'
    assert textual['whitespace_scope'] == 'zero characters between normalized name and opening parenthesis'
    assert textual['result_limit'] <= 20


def test_raised_textual_scan_failure_preserves_semantic_result(public_adapter, monkeypatch):
    adapter, _, _ = public_adapter
    def failed(*args, **kwargs):
        raise subprocess.TimeoutExpired(['git', 'cat-file'], 1)
    monkeypatch.setattr('kp_agent_tooling._impl.service.repository_coverage.call', failed)
    result = adapter.call('serena.inspect', {'repo_key': 'repo', 'path': 'connector.py',
        'symbol': 'Connector/discover', 'include_references': True,
        'include_imports': False})
    assert result['status'] == 'ok'
    assert result['report']['traversal']['edges'] == []
    assert result['diagnostic']['reference_state'] == 'zero_results'
    assert result['report']['textual_candidates']['status'] == 'error'
    assert result['report']['textual_candidates']['reason'] == 'textual_search_failed'


def test_textual_candidates_fit_inline_transport_boundary(public_adapter):
    adapter, _, _ = public_adapter
    result = adapter.call('serena.inspect', {'repo_key': 'repo', 'path': 'connector.py',
        'symbol': 'Connector/discover', 'include_references': True,
        'include_imports': False, 'response_mode': 'compact'})
    # Exercise the exact CLI/stdio response boundary: both call paths apply
    # DeliveryStore.deliver after AgentTooling.call.
    result['report']['traversal']['edges'] = [{'semantic': 'x' * (adapter.delivery.max_inline_bytes + 1_000)}]
    delivered = adapter.delivery.deliver(result)
    assert delivered['status'] == 'continued'
    calls = 0
    offset = 0
    fragments = []
    while offset is not None:
        page = adapter.call('delivery.read', {'token': delivered['token'], 'offset': offset})
        calls += 1
        fragments.append(page['text'])
        offset = page['next_offset']
    restored = json.loads(''.join(fragments))
    assert restored['status'] == 'ok'
    assert restored['report']['traversal']['edges'] == result['report']['traversal']['edges']
    assert restored['report']['textual_candidates'] == result['report']['textual_candidates']
    import math
    assert calls == math.ceil(delivered['total_bytes'] / adapter.delivery.max_page_bytes)
