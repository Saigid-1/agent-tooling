"""Text-free embedding population and ANN retrieval for git-backed documents."""

from __future__ import annotations

import hashlib
import math
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from kp_agent_tooling._impl.embeddings.embedders import Embedder
from kp_agent_tooling_ops._impl.embeddings.complete_text import document_embedder
from kp_agent_tooling._impl.embeddings.revision import EmbeddingRevision, embedding_identity
from kp_agent_tooling_ops._impl.embeddings.store import VectorStore


DESK_DOC_CONTENT_KIND = "desk_doc"
DESK_DOC_EMBEDDING_SPEC = "desk-doc-embed-v1"
DESK_DOC_INPUT_SPEC = "desk-doc-text-v1"

_COORDINATE_FIELDS = (
    "repo_key",
    "path",
    "blob_sha",
    "byte_offset",
    "byte_length",
    "heading",
    "local_path",
)


@dataclass(frozen=True, slots=True)
class DeskDocEmbedInput:
    """Transient blob text and its durable, text-free coordinate."""

    text: str
    content_digest: str
    chunk_content_id: str
    provenance: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class DeskDocEmbeddingUpsertResult:
    revision: EmbeddingRevision
    chunk_content_id: str
    embedding_id: str
    upserted: bool


def desk_doc_embedding_revision(embedder: Embedder) -> EmbeddingRevision:
    """Pin the document partition to its source-text contract."""

    base = document_embedder(embedder).revision
    return EmbeddingRevision(
        model_id=base.model_id,
        model_digest=base.model_digest,
        params=base.params,
        normalization=base.normalization,
        chunking_spec={
            "spec": DESK_DOC_EMBEDDING_SPEC,
            "input": DESK_DOC_INPUT_SPEC,
        },
    )


def build_desk_doc_embedding_input(
    chunk_node: Mapping[str, object],
) -> DeskDocEmbedInput:
    """Resolve one chunk from its immutable git blob, never the worktree."""

    chunk_content_id = chunk_node.get("id")
    if not isinstance(chunk_content_id, str) or not chunk_content_id:
        raise ValueError("DeskDocumentChunk id must be non-empty")
    if chunk_node.get("entity_type") != "DeskDocumentChunk":
        raise ValueError("desk-document input requires a DeskDocumentChunk node")
    attributes = chunk_node.get("attributes")
    if not isinstance(attributes, Mapping):
        raise TypeError("DeskDocumentChunk attributes must be a mapping")

    provenance = {name: attributes.get(name) for name in _COORDINATE_FIELDS}
    for name in ("repo_key", "path", "blob_sha", "local_path"):
        _required_string(provenance[name], name)
    if not isinstance(provenance["heading"], str):
        raise TypeError("DeskDocumentChunk attributes.heading must be a string")
    offset = _nonnegative_int(provenance["byte_offset"], "byte_offset")
    length = _positive_int(provenance["byte_length"], "byte_length")

    blob = _blob_bytes(
        Path(str(provenance["local_path"])), str(provenance["blob_sha"])
    )
    end = offset + length
    if end > len(blob):
        raise ValueError("DeskDocumentChunk coordinate exceeds its immutable blob")
    raw = blob[offset:end]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("DeskDocumentChunk coordinate is not valid UTF-8") from error
    content_digest = hashlib.sha256(raw).hexdigest()
    if attributes.get("content_digest") != content_digest:
        raise ValueError("DeskDocumentChunk content_digest diverges from its blob slice")
    return DeskDocEmbedInput(
        text=text,
        content_digest=content_digest,
        chunk_content_id=chunk_content_id,
        provenance=provenance,
    )


def upsert_desk_doc_embedding(
    chunk_node: Mapping[str, object],
    *,
    embedder: Embedder,
    store: VectorStore,
) -> DeskDocEmbeddingUpsertResult:
    """Embed one immutable document coordinate, with a duplicate fast path."""

    item = build_desk_doc_embedding_input(chunk_node)
    revision = desk_doc_embedding_revision(embedder)
    store.assert_embedding_model(revision.model_id)
    embedding_id = embedding_identity(item.content_digest, revision)
    existing = store.get(
        item.chunk_content_id,
        revision.revision_id,
        content_kind=DESK_DOC_CONTENT_KIND,
    )
    if existing is not None:
        if (
            existing.content_digest != item.content_digest
            or existing.provenance != dict(item.provenance)
        ):
            raise ValueError("immutable document vector identity carries conflicting data")
        return DeskDocEmbeddingUpsertResult(
            revision=revision,
            chunk_content_id=item.chunk_content_id,
            embedding_id=embedding_id,
            upserted=False,
        )

    raw_vectors = document_embedder(embedder).embed([item.text])
    if len(raw_vectors) != 1:
        raise ValueError("embedder returned a different vector count than documents")
    vector = tuple(float(value) for value in raw_vectors[0])
    if len(vector) != embedder.dim or any(not math.isfinite(value) for value in vector):
        raise ValueError("embedder returned an invalid document vector shape or value")
    upserted = store.upsert(
        item.chunk_content_id,
        revision.revision_id,
        vector,
        content_digest=item.content_digest,
        provenance=item.provenance,
        content_kind=DESK_DOC_CONTENT_KIND,
        embedding_model_id=revision.model_id,
    )
    return DeskDocEmbeddingUpsertResult(
        revision=revision,
        chunk_content_id=item.chunk_content_id,
        embedding_id=embedding_id,
        upserted=upserted,
    )


def _blob_bytes(root: Path, blob_sha: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "cat-file", "blob", blob_sha],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError(f"immutable document blob {blob_sha!r} is unavailable") from error


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"DeskDocumentChunk attributes.{name} must be non-empty")
    return value


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value
