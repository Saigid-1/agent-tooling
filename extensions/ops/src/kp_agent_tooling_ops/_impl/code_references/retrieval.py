"""Query-time selection of immutable reference manifests and graph positives.

This module never extracts or resolves a document reference.  It selects one
already-published outcome per occurrence/repository and keeps negative manifest
evidence separate from positive graph edges.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import subprocess
import time
from typing import Mapping

from .adapters import (FrozenNavigationReadiness, GitRevisionAdapter, RevisionInputError,
                       UnavailableNavigationReadiness)
from .extraction import EXTRACTION_POLICY_VERSION
from .manifest import FrozenReferenceRetrieval
from .resolution import RESOLUTION_POLICY_VERSION
from kp_agent_tooling_ops._impl.service.knowledge_telemetry import telemetry_stage
from kp_agent_tooling._impl import leaf


LIMITATION = ("A textual reference does not establish that the code is invoked, "
              "current, or correctly described. Target-file freshness does not "
              "establish current resolution validity.")


def _chunk_id(row: Mapping[str, object]) -> str:
    return leaf.deskdoc_chunk_id(str(row["repo_key"]), str(row["path"]), str(row["blob_sha"]),
                                 str(row["byte_offset"]), str(row["byte_length"]))


def _active_revisions(navigation: object | None) -> Mapping[str, str]:
    provider = getattr(navigation, "active_revisions", None)
    if provider is None:
        return {}
    try:
        value = provider()
        return value if isinstance(value, Mapping) else {}
    except Exception:
        return {}


class _RevisionAdapter(GitRevisionAdapter):
    def __init__(self, repositories, defaults):
        super().__init__(repositories)
        self._defaults = defaults

    def select(self, source_repo_key, explicit):
        # GitRevisionAdapter's configured-ref default is intentionally replaced:
        # retrieval defaults come only from the captured navigation profile.
        requested = {} if explicit is None else explicit
        if not isinstance(requested, Mapping):
            raise RevisionInputError("code_revisions must be an object")
        merged = dict(requested)
        if source_repo_key not in merged and source_repo_key in self._defaults:
            merged[source_repo_key] = self._defaults[source_repo_key]
        selected = super().select(source_repo_key, merged)
        if source_repo_key not in requested:
            row = selected[source_repo_key]
            if source_repo_key not in self._defaults:
                from .adapters import RevisionSelection
                selected[source_repo_key] = RevisionSelection(
                    source_repo_key, None, "unavailable",
                    "navigation_profile_revision_unavailable")
            elif row.selection == "explicit":
                from .adapters import RevisionSelection
                selected[source_repo_key] = RevisionSelection(
                    source_repo_key, row.revision, "defaulted", row.reason)
        return selected


_ENRICH_SCOPE = ContextVar("code_reference_enrichment_scope", default=False)


@contextmanager
def reference_enrichment_scope(telemetry):
    """One inclusive request envelope, including unchanged retrieval/context work.

    Stage children isolate feature work; the hard budget excludes document recall.
    ContextVar isolation preserves nested and concurrent request parentage.
    """
    if _ENRICH_SCOPE.get():
        yield
        return
    token = _ENRICH_SCOPE.set(True)
    try:
        with telemetry_stage(telemetry, "code_references.enrich"):
            yield
    finally:
        _ENRICH_SCOPE.reset(token)


def attach_code_references(report: dict, *, source_repo_key: str,
                           repositories: Mapping[str, Mapping[str, object]],
                           store: object | None, graph: object | None,
                           navigation: object | None, code_revisions: object | None,
                           reference_baseline_revisions: object | None,
                           telemetry: object | None = None,
                           prepared: tuple | None = None) -> None:
    """Mutate one enabled retrieval report while retaining its original rows."""
    with reference_enrichment_scope(telemetry):
        _attach_code_references(report, source_repo_key=source_repo_key,
            repositories=repositories, store=store, graph=graph, navigation=navigation,
            code_revisions=code_revisions,
            reference_baseline_revisions=reference_baseline_revisions,
            telemetry=telemetry, prepared=prepared)


def _attach_code_references(report: dict, *, source_repo_key: str,
                            repositories: Mapping[str, Mapping[str, object]],
                            store: object | None, graph: object | None,
                            navigation: object | None, code_revisions: object | None,
                            reference_baseline_revisions: object | None,
                            telemetry: object | None = None,
                            prepared: tuple | None = None) -> None:
    prepared_value = (prepared or prepare_code_reference_request(
        source_repo_key=source_repo_key, repositories=repositories,
        navigation=navigation, code_revisions=code_revisions,
        reference_baseline_revisions=reference_baseline_revisions, telemetry=telemetry))
    selected, baselines, feature_used = prepared_value
    context = {
        "requested_revisions": {key: row.as_dict() for key, row in selected.items()},
        "baseline_revisions": {key: {"revision": value.revision,
            "availability": "available" if value.available else "unavailable",
            "reason": value.reason} for key, value in sorted(baselines.items())},
        "extraction_policy_version": EXTRACTION_POLICY_VERSION,
        "resolution_policy_version": RESOLUTION_POLICY_VERSION,
        "bounded_work": {"rows": len(report.get("results", [])), "repositories": len(selected),
                         "selected_manifests": 0, "diagnostics_returned": 0,
                         "diagnostics_omitted": 0},
    }
    report["code_reference_context"] = context
    report.setdefault("limitations", []).append(LIMITATION)
    diagnostics_budget = 200
    references_budget = 200
    bounded_navigation = (navigation if isinstance(navigation, FrozenNavigationReadiness)
                          else UnavailableNavigationReadiness())
    tree = _TreeMetadata(repositories, bounded_navigation,
                         budget=max(0.0, 1.0-feature_used))
    bounded_store = store if isinstance(store, FrozenReferenceRetrieval) else None
    adapter_unavailable = store is not None and bounded_store is None
    generation = getattr(bounded_store, "generation_metadata", None)
    if isinstance(generation, Mapping):
        context["bounded_work"]["frozen_generation"] = dict(generation)
    if store is not None and bounded_store is None:
        context["bounded_work"]["adapter_state"] = "unavailable"
        context["bounded_work"]["adapter_reason"] = "reference_adapter_not_query_bounded"
    for row in report.get("results", []):
        with telemetry_stage(telemetry, "code_references.response_enrichment",
                             stage="compute", rows=1):
            coverage, references = _row(
                row, source_repo_key=source_repo_key, repositories=repositories,
                selected=selected, baselines=baselines, store=bounded_store, graph=graph,
                navigation=bounded_navigation,
                diagnostics_budget=diagnostics_budget,
                references_budget=references_budget, tree=tree, telemetry=telemetry,
            )
        if isinstance(generation, Mapping) and (coverage["diagnostics"] or coverage["diagnostics_omitted_count"]):
            coverage["diagnostics_call"] = {"name": "knowledge.reference_diagnostics", "arguments": {
                "repo_key": source_repo_key, "path": row["path"], "blob_sha": row["blob_sha"],
                "byte_offset": row["byte_offset"], "byte_length": row["byte_length"],
                "generation_digest": generation["generation_digest"],
                "code_revisions": {key: value["selected_resolution_revision"] for key, value in
                    coverage["by_repo"].items() if value.get("selected_resolution_revision")},
                "offset": 0, "limit": 10}}
        recovery_revisions = {key: value["selected_resolution_revision"] for key, value in
            coverage["by_repo"].items() if value.get("selection") == "exact" and
            value.get("selected_resolution_revision")}
        if (isinstance(generation, Mapping) and recovery_revisions
                and coverage.get("status_counts", {}).get("resolved", 0)):
            coverage["references_call"] = {"name": "knowledge.reference_recovery", "arguments": {
                "repo_key": source_repo_key, "path": row["path"], "blob_sha": row["blob_sha"],
                "byte_offset": row["byte_offset"], "byte_length": row["byte_length"],
                "generation_digest": generation["generation_digest"],
                "code_revisions": recovery_revisions, "offset": 0, "limit": 10}}
        row["code_reference_coverage"] = coverage
        if adapter_unavailable:
            coverage["state"] = "unavailable"
            for value in coverage["by_repo"].values():
                value.update(state="unavailable", reason="reference_adapter_not_query_bounded")
        row["code_references"] = references
        returned = len(coverage["diagnostics"])
        diagnostics_budget -= returned
        references_budget -= len(references)
        context["bounded_work"]["diagnostics_returned"] += returned
        context["bounded_work"]["diagnostics_omitted"] += coverage["diagnostics_omitted_count"]
        context["bounded_work"]["selected_manifests"] += sum(
            value["selection"] != "none" for value in coverage["by_repo"].values())
    _bound_enrichment(report)
    context["bounded_work"]["diagnostics_returned"] = sum(len(
        row.get("code_reference_coverage", {}).get("diagnostics", ()))
        for row in report.get("results", ()))
    context["bounded_work"]["diagnostics_omitted"] = sum(
        row.get("code_reference_coverage", {}).get("diagnostics_omitted_count", 0)
        for row in report.get("results", ()))


def prepare_code_reference_request(*, source_repo_key, repositories, navigation,
                                   code_revisions, reference_baseline_revisions, telemetry=None):
    """Resolve all moving revision inputs once, before corpus retrieval."""
    started = time.monotonic()
    adapter = _RevisionAdapter(repositories, _active_revisions(navigation))
    with telemetry_stage(telemetry, "code_references.revision_selection", stage="read"):
        selected = adapter.select(source_repo_key, code_revisions)
    if reference_baseline_revisions:
        with telemetry_stage(telemetry, "code_references.baseline_ancestry", stage="verify", resolver="git"):
            baselines = adapter.validate_baselines(selected, reference_baseline_revisions)
    else:
        baselines = adapter.validate_baselines(selected, reference_baseline_revisions)
    return selected, baselines, time.monotonic() - started


def _row(row, *, source_repo_key, repositories, selected, baselines, store, graph,
         navigation, diagnostics_budget, references_budget, tree, telemetry):
    chunk = _chunk_id(row)
    by_repo, references, diagnostics = {}, [], []
    counts = Counter()
    candidate_total = retained_count = overflow_count = None
    unowned_count = 0
    omitted_by_kind = Counter()
    omitted_diagnostics = 0
    omitted_references = 0
    states = []
    occurrence_keys = set()
    for repo_key, revision in selected.items():
        if tree.expired():
            by_repo[repo_key] = {"requested_revision": revision.revision,
                "selected_resolution_revision": None, "selection": "none",
                "state": "unavailable", "reason": "feature_budget_exhausted"}
            states.append("unavailable")
            continue
        if revision.revision is None or revision.selection == "unavailable":
            by_repo[repo_key] = {"requested_revision": revision.revision,
                "selected_resolution_revision": None, "selection": "none",
                "state": "unavailable", "reason": revision.reason}
            states.append("unavailable")
            continue
        if store is None:
            by_repo[repo_key] = {"requested_revision": revision.revision,
                "selected_resolution_revision": None, "selection": "none",
                "state": "unavailable", "reason": "backend_not_configured"}
            states.append("unavailable")
            continue
        shard = None
        selection = "none"
        if store is not None:
            with telemetry_stage(telemetry, "code_references.manifest_lookup",
                                 stage="read", cache_state="bypass"):
                shard = store.select(source_repo_key=source_repo_key,
                    document_path=row["path"], document_blob_sha=row["blob_sha"],
                    chunk_content_id=chunk, target_repo_key=repo_key,
                    target_revision=revision.revision,
                    extraction_policy_version=EXTRACTION_POLICY_VERSION,
                    resolution_policy_version=RESOLUTION_POLICY_VERSION)
        if shard is not None:
            selection = "exact"
        elif repo_key in baselines and baselines[repo_key].available and store is not None:
            with telemetry_stage(telemetry, "code_references.manifest_lookup",
                                 stage="read", cache_state="bypass"):
                shard = store.select(source_repo_key=source_repo_key,
                    document_path=row["path"], document_blob_sha=row["blob_sha"],
                    chunk_content_id=chunk, target_repo_key=repo_key,
                    target_revision=baselines[repo_key].revision,
                    extraction_policy_version=EXTRACTION_POLICY_VERSION,
                    resolution_policy_version=RESOLUTION_POLICY_VERSION)
            if shard is not None:
                selection = "baseline"
        if shard is None:
            baseline = baselines.get(repo_key)
            if baseline is not None and not baseline.available:
                by_repo[repo_key] = {"requested_revision": revision.revision,
                    "selected_resolution_revision": None, "selection": "none",
                    "state": "unavailable", "reason": baseline.reason}
                states.append("unavailable")
                continue
            by_repo[repo_key] = {"requested_revision": revision.revision,
                "selected_resolution_revision": None, "selection": "none",
                "state": "not_indexed", "reason": "manifest_not_found"}
            states.append("not_indexed")
            continue
        if (getattr(shard, "published", True) is not True or
                getattr(shard, "integrity_checked", True) is not True):
            by_repo[repo_key] = {"requested_revision": revision.revision,
                "selected_resolution_revision": shard.target_revision,
                "selection": selection, "state": "partial",
                "reason": "manifest_unpublished_or_unchecked"}
            states.append("partial")
            continue
        try:
            with telemetry_stage(telemetry, "code_references.graph_lookup",
                                 stage="read", resolver="graph"):
                view = store.retrieval_view(shard, graph)
                if any("_target_symbol" not in edge or "_target_change" not in edge
                       for edge in view.get("positive_edges", ())):
                    raise ValueError("positive edge lacks frozen endpoint metadata")
        except Exception:
            by_repo[repo_key] = {"requested_revision": revision.revision,
                "selected_resolution_revision": shard.target_revision,
                "selection": selection, "state": "partial",
                "reason": "manifest_graph_integrity_error"}
            states.append("partial")
            continue
        state = view["state"] if view["state"] in {"complete", "partial"} else "partial"
        by_repo[repo_key] = {"requested_revision": revision.revision,
            "selected_resolution_revision": shard.target_revision,
            "selection": selection, "state": state,
            "reason": None if state == "complete" else "manifest_incomplete"}
        states.append(state)
        outcomes = view["outcomes"]
        # Newer shards retain extraction counters; old pilot shards degrade
        # explicitly rather than presenting inferred zeros as complete coverage.
        retained = getattr(shard, "retained_count", None)
        total = getattr(shard, "candidate_total", None)
        overflow = getattr(shard, "overflow_count", None)
        if retained is None or total is None or overflow is None:
            retained, total, overflow = len(outcomes), len(outcomes), 0
            if outcomes and state == "complete":
                state = "partial"
                states[-1] = "partial"
                by_repo[repo_key]["state"] = "partial"
                by_repo[repo_key]["reason"] = "extraction_counters_unavailable"
        candidate_total = total if candidate_total is None else max(candidate_total, total)
        retained_count = retained if retained_count is None else max(retained_count, retained)
        overflow_count = overflow if overflow_count is None else max(overflow_count, overflow)
        unowned_count = max(unowned_count, getattr(shard, "unowned_count", 0))
        # Every target-repository shard carries the same document extraction
        # inventory. Its overflow is counted once, not once per selected repo.
        for kind, count in getattr(shard, "omitted_by_kind", {}).items():
            omitted_by_kind[kind] = max(omitted_by_kind[kind], count)
        edge_by_resolution = {}
        for edge in view["positive_edges"]:
            attrs = edge.get("attributes", edge)
            edge_by_resolution.setdefault(attrs.get("resolution_id"), []).append(edge)
        for outcome in outcomes:
            occurrence_keys.add(outcome.get("occurrence_id") or
                                outcome.get("resolution_id") or
                                (repo_key, len(occurrence_keys)))
            execution = outcome.get("execution_state")
            status = outcome.get("status") if execution == "complete" else None
            counts[status or "error"] += 1
            if status == "resolved":
                for edge in edge_by_resolution.get(outcome.get("resolution_id"), ()):
                    if len(references) < min(40, references_budget):
                        references.append(_positive(edge, outcome, repo_key=repo_key,
                            requested_revision=revision.revision, selected_revision=shard.target_revision,
                            exact=(selection == "exact"),
                            repositories=repositories, graph=graph, navigation=navigation,
                            tree=tree, telemetry=telemetry))
                    else:
                        omitted_references += 1
            else:
                diagnostic = {key: outcome.get(key) for key in (
                    "occurrence_id", "resolution_id", "kind", "ref_byte_offset",
                    "ref_byte_length", "literal_digest", "status", "reason",
                    "match_count", "truncated") if outcome.get(key) is not None}
                diagnostic["execution_state"] = execution
                diagnostic["derivation_rule"] = "doc-code-reference.v1"
                if len(diagnostics) < min(40, diagnostics_budget):
                    diagnostics.append(diagnostic)
                else:
                    omitted_diagnostics += 1
    # Outcomes are partitioned by target repository while extraction counters are
    # shared chunk facts.  Older shards may carry only their partition count, so
    # reconcile against the distinct retained occurrences without summing the
    # shared overflow inventory once per repository.
    if candidate_total is not None:
        retained_count = max(retained_count or 0, len(occurrence_keys))
        candidate_total = max(candidate_total, retained_count + (overflow_count or 0))
    state = _coverage_state(states, overflow_count or 0, counts["error"], unowned_count)
    references.sort(key=lambda item: (item["ref_byte_offset"], item["target"]["repo_key"],
                                      item["target"].get("path") or "",
                                      item["resolution_id"]))
    coverage = {"state": state, "by_repo": by_repo,
        "candidate_total": candidate_total, "retained_count": retained_count,
        "overflow_count": overflow_count, "status_counts": (
            {key: counts[key] for key in
             ("resolved", "ambiguous", "candidate", "unresolved")}
            if candidate_total is not None else None),
        "error_count": counts["error"] if candidate_total is not None else None,
        "returned_count": len(references),
        "omitted_count": omitted_references, "diagnostics": diagnostics,
        "diagnostics_omitted_count": omitted_diagnostics,
        "omitted_by_kind": dict(sorted(omitted_by_kind.items())),
        "unowned_count": unowned_count}
    return coverage, references


def _coverage_state(states, overflow, errors, unowned):
    if not states or all(state == "not_indexed" for state in states):
        return "not_indexed"
    if all(state == "unavailable" for state in states):
        return "unavailable"
    if overflow or errors or unowned or any(state == "partial" for state in states):
        return "partial"
    if any(state in {"not_indexed", "unavailable"} for state in states):
        return "partial"
    return "complete"


def _edge_endpoint(edge, graph):
    return (edge.get("to_id"), edge.get("_target_symbol", {}),
            edge.get("_target_change", {}))


def _positive(edge, outcome, *, repo_key, requested_revision, selected_revision,
              exact, repositories, graph, navigation, tree, telemetry):
    attrs = edge.get("attributes", edge)
    _, symbol, change = _edge_endpoint(edge, graph)
    path = change.get("file_path")
    blob = attrs.get("resolved_blob_sha") or change.get("blob_sha")
    with telemetry_stage(telemetry, "code_references.tree_freshness",
                         stage="read", resolver="git"):
        freshness, freshness_reason = tree.freshness(repo_key, requested_revision, path, blob)
    validity = "established_at_requested_revision" if exact else "historical_only"
    start = attrs.get("target_start_line")
    end = attrs.get("target_end_line")
    continuation = None
    navigation_status = {"state": "unavailable", "reason": "conditions_not_met",
                         "range_truncated": False}
    action = "reindex_references_at_requested_revision" if not exact else None
    if exact and tree.size(repo_key, requested_revision, path) == 0:
        navigation_status = {"state": "unavailable", "reason": "empty_file",
                             "range_truncated": False}
        action = None
    elif not exact:
        navigation_status["reason"] = "requires_resolution_at_requested_revision"
    elif exact and freshness == "current" and type(start) is int and type(end) is int and 1 <= start <= end:
        with telemetry_stage(telemetry, "code_references.navigation_readiness",
                             stage="verify", resolver="navigation"):
            try:
                continuation = navigation.continuation(repo_key=repo_key, revision=requested_revision,
                    path=path, blob_sha=blob, start_line=start, line_count=min(200, end-start+1))
            except Exception:
                continuation = None
        if continuation is not None:
            navigation_status = {"state": "ready", "reason": None,
                                 "range_truncated": (end-start+1) > 200}
        else:
            action = "make_exact_revision_available_to_navigation"
            navigation_status["reason"] = "exact_snapshot_blob_unavailable"
    elif exact and freshness == "not_checked":
        action = "retry_target_verification"
    elif exact and freshness != "current":
        action = "reindex_references_at_requested_revision"
    result = {"occurrence_id": outcome.get("occurrence_id"),
        "resolution_id": outcome.get("resolution_id"), "kind": outcome.get("kind"),
        "ref_byte_offset": outcome.get("ref_byte_offset"),
        "ref_byte_length": outcome.get("ref_byte_length"),
        "target": {"repo_key": repo_key, "path": path,
            "start_line": start, "end_line": end},
        "requested_code_revision": requested_revision,
        "resolved_code_revision": selected_revision, "resolved_blob_sha": blob,
        "target_file_freshness": freshness, "resolution_validity": validity,
        "navigation_status": navigation_status, "continuation": continuation,
        "action_required": action}
    if symbol.get("symbol_name"):
        result["target"]["symbol_name"] = symbol["symbol_name"]
    if freshness_reason:
        result["freshness_reason"] = freshness_reason
    return result


class _TreeMetadata:
    """One bounded exact-tree metadata read per selected repository/revision."""
    def __init__(self, repositories, navigation=None, budget=1.0):
        self.repositories, self.navigation, self.cache = repositories, navigation, {}
        self.deadline = time.monotonic() + budget

    def expired(self):
        return time.monotonic() >= self.deadline

    def _load(self, repo_key, revision):
        key = (repo_key, revision)
        if key in self.cache:
            return self.cache[key]
        provider = getattr(self.navigation, "tree_blobs", None)
        if provider is not None:
            try:
                blobs = provider(repo_key, revision)
            except Exception:
                blobs = None
            if blobs is not None:
                self.cache[key] = {path: ("100644", "blob", blob, None)
                                   for path, blob in blobs.items()}
                return self.cache[key]
        repo = self.repositories.get(repo_key)
        if repo is None:
            self.cache[key] = None
            return None
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self.cache[key] = None
            return None
        try:
            raw = subprocess.run(["git", "--no-optional-locks", "ls-tree", "-r", "-l", "-z", revision],
                cwd=Path(str(repo["path"])), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=min(.25, remaining), check=True).stdout
            if len(raw) > 16_000_000:
                raise ValueError("tree metadata budget exceeded")
            entries = {}
            for item in raw.split(b"\0"):
                if not item:
                    continue
                metadata, raw_path = item.split(b"\t", 1)
                mode, kind, blob, size = metadata.decode("ascii").split()
                entries[raw_path.decode("utf-8")] = (
                    mode, kind, blob, None if size == "-" else int(size))
            self.cache[key] = entries
        except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
            self.cache[key] = None
        return self.cache[key]

    def freshness(self, repo_key, revision, path, expected_blob):
        if not isinstance(path, str) or not isinstance(expected_blob, str):
            return "not_checked", "target_metadata_unavailable"
        entries = self._load(repo_key, revision)
        if entries is None:
            return "not_checked", "target_lookup_unavailable"
        entry = entries.get(path)
        if entry is None:
            return "absent_at_revision", None
        mode, kind, blob, _ = entry
        if mode not in {"100644", "100755"} or kind != "blob":
            return "changed_since_resolution", "unsupported_git_object"
        return (("current", None) if blob == expected_blob
                else ("changed_since_resolution", None))

    def size(self, repo_key, revision, path):
        provider = getattr(self.navigation, "file_size", None)
        if provider is not None:
            try:
                value = provider(repo_key, revision, path)
                if value is not None:
                    return value
            except Exception:
                pass
        entries = self._load(repo_key, revision)
        entry = entries.get(path) if entries is not None else None
        return entry[3] if entry is not None else None


def _bound_enrichment(report):
    """Trim only opt-in additions; existing retrieval rows remain untouched."""
    def size():
        return len((json.dumps(report, ensure_ascii=True) + "\n").encode())
    rows = report.get("results", [])
    while size() > 32768:
        changed = False
        for row in reversed(rows):
            coverage = row.get("code_reference_coverage", {})
            if coverage.get("diagnostics"):
                coverage["diagnostics"].pop()
                coverage["diagnostics_omitted_count"] += 1
                changed = True
                break
            if row.get("code_references"):
                row["code_references"].pop()
                coverage["omitted_count"] += 1
                coverage["returned_count"] = len(row["code_references"])
                changed = True
                break
        if not changed:
            break


def trim_code_reference_enrichment(report, maximum=32768):
    """Fit a public envelope by omitting enrichment metadata before source rows.

    ``report`` may be a retrieval report or a service/context envelope.  Every
    compacted row retains an explicit coverage state and omission marker.
    """
    def size():
        return len((json.dumps(report, ensure_ascii=True) + "\n").encode())

    data = report.get("data", report)
    rows = data.get("results") or data.get("guidance") or []
    context = data.get("code_reference_context", {})
    bounded_work = context.get("bounded_work", {}) if isinstance(context, dict) else {}
    diagnostic_total = (bounded_work.get("diagnostics_returned", 0) +
                        bounded_work.get("diagnostics_omitted", 0))
    while size() > maximum:
        changed = False
        for row in reversed(rows):
            coverage = row.get("code_reference_coverage")
            if not isinstance(coverage, dict):
                continue
            if coverage.get("diagnostics"):
                coverage["diagnostics"].pop()
                coverage["diagnostics_omitted_count"] = coverage.get(
                    "diagnostics_omitted_count", 0) + 1
                changed = True
                break
            if row.get("code_references"):
                row["code_references"].pop()
                coverage["omitted_count"] = coverage.get("omitted_count", 0) + 1
                coverage["returned_count"] = len(row["code_references"])
                changed = True
                break
            if not coverage.get("metadata_omitted"):
                row["code_reference_coverage"] = {
                    "state": coverage.get("state", "unavailable"),
                    "metadata_omitted": True,
                    **({"diagnostics_call": coverage["diagnostics_call"]} if "diagnostics_call" in coverage else {}),
                    **({"references_call": coverage["references_call"]} if "references_call" in coverage else {}),
                }
                row["code_references"] = []
                changed = True
                break
        if not changed:
            context = data.get("code_reference_context")
            if isinstance(context, dict) and not context.get("metadata_omitted"):
                data["code_reference_context"] = {"metadata_omitted": True}
                changed = True
        if not changed:
            break
    context = data.get("code_reference_context", {})
    bounded_work = context.get("bounded_work", {}) if isinstance(context, dict) else {}
    if isinstance(bounded_work, dict) and diagnostic_total:
        diagnostics_returned = sum(len(row.get("code_reference_coverage", {}).get(
            "diagnostics", ())) for row in rows)
        bounded_work["diagnostics_returned"] = diagnostics_returned
        bounded_work["diagnostics_omitted"] = max(
            0, diagnostic_total - diagnostics_returned)
    return size() <= maximum
