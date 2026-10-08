"""Index-time orchestration and positive graph publication."""

from __future__ import annotations

from dataclasses import dataclass

from .extraction import EXTRACTION_POLICY_VERSION, extract_code_references
from .manifest import SQLiteReferenceManifestStore, build_shard
from .models import (ChunkRange, Resolution, occurrence_id, reference_edge_id, resolution_id)
from .resolution import RESOLUTION_POLICY_VERSION, ReferenceResolver
from kp_agent_tooling_ops._impl.service.knowledge_telemetry import telemetry_stage


@dataclass(frozen=True, slots=True)
class ReferenceIndexReceipt:
    shard_ids: tuple[str, ...]
    candidate_occurrences: int
    unique_occurrences: int
    resolved: int
    errors: int
    edges_created: int
    edges_reused: int
    manifests_created: int
    manifests_reused: int
    publication_state: str


class DocumentReferenceIndexer:
    def __init__(self, *, graph: object, resolver: ReferenceResolver,
                 manifests: SQLiteReferenceManifestStore, telemetry: object | None = None,
                 target_preparer: object | None = None,
                 registry_loader: object | None = None) -> None:
        self._graph, self._resolver, self._manifests = graph, resolver, manifests
        self._telemetry = telemetry
        self._target_preparer = target_preparer
        self._registry_loader = registry_loader
        self.coverages: dict[tuple[str, str], object] = {}

    def index_document(self, document_bytes: bytes, **kwargs: object) -> ReferenceIndexReceipt:
        with self._stage("code_references.document", kind="document", bytes=len(document_bytes)):
            return self._index_document(document_bytes, **kwargs)

    def _index_document(self, document_bytes: bytes, *, source_repo_key: str,
                       document_path: str, document_blob_sha: str,
                       chunks: list[ChunkRange], registry_view: object | None,
                       code_revisions: dict[str, str]) -> ReferenceIndexReceipt:
        if registry_view is None and callable(self._registry_loader):
            source_revision = code_revisions.get(source_repo_key)
            if source_revision is not None:
                with self._stage("code_references.registry_read", kind="operation", stage="read"):
                    registry_view = self._registry_loader(source_repo_key, source_revision)
        with self._stage("code_references.extract", kind="document", bytes=len(document_bytes)):
            extraction = extract_code_references(document_bytes, chunks=chunks,
                                                 registry_view=registry_view)
        if extraction.status != "complete":
            return ReferenceIndexReceipt((), 0, 0, 0, 1, 0, 0, 0, 0, "partial")
        published = []
        unique_ids = set()
        resolved_count = error_count = edges_created = edges_reused = 0
        manifests_created = manifests_reused = 0
        for target_repo_key, revision in sorted(code_revisions.items()):
            resolution_cache = {}
            for chunk in extraction.chunks:
                outcomes = []
                fingerprints = []
                for candidate in chunk.candidates:
                    selected_repo = candidate.target_repo_key or source_repo_key
                    if selected_repo != target_repo_key:
                        if (target_repo_key == source_repo_key and candidate.target_repo_key is not None
                                and candidate.target_repo_key not in code_revisions):
                            occurrence = occurrence_id(source_repo_key=source_repo_key,
                                document_path=document_path, document_blob_sha=document_blob_sha,
                                candidate=candidate)
                            unique_ids.add((occurrence, candidate.target_repo_key, "unavailable"))
                            outcomes.append({"occurrence_id": occurrence, "resolution_id": None,
                                "kind": candidate.kind, "ref_byte_offset": candidate.ref_byte_offset,
                                "ref_byte_length": candidate.ref_byte_length,
                                "literal_digest": candidate.literal_digest,
                                "execution_state": "error", "status": None,
                                "reason": "unknown_target_repo", "coverage": "partial",
                                "match_count": 0, "truncated": False,
                                "absence_verdict": None, "targets": [], "edge_ids": [],
                                "requested_target_repo_key": candidate.target_repo_key})
                            error_count += 1
                        continue
                    occurrence = occurrence_id(source_repo_key=source_repo_key,
                        document_path=document_path, document_blob_sha=document_blob_sha,
                        candidate=candidate)
                    cached = resolution_cache.get(occurrence)
                    if cached is None:
                        unique_ids.add((occurrence, target_repo_key, revision))
                        namespaces = tuple(getattr(registry_view, "namespaces", ()))
                        outside_registry_namespace = (bool(namespaces)
                            and candidate.lookup_value.split(".", 1)[0] not in namespaces)
                        if (candidate.kind == "symbol" and "." in candidate.lookup_value
                                and (registry_view is None or outside_registry_namespace)):
                            reason = ("registry_namespace_unavailable"
                                      if outside_registry_namespace else "registry_unavailable")
                            fingerprint = getattr(registry_view, "fingerprint",
                                                  "registry:unavailable")
                            result = Resolution("error", None, reason, "partial", None,
                                                fingerprint)
                        else:
                            with self._stage("code_references.resolve", kind=candidate.kind,
                                             resolver=_telemetry_resolver(candidate.kind)):
                                result = self._resolver.resolve(candidate, source_repo_key=source_repo_key,
                                    target_repo_key=target_repo_key, code_revision=revision,
                                    registry_view=registry_view)
                        resolution = resolution_id(occurrence=occurrence, target_repo_key=target_repo_key,
                            code_revision=revision, extraction_policy_version=EXTRACTION_POLICY_VERSION,
                            resolution_policy_version=RESOLUTION_POLICY_VERSION,
                            dependency_fingerprint=result.dependency_fingerprint)
                        resolution_cache[occurrence] = (result, resolution)
                        resolved_count += int(result.status == "resolved")
                        error_count += int(result.execution_state == "error")
                    else:
                        result, resolution = cached
                    fingerprints.append(result.dependency_fingerprint)
                    record = {"occurrence_id": occurrence, "resolution_id": resolution,
                        "kind": candidate.kind, "ref_byte_offset": candidate.ref_byte_offset,
                        "ref_byte_length": candidate.ref_byte_length,
                        "literal_digest": candidate.literal_digest,
                        "execution_state": result.execution_state, "status": result.status,
                        "reason": result.reason, "coverage": result.coverage,
                        "match_count": result.match_count, "truncated": result.truncated,
                        "absence_verdict": result.absence_verdict,
                        "targets": [{"node_id": target.node_id, "repo_key": target.repo_key,
                            "path": target.path, "blob_sha": target.blob_sha,
                            "byte_offset": target.byte_offset, "byte_length": target.byte_length,
                            "symbol_name": target.symbol_name, "start_line": target.start_line,
                            "end_line": target.end_line} for target in result.targets[:20]],
                        "edge_ids": []}
                    if candidate.kind == "operation" and registry_view is not None:
                        record["registry_path"] = getattr(registry_view, "registry_path", None)
                        record["registry_blob_sha"] = getattr(registry_view, "blob_sha", None)
                    if result.execution_state == "complete" and result.status == "resolved":
                        for target in result.targets:
                            relationship = "REFERENCES_SYMBOL" if target.symbol_name else "REFERENCES_ARTIFACT"
                            edge = reference_edge_id(source_repo_key=source_repo_key,
                                chunk_content_id=chunk.chunk_content_id, resolution=resolution,
                                relationship_kind=relationship, target_node_id=target.node_id)
                            attrs = {"occurrence_id": occurrence, "resolution_id": resolution,
                                "ref_byte_offset": candidate.ref_byte_offset,
                                "ref_byte_length": candidate.ref_byte_length,
                                "literal_digest": candidate.literal_digest,
                                "reference_kind": candidate.kind, "resolution_status": "resolved",
                                "target_repo_key": target_repo_key, "resolved_code_revision": revision,
                                "resolved_blob_sha": target.blob_sha, "resolver": result.resolver,
                                "extraction_policy_version": EXTRACTION_POLICY_VERSION,
                                "resolution_policy_version": RESOLUTION_POLICY_VERSION,
                                "dependency_fingerprint": result.dependency_fingerprint,
                                "provenance_label": "deterministic-document-reference",
                                "derivation_rule": "doc-code-reference.v1",
                                "target_start_line": target.start_line,
                                "target_end_line": target.end_line}
                            with self._stage("code_references.graph_lookup", resolver="graph", stage="read"):
                                existing_edge = self._graph.adapter.get_edge(edge)
                            if existing_edge is None:
                                with self._stage("code_references.graph_write", resolver="graph", stage="write"):
                                    self._graph.relationships.create(relationship, chunk.chunk_content_id,
                                        target.node_id, attributes=attrs, edge_id=edge)
                                edges_created += 1
                            else:
                                edges_reused += 1
                            record["edge_ids"].append(edge)
                    outcomes.append(record)
                dependency = _combined(fingerprints)
                state = "partial" if chunk.overflow_count or extraction.unowned_count or any(
                    row["execution_state"] == "error" for row in outcomes) else "complete"
                with self._stage("code_references.graph_lookup", resolver="graph", stage="verify"):
                    if any(self._graph.adapter.get_edge(edge_id) is None for row in outcomes
                           for edge_id in row["edge_ids"]):
                        raise ValueError("reference graph publication integrity failure")
                shard = build_shard(source_repo_key=source_repo_key, document_path=document_path,
                    document_blob_sha=document_blob_sha, chunk_content_id=chunk.chunk_content_id,
                    target_repo_key=target_repo_key, target_revision=revision,
                    extraction_policy_version=EXTRACTION_POLICY_VERSION,
                    resolution_policy_version=RESOLUTION_POLICY_VERSION, state=state,
                    dependency_fingerprint=dependency,
                    # Extraction inventory is a chunk fact shared by every target
                    # shard. Outcomes below remain partitioned by target repository.
                    candidate_total=chunk.candidate_total,
                    retained_count=chunk.retained_count, overflow_count=chunk.overflow_count,
                    omitted_by_kind=chunk.omitted_by_kind, unowned_count=extraction.unowned_count,
                    integrity_checked=True, published=True, outcomes=tuple(outcomes))
                with self._stage("code_references.checkpoint", kind="manifest", stage="write"):
                    with self._stage("code_references.manifest_publish", kind="manifest", status=state,
                                     stage="publish"):
                        # Graph publication is deliberately edge-first. Never
                        # compensate by deleting an edge here: another concurrent
                        # publisher may already have reused it. A failed manifest
                        # write leaves a safe orphan for an idempotent retry.
                        created = self._manifests.publish(shard)
                manifests_created += int(created); manifests_reused += int(not created)
                published.append(shard.shard_id)
        candidate_occurrences = sum(chunk.candidate_total for chunk in extraction.chunks)
        publication = "partial" if error_count or any(c.overflow_count for c in extraction.chunks) else "complete"
        return ReferenceIndexReceipt(tuple(published), candidate_occurrences, len(unique_ids),
            resolved_count, error_count, edges_created, edges_reused, manifests_created,
            manifests_reused, publication)

    def _stage(self, name: str, **attrs: object):
        return telemetry_stage(self._telemetry, name, **attrs)

    def stage(self, name: str, **attrs: object):
        """Expose the fixed telemetry scope to the corpus ingestion owner."""
        return self._stage(name, **attrs)


def _combined(fingerprints: list[str]) -> str:
    from .models import canonical_digest
    return canonical_digest("resolver-inputs", sorted(fingerprints))


def _telemetry_resolver(kind: str) -> str:
    return {"path": "path", "symbol": "symbol", "operation": "operation"}[kind]
