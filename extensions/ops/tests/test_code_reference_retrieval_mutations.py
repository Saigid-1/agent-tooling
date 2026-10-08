"""Mutation guards for retrieval invariants that must never silently weaken."""

from kp_agent_tooling_ops._impl.code_references.adapters import (BaselineSelection, FrozenNavigationReadiness,
                                             RevisionSelection)
from kp_agent_tooling_ops._impl.code_references.manifest import FrozenReferenceRetrieval, ManifestShard
from kp_agent_tooling_ops._impl.code_references.retrieval import _TreeMetadata, _row
from kp_agent_tooling_ops._impl.code_references.retrieval import attach_code_references
from kp_agent_tooling_ops._impl.code_references.extraction import EXTRACTION_POLICY_VERSION
from kp_agent_tooling_ops._impl.code_references.resolution import RESOLUTION_POLICY_VERSION


SHA = "a" * 40
BASE = "b" * 40
BLOB = "c" * 40
ROW = {"repo_key": "docs", "path": "guide.md", "blob_sha": "d" * 40,
       "byte_offset": 0, "byte_length": 20}


def shard(revision, status="unresolved"):
    outcome = {"occurrence_id": "occ", "resolution_id": "res", "kind": "path",
               "ref_byte_offset": 1, "ref_byte_length": 6, "execution_state": "complete",
               "status": status, "reason": None, "match_count": 1, "truncated": False}
    return ManifestShard("shard-" + revision[0], "docs", "guide.md", "d" * 40,
        "deskdoc:" + "0" * 64, "code", revision, EXTRACTION_POLICY_VERSION,
        RESOLUTION_POLICY_VERSION, "complete", "inputs", 1, 1, 0, {}, 0,
        True, True, (outcome,))


class Store(FrozenReferenceRetrieval):
    query_bounded = True
    def __init__(self, exact, baseline=None, edge=None):
        self.exact, self.baseline, self.edge = exact, baseline, edge
        self.calls = []

    def resolve(self, *args, **kwargs):
        raise AssertionError("query-time resolution is forbidden")

    def select(self, **kwargs):
        self.calls.append(kwargs["target_revision"])
        return self.exact if kwargs["target_revision"] == SHA else self.baseline

    def retrieval_view(self, selected, graph):
        edge = dict(self.edge) if self.edge else None
        if edge:
            edge.setdefault("_target_symbol", {})
            edge.setdefault("_target_change", {"file_path": "api.py", "blob_sha": BLOB})
        return {"state": "complete", "outcomes": list(selected.outcomes),
                "positive_edges": [edge] if edge else []}


class Entities:
    def __init__(self, values): self.values = values
    def get(self, key): return self.values.get(key)


class Graph:
    def __init__(self):
        self.entities = Entities({"change": {"entity_type": "Change", "attributes": {
            "file_path": "api.py", "blob_sha": BLOB}}})


class Navigation(FrozenNavigationReadiness):
    def __init__(self, *, revision=SHA, blob=BLOB):
        self.revision, self.blob = revision, blob
    def tree_blobs(self, repo, revision):
        return {"api.py": self.blob} if revision == self.revision else None
    def continuation(self, **kwargs):
        if kwargs["revision"] != self.revision or kwargs["blob_sha"] != self.blob:
            return None
        return {"operation": "navigation.source", "arguments": kwargs}


def run(store, navigation):
    return _row(ROW, source_repo_key="docs", repositories={},
        selected={"code": RevisionSelection("code", SHA, "explicit")},
        baselines={"code": BaselineSelection(BASE, True)}, store=store, graph=Graph(),
        navigation=navigation, diagnostics_budget=200, references_budget=200,
        tree=_TreeMetadata({}, navigation), telemetry=None)


def test_exact_negative_shadows_baseline_and_never_invokes_query_resolver():
    store = Store(shard(SHA), shard(BASE, "resolved"), {"to_id": "change",
        "attributes": {"resolution_id": "res", "resolved_blob_sha": BLOB,
                       "target_start_line": 1, "target_end_line": 1}})
    coverage, references = run(store, Navigation())
    assert store.calls == [SHA]
    assert references == []
    assert coverage["diagnostics"][0]["status"] == "unresolved"


def test_missing_freshness_evidence_is_not_trusted_for_navigation():
    edge = {"to_id": "change", "attributes": {"resolution_id": "res",
        "resolved_blob_sha": BLOB, "target_start_line": 1, "target_end_line": 1}}
    _, references = run(Store(shard(SHA, "resolved"), edge=edge), Navigation(revision=BASE))
    reference = references[0]
    assert reference["target_file_freshness"] == "not_checked"
    assert reference["continuation"] is None
    assert reference["action_required"] == "retry_target_verification"


def test_snapshot_blob_substitution_cannot_produce_continuation():
    edge = {"to_id": "change", "attributes": {"resolution_id": "res",
        "resolved_blob_sha": BLOB, "target_start_line": 1, "target_end_line": 1}}
    _, references = run(Store(shard(SHA, "resolved"), edge=edge),
                        Navigation(blob="e" * 40))
    reference = references[0]
    assert reference["target_file_freshness"] == "changed_since_resolution"
    assert reference["continuation"] is None
    assert reference["navigation_status"]["state"] == "unavailable"


def test_fresh_historical_blob_does_not_promote_resolution_validity():
    edge = {"to_id": "change", "attributes": {"resolution_id": "res",
        "resolved_blob_sha": BLOB, "target_start_line": 1, "target_end_line": 1}}
    _, references = run(Store(None, shard(BASE, "resolved"), edge=edge), Navigation())
    reference = references[0]
    assert reference["target_file_freshness"] == "current"
    assert reference["resolution_validity"] == "historical_only"
    assert reference["continuation"] is None


def test_unbounded_adapter_fails_closed_without_calling_it():
    class SlowUnsupported:
        query_bounded = True  # a self-asserted marker must not grant trust
        def select(self, **kwargs):
            raise AssertionError("unsupported external adapter was invoked")
    report = {"results": [dict(ROW)]}
    attach_code_references(report, source_repo_key="docs", repositories={},
        store=SlowUnsupported(), graph=None, navigation=Navigation(), code_revisions=None,
        reference_baseline_revisions=None, prepared=(
            {"code": RevisionSelection("code", SHA, "explicit")}, {}, 0.0))
    assert report["code_reference_context"]["bounded_work"]["adapter_reason"] == \
        "reference_adapter_not_query_bounded"
    assert report["results"][0]["code_reference_coverage"]["state"] == "unavailable"


def test_exhausted_shared_budget_never_starts_git_tree_read(monkeypatch):
    import kp_agent_tooling_ops._impl.code_references.retrieval as retrieval
    clock = iter((10.0, 10.0))
    monkeypatch.setattr(retrieval.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(retrieval.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("late work")))
    tree = _TreeMetadata({"code": {"path": "/unused"}}, budget=0.0)
    assert tree.freshness("code", SHA, "api.py", BLOB) == (
        "not_checked", "target_lookup_unavailable")


def test_context_outer_budget_preserves_original_guidance_row():
    from kp_agent_tooling_ops._impl.service.knowledge_context import _bound
    original = {"id": "guidance-1", "text": "maintained architecture", "path": "guide.md"}
    row = dict(original)
    row["code_references"] = [{"resolution_id": str(i), "padding": "x" * 1400}
                              for i in range(40)]
    row["code_reference_coverage"] = {"diagnostics": [], "returned_count": 40,
        "omitted_count": 0, "diagnostics_omitted_count": 0}
    data = {"guidance": [row], "gaps": [], "discovery": {},
            "excluded_evidence": [], "references": {}, "next_steps": []}
    report = {"status": "ok", "omitted": 0, "data": data}
    bounded = _bound(report)
    assert [{key: bounded["data"]["guidance"][0][key] for key in original}] == [original]
    assert bounded["data"]["guidance"][0]["code_reference_coverage"]["returned_count"] < 40
