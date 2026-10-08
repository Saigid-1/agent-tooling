"""Adversarial public boundaries for reference-enriched knowledge responses."""
import pytest
from dataclasses import replace
import json
import subprocess

from kp_agent_tooling_ops._impl.code_references.adapters import RevisionSelection
from kp_agent_tooling_ops._impl.code_references.manifest import AtomicReferenceGeneration, FrozenReferenceRetrieval
from kp_agent_tooling_ops._impl.code_references.retrieval import _chunk_id, attach_code_references
from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeService
from test_code_reference_retrieval_mutations import ROW, SHA, shard
from test_knowledge_service import Hit, Reader, setup
from test_knowledge_context import case


def _revision(repo):
    return subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()


@pytest.mark.xfail(strict=True, reason='S5 divergence: synthetic hits absent at the target revision are excluded, so len(results) == 20 gets 0 (also red at OPS at the extraction commit)')
def test_enabled_references_preserve_all_near_limit_retrieval_rows(setup):
    repo, config = setup
    revision = _revision(repo)
    hits = [Hit(path=f"docs/{index}.md", text="x" * 1200, score=index / 100)
            for index in range(20)]
    config["repositories"]["repo"]["artifacts"] = {
        hit.path: {"status": "maintained", "owner": "OPS"} for hit in hits}
    service = KnowledgeService(config, lambda: Reader(hits))
    disabled = service.execute("retrieve", {
        "repo_key": "repo", "query": "x", "top_k": 20})
    enabled = service.execute("retrieve", {
        "repo_key": "repo", "query": "x", "top_k": 20,
        "include_code_references": True,
        "code_revisions": {"repo": revision}})

    project = lambda value: [(row["path"], row["text"], row["score"])
                             for row in value["data"]["results"]]
    assert len(disabled["data"]["results"]) == 20
    assert project(enabled) == project(disabled)
    assert enabled["omitted"] == disabled["omitted"] == 0
    assert len((json.dumps(enabled, ensure_ascii=True) + "\n").encode()) <= 32768
    assert any(row["code_reference_coverage"].get("metadata_omitted")
               for row in enabled["data"]["results"])


@pytest.mark.xfail(strict=True, reason='S5 divergence: synthetic hits absent at the target revision are excluded, so 0 < len(paths) < 50 gets 0 (also red at OPS at the extraction commit)')
def test_enabled_references_preserve_baseline_selection_when_baseline_is_truncated(setup):
    repo, config = setup
    revision = _revision(repo)
    hits = [Hit(path=f"docs/{index}.md", text="x" * 1200, score=index / 100)
            for index in range(50)]
    config["repositories"]["repo"]["artifacts"] = {
        hit.path: {"status": "maintained", "owner": "OPS"} for hit in hits}
    service = KnowledgeService(config, lambda: Reader(hits))
    disabled = service.execute("retrieve", {
        "repo_key": "repo", "query": "x", "top_k": 50})
    enabled = service.execute("retrieve", {
        "repo_key": "repo", "query": "x", "top_k": 50,
        "include_code_references": True,
        "code_revisions": {"repo": revision}})
    paths = lambda value: [row["path"] for row in value["data"]["results"]]
    assert 0 < len(paths(disabled)) < 50
    assert paths(enabled) == paths(disabled)
    assert enabled["omitted"] == disabled["omitted"]


@pytest.mark.xfail(strict=True, reason='S5 divergence: default Hit is excluded at the target revision, so results[0] raises IndexError (also red at OPS at the extraction commit)')
def test_missing_reference_store_is_backend_unavailable(setup):
    repo, config = setup
    report = KnowledgeService(config, lambda: Reader([Hit()])).execute("retrieve", {
        "repo_key": "repo", "query": "x", "include_code_references": True,
        "code_revisions": {"repo": _revision(repo)}})
    coverage = report["data"]["results"][0]["code_reference_coverage"]
    assert coverage["state"] == "unavailable"
    assert coverage["by_repo"]["repo"]["state"] == "unavailable"
    assert coverage["by_repo"]["repo"]["reason"] == "backend_not_configured"


def test_atomic_generation_is_snapshotted_once_only_for_enabled_request(setup, monkeypatch):
    repo, config = setup
    revision = _revision(repo)
    frozen = FrozenReferenceRetrieval((), {}, source_repo_key="repo",
                                      target_revisions={"repo": revision})
    generation = AtomicReferenceGeneration(frozen)
    calls = []
    original = generation.snapshot
    monkeypatch.setattr(generation, "snapshot", lambda: calls.append(1) or original())
    service = KnowledgeService(config, lambda: Reader([Hit()]),
                               reference_store=generation)
    service.execute("retrieve", {"repo_key": "repo", "query": "x"})
    assert calls == []
    service.execute("retrieve", {"repo_key": "repo", "query": "x",
        "include_code_references": True, "code_revisions": {"repo": revision}})
    assert calls == [1]


@pytest.mark.xfail(strict=True, reason='S5 divergence: nested retrieve yields no result row, so observed[0] raises IndexError (also red at OPS at the extraction commit)')
def test_nested_enabled_service_does_not_borrow_outer_generation(setup):
    repo, config = setup
    revision = _revision(repo)
    frozen = FrozenReferenceRetrieval((), {}, source_repo_key="repo",
                                      target_revisions={"repo": revision})
    inner = KnowledgeService(config, lambda: Reader([Hit()]))
    observed = []

    class NestedReader(Reader):
        def recall(self, **kwargs):
            nested = inner.execute("retrieve", {"repo_key": "repo", "query": "inner",
                "include_code_references": True, "code_revisions": {"repo": revision}})
            observed.append(nested["data"]["results"][0]["code_reference_coverage"])
            return super().recall(**kwargs)

    outer = KnowledgeService(config, lambda: NestedReader([Hit()]),
        reference_store=AtomicReferenceGeneration(frozen))
    report = outer.execute("retrieve", {"repo_key": "repo", "query": "outer",
        "include_code_references": True, "code_revisions": {"repo": revision}})
    assert observed[0]["by_repo"]["repo"]["reason"] == "backend_not_configured"
    assert report["data"]["results"][0]["code_reference_coverage"]["state"] == "not_indexed"


def test_partitioned_cross_repo_outcomes_reconcile_shared_counts():
    base = replace(shard(SHA, "resolved"), chunk_content_id=_chunk_id(ROW),
                   candidate_total=1, retained_count=1)
    first_outcome = dict(base.outcomes[0], occurrence_id="ops-occ", resolution_id="ops-res")
    second_outcome = dict(base.outcomes[0], occurrence_id="ats-occ", resolution_id="ats-res")
    first = replace(base, outcomes=(first_outcome,))
    second = replace(base, shard_id="ats-shard", target_repo_key="ats",
                     outcomes=(second_outcome,))
    views = {
        first.shard_id: {"state": "complete", "outcomes": [first_outcome],
                         "positive_edges": []},
        second.shard_id: {"state": "complete", "outcomes": [second_outcome],
                          "positive_edges": []},
    }
    frozen = FrozenReferenceRetrieval((first, second), views,
        source_repo_key="docs", target_revisions={"code": SHA, "ats": SHA})
    report = {"results": [dict(ROW)]}
    attach_code_references(report, source_repo_key="docs", repositories={},
        store=frozen, graph=None, navigation=None, code_revisions=None,
        reference_baseline_revisions=None,
        prepared=({key: RevisionSelection(key, SHA, "explicit")
                   for key in ("code", "ats")}, {}, 0.0))

    coverage = report["results"][0]["code_reference_coverage"]
    assert coverage["candidate_total"] == 2
    assert coverage["retained_count"] == 2
    assert coverage["status_counts"]["resolved"] == 2


@pytest.mark.skip(reason='S5 excluded: needs OPS-only scripts/doc_code_reference_scenarios.py (kp_core graph)')
def test_near_limit_diagnostic_totals_reconcile_after_public_service_trim():
    from scripts.doc_code_reference_scenarios import build

    system = build(None)
    report = system["service"].execute("retrieve", {
        "repo_key": "ops", "query": "Reader source guide", "top_k": 6,
        "include_code_references": True,
        "code_revisions": {"ops": system["revisions"]["ambiguous"]}})
    rows = report["data"]["results"]
    bounded = report["data"]["code_reference_context"]["bounded_work"]
    actual = sum(len(row.get("code_reference_coverage", {}).get("diagnostics", ()))
                 for row in rows)
    assert bounded["diagnostics_returned"] == actual
    assert bounded["diagnostics_returned"] + bounded["diagnostics_omitted"] == 120
    assert bounded["diagnostics_omitted"] > 0
    for row in rows:
        coverage = row["code_reference_coverage"]
        if len(coverage.get("diagnostics", ())) < 20:
            assert (coverage.get("diagnostics_omitted_count", 0) > 0 or
                    coverage.get("metadata_omitted") is True)


@pytest.mark.xfail(strict=True, reason='S5 divergence: context guidance rows carry no code_reference_coverage (KeyError) (also red at OPS at the extraction commit)')
def test_context_bound_preserves_guidance_with_explicit_reference_omission(case):
    repo, config, target, _, hit = case
    hit.text = "x" * 5400
    report = KnowledgeService(config, lambda: Reader([hit] * 5)).execute("context", {
        "repo_key": "repo", "capability_id": "cap", "target_revision": target,
        "query": "x", "include_code_references": True,
        "code_revisions": {"repo": _revision(repo)}})

    assert len(report["data"]["guidance"]) == 5
    assert all(row["text"] == "x" * 5400 for row in report["data"]["guidance"])
    assert len((json.dumps(report, ensure_ascii=True) + "\n").encode()) <= 32768
    assert any(row["code_reference_coverage"].get("metadata_omitted")
               for row in report["data"]["guidance"])


def test_context_exact_limit_returns_explicit_error_when_marker_cannot_fit():
    from kp_agent_tooling_ops._impl.service.knowledge_context import _bound, _size

    data = {"guidance": [], "gaps": [], "discovery": {}, "excluded_evidence": [],
            "references": {}, "next_steps": []}
    report = {"schema_version": "ops.knowledge.v1", "operation": "context",
              "repo_key": "repo", "source_revision": "a" * 40,
              "status": "ok", "omitted": 0, "limitations": [], "data": data}
    row = {"path": "guide.md", "text": ""}
    data["guidance"] = [row]
    # Leave fewer bytes than the explicit omission marker needs while retaining
    # the original guidance row in the corresponding unenriched envelope.
    row["text"] = "x" * (32768 - _size(report) - 8)
    row["code_references"] = []
    row["code_reference_coverage"] = {"state": "unavailable", "diagnostics": [],
        "returned_count": 0, "omitted_count": 0, "diagnostics_omitted_count": 0}
    result = _bound(report)
    assert result["status"] == "error"
    assert result["data"]["error"]["code"] == "response_budget_exhausted"
    assert _size(result) <= 32768
