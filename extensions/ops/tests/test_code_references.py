from pathlib import Path
import subprocess

import pytest

from kp_agent_tooling_ops._impl.code_references.extraction import extract_code_references
from kp_agent_tooling_ops._impl.code_references.manifest import SQLiteReferenceManifestStore, build_shard
from kp_agent_tooling_ops._impl.code_references.registry import (
    CommittedRegistryView,
    load_committed_operation_declarations, read_committed_operation_declarations,
    read_committed_registry,
)
from kp_agent_tooling_ops._impl.code_references.models import ChunkRange, occurrence_id, reference_edge_id
from kp_agent_tooling_ops._impl.code_references.models import Resolution
from kp_agent_tooling_ops._impl.code_references.models import ResolutionTarget
from kp_agent_tooling_ops._impl.code_references.indexing import DocumentReferenceIndexer


def test_extraction_is_byte_exact_cross_chunk_and_text_is_transient():
    raw = "é `Reader()` and [source](./pkg/read.py:L2-3)\n```\n`Ignored`\n```\n".encode()
    chunks = [ChunkRange("a", 0, 7), ChunkRange("b", 7, len(raw) - 7)]
    result = extract_code_references(raw, chunks=chunks, registry_view=frozenset())
    assert result.status == "complete"
    found = [item for chunk in result.chunks for item in chunk.candidates]
    assert [(c.kind, c.lookup_value) for c in found] == [
        ("symbol", "Reader"), ("path", "pkg/read.py")]
    assert found[0].ref_byte_offset == raw.index(b"Reader()")
    assert found[1].line_hint == (2, 3)
    assert "Reader" not in repr(found[0])


def test_overflow_is_counted_after_full_scan():
    raw = " ".join(f"`Symbol{i}`" for i in range(45)).encode()
    result = extract_code_references(raw, chunks=[ChunkRange("a", 0, len(raw))], registry_view=())
    chunk = result.chunks[0]
    assert (chunk.candidate_total, chunk.retained_count, chunk.overflow_count) == (45, 40, 5)
    assert chunk.omitted_by_kind == {"symbol": 5}


def test_occurrence_and_edge_id_include_source_and_resolution_identity():
    raw = b"`Reader`"
    candidate = extract_code_references(raw, chunks=[ChunkRange("c", 0, len(raw))], registry_view=()).chunks[0].candidates[0]
    left = occurrence_id(source_repo_key="ops", document_path="a.md", document_blob_sha="a"*40, candidate=candidate)
    right = occurrence_id(source_repo_key="ops", document_path="b.md", document_blob_sha="a"*40, candidate=candidate)
    assert left != right
    other_repo = occurrence_id(source_repo_key="ats", document_path="a.md",
        document_blob_sha="a"*40, candidate=candidate)
    assert left != other_repo
    assert reference_edge_id(source_repo_key="ops", chunk_content_id="c", resolution="r1",
        relationship_kind="REFERENCES_SYMBOL", target_node_id="s") != reference_edge_id(
        source_repo_key="ops", chunk_content_id="c", resolution="r2",
        relationship_kind="REFERENCES_SYMBOL", target_node_id="s")


def test_manifest_publishes_idempotently_and_refuses_conflict(tmp_path: Path):
    store = SQLiteReferenceManifestStore(tmp_path / "references.sqlite")
    values = dict(source_repo_key="ops", document_path="docs/a.md", document_blob_sha="a"*40,
        chunk_content_id="c", target_repo_key="ops", target_revision="b"*40,
        extraction_policy_version="e1", resolution_policy_version="r1", state="complete",
        dependency_fingerprint="d", candidate_total=1, retained_count=1, overflow_count=0,
        omitted_by_kind={}, unowned_count=0,
        integrity_checked=True, published=True,
        outcomes=({"occurrence_id": "o", "status": "unresolved"},))
    shard = build_shard(**values)
    store.publish(shard)
    store.publish(shard)
    assert store.select(**{k: values[k] for k in ("source_repo_key", "document_path",
        "document_blob_sha", "chunk_content_id", "target_repo_key", "target_revision",
        "extraction_policy_version", "resolution_policy_version")}) == shard
    with pytest.raises(ValueError, match="conflicting"):
        store.publish(build_shard(**{**values, "outcomes": ({"status": "resolved"},)}))


def test_invalid_utf8_is_an_error_with_null_counts():
    result = extract_code_references(b"bad\xff", chunks=[ChunkRange("c", 0, 4)], registry_view=())
    assert (result.status, result.reason, result.chunks) == ("error", "invalid_utf8", ())


def test_committed_registry_mapping_and_fingerprint_change_with_commit(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=tmp_path, check=True)
    registry = tmp_path / "registry.py"
    registry.write_text('CODE_REFERENCE_OPERATION_BINDINGS = {"knowledge.context": "pkg.Service.first"}\n')
    subprocess.run(["git", "add", "registry.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=tmp_path, check=True)
    first_revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
                                    text=True, stdout=subprocess.PIPE).stdout.strip()
    first = read_committed_registry(tmp_path, first_revision, "registry.py")
    registry.write_text('CODE_REFERENCE_OPERATION_BINDINGS = {"knowledge.context": "pkg.Service.second"}\n')
    subprocess.run(["git", "commit", "-qam", "second"], cwd=tmp_path, check=True)
    second_revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
                                     text=True, stdout=subprocess.PIPE).stdout.strip()
    second = read_committed_registry(tmp_path, second_revision, "registry.py")
    assert first.bindings["knowledge.context"] == "pkg.Service.first"
    assert second.bindings["knowledge.context"] == "pkg.Service.second"
    assert first.fingerprint != second.fingerprint


def test_committed_dispatch_declarations_derive_bindings_without_execution(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=tmp_path, check=True)
    declaration = tmp_path / "operations.py"
    declaration.write_text("""KNOWLEDGE_OPERATION_DECLARATIONS = (
    {"operation": "context", "description": "context", "handler": "kp_agent_tooling_ops._impl.service.knowledge.KnowledgeService.execute", "dispatch_method": "execute", "binding": "nameable"},
    {"operation": "dynamic", "description": "dynamic", "handler": None, "dispatch_method": "dispatch_dynamic", "binding": "dynamic"},
)
raise AssertionError("committed registry source must not execute")
""")
    subprocess.run(["git", "add", "operations.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "declarations"], cwd=tmp_path, check=True)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
                              text=True, stdout=subprocess.PIPE).stdout.strip()
    view = read_committed_operation_declarations(tmp_path, revision, "operations.py")
    assert view.bindings == {
        "knowledge.context": "kp_agent_tooling_ops._impl.service.knowledge.KnowledgeService.execute",
        "knowledge.dynamic": None}
    assert view.registry_path == "operations.py"
    assert view.namespaces == ("knowledge",)


def test_committed_dispatch_declarations_reject_duplicate_operation(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=tmp_path, check=True)
    row = '{"operation":"same","description":"x","handler":None,"dispatch_method":"dynamic","binding":"dynamic"}'
    (tmp_path / "operations.py").write_text(
        "KNOWLEDGE_OPERATION_DECLARATIONS = (" + row + "," + row + ",)\n")
    subprocess.run(["git", "add", "operations.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "duplicates"], cwd=tmp_path, check=True)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
                              text=True, stdout=subprocess.PIPE).stdout.strip()
    with pytest.raises(ValueError, match="values are invalid"):
        read_committed_operation_declarations(tmp_path, revision, "operations.py")


def test_absent_committed_dispatch_declaration_retains_unavailable_state(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("baseline\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
                              text=True, stdout=subprocess.PIPE).stdout.strip()
    assert load_committed_operation_declarations(
        tmp_path, revision, "missing-operations.py") is None


def test_partial_manifest_can_retry_to_complete_but_complete_is_immutable(tmp_path: Path):
    store = SQLiteReferenceManifestStore(tmp_path / "retry.sqlite")
    common = dict(source_repo_key="ops", document_path="docs/a.md", document_blob_sha="a"*40,
        chunk_content_id="c", target_repo_key="ops", target_revision="b"*40,
        extraction_policy_version="e1", resolution_policy_version="r1", candidate_total=1,
        retained_count=1, overflow_count=0, omitted_by_kind={}, unowned_count=0,
        integrity_checked=True, published=True)
    partial = build_shard(**common, state="partial", dependency_fingerprint="failed",
                          outcomes=({"execution_state": "error"},))
    complete = build_shard(**common, state="complete", dependency_fingerprint="complete",
                           outcomes=({"execution_state": "complete", "status": "unresolved"},))
    assert store.publish(partial) is True
    assert store.publish(complete) is True
    assert store.publish(complete) is False
    selected = store.select(**{k: common[k] for k in ("source_repo_key", "document_path",
        "document_blob_sha", "chunk_content_id", "target_repo_key", "target_revision",
        "extraction_policy_version", "resolution_policy_version")})
    assert selected is not None
    assert (selected.state, selected.integrity_checked, selected.published) == ("complete", True, True)


def test_unavailable_registry_is_retryable_error_for_dotted_candidate(tmp_path: Path):
    class Resolver:
        def resolve(self, candidate, **kwargs):
            return Resolution("complete", "unresolved", "no_exact_declaration", "complete",
                              "scip", "index:complete", absence_verdict="not-established")

    store = SQLiteReferenceManifestStore(tmp_path / "registry-retry.sqlite")
    indexer = DocumentReferenceIndexer(graph=object(), resolver=Resolver(), manifests=store)
    raw = b"`knowledge.context`"
    kwargs = dict(source_repo_key="ops", document_path="docs/a.md", document_blob_sha="a"*40,
        chunks=[ChunkRange("c", 0, len(raw))], code_revisions={"ops": "b"*40})
    failed = indexer.index_document(raw, registry_view=None, **kwargs)
    assert (failed.errors, failed.publication_state) == (1, "partial")
    assert store.all_shards()[0].outcomes[0]["reason"] == "registry_unavailable"
    recovered = indexer.index_document(raw, registry_view=frozenset(), **kwargs)
    assert (recovered.errors, recovered.publication_state) == (0, "complete")
    assert len(store.all_shards()) == 1
    assert store.all_shards()[0].outcomes[0]["status"] == "unresolved"


def test_partial_registry_preserves_external_dotted_namespace_as_retryable(tmp_path: Path):
    class Resolver:
        def resolve(self, candidate, **kwargs):
            raise AssertionError("external operation must not become a symbol lookup")

    store = SQLiteReferenceManifestStore(tmp_path / "namespace-retry.sqlite")
    indexer = DocumentReferenceIndexer(graph=object(), resolver=Resolver(), manifests=store)
    raw = b"`navigation.source`"
    registry = CommittedRegistryView({}, "registry:knowledge", "operations.py", "a" * 40,
                                     ("knowledge",))
    receipt = indexer.index_document(raw, source_repo_key="ops", document_path="docs/a.md",
        document_blob_sha="b" * 40, chunks=[ChunkRange("c", 0, len(raw))],
        registry_view=registry, code_revisions={"ops": "c" * 40})
    outcome = store.all_shards()[0].outcomes[0]
    assert (receipt.errors, receipt.publication_state) == (1, "partial")
    assert outcome["reason"] == "registry_namespace_unavailable"
    assert outcome["status"] is None and outcome["coverage"] == "partial"


def test_resolved_manifest_cannot_publish_read_view_before_graph_edge():
    class Adapter:
        def get_edge(self, edge_id):
            return None
    class Graph:
        adapter = Adapter()
    shard = build_shard(source_repo_key="ops", document_path="docs/a.md",
        document_blob_sha="a"*40, chunk_content_id="c", target_repo_key="ops",
        target_revision="b"*40, extraction_policy_version="e", resolution_policy_version="r",
        state="complete", dependency_fingerprint="d", candidate_total=1, retained_count=1,
        overflow_count=0, omitted_by_kind={}, unowned_count=0, integrity_checked=True,
        published=True, outcomes=({"status": "resolved", "edge_ids": ["missing"]},))
    with pytest.raises(ValueError, match="missing its graph edge"):
        SQLiteReferenceManifestStore.retrieval_view(shard, Graph())


def test_partial_retry_cannot_rewrite_completed_outcome(tmp_path: Path):
    store = SQLiteReferenceManifestStore(tmp_path / "immutable.sqlite")
    common = dict(source_repo_key="ops", document_path="d.md", document_blob_sha="a"*40,
        chunk_content_id="c", target_repo_key="ops", target_revision="b"*40,
        extraction_policy_version="e", resolution_policy_version="r", candidate_total=2,
        retained_count=2, overflow_count=0, omitted_by_kind={}, unowned_count=0,
        integrity_checked=True, published=True)
    stable = {"occurrence_id": "o1", "resolution_id": "r1", "execution_state": "complete",
              "status": "unresolved", "reason": "no_exact_declaration"}
    error = {"occurrence_id": "o2", "resolution_id": "r2", "execution_state": "error"}
    store.publish(build_shard(**common, state="partial", dependency_fingerprint="first",
                              outcomes=(stable, error)))
    changed = {**stable, "status": "resolved", "reason": None}
    with pytest.raises(ValueError, match="completed reference outcomes are immutable"):
        store.publish(build_shard(**common, state="complete", dependency_fingerprint="second",
                                  outcomes=(changed, {**error, "execution_state": "complete"})))


def test_crash_after_edge_write_reuses_orphan_then_replay_writes_zero(tmp_path: Path):
    class Adapter:
        def __init__(self): self.edges = {}
        def get_edge(self, key): return self.edges.get(key)
        def delete_edge(self, key): self.edges.pop(key, None)
    class Relationships:
        def __init__(self, adapter): self.adapter = adapter
        def create(self, kind, source, target, *, attributes, edge_id):
            self.adapter.edges[edge_id] = {"id": edge_id, "relationship_type": kind,
                "from_id": source, "to_id": target, "attributes": attributes}
    class Graph:
        def __init__(self):
            self.adapter = Adapter(); self.relationships = Relationships(self.adapter)
    class Resolver:
        def resolve(self, candidate, **kwargs):
            target = ResolutionTarget("symbol", "ops", "reader.py", "c"*40, 0, 6,
                                      "reader.Reader", 1, 1)
            return Resolution("complete", "resolved", None, "complete", "python_ast",
                              "index:fingerprint", (target,), 1)
    real = SQLiteReferenceManifestStore(tmp_path / "crash.sqlite")
    class CrashOnce:
        def __init__(self): self.crash = True
        def publish(self, shard):
            if self.crash:
                self.crash = False
                raise KeyboardInterrupt("simulated process death")
            return real.publish(shard)
    graph = Graph(); crashing = CrashOnce()
    indexer = DocumentReferenceIndexer(graph=graph, resolver=Resolver(), manifests=crashing)
    raw = b"`Reader`"
    kwargs = dict(source_repo_key="ops", document_path="d.md", document_blob_sha="a"*40,
        chunks=[ChunkRange("chunk", 0, len(raw))], registry_view=frozenset(),
        code_revisions={"ops": "b"*40})
    with pytest.raises(KeyboardInterrupt):
        indexer.index_document(raw, **kwargs)
    assert len(graph.adapter.edges) == 1 and real.all_shards() == ()
    recovered = indexer.index_document(raw, **kwargs)
    assert (recovered.edges_created, recovered.edges_reused, recovered.manifests_created) == (0, 1, 1)
    replay = indexer.index_document(raw, **kwargs)
    assert (replay.edges_created, replay.edges_reused, replay.manifests_created,
            replay.manifests_reused) == (0, 1, 0, 1)
    assert len(graph.adapter.edges) == 1 and len(real.all_shards()) == 1
