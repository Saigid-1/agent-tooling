"""SQLite sidecar for immutable, text-free resolution manifests."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .models import canonical_digest
from kp_agent_tooling._impl import leaf


@dataclass(frozen=True, slots=True)
class ManifestShard:
    shard_id: str
    source_repo_key: str
    document_path: str
    document_blob_sha: str
    chunk_content_id: str
    target_repo_key: str
    target_revision: str
    extraction_policy_version: str
    resolution_policy_version: str
    state: str
    dependency_fingerprint: str
    candidate_total: int
    retained_count: int
    overflow_count: int
    omitted_by_kind: Mapping[str, int]
    unowned_count: int
    integrity_checked: bool
    published: bool
    outcomes: tuple[Mapping[str, object], ...]


class SQLiteReferenceManifestStore:
    def __init__(self, path: Path) -> None:
        self._connection = leaf.sqlite_connect(path, mode='rwc', resolve=True, timeout=30.0, check_same_thread=False)
        self._lock = threading.RLock()
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        self._connection.executescript("""
        CREATE TABLE IF NOT EXISTS reference_manifest_shards (
          shard_id TEXT PRIMARY KEY, source_repo_key TEXT NOT NULL,
          document_path TEXT NOT NULL, document_blob_sha TEXT NOT NULL,
          chunk_content_id TEXT NOT NULL, target_repo_key TEXT NOT NULL,
          target_revision TEXT NOT NULL, extraction_policy_version TEXT NOT NULL,
          resolution_policy_version TEXT NOT NULL, state TEXT NOT NULL,
          dependency_fingerprint TEXT NOT NULL, candidate_total INTEGER NOT NULL,
          retained_count INTEGER NOT NULL, overflow_count INTEGER NOT NULL,
          omitted_by_kind_json TEXT NOT NULL, unowned_count INTEGER NOT NULL,
          integrity_checked INTEGER NOT NULL, published INTEGER NOT NULL,
          outcomes_json TEXT NOT NULL,
          UNIQUE(source_repo_key,document_path,document_blob_sha,chunk_content_id,target_repo_key,
                 target_revision,extraction_policy_version,resolution_policy_version));
        CREATE INDEX IF NOT EXISTS reference_manifest_lookup ON reference_manifest_shards
          (source_repo_key,document_path,document_blob_sha,chunk_content_id,target_repo_key,target_revision);
        """)

    def publish(self, shard: ManifestShard) -> bool:
        _validate_outcomes(shard.outcomes)
        payload = leaf.canonical_json(shard.outcomes, ascii=True, allow_nan=True)
        # BEGIN IMMEDIATE establishes the single-writer order before the read.  A
        # partial replacement is therefore one atomic commit to other processes,
        # and a killed/interrupted writer leaves the prior row intact.
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                created = self._publish_in_transaction(shard, payload)
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            return created

    def _publish_in_transaction(self, shard: ManifestShard, payload: str) -> bool:
        existing = self._connection.execute(
            "SELECT shard_id,outcomes_json,state,dependency_fingerprint,candidate_total,retained_count,"
            "overflow_count,omitted_by_kind_json,unowned_count,integrity_checked,published "
            "FROM reference_manifest_shards "
            "WHERE source_repo_key=? AND document_path=? AND document_blob_sha=? AND chunk_content_id=? "
            "AND target_repo_key=? AND target_revision=? AND extraction_policy_version=? "
            "AND resolution_policy_version=?", _key(shard)).fetchone()
        if existing is not None:
            expected = (shard.shard_id, payload, shard.state, shard.dependency_fingerprint,
                shard.candidate_total, shard.retained_count, shard.overflow_count,
                leaf.canonical_json(shard.omitted_by_kind, ascii=True, allow_nan=True),
                shard.unowned_count, int(shard.integrity_checked), int(shard.published))
            if existing != expected:
                # Pre full-inventory shards stored per-target subset counts. Keep a
                # completed historical claim byte-for-byte when every semantic field
                # except those two legacy counters is identical. Query aggregation
                # treats these rows as the legacy counter format.
                if (existing[2] == shard.state == "complete" and
                        existing[:4] + existing[6:] == expected[:4] + expected[6:]):
                    return False
                if existing[2] == "partial" and shard.state in {"partial", "complete"}:
                    _assert_completed_outcomes_unchanged(existing[1], payload)
                    self._connection.execute("DELETE FROM reference_manifest_shards WHERE shard_id=?",
                                             (existing[0],))
                else:
                    raise ValueError("conflicting reference manifest shard")
            else:
                return False
        self._connection.execute("INSERT INTO reference_manifest_shards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (shard.shard_id, *_key(shard), shard.state, shard.dependency_fingerprint,
             shard.candidate_total, shard.retained_count, shard.overflow_count,
             leaf.canonical_json(shard.omitted_by_kind, ascii=True, allow_nan=True),
             shard.unowned_count, int(shard.integrity_checked), int(shard.published), payload))
        return True

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def all_shards(self) -> tuple[ManifestShard, ...]:
        with self._lock:
            return self._all_shards()

    def _all_shards(self) -> tuple[ManifestShard, ...]:
        keys = self._connection.execute("SELECT source_repo_key,document_path,document_blob_sha,"
            "chunk_content_id,target_repo_key,target_revision,extraction_policy_version,"
            "resolution_policy_version FROM reference_manifest_shards ORDER BY shard_id").fetchall()
        result = []
        for key in keys:
            shard = self.select(source_repo_key=key[0], document_path=key[1],
                document_blob_sha=key[2], chunk_content_id=key[3], target_repo_key=key[4],
                target_revision=key[5], extraction_policy_version=key[6],
                resolution_policy_version=key[7])
            if shard is not None:
                result.append(shard)
        return tuple(result)

    def shards_for_revisions(self, *, source_repo_key: str,
                             target_revisions: Mapping[str, object]) -> tuple[ManifestShard, ...]:
        """Load only the explicitly selected query scope for a frozen generation."""
        with self._lock:
            return self._shards_for_revisions(source_repo_key=source_repo_key,
                                              target_revisions=target_revisions)

    def _shards_for_revisions(self, *, source_repo_key: str,
                              target_revisions: Mapping[str, object]) -> tuple[ManifestShard, ...]:
        result = []
        for target_repo_key, revisions in sorted(target_revisions.items()):
            values = (revisions,) if isinstance(revisions, str) else tuple(revisions)  # type: ignore[arg-type]
            for target_revision in sorted(set(values)):
                keys = self._connection.execute(
                "SELECT source_repo_key,document_path,document_blob_sha,chunk_content_id,"
                "target_repo_key,target_revision,extraction_policy_version,resolution_policy_version "
                "FROM reference_manifest_shards WHERE source_repo_key=? AND target_repo_key=? "
                "AND target_revision=? ORDER BY shard_id",
                    (source_repo_key, target_repo_key, target_revision)).fetchall()
                for key in keys:
                    item = self.select(source_repo_key=key[0], document_path=key[1],
                        document_blob_sha=key[2], chunk_content_id=key[3], target_repo_key=key[4],
                        target_revision=key[5], extraction_policy_version=key[6],
                        resolution_policy_version=key[7])
                    if item is not None:
                        result.append(item)
        return tuple(result)

    def select(self, *, source_repo_key: str, document_path: str, document_blob_sha: str,
               chunk_content_id: str, target_repo_key: str, target_revision: str,
               extraction_policy_version: str, resolution_policy_version: str) -> ManifestShard | None:
        with self._lock:
            return self._select(source_repo_key=source_repo_key, document_path=document_path,
                document_blob_sha=document_blob_sha, chunk_content_id=chunk_content_id,
                target_repo_key=target_repo_key, target_revision=target_revision,
                extraction_policy_version=extraction_policy_version,
                resolution_policy_version=resolution_policy_version)

    def _select(self, *, source_repo_key: str, document_path: str, document_blob_sha: str,
                chunk_content_id: str, target_repo_key: str, target_revision: str,
                extraction_policy_version: str, resolution_policy_version: str) -> ManifestShard | None:
        key = (source_repo_key, document_path, document_blob_sha, chunk_content_id,
               target_repo_key, target_revision, extraction_policy_version, resolution_policy_version)
        rows = self._connection.execute("SELECT shard_id,state,dependency_fingerprint,candidate_total,"
            "retained_count,overflow_count,omitted_by_kind_json,unowned_count,integrity_checked,"
            "published,outcomes_json "
            "FROM reference_manifest_shards WHERE source_repo_key=? AND document_path=? AND document_blob_sha=? "
            "AND chunk_content_id=? AND target_repo_key=? AND target_revision=? AND extraction_policy_version=? "
            "AND resolution_policy_version=?", key).fetchall()
        if len(rows) > 1:
            raise ValueError("conflicting reference manifest shards")
        if not rows:
            return None
        shard_id, state, fingerprint, total, retained, overflow, omitted, unowned, integrity, published, payload = rows[0]
        return ManifestShard(shard_id, *key, state, fingerprint, total, retained, overflow,
                             json.loads(omitted), unowned, bool(integrity), bool(published),
                             tuple(json.loads(payload)))

    @staticmethod
    def retrieval_view(shard: ManifestShard, graph: object) -> dict[str, object]:
        outcomes, positive = [], []
        for item in shard.outcomes:
            row = dict(item)
            edge_ids = tuple(row.pop("edge_ids", ()))
            if row.get("status") == "resolved":
                edges = []
                for edge_id in edge_ids:
                    edge = graph.adapter.get_edge(edge_id)
                    if edge is None:
                        raise ValueError("resolved manifest record is missing its graph edge")
                    edges.append(edge)
                positive.extend(edges)
            outcomes.append(row)
        return {"shard_id": shard.shard_id, "state": shard.state,
                "candidate_total": shard.candidate_total, "retained_count": shard.retained_count,
                "overflow_count": shard.overflow_count, "omitted_by_kind": dict(shard.omitted_by_kind),
                "unowned_count": shard.unowned_count,
                "integrity_checked": shard.integrity_checked, "published": shard.published,
                "outcomes": outcomes, "positive_edges": positive}


class FrozenReferenceRetrieval:
    """Composition-time snapshot; query operations are bounded dictionary reads."""

    query_bounded = True

    def __init__(self, shards: tuple[ManifestShard, ...], views: Mapping[str, object], *,
                 source_repo_key: str, target_revisions: Mapping[str, object]):
        frozen_shards = deepcopy(shards)
        self._shards = {_key(item): item for item in frozen_shards}
        self._views = deepcopy(dict(views))
        normalized_revisions = {key: sorted((value,) if isinstance(value, str) else value)
                                for key, value in sorted(target_revisions.items())}
        effective_shards = [{"shard_id": item.shard_id,
            "view_digest": canonical_digest("frozen-reference-view", self._views[item.shard_id])}
            for item in sorted(frozen_shards, key=lambda value: value.shard_id)]
        self._generation_metadata = {"source_repo_key": source_repo_key,
            "target_revisions": normalized_revisions,
            "shard_count": len(shards),
            "generation_digest": canonical_digest("frozen-reference-generation", {
                "source_repo_key": source_repo_key,
                "target_revisions": normalized_revisions,
                "effective_shards": effective_shards})}

    @property
    def generation_metadata(self) -> dict[str, object]:
        return deepcopy(self._generation_metadata)

    def select(self, **values: object) -> ManifestShard | None:
        key = tuple(values[name] for name in ("source_repo_key", "document_path",
            "document_blob_sha", "chunk_content_id", "target_repo_key", "target_revision",
            "extraction_policy_version", "resolution_policy_version"))
        return deepcopy(self._shards.get(key))

    def retrieval_view(self, shard: ManifestShard, graph: object) -> dict[str, object]:
        return deepcopy(self._views[shard.shard_id])  # type: ignore[return-value]


class AtomicReferenceGeneration:
    """Publish a fully composed serving generation with one atomic pointer swap."""

    query_bounded = True

    def __init__(self, initial: FrozenReferenceRetrieval) -> None:
        self._current = initial
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    def snapshot(self) -> FrozenReferenceRetrieval:
        with self._lock:
            return self._current

    def refresh(self, store: SQLiteReferenceManifestStore, graph: object, *,
                source_repo_key: str, target_revisions: Mapping[str, object]
                ) -> FrozenReferenceRetrieval:
        # Verification and view materialization happen before publication. Requests
        # continue using the last complete frozen object if this work raises.
        with self._refresh_lock:
            candidate = freeze_reference_retrieval(store, graph, source_repo_key=source_repo_key,
                                                   target_revisions=target_revisions)
            with self._lock:
                self._current = candidate
        return candidate


def freeze_reference_retrieval(store: SQLiteReferenceManifestStore, graph: object, *,
                               source_repo_key: str | None = None,
                               target_revisions: Mapping[str, object] | None = None
                               ) -> FrozenReferenceRetrieval:
    """Resolve graph dependencies before serving any latency-bounded query."""
    if source_repo_key is None or target_revisions is None:
        raise ValueError("frozen reference retrieval requires an explicit source and revision scope")
    shards = store.shards_for_revisions(source_repo_key=source_repo_key,
                                        target_revisions=target_revisions)
    views: dict[str, object] = {}
    for shard in shards:
        try:
            view = store.retrieval_view(shard, graph)
        except Exception:
            views[shard.shard_id] = _partial_graph_view(shard)
            continue
        edges = []
        invalid = False
        resolved_ids = {row.get("resolution_id") for row in shard.outcomes
                        if row.get("status") == "resolved"}
        for original in view["positive_edges"]:
            edge = dict(original)
            edge_attrs = edge.get("attributes", edge)
            target_id = edge.get("to_id") or edge.get("to") or edge.get("target_id")
            try:
                node = graph.entities.get(target_id) if target_id else None
            except Exception:
                invalid = True
                break
            attrs = (node or {}).get("attributes", {})
            change = {}
            if (node or {}).get("entity_type") == "CodeSymbol":
                try:
                    source = graph.entities.get(attrs.get("source_change_id"))
                except Exception:
                    invalid = True
                    break
                change = (source or {}).get("attributes", {})
            elif node:
                change = attrs
                attrs = {}
            relation = edge.get("relationship_type") or edge.get("type")
            source_id = edge.get("from_id") or edge.get("from") or edge.get("source_id")
            if (relation not in {"REFERENCES_SYMBOL", "REFERENCES_ARTIFACT"} or
                    source_id != shard.chunk_content_id or
                    edge_attrs.get("resolution_id") not in resolved_ids or
                    edge_attrs.get("target_repo_key") != shard.target_repo_key or
                    edge_attrs.get("resolved_code_revision") != shard.target_revision or
                    change.get("repo_key") != shard.target_repo_key or
                    change.get("commit_sha") != shard.target_revision or
                    change.get("blob_sha") != edge_attrs.get("resolved_blob_sha")):
                invalid = True
                break
            edge["_target_symbol"] = dict(attrs)
            edge["_target_change"] = dict(change)
            edges.append(edge)
        if invalid:
            views[shard.shard_id] = _partial_graph_view(shard)
            continue
        view = dict(view)
        view["positive_edges"] = edges
        views[shard.shard_id] = view
    return FrozenReferenceRetrieval(shards, views, source_repo_key=source_repo_key,
                                    target_revisions=target_revisions)


def _partial_graph_view(shard: ManifestShard) -> dict[str, object]:
    outcomes = []
    for original in shard.outcomes:
        row = dict(original)
        row.pop("edge_ids", None)
        if row.get("status") == "resolved":
            row.update(execution_state="error", status=None,
                       reason="graph_snapshot_incomplete")
        outcomes.append(row)
    return {"shard_id": shard.shard_id, "state": "partial",
            "outcomes": outcomes, "positive_edges": []}


def build_shard(**values: object) -> ManifestShard:
    identity = {key: values[key] for key in ("source_repo_key", "document_path", "document_blob_sha",
        "chunk_content_id", "target_repo_key", "target_revision", "extraction_policy_version",
        "resolution_policy_version", "dependency_fingerprint")}
    return ManifestShard(canonical_digest("reference-manifest", identity), **values)  # type: ignore[arg-type]


def _key(shard: ManifestShard) -> tuple[str, ...]:
    return (shard.source_repo_key, shard.document_path, shard.document_blob_sha,
            shard.chunk_content_id, shard.target_repo_key, shard.target_revision,
            shard.extraction_policy_version, shard.resolution_policy_version)


def _assert_completed_outcomes_unchanged(existing_payload: str, new_payload: str) -> None:
    def completed(payload: str) -> dict[tuple[object, object], object]:
        rows = json.loads(payload)
        return {(row.get("occurrence_id"), row.get("resolution_id")): row for row in rows
                if row.get("execution_state") == "complete"}
    old = completed(existing_payload)
    new = completed(new_payload)
    if any(new.get(identity) != row for identity, row in old.items()):
        raise ValueError("completed reference outcomes are immutable within one policy")


def _validate_outcomes(outcomes: tuple[Mapping[str, object], ...]) -> None:
    prohibited = {"literal", "lookup_value", "text", "document_bytes", "query"}
    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            if prohibited.intersection(value):
                raise ValueError("reference manifest contains prohibited document-derived payload")
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
    visit(outcomes)
