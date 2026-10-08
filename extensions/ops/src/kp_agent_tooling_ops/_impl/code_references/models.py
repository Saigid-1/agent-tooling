"""Text-free durable identities and transient reference values."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from kp_agent_tooling._impl import leaf


def canonical_digest(prefix: str, value: object) -> str:
    return leaf.content_id(prefix, value, ascii=True, allow_nan=True)


@dataclass(frozen=True, slots=True)
class ChunkRange:
    chunk_content_id: str
    byte_offset: int
    byte_length: int

    def __post_init__(self) -> None:
        if not self.chunk_content_id or self.byte_offset < 0 or self.byte_length < 0:
            raise ValueError("invalid chunk range")


@dataclass(frozen=True, slots=True)
class ReferenceCandidate:
    kind: str
    ref_byte_offset: int
    ref_byte_length: int
    literal_digest: str
    literal: str = field(repr=False)
    lookup_value: str = field(repr=False)
    line_hint: tuple[int, int] | None = None
    target_repo_key: str | None = None


@dataclass(frozen=True, slots=True)
class ChunkExtraction:
    chunk_content_id: str
    candidates: tuple[ReferenceCandidate, ...]
    candidate_total: int
    retained_count: int
    overflow_count: int
    omitted_by_kind: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    policy_version: str
    status: str
    reason: str | None
    chunks: tuple[ChunkExtraction, ...]
    unowned_count: int


@dataclass(frozen=True, slots=True)
class ResolutionTarget:
    node_id: str
    repo_key: str
    path: str
    blob_sha: str
    byte_offset: int
    byte_length: int
    symbol_name: str | None = None
    start_line: int = 0
    end_line: int = 0


@dataclass(frozen=True, slots=True)
class Resolution:
    execution_state: str
    status: str | None
    reason: str | None
    coverage: str
    resolver: str | None
    dependency_fingerprint: str
    targets: tuple[ResolutionTarget, ...] = ()
    match_count: int = 0
    truncated: bool = False
    absence_verdict: str | None = None


def occurrence_id(*, source_repo_key: str, document_path: str, document_blob_sha: str,
                  candidate: ReferenceCandidate) -> str:
    return canonical_digest("reference-occurrence", [source_repo_key, document_path,
        document_blob_sha, candidate.ref_byte_offset, candidate.ref_byte_length,
        candidate.literal_digest])


def resolution_id(*, occurrence: str, target_repo_key: str, code_revision: str,
                  extraction_policy_version: str, resolution_policy_version: str,
                  dependency_fingerprint: str) -> str:
    return canonical_digest("reference-resolution", [occurrence, target_repo_key,
        code_revision, extraction_policy_version, resolution_policy_version,
        dependency_fingerprint])


def reference_edge_id(*, source_repo_key: str, chunk_content_id: str,
                      resolution: str, relationship_kind: str, target_node_id: str) -> str:
    return canonical_digest("reference-edge", [source_repo_key, chunk_content_id,
        resolution, relationship_kind, target_node_id])
