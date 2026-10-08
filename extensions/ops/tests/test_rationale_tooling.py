"""CLI/MCP share AgentTooling; no gateway, graph, or inferred source admission."""
import json
import pytest
import kp_agent_tooling._impl.service.agent_tooling as agent_tooling_module
from kp_agent_tooling._impl.service.agent_tooling import AgentTooling
from test_commit_rationale import _compound_fixture, git, commit, entry


def setup_tool(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init')
    body = 'Snooze is capped because reminders must return.'
    sha = commit(repo, 'docs/snooze.md', 'fixture only\n', 'Fixture policy', body)
    manifest = tmp_path / 'rationale.json'
    manifest.write_text(json.dumps({'schema_version': 'ops.commit-rationale.v1',
                                   'entries': [entry(sha, body)]}))
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'schema_version': 'ops.agent-tooling.v1',
        'repos': {'ops': {'path': str(repo), 'revision': sha}},
        'rationale_manifest': str(manifest), 'enabled_tools': ['knowledge.rationale']}))
    return AgentTooling(config), manifest, sha


def test_rationale_first_call_and_revocation(tmp_path, monkeypatch):
    tool, manifest, sha = setup_tool(tmp_path)
    monkeypatch.setattr(tool, 'rpc', lambda *args: pytest.fail('must not use gateway'))
    args = {'repo_key': 'ops', 'query': 'Why is snooze capped?', 'target_revision': sha}
    assert [t['name'] for t in tool.tools()] == ['knowledge.rationale']
    result = tool.call('knowledge.rationale', args)
    assert result['status'] == 'evidence'
    assert result['results'][0]['commit'] == sha
    assert result['runtime_execution'] == 'not-assessed'
    manifest.write_text(json.dumps({'schema_version': 'ops.commit-rationale.v1', 'entries': []}))
    assert tool.call('knowledge.rationale', args)['status'] == 'missing'


def test_unconfigured_invalid_unavailable_are_not_missing(tmp_path):
    tool, manifest, sha = setup_tool(tmp_path)
    args = {'repo_key': 'ops', 'query': 'Why is snooze capped?', 'target_revision': sha}
    manifest.write_text('invalid')
    assert tool.call('knowledge.rationale', args)['status'] == 'unverified'
    tool.config.pop('rationale_manifest')
    result = tool.call('knowledge.rationale', args)
    assert result['status'] == 'unverified'
    assert result['reason'] == 'rationale_catalog_unconfigured'
    with pytest.raises(Exception):
        tool.call('knowledge.rationale', dict(args, repo_key='other'))
    with pytest.raises(Exception):
        tool.call('knowledge.rationale', dict(args, rationale_manifest='/caller/path'))


def test_rationale_reports_measured_phase_timing_without_claiming_unstarted_work(tmp_path, monkeypatch):
    tool, manifest, sha = setup_tool(tmp_path)
    args = {'repo_key': 'ops', 'query': 'Why is snooze capped?', 'target_revision': sha}

    def measured_call(ticks, request=args):
        clock = iter(ticks)
        monkeypatch.setattr(agent_tooling_module, 'perf_counter_ns', lambda: next(clock))
        return tool.call('knowledge.rationale', request)

    result = measured_call([100_000_000, 110_000_000, 120_000_000, 130_000_000])
    assert result['status'] == 'evidence'
    assert result['timing_ms'] == {'total': 30.0, 'catalog_validation': 10.0, 'search': 10.0}

    # A malformed catalog never enters search; its elapsed validation is still reported.
    manifest.write_text('invalid')
    result = measured_call([200_000_000, 210_000_000, 220_000_000])
    assert result['status'] == 'unverified'
    assert result['timing_ms'] == {'total': 20.0, 'catalog_validation': 10.0, 'search': None}

    # A configured catalog can fail during pinned source lookup after validation.
    manifest.write_text(json.dumps({'schema_version': 'ops.commit-rationale.v1',
                                    'entries': [entry(sha, 'Snooze is capped because reminders must return.')]}))
    result = measured_call([300_000_000, 310_000_000, 320_000_000, 330_000_000],
                           dict(args, target_revision='0' * 40))
    assert result['status'] == 'unverified'
    assert result['timing_ms'] == {'total': 30.0, 'catalog_validation': 10.0, 'search': 10.0}

    tool.config.pop('rationale_manifest')
    result = measured_call([400_000_000, 410_000_000])
    assert result['reason'] == 'rationale_catalog_unconfigured'
    assert result['timing_ms'] == {'total': 10.0, 'catalog_validation': None, 'search': None}


def test_compound_response_preserves_reviewed_scope_and_measured_facade_timing(tmp_path, monkeypatch):
    load, _, route, target = _compound_fixture(tmp_path)
    catalog = load()
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'schema_version': 'ops.agent-tooling.v1',
                                  'repos': {key: {'path': str(path),
                                                  'revision': (target if key == 'ops' else route['groups'][2]['target_revision'])}
                                            for key, path in catalog.repositories.items()},
                                  'rationale_manifest': str(catalog.manifest_path),
                                  'enabled_tools': ['knowledge.rationale']}))
    tool = AgentTooling(config)
    clock = iter([100_000_000, 110_000_000, 120_000_000, 130_000_000])
    monkeypatch.setattr(agent_tooling_module, 'perf_counter_ns', lambda: next(clock))
    result = tool.call('knowledge.rationale', {'repo_key': 'ops', 'target_revision': target,
                                               'query': route['questions'][0], 'top_k': 2})
    assert result['status'] == 'unverified'
    assert result['runtime_execution'] == 'not-assessed'
    assert result['not_established'] == ['Date of Core split', 'Running production behavior']
    assert result['groups'][2]['omitted_entry_ids'] == ['production-gap']
    assert result['timing_ms'] == {'total': 30.0, 'catalog_validation': 10.0, 'search': 10.0}


def test_catalog_cache_reuses_validated_git_and_revokes_on_manifest_change(tmp_path, monkeypatch):
    tool, manifest, sha = setup_tool(tmp_path)
    args = {'repo_key':'ops','target_revision':sha,'query':'Why is snooze capped?'}
    cold = tool.call('knowledge.rationale', args)
    assert cold['catalog_cache']['hit'] is False
    import kp_agent_tooling_ops._impl.commit_rationale as module
    original = module.RationaleCatalog
    monkeypatch.setattr(module, 'RationaleCatalog', lambda *a, **k: pytest.fail('warm catalog rebuilt'))
    warm = tool.call('knowledge.rationale', dict(args, response_mode='compact'))
    assert warm['catalog_cache']['hit'] is True
    assert warm['results'][0]['body_sha256'] == cold['results'][0]['body_sha256']
    assert 'excerpt' not in warm['results'][0]
    monkeypatch.setattr(module, 'RationaleCatalog', original)
    manifest.write_text(json.dumps({'schema_version':'ops.commit-rationale.v1','entries':[]}))
    revoked = tool.call('knowledge.rationale', args)
    assert revoked['catalog_cache']['hit'] is False and revoked['status'] == 'missing'


def test_catalog_cache_invalidates_when_host_repository_pin_changes(tmp_path):
    tool, manifest, sha = setup_tool(tmp_path)
    args = {'repo_key':'ops','target_revision':sha,'query':'Why is snooze capped?'}
    tool.call('knowledge.rationale', args)
    repo = tool.config['repos']['ops']['path']
    changed = commit(__import__('pathlib').Path(repo), 'other.txt', 'new', 'New', 'Other body.')
    tool.config['repos']['ops']['revision'] = changed
    assert tool.call('knowledge.rationale', args)['catalog_cache']['hit'] is False


def setup_contested_tool(tmp_path):
    load, rows, route, target = _compound_fixture(tmp_path)
    rows[2]['contested_by'] = [{'entry_id': 'foundation', 'body_lines': [2, 2]}]
    rows[2]['review_provenance'] = {
        'kind': 'agent', 'reviewer_id': 'agent:test', 'source': 'fixture review'}
    rows[2]['evidence_scope']['observations'][0] = {
        'kind': 'relayed_observation', 'body_lines': [1, 1],
        'attribution': {'reported_by': 'commit author', 'attributed_to': 'requester'}}
    catalog = load(changed_rows=rows)
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({
        'schema_version': 'ops.agent-tooling.v1',
        'repos': {key: {'path': str(path),
                        'revision': target if key == 'ops' else route['groups'][2]['target_revision']}
                  for key, path in catalog.repositories.items()},
        'rationale_manifest': str(catalog.manifest_path),
        'enabled_tools': ['knowledge.rationale']}))
    return AgentTooling(config), rows, route, target


def test_facade_dispute_does_not_promote_lexical_candidate_or_compound_scope(tmp_path):
    tool, rows, route, target = setup_contested_tool(tmp_path)
    core_target = rows[2]['commit']
    candidate = tool.call('knowledge.rationale', {
        'repo_key': 'core', 'target_revision': core_target,
        'query': 'Was production login tenant context verified?'})
    assert candidate['status'] == 'unverified'
    assert candidate['retrieval_status'] == 'unverified'
    assert candidate['evidence_kind'] == 'candidate_only'
    assert candidate['answerability'] == 'unverified'
    assert candidate['dispute_status'] == 'disputed'
    assert candidate['claim_status'] == 'disputed'
    assert candidate['results'][0]['claim_status'] == 'disputed'

    compound = tool.call('knowledge.rationale', {
        'repo_key': 'ops', 'target_revision': target, 'query': route['questions'][0], 'top_k': 5})
    assert compound['status'] == compound['retrieval_status'] == 'unverified'
    assert compound['evidence_kind'] == 'reviewed_source_passages'
    assert compound['answerability'] == 'claim_scope_unverified'
    assert compound['dispute_status'] == 'disputed'
    assert compound['claim_status'] == 'disputed'
    assert compound['not_established'] == route['not_established']


def test_facade_keeps_review_state_and_unassessed_gaps_separate_in_compact_mode(tmp_path):
    tool, rows, route, target = setup_contested_tool(tmp_path)
    request = {'repo_key': 'core', 'target_revision': rows[2]['commit'],
               'query': 'What production gap was reported?'}
    exact = tool.call('knowledge.rationale', request)
    claim = exact['results'][0]
    assert exact['retrieval_status'] == 'evidence'
    assert exact['claim_status'] == 'disputed'
    assert claim['review_state'] == 'reviewed'
    assert claim['review_label'] == 'agent-reviewed'
    assert claim['evidence_scope']['not_established'] is None
    assert claim['evidence_scope']['entry_not_established'] == ['Production fix after deployment']
    assert claim['evidence_scope']['gap_scope'] == 'not_assessed_for_this_question'
    assert claim['source_authorship']['identity_authenticated'] is False
    assert claim['evidence_scope']['observations'][0]['direct_observation'] is False
    assert claim['contested_by'][0]['counter_statement'] == 'Verified: login to candidate modal.'
    assert claim['contested_by'][0]['source']['target_revision'] == target
    assert claim['contested_by'][0]['source']['source_lineage'] == 'ancestor'

    compact = tool.call('knowledge.rationale', dict(request, response_mode='compact'))
    small = compact['results'][0]
    assert compact['retrieval_status'] == exact['retrieval_status']
    assert compact['dispute_status'] == exact['dispute_status']
    assert 'excerpt' not in small
    for key in ('review_state', 'review_label', 'review_provenance', 'source_authorship',
                'contested_by', 'claim_status', 'source_lineage'):
        assert small[key] == claim[key]
    for key in ('coverage_basis', 'not_established', 'entry_not_established', 'gap_scope'):
        assert small['evidence_scope'][key] == claim['evidence_scope'][key]
    for compact_observation, full_observation in zip(
            small['evidence_scope']['observations'], claim['evidence_scope']['observations']):
        assert 'excerpt' not in compact_observation
        assert {k: v for k, v in compact_observation.items()} == {
            k: v for k, v in full_observation.items() if k != 'excerpt'}
    assert small['body_sha256'] == claim['body_sha256']
    assert compact['expand_call']['arguments']['response_mode'] == 'full'


def test_facade_counter_lineage_uses_requested_target_not_host_head(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init')
    claim_body = 'Claim about production card moves.'
    claim_sha = commit(repo, 'claim.md', 'claim\n', 'Claim', claim_body)
    counter_body = 'Counter to the production card moves claim.'
    counter_sha = commit(repo, 'counter.md', 'counter\n', 'Counter', counter_body)
    rows = [entry(claim_sha, claim_body, id='claim', artifact_refs=['claim.md'],
                  questions=['Why did production card moves happen?'],
                  contested_by=[{'entry_id': 'counter', 'body_lines': [1, 1]}]),
            entry(counter_sha, counter_body, id='counter', artifact_refs=['counter.md'],
                  questions=['What is the counter-statement?'])]
    manifest = tmp_path / 'rationale.json'
    manifest.write_text(json.dumps({'schema_version': 'ops.commit-rationale.v1', 'entries': rows}))
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'schema_version': 'ops.agent-tooling.v1',
                                  'repos': {'ops': {'path': str(repo), 'revision': counter_sha}},
                                  'rationale_manifest': str(manifest),
                                  'enabled_tools': ['knowledge.rationale']}))
    tool = AgentTooling(config)
    request = {'repo_key': 'ops', 'query': rows[0]['questions'][0]}
    before_counter = tool.call('knowledge.rationale', dict(request, target_revision=claim_sha))
    assert before_counter['status'] == 'unverified'
    assert before_counter['reason'] == 'rationale_catalog_or_source_unavailable'
    assert 'counter-statement' in before_counter['detail']
    assert before_counter['results'] == []
    after_counter = tool.call('knowledge.rationale', dict(request, target_revision=counter_sha))
    assert after_counter['results'][0]['contested_by'][0]['source']['commit'] == counter_sha
    assert after_counter['results'][0]['contested_by'][0]['source']['target_revision'] == counter_sha


def test_facade_cross_repo_counter_uses_configured_pin_not_mutable_head_or_artifact_ref(tmp_path):
    ops = tmp_path / 'ops'
    core = tmp_path / 'core'
    ops.mkdir(); core.mkdir()
    git(ops, 'init'); git(core, 'init')
    claim_body = 'Claim about live card movement.'
    claim_sha = commit(ops, 'claim.md', 'claim\n', 'Claim', claim_body)
    core_base = commit(core, 'counter.md', 'first\n', 'Base', 'Earlier Core source.')
    counter_body = 'Counter-statement about live card movement.'
    counter_sha = commit(core, 'counter.md', 'later\n', 'Counter', counter_body)
    rows = [entry(claim_sha, claim_body, id='claim', questions=['Why live card movement?'],
                  artifact_refs=['claim.md'],
                  current_artifact_refs=[{'repo_key': 'core', 'revision': counter_sha,
                                          'path': 'counter.md'}],
                  contested_by=[{'entry_id': 'counter', 'body_lines': [1, 1]}]),
            entry(counter_sha, counter_body, id='counter', repo_key='core',
                  questions=['What was the Core counter-statement?'],
                  artifact_refs=['counter.md'])]
    manifest = tmp_path / 'rationale.json'
    manifest.write_text(json.dumps({'schema_version': 'ops.commit-rationale.v1', 'entries': rows}))
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'schema_version': 'ops.agent-tooling.v1',
                                  'repos': {'ops': {'path': str(ops), 'revision': claim_sha},
                                            'core': {'path': str(core), 'revision': core_base}},
                                  'rationale_manifest': str(manifest),
                                  'enabled_tools': ['knowledge.rationale']}))
    tool = AgentTooling(config)
    request = {'repo_key': 'ops', 'target_revision': claim_sha, 'query': rows[0]['questions'][0]}
    before_pin = tool.call('knowledge.rationale', request)
    assert before_pin['status'] == 'unverified'
    assert before_pin['reason'] == 'rationale_catalog_or_source_unavailable'
    assert 'counter-statement' in before_pin['detail']
    assert before_pin['results'] == []

    tool.config['repos']['core']['revision'] = counter_sha
    after_pin = tool.call('knowledge.rationale', request)
    counter_source = after_pin['results'][0]['contested_by'][0]['source']
    assert counter_source['commit'] == counter_sha
    assert counter_source['target_revision'] == counter_sha
    assert counter_source['source_lineage'] == 'ancestor'
