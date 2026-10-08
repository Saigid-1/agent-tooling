"""Adversarial contract checks independent of the implementation's own fixtures."""
from __future__ import annotations

import hashlib

import pytest

from kp_agent_tooling_ops._impl.code_references.extraction import extract_code_references
from kp_agent_tooling_ops._impl.code_references.models import ChunkRange


def extracted(raw: bytes, *, chunks=None):
    return extract_code_references(raw, chunks=chunks or [ChunkRange('fixture', 0, len(raw))],
                                   registry_view=frozenset({'knowledge.context'}))


def test_link_label_is_not_an_independent_reference():
    """GREEN-IF: only the Markdown destination is a mention, regardless of label formatting."""
    raw = b'[`Reader`](pkg/reader.py) and `Other`'
    candidates = extracted(raw).chunks[0].candidates
    assert [c.literal for c in candidates] == ['pkg/reader.py', 'Other']


def test_image_html_block_and_escaped_opening_are_excluded():
    raw = (b'![`Reader`](src/reader.py)\n<div>\n`Hidden`\n</div>\n'
           b'\\`Escaped` and `Visible`\n')
    candidates = extracted(raw).chunks[0].candidates
    assert [c.literal for c in candidates] == ['Visible']


def test_fence_delimiter_type_and_length_must_match():
    """GREEN-IF: a tilde line or shorter backtick run cannot close a backtick fence."""
    raw = b'````python\n~~~\n`HiddenOne`\n```\n`HiddenTwo`\n````\n`Visible`\n'
    assert [c.literal for c in extracted(raw).chunks[0].candidates] == ['Visible']


def test_longer_closer_closes_fence_and_four_space_fence_is_indented_code():
    raw = b'```\n`Hidden`\n`````\n`Visible`\n    ```\n    `Indented`\n    ```\n'
    assert [c.literal for c in extracted(raw).chunks[0].candidates] == ['Visible']


def test_fence_with_trailing_info_cannot_close_and_backtick_info_cannot_open():
    raw = b'```python\n`HiddenOne`\n```still-info\n`HiddenTwo`\n```\n`Visible`\n```bad`info\n`AlsoVisible`\n'
    assert [c.literal for c in extracted(raw).chunks[0].candidates] == ['Visible', 'AlsoVisible']


def test_multiple_backtick_delimiters_preserve_source_span():
    """GREEN-IF: supported Markdown inline spans use their full delimiter run and retain exact bytes."""
    raw = 'é ``Reader`` and `Other()`\r\n'.encode()
    candidates = extracted(raw).chunks[0].candidates
    assert [c.literal for c in candidates] == ['Reader', 'Other()']
    for c in candidates:
        span = raw[c.ref_byte_offset:c.ref_byte_offset + c.ref_byte_length]
        assert span.decode() == c.literal
        assert hashlib.sha256(span).hexdigest() == c.literal_digest
    assert candidates[0].ref_byte_offset == raw.index(b'Reader')
    assert candidates[1].lookup_value == 'Other'


def test_fence_context_is_document_wide_with_overlapping_chunks():
    """GREEN-IF: chunk boundaries do not reset Markdown context or alter occurrence coordinates."""
    raw = b'```\n`Hidden`\n```\n`Visible`'
    start = raw.index(b'Visible')
    chunks = [ChunkRange('first', 0, start + 1), ChunkRange('second', start, len(raw)-start)]
    result = extracted(raw, chunks=chunks)
    assert [[c.literal for c in ch.candidates] for ch in result.chunks] == [['Visible'], ['Visible']]
    assert result.chunks[0].candidates[0] == result.chunks[1].candidates[0]
    assert result.unowned_count == 0


def test_overflow_counts_all_qualifying_occurrences_after_limit():
    """GREEN-IF: deterministic document-order retention reports all 460 omitted names."""
    raw = ' '.join(f'`Symbol{i}`' for i in range(500)).encode()
    chunk = extracted(raw).chunks[0]
    assert (chunk.candidate_total, chunk.retained_count, chunk.overflow_count) == (500, 40, 460)
    assert chunk.omitted_by_kind == {'symbol': 460}
    assert [c.literal for c in chunk.candidates] == [f'Symbol{i}' for i in range(40)]


def test_registry_classification_does_not_change_occurrence_inventory():
    """GREEN-IF: a dotted name changing registry membership changes kind, never position or retention."""
    raw = b'`knowledge.context` `Reader`'
    before = extract_code_references(raw, chunks=[ChunkRange('fixture', 0, len(raw))],
                                     registry_view=frozenset()).chunks[0]
    after = extracted(raw).chunks[0]
    assert before.retained_count == after.retained_count == 2
    assert [(c.ref_byte_offset,c.ref_byte_length,c.literal_digest) for c in before.candidates] == [
        (c.ref_byte_offset,c.ref_byte_length,c.literal_digest) for c in after.candidates]
    assert before.candidates[0].kind == 'symbol'
    assert after.candidates[0].kind == 'operation'


def _resolution_system(tmp_path, files):
    import subprocess
    from kp_core.adapters.kp_memory_adapter import MemoryAdapter
    from kp_agent_tooling._impl.persist.graph import build_graph
    from kp_agent_tooling_ops._impl.code_references.population import populate_python_targets
    from kp_agent_tooling_ops._impl.code_references.resolution import ReferenceResolver
    def git(*args):
        return subprocess.check_output(['git','-C',str(tmp_path),*args],text=True).strip()
    git('init','-q'); git('config','user.name','Reference fixture'); git('config','user.email','fixture@example.invalid')
    for path, content in files.items():
        p=tmp_path/path; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(content)
    git('add','.'); git('commit','-qm','fixture')
    revision=git('rev-parse','HEAD')
    memory=MemoryAdapter(); memory.connect(); graph=build_graph(memory)
    coverage=populate_python_targets(graph=graph,root=tmp_path,repo_key='ops',revision=revision)
    resolver=ReferenceResolver(repositories={'ops':tmp_path},graph=graph,
        symbol_index_fingerprint=coverage.fingerprint,symbol_index_complete=coverage.complete,
        coverage_provider=lambda repo,rev: coverage)
    return graph,revision,resolver,git


def _candidate(literal, kind='symbol'):
    from kp_agent_tooling_ops._impl.code_references.models import ReferenceCandidate
    return ReferenceCandidate(kind,0,len(literal.encode()),hashlib.sha256(literal.encode()).hexdigest(),literal,literal)


@pytest.mark.skip(reason='S5 excluded: _resolution_system needs kp_core MemoryAdapter and the OPS graph kp_ops.persist.graph')
def test_real_committed_declarations_distinguish_exact_ambiguous_and_weaker(tmp_path):
    """GREEN-IF: complete supported declarations resolve uniquely without promoting suffix/case matches."""
    graph,revision,resolver,_ = _resolution_system(tmp_path,{
        'pkg/one.py':b'class Unique: pass\nclass Duplicate: pass\n',
        'pkg/two.py':b'class Duplicate: pass\n'})
    def resolve(literal,kind='symbol'):
        return resolver.resolve(_candidate(literal,kind),source_repo_key='ops',target_repo_key='ops',code_revision=revision)
    assert resolve('Unique').status == 'resolved'
    assert resolve('pkg.one.Unique').status == 'resolved'
    assert resolve('Duplicate').status == 'ambiguous' and resolve('Duplicate').match_count==2
    assert resolve('one.Unique').status == 'candidate'
    assert resolve('unique').status == 'candidate'
    assert resolve('pkg/one.py','path').status == 'resolved'
    absent=resolve('pkg/missing.py','path')
    assert absent.status == 'unresolved' and absent.absence_verdict == 'not-established'
    assert not graph.relationships.find()


@pytest.mark.skip(reason='S5 excluded: _resolution_system needs kp_core MemoryAdapter and the OPS graph kp_ops.persist.graph')
def test_symlink_is_never_followed_as_a_regular_file(tmp_path):
    """GREEN-IF: an exact symlink path remains unsupported even when its referent exists."""
    _,_,resolver,git = _resolution_system(tmp_path,{'actual.py':b'class Reader: pass\n'})
    (tmp_path/'link.py').symlink_to('actual.py'); git('add','link.py'); git('commit','-qm','symlink')
    revision=git('rev-parse','HEAD')
    outcome=resolver.resolve(_candidate('link.py','path'),source_repo_key='ops',target_repo_key='ops',code_revision=revision)
    assert outcome.status=='unresolved' and outcome.reason=='unsupported_git_object'


@pytest.mark.skip(reason='S5 excluded: _resolution_system needs kp_core MemoryAdapter and the OPS graph kp_ops.persist.graph')
def test_incomplete_index_cannot_promote_single_observed_declaration(tmp_path):
    """GREEN-IF: one visible declaration is not unique while another supported file fails to parse."""
    _,revision,resolver,_ = _resolution_system(tmp_path,{
        'reader.py':b'class Reader: pass\n','broken.py':b'class Broken(\n'})
    outcome=resolver.resolve(_candidate('Reader'),source_repo_key='ops',target_repo_key='ops',code_revision=revision)
    assert outcome.execution_state=='error' and outcome.status is None


@pytest.mark.skip(reason='S5 excluded: _resolution_system needs kp_core MemoryAdapter and the OPS graph kp_ops.persist.graph')
def test_operation_binding_is_separate_from_exact_symbol_name(tmp_path):
    """GREEN-IF: a revision-qualified operation binding resolves its named handler rather than its operation string."""
    from kp_agent_tooling_ops._impl.code_references.resolution import ReferenceResolver
    from kp_agent_tooling_ops._impl.code_references.population import populate_python_targets
    graph,revision,_,_ = _resolution_system(tmp_path,{'handlers.py':b'def handle(): pass\n'})
    coverage=populate_python_targets(graph=graph,root=tmp_path,repo_key='ops',revision=revision)
    resolver=ReferenceResolver(repositories={'ops':tmp_path},graph=graph,
        symbol_index_fingerprint=coverage.fingerprint,symbol_index_complete=True,
        registry_bindings={'knowledge.context':'handlers.handle','knowledge.dynamic':None},
        registry_fingerprint='fixture-committed-registry',coverage_provider=lambda repo,rev:coverage)
    result=resolver.resolve(_candidate('knowledge.context','operation'),source_repo_key='ops',target_repo_key='ops',code_revision=revision)
    assert result.status=='resolved' and result.targets[0].symbol_name=='handlers.handle'
    missing=resolver.resolve(_candidate('knowledge.dynamic','operation'),source_repo_key='ops',target_repo_key='ops',code_revision=revision)
    assert missing.status=='unresolved' and missing.reason=='handler_not_nameable'


@pytest.mark.skip(reason='S5 excluded: _resolution_system needs kp_core MemoryAdapter and the OPS graph kp_ops.persist.graph')
def test_symbol_resolution_does_not_read_unrelated_declaration_blobs(tmp_path, monkeypatch):
    graph, revision, resolver, _ = _resolution_system(tmp_path, {
        'wanted.py': b'class Reader: pass\n',
        'unrelated.py': b'class Unrelated: pass\nclass Other: pass\n'})
    import kp_agent_tooling_ops._impl.code_references.resolution as module
    original = module.subprocess.run
    blobs = []
    def observed(command, **kwargs):
        if command[:3] == ['git', 'cat-file', 'blob']:
            blobs.append(command[3])
        return original(command, **kwargs)
    monkeypatch.setattr(module.subprocess, 'run', observed)
    result = resolver.resolve(_candidate('Reader'), source_repo_key='ops',
        target_repo_key='ops', code_revision=revision)
    assert result.status == 'resolved'
    assert len(blobs) == 1
    replay = resolver.resolve(_candidate('Reader'), source_repo_key='ops',
        target_repo_key='ops', code_revision=revision)
    assert replay.status == 'resolved' and len(blobs) == 1


@pytest.mark.skip(reason='S5 excluded: _resolution_system needs kp_core MemoryAdapter and the OPS graph kp_ops.persist.graph')
def test_more_than_twenty_exact_matches_report_total_and_bounded_targets(tmp_path):
    files={f'pkg/mod{i:02d}.py':b'class Duplicate: pass\n' for i in range(25)}
    _,revision,resolver,_=_resolution_system(tmp_path,files)
    result=resolver.resolve(_candidate('Duplicate'),source_repo_key='ops',target_repo_key='ops',code_revision=revision)
    assert result.status=='ambiguous' and result.match_count==25 and result.truncated is True
    assert len(result.targets)==20
    assert [t.path for t in result.targets]==sorted(files)[:20]


@pytest.mark.skip(reason='S5 excluded: _resolution_system needs kp_core MemoryAdapter and the OPS graph kp_ops.persist.graph')
@pytest.mark.parametrize('literal',[
    '/private/secret.py','../private/secret.py','C:/private/secret.py',
    'src/%2e%2e/secret.py',r'src\\private/secret.py'])
def test_unsafe_pathlike_inputs_are_retained_as_non_dereferenced_diagnostics(tmp_path,literal):
    _,revision,resolver,_=_resolution_system(tmp_path,{'actual.py':b'class Reader: pass\n'})
    raw=('`'+literal+'`').encode()
    inventory=extracted(raw).chunks[0].candidates
    assert len(inventory)==1
    outcome=resolver.resolve(inventory[0],source_repo_key='ops',target_repo_key='ops',code_revision=revision)
    assert outcome.status=='unresolved' and outcome.reason=='unsafe_path'


@pytest.mark.skip(reason='S5 excluded: _resolution_system needs kp_core MemoryAdapter and the OPS graph kp_ops.persist.graph')
def test_submodule_path_is_never_dereferenced(tmp_path):
    _,_,resolver,git=_resolution_system(tmp_path,{'actual.py':b'class Reader: pass\n'})
    sha=git('rev-parse','HEAD')
    git('update-index','--add','--cacheinfo',f'160000,{sha},vendor/module.py')
    git('commit','-qm','gitlink'); revision=git('rev-parse','HEAD')
    outcome=resolver.resolve(_candidate('vendor/module.py','path'),source_repo_key='ops',target_repo_key='ops',code_revision=revision)
    assert outcome.status=='unresolved' and outcome.reason=='unsupported_git_object'
