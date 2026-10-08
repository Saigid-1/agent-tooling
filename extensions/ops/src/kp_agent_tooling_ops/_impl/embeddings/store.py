"""Text-free vector stores with exposed hybrid score components."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from kp_agent_tooling._impl import leaf


_TEXT_KEYS = frozenset(
    {"text", "source_text", "raw_text", "input_text", "hygiened_text", "content"}
)
_SQL_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTENT_KIND = re.compile(r"^[a-z][a-z0-9_]*$")
DEFAULT_EMBEDDING_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"


class MixedRevisionError(ValueError):
    """A query requested a revision absent from a non-empty store."""


class VectorConflictError(ValueError):
    """An immutable vector key was offered different durable data."""


class EmbeddingSpaceError(ValueError):
    """A write attempted to mix embedding models in one vector space."""


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """One text-free row keyed by kind, content id, and embedding revision.

    The historical ``chunk_content_id`` name intentionally remains unchanged;
    ``content_kind`` generalizes its meaning without renaming the column or the
    Python interface in this additive migration.
    """

    chunk_content_id: str
    revision_id: str
    vector: tuple[float, ...]
    content_digest: str
    provenance: dict[str, object]
    content_kind: str = "chunk"
    embedding_model_id: str | None = None


@dataclass(frozen=True, slots=True)
class HybridResult:
    """A retrieval result whose vector, lexical, and combined scores are visible."""

    chunk_content_id: str
    vector_score: float
    lexical_score: float
    combined_score: float
    content_kind: str = "chunk"


@dataclass(frozen=True, slots=True)
class AnnCandidate:
    """One text-free Stage-1 result scored by vector cosine only."""

    chunk_content_id: str
    revision_id: str
    vector_score: float
    provenance: dict[str, object]
    content_kind: str = "chunk"


@runtime_checkable
class VectorStore(Protocol):
    """Text-free vector persistence plus staged-retrieval primitives."""

    def upsert(
        self,
        chunk_content_id: str,
        revision_id: str,
        vector: Sequence[float],
        *,
        content_digest: str,
        provenance: Mapping[str, object],
        content_kind: str = "chunk",
        embedding_model_id: str | None = None,
    ) -> bool: ...

    def get(
        self,
        chunk_content_id: str,
        revision_id: str,
        *,
        content_kind: str = "chunk",
    ) -> VectorRecord | None: ...

    def document_sources(self, *, repo_key: str) -> tuple[tuple[str, str], ...]: ...

    def remove_documents(self, *, repo_key: str, path: str, blob_sha: str | None = None) -> int: ...

    def ann_candidates(
        self,
        vector: Sequence[float],
        *,
        top_k: int,
        revision_id: str | None = None,
        content_kind: str = "chunk",
    ) -> tuple[AnnCandidate, ...]: ...

    def lexical_scores(
        self,
        text: str,
        chunk_content_ids: Sequence[str],
        *,
        revision_id: str | None = None,
        content_kind: str = "chunk",
    ) -> Mapping[str, float]: ...

    def query(
        self,
        vector: Sequence[float],
        text: str,
        *,
        top_k: int,
        revision_id: str,
        content_kind: str = "chunk",
    ) -> tuple[HybridResult, ...]: ...

    def assert_embedding_model(self, embedding_model_id: str) -> None: ...


def _validated_content_kind(content_kind: str) -> str:
    if not isinstance(content_kind, str) or not _CONTENT_KIND.fullmatch(content_kind):
        raise ValueError(
            "content_kind must be a lowercase identifier beginning with a letter"
        )
    return content_kind


def _validated_model_id(embedding_model_id: str) -> str:
    if not isinstance(embedding_model_id, str) or not embedding_model_id:
        raise ValueError("embedding_model_id must be a non-empty string")
    return embedding_model_id


def _validate_provenance(provenance: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(provenance, Mapping):
        raise TypeError("provenance must be a mapping")

    def inspect(value: object) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise TypeError("provenance keys must be strings")
                if key.casefold() in _TEXT_KEYS:
                    raise ValueError(f"vector provenance refuses source-text field {key!r}")
                inspect(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                inspect(child)

    inspect(provenance)
    try:
        encoded = leaf.canonical_json(dict(provenance), ascii=False, allow_nan=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise ValueError("provenance must contain finite JSON values") from error
    return decoded


def _vector_tuple(vector: Sequence[float], *, expected_dim: int | None = None) -> tuple[float, ...]:
    if isinstance(vector, (str, bytes)):
        raise TypeError("vector must be a numeric sequence")
    try:
        result = tuple(float(value) for value in vector)
    except (TypeError, ValueError) as error:
        raise TypeError("vector must be a numeric sequence") from error
    if not result or any(not math.isfinite(value) for value in result):
        raise ValueError("vector must contain finite values and must not be empty")
    if expected_dim is not None and len(result) != expected_dim:
        raise ValueError(
            f"vector dimension mismatch: expected {expected_dim}, got {len(result)}"
        )
    return result


def _positive_top_k(top_k: int) -> int:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        raise ValueError("top_k must be a positive integer")
    return top_k


def _requested_ids(chunk_content_ids: Sequence[str]) -> tuple[str, ...]:
    if isinstance(chunk_content_ids, (str, bytes)):
        raise TypeError("chunk_content_ids must be a sequence of identifiers")
    result = tuple(chunk_content_ids)
    if any(not isinstance(item, str) or not item for item in result):
        raise ValueError("chunk_content_ids must contain non-empty strings")
    if len(result) != len(set(result)):
        raise ValueError("chunk_content_ids must not contain duplicates")
    return result


def _select_revision(
    available: Sequence[str],
    *,
    requested: str | None,
    configured: str | None,
) -> str | None:
    revisions = tuple(sorted(set(available)))
    selected = requested or configured
    if selected is not None:
        if not isinstance(selected, str) or not selected:
            raise ValueError("retrieval revision id must be a non-empty string")
        if revisions and selected not in revisions:
            raise MixedRevisionError(
                "mixed embedding revision query refused before retrieval: "
                f"requested={selected!r}, available={list(revisions)!r}"
            )
        return selected
    if len(revisions) > 1:
        raise MixedRevisionError(
            "mixed embedding revision query refused before retrieval: "
            f"requested=None, available={list(revisions)!r}; configure a retrieval revision"
        )
    return revisions[0] if revisions else None


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("cannot compare vectors with different dimensions")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def _trigrams(text: str) -> set[str]:
    normalized = " ".join(text.casefold().split())
    if not normalized:
        return set()
    padded = f"  {normalized}  "
    return {padded[index : index + 3] for index in range(len(padded) - 2)}


def lexical_score(query: str, document: str) -> float:
    """Deterministic substring/trigram score used by the memory test store."""

    normalized_query = " ".join(query.casefold().split())
    normalized_document = " ".join(document.casefold().split())
    if not normalized_query or not normalized_document:
        return 0.0
    substring = 1.0 if normalized_query in normalized_document else 0.0
    query_trigrams = _trigrams(normalized_query)
    document_trigrams = _trigrams(normalized_document)
    union = query_trigrams | document_trigrams
    trigram = len(query_trigrams & document_trigrams) / len(union) if union else 0.0
    return max(substring, trigram)


class MemoryVectorStore:
    """In-memory cosine store with externally resolved lexical documents.

    ``lexical_resolver`` is intentionally external: it may transiently resolve
    a Ring-0 coordinate, but source text is never retained in ``VectorRecord``.
    """

    def __init__(
        self,
        *,
        lexical_resolver: Callable[[str], str | None] | None = None,
        vector_weight: float = 0.7,
        lexical_weight: float = 0.3,
        retrieval_revision_id: str | None = None,
        embedding_model_id: str | None = None,
    ) -> None:
        if vector_weight < 0 or lexical_weight < 0:
            raise ValueError("hybrid weights must be non-negative")
        if vector_weight == 0 and lexical_weight == 0:
            raise ValueError("at least one hybrid signal must be enabled")
        self._lexical_resolver = lexical_resolver
        self._vector_weight = float(vector_weight)
        self._lexical_weight = float(lexical_weight)
        if retrieval_revision_id is not None and not retrieval_revision_id:
            raise ValueError("retrieval_revision_id must not be empty")
        self._retrieval_revision_id = retrieval_revision_id
        self._embedding_model_id = (
            _validated_model_id(embedding_model_id)
            if embedding_model_id is not None
            else None
        )
        self._records: dict[tuple[str, str, str], VectorRecord] = {}

    def assert_embedding_model(self, embedding_model_id: str) -> None:
        """Bind an empty fixture store, then refuse every model-axis variation."""

        requested = _validated_model_id(embedding_model_id)
        if self._embedding_model_id is None:
            self._embedding_model_id = requested
        elif requested != self._embedding_model_id:
            raise EmbeddingSpaceError(
                "one embedding table is one embedding model: "
                f"configured={self._embedding_model_id!r}, requested={requested!r}"
            )

    @property
    def records(self) -> tuple[VectorRecord, ...]:
        return tuple(self._copy(record) for record in self._records.values())

    @staticmethod
    def _copy(record: VectorRecord) -> VectorRecord:
        return VectorRecord(
            chunk_content_id=record.chunk_content_id,
            revision_id=record.revision_id,
            vector=record.vector,
            content_digest=record.content_digest,
            provenance=dict(record.provenance),
            content_kind=record.content_kind,
            embedding_model_id=record.embedding_model_id,
        )

    def upsert(
        self,
        chunk_content_id: str,
        revision_id: str,
        vector: Sequence[float],
        *,
        content_digest: str,
        provenance: Mapping[str, object],
        content_kind: str = "chunk",
        embedding_model_id: str | None = None,
    ) -> bool:
        if not chunk_content_id or not revision_id or not content_digest:
            raise ValueError("vector identities and content digest must not be empty")
        kind = _validated_content_kind(content_kind)
        if embedding_model_id is not None:
            self.assert_embedding_model(embedding_model_id)
        stored = VectorRecord(
            chunk_content_id=chunk_content_id,
            revision_id=revision_id,
            vector=_vector_tuple(vector),
            content_digest=content_digest,
            provenance=_validate_provenance(provenance),
            content_kind=kind,
            embedding_model_id=self._embedding_model_id,
        )
        key = (chunk_content_id, revision_id, kind)
        existing = self._records.get(key)
        if existing is None:
            self._records[key] = stored
            return True
        if existing != stored:
            raise VectorConflictError(
                "immutable vector key already carries different digest, vector, or provenance"
            )
        return False

    def get(
        self,
        chunk_content_id: str,
        revision_id: str,
        *,
        content_kind: str = "chunk",
    ) -> VectorRecord | None:
        kind = _validated_content_kind(content_kind)
        record = self._records.get((chunk_content_id, revision_id, kind))
        return self._copy(record) if record is not None else None

    def document_sources(self, *, repo_key: str) -> tuple[tuple[str, str], ...]:
        return tuple(sorted({(row.provenance['path'], row.provenance['blob_sha'])
            for row in self._records.values()
            if row.content_kind == 'desk_doc' and row.provenance.get('repo_key') == repo_key}))

    def remove_documents(self, *, repo_key: str, path: str, blob_sha: str | None = None) -> int:
        from kp_agent_tooling_ops._impl.document_identity import document_identity as _identity
        _identity(repo_key, path, blob_sha)
        keys = [key for key, row in self._records.items()
                if row.content_kind == 'desk_doc' and row.provenance.get('repo_key') == repo_key
                and row.provenance.get('path') == path
                and (blob_sha is None or row.provenance.get('blob_sha') == blob_sha)]
        for key in keys:
            del self._records[key]
        return len(keys)

    def _resolve_revision(
        self, requested: str | None = None, *, content_kind: str = "chunk"
    ) -> str | None:
        kind = _validated_content_kind(content_kind)
        return _select_revision(
            [
                record.revision_id
                for record in self._records.values()
                if record.content_kind == kind
            ],
            requested=requested,
            configured=self._retrieval_revision_id,
        )

    def ann_candidates(
        self,
        vector: Sequence[float],
        *,
        top_k: int,
        revision_id: str | None = None,
        content_kind: str = "chunk",
    ) -> tuple[AnnCandidate, ...]:
        """Return Stage-1 cosine candidates without resolving lexical content."""

        limit = _positive_top_k(top_k)
        kind = _validated_content_kind(content_kind)
        selected_revision = self._resolve_revision(revision_id, content_kind=kind)
        if selected_revision is None:
            return ()
        records = [
            record
            for record in self._records.values()
            if record.revision_id == selected_revision and record.content_kind == kind
        ]
        if not records:
            return ()
        query_vector = _vector_tuple(vector, expected_dim=len(records[0].vector))
        candidates = [
            AnnCandidate(
                chunk_content_id=record.chunk_content_id,
                revision_id=record.revision_id,
                vector_score=_cosine(query_vector, record.vector),
                provenance=dict(record.provenance),
                content_kind=record.content_kind,
            )
            for record in records
        ]
        candidates.sort(key=lambda item: (-item.vector_score, item.chunk_content_id))
        return tuple(candidates[:limit])

    def lexical_scores(
        self,
        text: str,
        chunk_content_ids: Sequence[str],
        *,
        revision_id: str | None = None,
        content_kind: str = "chunk",
    ) -> Mapping[str, float]:
        """Score lexical overlap for only the supplied Stage-1 candidate ids."""

        if not isinstance(text, str):
            raise TypeError("query text must be a string")
        requested = _requested_ids(chunk_content_ids)
        kind = _validated_content_kind(content_kind)
        selected_revision = self._resolve_revision(revision_id, content_kind=kind)
        if selected_revision is None:
            return {item: 0.0 for item in requested}
        scores: dict[str, float] = {}
        for chunk_content_id in requested:
            record = self._records.get((chunk_content_id, selected_revision, kind))
            document = (
                self._lexical_resolver(chunk_content_id)
                if record is not None and self._lexical_resolver is not None
                else None
            )
            scores[chunk_content_id] = (
                lexical_score(text, document) if isinstance(document, str) else 0.0
            )
        return scores

    def query(
        self,
        vector: Sequence[float],
        text: str,
        *,
        top_k: int,
        revision_id: str,
        content_kind: str = "chunk",
    ) -> tuple[HybridResult, ...]:
        limit = _positive_top_k(top_k)
        kind = _validated_content_kind(content_kind)
        if not isinstance(text, str):
            raise TypeError("query text must be a string")
        candidates = self.ann_candidates(
            vector, top_k=limit, revision_id=revision_id, content_kind=kind
        )
        if not candidates:
            return ()
        lexical = self.lexical_scores(
            text,
            [candidate.chunk_content_id for candidate in candidates],
            revision_id=revision_id,
            content_kind=kind,
        )
        scored: list[HybridResult] = []
        for candidate in candidates:
            lexical_component = lexical[candidate.chunk_content_id]
            combined = (
                self._vector_weight * candidate.vector_score
                + self._lexical_weight * lexical_component
            )
            scored.append(
                HybridResult(
                    chunk_content_id=candidate.chunk_content_id,
                    vector_score=candidate.vector_score,
                    lexical_score=lexical_component,
                    combined_score=combined,
                    content_kind=kind,
                )
            )
        scored.sort(key=lambda item: (-item.combined_score, item.chunk_content_id))
        return tuple(scored[:limit])


class PgVectorStore:
    """Migration-first pgvector/pg_trgm store over an injected DB connection.

    Lexical SQL joins the coordinate provenance back to Ring-0 ``kp_nodes``;
    the embedding table itself has no source-text column. This scaffold uses
    the Python DB-API surface and imports no PostgreSQL driver.
    """

    def __init__(
        self,
        connection: object,
        *,
        dim: int = 384,
        table_name: str = "rag_chunk_embeddings",
        nodes_table: str = "kp_nodes",
        vector_weight: float = 0.7,
        lexical_weight: float = 0.3,
        retrieval_revision_id: str | None = None,
        embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
    ) -> None:
        if not _SQL_IDENTIFIER.fullmatch(table_name) or not _SQL_IDENTIFIER.fullmatch(
            nodes_table
        ):
            raise ValueError("PostgreSQL table names must be plain identifiers")
        if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
            raise ValueError("PgVectorStore dim must be positive")
        if vector_weight < 0 or lexical_weight < 0 or (
            vector_weight == 0 and lexical_weight == 0
        ):
            raise ValueError("hybrid weights must be non-negative with one enabled")
        self._connection = connection
        self._dim = dim
        self._table = table_name
        self._nodes = nodes_table
        self._vector_weight = float(vector_weight)
        self._lexical_weight = float(lexical_weight)
        self._embedding_model_id = _validated_model_id(embedding_model_id)
        if retrieval_revision_id is not None and not retrieval_revision_id:
            raise ValueError("retrieval_revision_id must not be empty")
        self._retrieval_revision_id = retrieval_revision_id

    def assert_embedding_model(self, embedding_model_id: str) -> None:
        requested = _validated_model_id(embedding_model_id)
        if requested != self._embedding_model_id:
            raise EmbeddingSpaceError(
                "one embedding table is one embedding model: "
                f"configured={self._embedding_model_id!r}, requested={requested!r}"
            )

    @staticmethod
    def _vector_literal(vector: Sequence[float], dim: int) -> str:
        values = _vector_tuple(vector, expected_dim=dim)
        return "[" + ",".join(format(value, ".17g") for value in values) + "]"

    def _cursor(self) -> Any:
        cursor = getattr(self._connection, "cursor", None)
        if not callable(cursor):
            raise TypeError("PgVectorStore requires a Python DB-API connection")
        return cursor()

    def _commit(self) -> None:
        commit = getattr(self._connection, "commit", None)
        if callable(commit):
            commit()

    def _rollback(self) -> None:
        rollback = getattr(self._connection, "rollback", None)
        if callable(rollback):
            rollback()

    def upsert(
        self,
        chunk_content_id: str,
        revision_id: str,
        vector: Sequence[float],
        *,
        content_digest: str,
        provenance: Mapping[str, object],
        content_kind: str = "chunk",
        embedding_model_id: str | None = None,
    ) -> bool:
        kind = _validated_content_kind(content_kind)
        if embedding_model_id is not None:
            self.assert_embedding_model(embedding_model_id)
        existing = self.get(chunk_content_id, revision_id, content_kind=kind)
        record = VectorRecord(
            chunk_content_id=chunk_content_id,
            revision_id=revision_id,
            vector=_vector_tuple(vector, expected_dim=self._dim),
            content_digest=content_digest,
            provenance=_validate_provenance(provenance),
            content_kind=kind,
            embedding_model_id=self._embedding_model_id,
        )
        if existing is not None:
            if existing != record:
                raise VectorConflictError(
                    "immutable vector key already carries different durable data"
                )
            return False
        sql = f"""
            INSERT INTO {self._table}
                (content_kind, chunk_content_id, revision_id, vector,
                 content_digest, provenance, embedding_model_id)
            VALUES (%s, %s, %s, %s::vector, %s, %s::jsonb, %s)
            ON CONFLICT (chunk_content_id, revision_id, content_kind) DO NOTHING
            RETURNING chunk_content_id
        """
        try:
            cursor = self._cursor()
            try:
                cursor.execute(
                    sql,
                    (
                        kind,
                        chunk_content_id,
                        revision_id,
                        self._vector_literal(record.vector, self._dim),
                        content_digest,
                        leaf.canonical_json(record.provenance, ascii=True, allow_nan=True),
                        self._embedding_model_id,
                    ),
                )
                inserted = cursor.fetchone() is not None
            finally:
                cursor.close()
            self._commit()
            if not inserted:
                raced = self.get(chunk_content_id, revision_id, content_kind=kind)
                if raced != record:
                    raise VectorConflictError("concurrent vector key conflict")
            return inserted
        except BaseException:
            self._rollback()
            raise

    def get(
        self,
        chunk_content_id: str,
        revision_id: str,
        *,
        content_kind: str = "chunk",
    ) -> VectorRecord | None:
        kind = _validated_content_kind(content_kind)
        sql = f"""
            SELECT chunk_content_id, revision_id, vector::text,
                   content_digest, provenance, content_kind, embedding_model_id
            FROM {self._table}
            WHERE content_kind = '{kind}'
              AND chunk_content_id = %s AND revision_id = %s
        """
        cursor = self._cursor()
        try:
            cursor.execute(sql, (chunk_content_id, revision_id))
            row = cursor.fetchone()
        finally:
            cursor.close()
        if row is None:
            return None
        vector_text = str(row[2]).strip("[]")
        vector = tuple(float(value) for value in vector_text.split(","))
        provenance = row[4]
        if isinstance(provenance, str):
            provenance = json.loads(provenance)
        if not isinstance(provenance, Mapping):
            raise ValueError("stored vector provenance must be a mapping")
        return VectorRecord(
            str(row[0]),
            str(row[1]),
            vector,
            str(row[3]),
            dict(provenance),
            str(row[5]),
            str(row[6]),
        )

    def document_sources(self, *, repo_key: str) -> tuple[tuple[str, str], ...]:
        cursor = self._cursor()
        try:
            cursor.execute(f"SELECT DISTINCT provenance->>'path', provenance->>'blob_sha' FROM {self._table} WHERE content_kind = 'desk_doc' AND provenance->>'repo_key' = %s ORDER BY 1,2", (repo_key,))
            return tuple(cursor.fetchall())
        finally:
            cursor.close()

    def remove_documents(self, *, repo_key: str, path: str, blob_sha: str | None = None) -> int:
        from kp_agent_tooling_ops._impl.document_identity import document_identity as _identity
        _identity(repo_key, path, blob_sha)
        sql = f"DELETE FROM {self._table} WHERE content_kind = 'desk_doc' AND provenance->>'repo_key' = %s AND provenance->>'path' = %s"
        params = [repo_key, path]
        if blob_sha is not None:
            sql += " AND provenance->>'blob_sha' = %s"
            params.append(blob_sha)
        try:
            cursor = self._cursor()
            try:
                cursor.execute(sql, tuple(params))
                removed = cursor.rowcount
            finally:
                cursor.close()
            self._commit()
            return removed
        except BaseException:
            self._rollback()
            raise

    def _available_revisions(self, *, content_kind: str = "chunk") -> tuple[str, ...]:
        kind = _validated_content_kind(content_kind)
        cursor = self._cursor()
        try:
            cursor.execute(
                f"SELECT DISTINCT revision_id FROM {self._table} "
                f"WHERE content_kind = '{kind}' ORDER BY revision_id"
            )
            return tuple(str(row[0]) for row in cursor.fetchall())
        finally:
            cursor.close()

    def _resolve_revision(
        self, requested: str | None = None, *, content_kind: str = "chunk"
    ) -> str | None:
        # A configured scope is the live fast path: no table-wide revision census
        # precedes the indexed ANN query. Explicit CP-3 calls still validate their
        # requested revision before returning any result.
        if requested is None and self._retrieval_revision_id is not None:
            return self._retrieval_revision_id
        return _select_revision(
            self._available_revisions(content_kind=content_kind),
            requested=requested,
            configured=self._retrieval_revision_id,
        )

    def ann_candidates(
        self,
        vector: Sequence[float],
        *,
        top_k: int,
        revision_id: str | None = None,
        content_kind: str = "chunk",
    ) -> tuple[AnnCandidate, ...]:
        """Use pgvector's KNN index path and return vector-only candidates."""

        limit = _positive_top_k(top_k)
        kind = _validated_content_kind(content_kind)
        selected_revision = self._resolve_revision(revision_id, content_kind=kind)
        if selected_revision is None:
            return ()
        vector_literal = self._vector_literal(vector, self._dim)
        cursor = self._cursor()
        try:
            cursor.execute(
                f"""
                    SELECT chunk_content_id, revision_id,
                           1 - (vector <=> %s::vector) AS vector_score,
                           provenance
                    FROM {self._table}
                    WHERE content_kind = '{kind}'
                      AND revision_id = %s
                    ORDER BY vector <=> %s::vector
                    LIMIT %s
                """,
                (vector_literal, selected_revision, vector_literal, limit),
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()
        candidates: list[AnnCandidate] = []
        for row in rows:
            provenance = row[3]
            if isinstance(provenance, str):
                provenance = json.loads(provenance)
            if not isinstance(provenance, Mapping):
                raise ValueError("stored vector provenance must be a mapping")
            candidates.append(
                AnnCandidate(
                    chunk_content_id=str(row[0]),
                    revision_id=str(row[1]),
                    vector_score=max(-1.0, min(1.0, float(row[2]))),
                    provenance=dict(provenance),
                    content_kind=kind,
                )
            )
        return tuple(candidates)

    def lexical_scores(
        self,
        text: str,
        chunk_content_ids: Sequence[str],
        *,
        revision_id: str | None = None,
        content_kind: str = "chunk",
    ) -> Mapping[str, float]:
        """Resolve Ring-0 text and score only the bounded ANN candidate set."""

        if not isinstance(text, str):
            raise TypeError("query text must be a string")
        requested = _requested_ids(chunk_content_ids)
        kind = _validated_content_kind(content_kind)
        if not requested:
            return {}
        selected_revision = self._resolve_revision(revision_id, content_kind=kind)
        if selected_revision is None:
            return {item: 0.0 for item in requested}
        cursor = self._cursor()
        try:
            cursor.execute(
                f"""
                    SELECT e.chunk_content_id,
                           similarity(
                               %s,
                               convert_from(
                                   substring(
                                       convert_to(a.attributes->>'content', 'UTF8')
                                       FROM (e.provenance->>'byte_offset')::integer + 1
                                       FOR (e.provenance->>'byte_length')::integer
                                   ),
                                   'UTF8'
                               )
                           ) AS lexical_score
                    FROM {self._table} e
                    JOIN {self._nodes} c
                      ON c.id::text = e.provenance->>'chunk_id'
                     AND c.entity_type = 'TranscriptChunk'
                    JOIN {self._nodes} a
                      ON a.id::text = e.provenance->>'content_artifact_id'
                     AND a.entity_type = 'TranscriptContentArtifact'
                    WHERE e.content_kind = '{kind}'
                      AND e.revision_id = %s
                      AND e.chunk_content_id = ANY(%s)
                """,
                (text, selected_revision, list(requested)),
            )
            rows = cursor.fetchall()
        finally:
            cursor.close()
        scores = {item: 0.0 for item in requested}
        for row in rows:
            scores[str(row[0])] = max(0.0, min(1.0, float(row[1])))
        return scores

    def query(
        self,
        vector: Sequence[float],
        text: str,
        *,
        top_k: int,
        revision_id: str,
        content_kind: str = "chunk",
    ) -> tuple[HybridResult, ...]:
        limit = _positive_top_k(top_k)
        kind = _validated_content_kind(content_kind)
        candidates = self.ann_candidates(
            vector, top_k=limit, revision_id=revision_id, content_kind=kind
        )
        if not candidates:
            return ()
        lexical = self.lexical_scores(
            text,
            [candidate.chunk_content_id for candidate in candidates],
            revision_id=revision_id,
            content_kind=kind,
        )
        scored = [
            HybridResult(
                chunk_content_id=candidate.chunk_content_id,
                vector_score=candidate.vector_score,
                lexical_score=lexical[candidate.chunk_content_id],
                combined_score=(
                    self._vector_weight * candidate.vector_score
                    + self._lexical_weight * lexical[candidate.chunk_content_id]
                ),
                content_kind=kind,
            )
            for candidate in candidates
        ]
        scored.sort(key=lambda item: (-item.combined_score, item.chunk_content_id))
        return tuple(scored[:limit])
