"""Structural review ledger for one immutable requirement specification.

This checks identities, declared coverage, and evidence provenance. It cannot decide
whether cited evidence actually proves a requirement; a human must review that link.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping

from kp_agent_tooling._impl import leaf
from kp_agent_tooling_ops._impl.evidence_references import EvidenceReferenceError, resolve_reference

_SHA = re.compile(r"[0-9a-f]{64}\Z")


def canonical_digest(value: object) -> str:
    """SHA256 of sorted-key compact ASCII JSON; list order is significant."""
    return leaf.canonical_sha256(value, ascii=True, allow_nan=False)


def specification(requirements: list[dict[str, str]]) -> dict:
    """Freeze the author's exact IDs and text; no implicit text normalization."""
    if not isinstance(requirements, list) or not requirements or len(requirements) > 500:
        raise ValueError("requirements_required")
    seen = set()
    for row in requirements:
        if (not isinstance(row, dict) or set(row) != {"id", "text"} or
                any(not isinstance(row[k], str) or not row[k].strip() or len(row[k]) > 10000 for k in ("id", "text"))):
            raise ValueError("invalid_requirement")
        if row["id"] in seen:
            raise ValueError("duplicate_requirement")
        seen.add(row["id"])
    frozen = [dict(row) for row in requirements]
    return {"requirements": frozen, "sha256": canonical_digest(frozen)}


def ledger_schema() -> dict:
    """Public shape for review input. Validation also enforces cross-field invariants."""
    return {
        "schema_version": "ops.review-ledger.v1",
        "specification": {"sha256": "64 lowercase hex", "requirements": [{"id": "exact ID", "text": "exact original text"}]},
        "reviews": [{"requirement_id": "exact ID", "requirement_text": "exact original text",
                     "decision": "supported|contradicted|unverified", "oracle": "explicit test/review criterion",
                     "evidence": ["typed evidence reference"],
                     "normalization_rule_ids": ["explicitly approved rule IDs"]}],
        "approved_normalization_rules": [{"id": "unique ID", "requirement_id": "exact ID",
                                          "rule": "explicit rule text", "approval_ref": "human approval reference"}],
    }


def validate_review_ledger(config: Mapping, original_spec: Mapping, ledger: Mapping,
                           *, approved_rules: list[dict] | None = None, resolver=resolve_reference) -> dict:
    """Validate a complete ledger against a separately supplied original specification.

    Resolver may be injected for deterministic tests, but production uses the same
    typed reference resolution as verification.finding. The original_spec must come
    from a trusted caller, rather than being taken solely from the submitted ledger.
    """
    errors = []
    def fail(path, code):
        errors.append({"path": path, "code": code})
    try:
        trusted = specification(original_spec["requirements"])
        if original_spec["sha256"] != trusted["sha256"]:
            fail("/original_spec/sha256", "specification_digest_mismatch")
    except (ValueError, KeyError, TypeError):
        return {"schema_version": "ops.review-ledger-validation.v1", "status": "invalid",
                "semantic_verdict": "not-assessed", "errors": [{"path": "/original_spec", "code": "invalid_original_spec"}]}
    base = {"schema_version": "ops.review-ledger-validation.v1", "specification_sha256": trusted["sha256"],
            "semantic_verdict": "not-assessed", "evidence_provenance": "checked", "errors": errors}
    if not isinstance(ledger, Mapping) or ledger.get("schema_version") != "ops.review-ledger.v1":
        fail("/schema_version", "invalid_ledger_schema")
        return dict(base, status="invalid")
    submitted = ledger.get("specification")
    if not isinstance(submitted, Mapping) or submitted.get("sha256") != trusted["sha256"] or submitted.get("requirements") != trusted["requirements"]:
        fail("/specification", "original_specification_changed")
    # Approval provenance is supplied by a trusted caller, never self-approved by the ledger.
    rules = approved_rules if approved_rules is not None else []
    if ledger.get("approved_normalization_rules", []) != rules:
        fail("/approved_normalization_rules", "approval_registry_mismatch")
    if not isinstance(rules, list):
        fail("/approved_normalization_rules", "invalid_rules")
        rules = []
    rule_ids = {}
    expected = {r["id"]: r["text"] for r in trusted["requirements"]}
    if len(rules) > 500:
        fail("/approved_normalization_rules", "too_many_rules")
        rules = []
    for i, rule in enumerate(rules):
        if (not isinstance(rule, dict) or set(rule) != {"id", "requirement_id", "rule", "approval_ref"} or
                any(not isinstance(v, str) or not v.strip() for v in rule.values()) or
                not isinstance(rule["requirement_id"], str) or rule["requirement_id"] not in expected):
            fail(f"/approved_normalization_rules/{i}", "invalid_or_unapproved_rule")
            continue
        if rule["id"] in rule_ids:
            fail(f"/approved_normalization_rules/{i}", "duplicate_rule")
        rule_ids[rule["id"]] = rule
    reviews = ledger.get("reviews")
    if not isinstance(reviews, list) or len(reviews) > 500:
        fail("/reviews", "reviews_required")
        reviews = []
    seen = set()
    checked = []
    for i, row in enumerate(reviews):
        path = f"/reviews/{i}"
        if not isinstance(row, dict) or set(row) != {"requirement_id", "requirement_text", "decision", "oracle", "evidence", "normalization_rule_ids"}:
            fail(path, "invalid_review_row")
            continue
        rid = row["requirement_id"]
        if not isinstance(rid, str) or rid not in expected:
            fail(path + "/requirement_id", "unknown_or_renamed_requirement")
        elif rid in seen:
            fail(path + "/requirement_id", "duplicate_requirement_review")
        if isinstance(rid, str):
            seen.add(rid)
        if isinstance(rid, str) and rid in expected and row["requirement_text"] != expected[rid]:
            fail(path + "/requirement_text", "requirement_text_changed")
        if not isinstance(row["decision"], str) or row["decision"] not in {"supported", "contradicted", "unverified"}:
            fail(path + "/decision", "invalid_decision")
        if not isinstance(row["oracle"], str) or not row["oracle"].strip() or len(row["oracle"]) > 10000:
            fail(path + "/oracle", "oracle_required")
        used_rules = row["normalization_rule_ids"]
        if not isinstance(used_rules, list) or len(used_rules) > 100 or any(not isinstance(x, str) or x not in rule_ids or rule_ids[x]["requirement_id"] != rid for x in used_rules) or len(used_rules) != len(set(x for x in used_rules if isinstance(x, str))):
            fail(path + "/normalization_rule_ids", "normalization_not_approved_for_requirement")
        refs = row["evidence"]
        if not isinstance(refs, list) or len(refs) > 100 or (row["decision"] != "unverified" and not refs):
            fail(path + "/evidence", "evidence_required")
            continue
        ref_seen = set()
        for j, ref in enumerate(refs):
            try:
                citation = ref
                if isinstance(ref, dict) and "reference" in ref:
                    if set(ref) != {"reference", "start_line", "end_line", "excerpt_sha256"}:
                        raise EvidenceReferenceError("invalid_citation_shape")
                    citation = ref["reference"]
                ref_key = canonical_digest(ref)
                if ref_key in ref_seen:
                    fail(f"{path}/evidence/{j}", "duplicate_evidence")
                ref_seen.add(ref_key)
                if not isinstance(citation, dict):
                    raise EvidenceReferenceError("invalid_evidence_reference")
                resolved = resolver(config, citation)
                if citation.get("kind") == "source":
                    if citation is ref:
                        raise EvidenceReferenceError("source_excerpt_required")
                    content = resolved["evidence"]
                    lines = content.splitlines()
                    start, end = ref["start_line"], ref["end_line"]
                    if (type(start) is not int or type(end) is not int or start < 1 or end < start or end > len(lines) or
                            not isinstance(ref["excerpt_sha256"], str) or not _SHA.fullmatch(ref["excerpt_sha256"])):
                        raise EvidenceReferenceError("invalid_source_range")
                    excerpt = "\n".join(lines[start - 1:end])
                    if hashlib.sha256(excerpt.encode()).hexdigest() != ref["excerpt_sha256"]:
                        raise EvidenceReferenceError("source_excerpt_changed")
                checked.append({"requirement_id": rid, "reference": ref,
                                "evidence_class": resolved["evidence_class"]})
            except (EvidenceReferenceError, OSError, ValueError, KeyError, TypeError) as exc:
                fail(f"{path}/evidence/{j}", getattr(exc, "code", "unresolved_evidence_reference"))
    for rid in expected.keys() - seen:
        fail("/reviews", "missing_requirement:" + rid)
    return dict(base, status="invalid" if errors else "valid", checked_evidence=checked)
