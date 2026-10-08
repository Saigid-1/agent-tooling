from __future__ import annotations

import threading

import pytest

from kp_agent_tooling_ops._impl.code_references.manifest import (
    AtomicReferenceGeneration,
    FrozenReferenceRetrieval,
    SQLiteReferenceManifestStore,
    build_shard,
    freeze_reference_retrieval,
)


def _values(*, state="complete", dependency="ready", outcomes=()):
    return dict(
        source_repo_key="ops", document_path="docs/a.md", document_blob_sha="a" * 40,
        chunk_content_id="chunk", target_repo_key="ops", target_revision="b" * 40,
        extraction_policy_version="e1", resolution_policy_version="r1", state=state,
        dependency_fingerprint=dependency, candidate_total=1, retained_count=1,
        overflow_count=0, omitted_by_kind={}, unowned_count=0,
        integrity_checked=True, published=True, outcomes=outcomes,
    )


def test_effective_graph_integrity_changes_frozen_generation(tmp_path):
    outcome = {"occurrence_id": "occ", "resolution_id": "resolution",
               "execution_state": "complete", "status": "resolved", "edge_ids": ["edge"]}
    shard = build_shard(**_values(outcomes=(outcome,)))
    store = SQLiteReferenceManifestStore(tmp_path / "manifest.sqlite")
    store.publish(shard)

    class Adapter:
        def __init__(self):
            self.edge = {"id": "edge", "relationship_type": "REFERENCES_ARTIFACT",
                "from_id": "chunk", "to_id": "change", "attributes": {
                    "resolution_id": "resolution", "target_repo_key": "ops",
                    "resolved_code_revision": "b" * 40, "resolved_blob_sha": "c" * 40}}

        def get_edge(self, edge_id):
            return self.edge if edge_id == "edge" else None

    class Entities:
        def get(self, node_id):
            if node_id == "change":
                return {"entity_type": "Change", "attributes": {"repo_key": "ops",
                    "commit_sha": "b" * 40, "blob_sha": "c" * 40}}
            return None

    class Graph:
        adapter = Adapter()
        entities = Entities()

    graph = Graph()
    complete = freeze_reference_retrieval(store, graph, source_repo_key="ops",
                                           target_revisions={"ops": "b" * 40})
    graph.adapter.edge = None
    partial = freeze_reference_retrieval(store, graph, source_repo_key="ops",
                                          target_revisions={"ops": "b" * 40})
    assert complete.generation_metadata["generation_digest"] != partial.generation_metadata["generation_digest"]
    assert complete.retrieval_view(shard, graph)["state"] == "complete"
    assert partial.retrieval_view(shard, graph)["state"] == "partial"


def test_frozen_generation_is_defensive_against_caller_mutation(tmp_path):
    outcome = {"occurrence_id": "occ", "execution_state": "complete", "status": "unresolved"}
    shard = build_shard(**_values(outcomes=(outcome,)))
    view = {"shard_id": shard.shard_id, "state": "complete", "outcomes": [outcome],
            "positive_edges": [], "endpoint": {"blob_sha": "c" * 40}}
    frozen = FrozenReferenceRetrieval((shard,), {shard.shard_id: view}, source_repo_key="ops",
                                      target_revisions={"ops": "b" * 40})
    digest = frozen.generation_metadata["generation_digest"]
    outcome["status"] = "resolved"
    view["state"] = "partial"
    selected = frozen.select(source_repo_key="ops", document_path="docs/a.md",
        document_blob_sha="a" * 40, chunk_content_id="chunk", target_repo_key="ops",
        target_revision="b" * 40, extraction_policy_version="e1", resolution_policy_version="r1")
    assert selected is not None
    selected.outcomes[0]["status"] = "resolved"
    returned = frozen.retrieval_view(selected, None)
    returned["endpoint"]["blob_sha"] = "d" * 40
    assert frozen.generation_metadata["generation_digest"] == digest
    assert frozen.select(**{name: getattr(shard, name) for name in (
        "source_repo_key", "document_path", "document_blob_sha", "chunk_content_id",
        "target_repo_key", "target_revision", "extraction_policy_version",
        "resolution_policy_version")}).outcomes[0]["status"] == "unresolved"
    assert frozen.retrieval_view(shard, None)["endpoint"]["blob_sha"] == "c" * 40


def test_interrupted_partial_replacement_rolls_back_to_prior_generation(tmp_path, monkeypatch):
    store = SQLiteReferenceManifestStore(tmp_path / "manifest.sqlite")
    partial = build_shard(**_values(state="partial", dependency="failed",
        outcomes=({"occurrence_id": "occ", "execution_state": "error"},)))
    complete = build_shard(**_values(outcomes=({"occurrence_id": "occ",
        "execution_state": "complete", "status": "unresolved"},)))
    store.publish(partial)
    original = store._publish_in_transaction

    def interrupted(shard, payload):
        original(shard, payload)
        raise KeyboardInterrupt("simulated termination before commit")

    monkeypatch.setattr(store, "_publish_in_transaction", interrupted)
    with pytest.raises(KeyboardInterrupt):
        store.publish(complete)
    assert store.all_shards() == (partial,)


def test_concurrent_identical_writers_publish_once(tmp_path):
    path = tmp_path / "manifest.sqlite"
    left, right = SQLiteReferenceManifestStore(path), SQLiteReferenceManifestStore(path)
    shard = build_shard(**_values(outcomes=({"occurrence_id": "occ",
        "execution_state": "complete", "status": "unresolved"},)))
    barrier = threading.Barrier(2)
    results, errors = [], []

    def publish(store):
        try:
            barrier.wait()
            results.append(store.publish(shard))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=publish, args=(store,)) for store in (left, right)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert errors == []
    assert sorted(results) == [False, True]
    assert left.all_shards() == (shard,)


def test_same_store_reader_cannot_observe_uncommitted_replacement(tmp_path, monkeypatch):
    store = SQLiteReferenceManifestStore(tmp_path / "manifest.sqlite")
    partial = build_shard(**_values(state="partial", dependency="failed",
        outcomes=({"occurrence_id": "occ", "execution_state": "error"},)))
    complete = build_shard(**_values(outcomes=({"occurrence_id": "occ",
        "execution_state": "complete", "status": "unresolved"},)))
    store.publish(partial)
    changed = threading.Event()
    release = threading.Event()
    original = store._publish_in_transaction

    def paused(shard, payload):
        result = original(shard, payload)
        changed.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(store, "_publish_in_transaction", paused)
    writer = threading.Thread(target=store.publish, args=(complete,))
    writer.start()
    assert changed.wait(5)
    observed = []
    reader = threading.Thread(target=lambda: observed.extend(store.all_shards()))
    reader.start()
    reader.join(timeout=0.05)
    assert reader.is_alive()
    release.set()
    writer.join(timeout=5)
    reader.join(timeout=5)
    assert observed == [complete]


def test_atomic_generation_keeps_last_snapshot_when_refresh_fails(monkeypatch):
    first = object.__new__(FrozenReferenceRetrieval)
    serving = AtomicReferenceGeneration(first)
    monkeypatch.setattr("kp_agent_tooling_ops._impl.code_references.manifest.freeze_reference_retrieval",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("verify failed")))
    with pytest.raises(RuntimeError, match="verify failed"):
        serving.refresh(object(), object(), source_repo_key="ops", target_revisions={"ops": "b" * 40})
    assert serving.snapshot() is first


def test_atomic_generation_serializes_refresh_publication(monkeypatch):
    initial = object.__new__(FrozenReferenceRetrieval)
    first, second = object.__new__(FrozenReferenceRetrieval), object.__new__(FrozenReferenceRetrieval)
    serving = AtomicReferenceGeneration(initial)
    entered = threading.Event()
    release = threading.Event()

    def compose(store, *args, **kwargs):
        if store == "first":
            entered.set()
            assert release.wait(5)
            return first
        return second

    monkeypatch.setattr("kp_agent_tooling_ops._impl.code_references.manifest.freeze_reference_retrieval", compose)
    one = threading.Thread(target=serving.refresh, args=("first", None),
        kwargs={"source_repo_key": "ops", "target_revisions": {"ops": "a" * 40}})
    two = threading.Thread(target=serving.refresh, args=("second", None),
        kwargs={"source_repo_key": "ops", "target_revisions": {"ops": "b" * 40}})
    one.start()
    assert entered.wait(5)
    two.start()
    release.set()
    one.join(timeout=5)
    two.join(timeout=5)
    assert serving.snapshot() is second


def test_legacy_complete_subset_counters_replay_without_rewrite(tmp_path):
    store = SQLiteReferenceManifestStore(tmp_path / "manifest.sqlite")
    outcomes = ({"occurrence_id": "occ", "execution_state": "complete",
                 "status": "unresolved"},)
    legacy = build_shard(**_values(outcomes=outcomes))
    current = build_shard(**{**_values(outcomes=outcomes),
                             "candidate_total": 2, "retained_count": 2})
    assert legacy.shard_id == current.shard_id
    assert store.publish(legacy) is True
    assert store.publish(current) is False
    assert store.all_shards() == (legacy,)
