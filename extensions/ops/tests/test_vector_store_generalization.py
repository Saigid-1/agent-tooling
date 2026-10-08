"""DB-free contract for one content-kind-agnostic embedding space."""

from __future__ import annotations

from pathlib import Path

import pytest

from kp_agent_tooling_ops._impl.embeddings.store import (
    EmbeddingSpaceError,
    MemoryVectorStore,
    PgVectorStore,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    REPOSITORY_ROOT / "migrations" / "20260830_vector_store_generalize.sql"
)
REVISION_ID = "embedding-revision:golden-v1"
MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"


def _upsert_golden_chunks(store: MemoryVectorStore) -> None:
    rows = (
        ("shared-a", (1.0, 0.0), "alpha launch checklist"),
        ("chunk-b", (0.8, 0.2), "beta incident review"),
        ("chunk-c", (0.0, 1.0), "gamma roadmap"),
    )
    for content_id, vector, _text in rows:
        assert store.upsert(
            content_id,
            REVISION_ID,
            vector,
            content_digest=content_id,
            provenance={"chunk_id": content_id},
            embedding_model_id=MODEL_ID,
        )


def test_golden_chunk_queries_are_identical_before_and_after_generalization() -> None:
    """The default chunk path is a byte-for-byte semantic parity probe."""

    documents = {
        "shared-a": "alpha launch checklist",
        "chunk-b": "beta incident review",
        "chunk-c": "gamma roadmap",
    }
    before = MemoryVectorStore(lexical_resolver=documents.get)
    after = MemoryVectorStore(lexical_resolver=documents.get)
    _upsert_golden_chunks(before)
    _upsert_golden_chunks(after)

    # The colliding card would win both golden ANN queries if the default
    # content_kind filter were absent.
    assert after.upsert(
        "shared-a",
        REVISION_ID,
        (1.0, 1.0),
        content_digest="card-shared-a",
        provenance={"card_id": "shared-a"},
        content_kind="card",
        embedding_model_id=MODEL_ID,
    )

    golden_queries = (
        ((1.0, 0.0), "alpha launch", 3),
        ((0.0, 1.0), "gamma roadmap", 2),
        ((0.7, 0.3), "incident", 1),
    )
    for vector, text, top_k in golden_queries:
        legacy_result = before.query(
            vector, text, top_k=top_k, revision_id=REVISION_ID
        )
        default_chunk_result = after.query(
            vector, text, top_k=top_k, revision_id=REVISION_ID
        )
        explicit_chunk_result = after.query(
            vector,
            text,
            top_k=top_k,
            revision_id=REVISION_ID,
            content_kind="chunk",
        )
        assert default_chunk_result == legacy_result
        assert explicit_chunk_result == legacy_result


def test_content_kind_extends_the_key_and_filters_results() -> None:
    store = MemoryVectorStore(embedding_model_id=MODEL_ID)
    common = {
        "content_digest": "same-id-different-kind",
        "provenance": {"coordinate": "same-id"},
        "embedding_model_id": MODEL_ID,
    }
    assert store.upsert("same-id", REVISION_ID, (1.0, 0.0), **common)
    assert store.upsert(
        "same-id",
        REVISION_ID,
        (0.0, 1.0),
        content_kind="card",
        **common,
    )

    chunk = store.get("same-id", REVISION_ID)
    card = store.get("same-id", REVISION_ID, content_kind="card")
    assert chunk is not None and chunk.content_kind == "chunk"
    assert card is not None and card.content_kind == "card"
    assert chunk.vector != card.vector
    chunk_candidates = store.ann_candidates(
        (1.0, 1.0), top_k=10, revision_id=REVISION_ID, content_kind="chunk"
    )
    card_candidates = store.ann_candidates(
        (1.0, 1.0), top_k=10, revision_id=REVISION_ID, content_kind="card"
    )
    card_results = store.query(
        (1.0, 1.0),
        "",
        top_k=10,
        revision_id=REVISION_ID,
        content_kind="card",
    )
    assert {candidate.content_kind for candidate in chunk_candidates} == {"chunk"}
    assert {candidate.content_kind for candidate in card_candidates} == {"card"}
    assert {result.content_kind for result in card_results} == {"card"}


class _Cursor:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection

    def execute(self, sql: str, params: object = None) -> None:
        self.connection.executions.append((sql, params))

    def fetchall(self) -> list[tuple[object, ...]]:
        return [("same-id", REVISION_ID, 0.75, {"card_id": "same-id"})]

    def close(self) -> None:
        return None


class _Connection:
    def __init__(self) -> None:
        self.executions: list[tuple[str, object]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


@pytest.mark.skip(reason='S5 excluded: reads OPS-only PostgreSQL migration migrations/20260830_vector_store_generalize.sql')
def test_pg_kind_filter_is_planner_visible_and_has_per_kind_partial_indexes() -> None:
    connection = _Connection()
    store = PgVectorStore(
        connection,
        retrieval_revision_id=REVISION_ID,
        embedding_model_id=MODEL_ID,
    )
    candidates = store.ann_candidates(
        (1.0,) + (0.0,) * 383, top_k=4, content_kind="card"
    )
    ann_sql, _params = connection.executions[-1]
    assert "content_kind = 'card'" in ann_sql
    assert {candidate.content_kind for candidate in candidates} == {"card"}

    sql = MIGRATION.read_text(encoding="utf-8")
    normalized = " ".join(sql.lower().split())
    assert "content_kind text not null default 'chunk'" in normalized
    assert "primary key (chunk_content_id, revision_id, content_kind)" in normalized
    assert (
        "using hnsw (vector vector_cosine_ops) where content_kind = 'chunk'"
        in normalized
    )
    assert (
        "using hnsw (vector vector_cosine_ops) where content_kind = 'card'"
        in normalized
    )
    assert "post-filter" in sql.lower()
    assert "recall" in sql.lower()


@pytest.mark.skip(reason='S5 excluded: reads OPS-only PostgreSQL migration migrations/20260830_vector_store_generalize.sql')
def test_one_table_one_model_space_refuses_mismatched_model_writes() -> None:
    store = MemoryVectorStore(embedding_model_id=MODEL_ID)
    assert store.upsert(
        "chunk-a",
        REVISION_ID,
        (1.0, 0.0),
        content_digest="chunk-a",
        provenance={"chunk_id": "chunk-a"},
        embedding_model_id=MODEL_ID,
    )
    with pytest.raises(EmbeddingSpaceError, match="one embedding model"):
        store.upsert(
            "card-a",
            REVISION_ID,
            (0.0, 1.0),
            content_digest="card-a",
            provenance={"card_id": "card-a"},
            content_kind="card",
            embedding_model_id="different/model",
        )

    sql = MIGRATION.read_text(encoding="utf-8")
    assert "embedding_model_id" in sql
    assert MODEL_ID in sql
    assert "44,210" in sql
    assert "ACCESS EXCLUSIVE" in sql
