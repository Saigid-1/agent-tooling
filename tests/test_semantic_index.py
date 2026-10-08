"""navigation.semantic: a ranking layer with exact citations and no absence verdict."""
import json
import subprocess

import pytest

from kp_agent_tooling._impl.embeddings.embedders import DeterministicEmbedder
from kp_agent_tooling._impl.semantic_index import (SemanticIndexError, build_semantic_index, chunk_text,
                                   load_semantic_index, query_semantic_index, validate_semantic_index)


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


PY = '''"""Module doc."""


def sweep_expired_tombstones(db):
    """Hard-delete each due tombstone in its own committed transaction."""
    return db


class Executor:
    def restore_person(self, person_id):
        """Reverse a tombstone inside the retention window."""
        return person_id
'''


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / 'repo'; repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main'); git(repo, 'config', 'user.name', 't'); git(repo, 'config', 'user.email', 't@t')
    (repo / 'executor.py').write_text(PY)
    (repo / 'README.md').write_text('# Deletion\n\nThe sweep runs nightly.\n' + ('filler line\n' * 130))
    (repo / 'blob.bin').write_bytes(b'\0\1\2')
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'initial')
    revision = git(repo, 'rev-parse', 'HEAD')
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'schema_version': 'ops.navigation-profile.v1', 'profile': 'fixture',
                                   'repos': {'repo': {'path': str(repo), 'revision': revision}}}))
    config = {'repos': {'repo': {'path': str(repo), 'revision': revision}}, 'navigation_profile': str(profile),
              'navigation_registry_path': str(tmp_path / 'registry')}
    (tmp_path / 'registry').mkdir()
    return repo, config, revision, tmp_path


def test_python_files_chunk_by_symbol_and_others_by_window():
    rows = chunk_text('executor.py', PY)
    assert [(r['kind'], r['symbol']) for r in rows] == [
        ('function', 'sweep_expired_tombstones'), ('class', 'Executor'), ('method', 'Executor.restore_person')]
    assert rows[0]['start_line'] == 4 and rows[0]['end_line'] == 6
    windows = chunk_text('README.md', 'x\n' * 130)
    assert [w['kind'] for w in windows] == ['window', 'window', 'window']
    assert windows[1]['start_line'] == 51  # 60-line windows with 10 overlap


def test_build_then_query_returns_exact_citations_and_no_absence_claim(source):
    repo, config, revision, tmp = source
    embedder = DeterministicEmbedder(dim=32)
    summary = build_semantic_index(config, 'repo', revision, embedder=embedder, out_dir=tmp / 'idx')
    assert summary['chunk_count'] == 6 and summary['files_indexed'] == 2  # blob.bin never text-eligible
    assert summary['chunk_kinds'] == {'class': 1, 'function': 1, 'method': 1, 'window': 3}
    envelope, vectors = load_semantic_index(tmp / 'idx', 'repo', revision)
    assert vectors.shape == (6, 32)
    # The deterministic embedder maps identical text to identical vectors, so the chunk's
    # own text ranks first — enough to prove the plumbing, not the model.
    text = next(c for c in envelope['chunks'] if c['symbol'] == 'sweep_expired_tombstones')
    body = 'executor.py function sweep_expired_tombstones\n' + '\n'.join(PY.splitlines()[3:6])
    result = query_semantic_index(tmp / 'idx', 'repo', revision, body, embedder=embedder, limit=3)
    top = result['results'][0]
    assert top['path'] == 'executor.py' and top['symbol'] == 'sweep_expired_tombstones'
    assert top['start_line'] == 4 and top['end_line'] == 6 and top['cosine'] == 1.0 and top['score'] >= 1.0
    assert top['blob_sha'] == git(repo, 'rev-parse', revision + ':executor.py')
    assert top['retrieval']['arguments'] == {'repo_key': 'repo', 'target_revision': revision, 'path': 'executor.py',
                                             'start_line': 4, 'line_count': 3}
    assert result['absence_verdict'] == 'not-established' and result['evidence_kind'] == 'semantic_ranking'
    assert result['index']['index_sha256'] == envelope['index_sha256']
    filtered = query_semantic_index(tmp / 'idx', 'repo', revision, 'sweep', embedder=embedder, path_pattern='*.md')
    assert {r['path'] for r in filtered['results']} == {'README.md'} and filtered['candidates_considered'] == 3


def test_lexical_boost_and_test_penalty_are_applied_and_reported(source):
    repo, config, revision, tmp = source
    (repo / 'tests').mkdir(); (repo / 'tests' / 'test_x.py').write_text('def test_sweep():\n    pass\n')
    git(repo, 'add', '.'); git(repo, 'commit', '-qm', 'tests'); revision = git(repo, 'rev-parse', 'HEAD')
    import json as _json
    profile = _json.loads((tmp / 'profile.json').read_text()); profile['repos']['repo']['revision'] = revision
    (tmp / 'profile.json').write_text(_json.dumps(profile)); config['repos']['repo']['revision'] = revision
    embedder = DeterministicEmbedder(dim=32)
    build_semantic_index(config, 'repo', revision, embedder=embedder, out_dir=tmp / 'idx2')
    # The fixture embedder's cosines are hash noise, so assert the documented signals per hit
    # rather than a rank they cannot make decisive: 'restore' and 'person' appear only in
    # Executor.restore_person's symbol; README windows and the test file carry no boost.
    result = query_semantic_index(tmp / 'idx2', 'repo', revision, 'restore the person record', embedder=embedder, limit=10)
    by_symbol = {r['symbol']: r for r in result['results']}
    boosted = by_symbol['Executor.restore_person']
    assert boosted['lexical_boost'] == round(0.15 * 2 / 3, 4) and round(boosted['score'] - boosted['cosine'], 4) == boosted['lexical_boost']
    assert all(r['lexical_boost'] == 0 for r in result['results'] if r['path'] == 'README.md')
    assert result['ranking']['spec'] == 'cosine+lexical-path-symbol-v1' and result['ranking']['test_penalty_applied'] is True
    test_rows = [r for r in result['results'] if r['path'].startswith('tests/')]
    assert test_rows and all(r['test_penalty'] for r in test_rows)
    assert all(round(r['cosine'] - r['score'], 4) == 0.05 for r in test_rows)
    asked = query_semantic_index(tmp / 'idx2', 'repo', revision, 'test for sweep', embedder=embedder, limit=10)
    assert asked['ranking']['test_penalty_applied'] is False
    assert all(not r['test_penalty'] for r in asked['results'])


def test_index_refuses_tamper_wrong_revision_and_other_embedder(source):
    repo, config, revision, tmp = source
    embedder = DeterministicEmbedder(dim=32)
    build_semantic_index(config, 'repo', revision, embedder=embedder, out_dir=tmp / 'idx')
    with pytest.raises(SemanticIndexError, match='index_unavailable|no semantic index'):
        query_semantic_index(tmp / 'idx', 'repo', 'f' * 40, 'x', embedder=embedder)
    with pytest.raises(SemanticIndexError) as excinfo:
        query_semantic_index(tmp / 'idx', 'repo', revision, 'x', embedder=DeterministicEmbedder(dim=16))
    assert excinfo.value.reason == 'embedder_mismatch'
    vectors = tmp / 'idx' / f'repo-{revision}.semantic.vectors.f32'
    vectors.write_bytes(b'\0' * len(vectors.read_bytes()))
    with pytest.raises(SemanticIndexError) as excinfo:
        load_semantic_index(tmp / 'idx', 'repo', revision)
    assert excinfo.value.reason == 'index_integrity'


def test_tool_reports_missing_index_as_unavailable_and_serves_a_built_one(source, monkeypatch):
    repo, config, revision, tmp = source
    from kp_agent_tooling._impl.service.agent_tooling import AgentTooling
    tooling = tmp / 'tooling.json'
    tooling.write_text(json.dumps({'schema_version': 'ops.agent-tooling.v1', **config}))
    adapter = AgentTooling(tooling)
    embedder = DeterministicEmbedder(dim=32)
    monkeypatch.setattr(adapter, '_semantic_embedder', lambda: embedder)
    assert any(t['name'] == 'navigation.semantic' for t in adapter.tools())
    missing = adapter.call('navigation.semantic', {'repo_key': 'repo', 'query': 'sweep'})
    assert missing['status'] == 'unavailable' and missing['reason'] == 'index_unavailable'
    assert missing['absence_verdict'] == 'not-established'
    build_semantic_index(config, 'repo', revision, embedder=embedder, out_dir=tmp / 'registry' / 'semantic')
    served = adapter.call('navigation.semantic', {'repo_key': 'repo', 'query': 'sweep', 'limit': 2})
    assert served['status'] == 'ok' and len(served['results']) == 2
    other = adapter.call('navigation.semantic', {'repo_key': 'repo', 'query': 'sweep', 'target_revision': 'e' * 40})
    assert other['reason'] == 'revision_not_indexed'


def test_partitioned_semantic_query_crosses_parts_and_rejects_missing_part(source, monkeypatch):
    import kp_agent_tooling._impl.semantic_index as module
    repo, config, revision, tmp = source
    monkeypatch.setattr(module, 'PART_CHUNKS', 2)
    embedder = DeterministicEmbedder(dim=32)
    summary = build_semantic_index(config, 'repo', revision, embedder=embedder, out_dir=tmp / 'parts')
    root = json.loads((tmp / 'parts' / f'repo-{revision}.semantic.json').read_text())
    assert len(root['parts']) == 3 and summary['chunk_count'] == 6
    assert validate_semantic_index(tmp / 'parts', 'repo', revision, expected_sha256=root['index_sha256'])['chunk_count'] == 6
    with pytest.raises(SemanticIndexError, match='partitioned'):
        load_semantic_index(tmp / 'parts', 'repo', revision)
    found = query_semantic_index(tmp / 'parts', 'repo', revision, 'restore person', embedder=embedder, limit=6)
    assert found['candidates_considered'] == 6
    assert any(row['symbol'] == 'Executor.restore_person' for row in found['results'])
    assert found['index']['index_sha256'] == root['index_sha256']
    last_chunk = tmp / 'parts' / root['parts'][-1]['chunks_file']
    original_chunk = last_chunk.read_bytes()
    last_chunk.unlink()
    with pytest.raises(SemanticIndexError, match='partition'):
        query_semantic_index(tmp / 'parts', 'repo', revision, 'restore person', embedder=embedder)
    last_chunk.write_bytes(original_chunk)
    last_vectors = tmp / 'parts' / root['parts'][-1]['vectors_file']
    original_vectors = last_vectors.read_bytes()
    last_vectors.write_bytes(bytes([original_vectors[0] ^ 1]) + original_vectors[1:])
    with pytest.raises(SemanticIndexError, match='digest'):
        validate_semantic_index(tmp / 'parts', 'repo', revision)
    last_vectors.write_bytes(original_vectors)
    other_revision = 'f' * 40
    (tmp / 'parts' / f'repo-{other_revision}.semantic.json').write_text(json.dumps(root))
    with pytest.raises(SemanticIndexError, match='another repository or revision'):
        validate_semantic_index(tmp / 'parts', 'repo', other_revision)
    root_path = tmp / 'parts' / f'repo-{revision}.semantic.json'
    invalid = {**root, 'chunk_count': root['chunk_count'] + 1}
    invalid.pop('index_sha256')
    invalid['index_sha256'] = module._digest(module._canonical(invalid))
    root_path.write_text(json.dumps(invalid))
    with pytest.raises(SemanticIndexError, match='coverage'):
        validate_semantic_index(tmp / 'parts', 'repo', revision)


def test_source_budget_is_applied_per_group(source, monkeypatch):
    import kp_agent_tooling._impl.semantic_index as module
    repo, config, revision, tmp = source
    sizes = [p.stat().st_size for p in (repo / 'executor.py', repo / 'README.md')]
    monkeypatch.setattr(module, 'PART_SOURCE_BYTES', max(sizes))
    assert sum(sizes) > module.PART_SOURCE_BYTES  # scaled form of the former global-source failure
    summary = build_semantic_index(config, 'repo', revision, embedder=DeterministicEmbedder(dim=32), out_dir=tmp / 'grouped')
    assert summary['files_indexed'] == 2 and summary['chunk_count'] == 6


@pytest.mark.parametrize('state', ['building', 'unavailable', 'not_requested'])
def test_published_readiness_prevents_model_load_and_manual_rebuild_advice(source, monkeypatch, state):
    repo, config, revision, tmp = source
    from kp_agent_tooling._impl.service.agent_tooling import AgentTooling
    from kp_agent_tooling._impl import navigation_workspace
    tooling = tmp / 'tooling.json'
    tooling.write_text(json.dumps({'schema_version': 'ops.agent-tooling.v1', **config}))
    adapter = AgentTooling(tooling)
    monkeypatch.setattr(navigation_workspace, 'active', lambda _: {
        'repos': config['repos'], 'repository_readiness': {'repo': {'semantic': state}}})
    monkeypatch.setattr(adapter, '_semantic_embedder', lambda: pytest.fail('must not load model'))
    result = adapter._semantic(config, None, {'repo_key': 'repo', 'query': 'sweep'})
    assert result['build_state'] == state and result['status'] == 'unavailable'
    assert result['absence_verdict'] == 'not-established'
    assert 'scripts/' not in json.dumps(result)
    assert 'navigation.search' in result['next_action']
