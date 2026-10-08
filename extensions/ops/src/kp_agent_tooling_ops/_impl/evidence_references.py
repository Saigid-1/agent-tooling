"""Resolve closed, typed evidence references against their original namespaces.

Resolution checks identity and provenance only. A source reference identifies
committed file content, not a Serena invocation or runtime execution. It does not
certify a claim made about the referenced evidence.
"""
import re
import subprocess

from kp_agent_tooling_ops._impl.observations import ObservationStore
from kp_agent_tooling._impl.source_citations import source, source_reference  # noqa: F401 (re-export)


HEX = r"^[0-9a-f]{64}$"
REVISION = r"^[0-9a-f]{40}([0-9a-f]{24})?$"


class EvidenceReferenceError(ValueError):
    """A typed reference cannot be resolved at its declared identity."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def reference_schema():
    """JSON Schema for the four intentionally separate evidence namespaces."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "oneOf": [
            {"type": "object", "properties": {
                "kind": {"const": "packet"}, "slice_id": {"type": "string", "minLength": 1},
                "id": {"type": "string", "minLength": 1},
                "packet_identity": {"type": "string", "pattern": HEX}},
             "required": ["kind", "slice_id", "id", "packet_identity"],
             "additionalProperties": False},
            {"type": "object", "properties": {
                "kind": {"const": "observation"},
                "id": {"type": "string", "pattern": HEX}},
             "required": ["kind", "id"], "additionalProperties": False},
            {"type": "object", "properties": {
                "kind": {"const": "lifecycle"},
                "evidence_id": {"type": "string", "minLength": 1},
                "revision": {"type": "string", "pattern": REVISION},
                "blob_sha": {"type": "string", "pattern": REVISION}},
             "required": ["kind", "evidence_id", "revision", "blob_sha"],
             "additionalProperties": False},
            {"type": "object", "properties": {
                "kind": {"const": "source"},
                "repo_key": {"type": "string", "minLength": 1},
                "path": {"type": "string", "minLength": 1},
                "revision": {"type": "string", "pattern": REVISION},
                "blob_sha": {"type": "string", "pattern": REVISION}},
             "required": ["kind", "repo_key", "path", "revision", "blob_sha"],
             "additionalProperties": False},
        ],
    }


def packet_reference(slice_id, id, packet_identity):
    return {"kind": "packet", "slice_id": slice_id, "id": id,
            "packet_identity": packet_identity}


def observation_reference(id):
    return {"kind": "observation", "id": id}


def lifecycle_reference(evidence_id, revision, blob_sha):
    return {"kind": "lifecycle", "evidence_id": evidence_id,
            "revision": revision, "blob_sha": blob_sha}


def _valid(reference):
    if not isinstance(reference, dict):
        return False
    required = {"packet": {"kind", "slice_id", "id", "packet_identity"},
                "observation": {"kind", "id"},
                "lifecycle": {"kind", "evidence_id", "revision", "blob_sha"},
                "source": {"kind", "repo_key", "path", "revision", "blob_sha"}}
    kind = reference.get("kind")
    if kind not in required or set(reference) != required[kind]:
        return False
    if kind == "packet":
        return (all(isinstance(reference[key], str) and reference[key]
                    for key in ("slice_id", "id"))
                and bool(re.fullmatch(r"[0-9a-f]{64}", reference["packet_identity"] or "")))
    if kind == "observation":
        return isinstance(reference["id"], str) and bool(re.fullmatch(r"[0-9a-f]{64}", reference["id"]))
    identity_key = "evidence_id" if kind == "lifecycle" else "repo_key"
    return (isinstance(reference[identity_key], str) and bool(reference[identity_key])
            and (kind != "source" or isinstance(reference["path"], str) and bool(reference["path"]))
            and all(isinstance(reference[key], str) and bool(re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", reference[key]))
                    for key in ("revision", "blob_sha")))


def resolve_reference(config, reference):
    """Return canonical evidence and its source identity, or a stable error code.

    No caller-supplied path, URL, provider alias, or live navigation is accepted.
    """
    if not _valid(reference):
        raise EvidenceReferenceError("invalid_evidence_reference")
    kind = reference["kind"]
    if kind == "packet":
        from kp_agent_tooling_ops._impl.verification_packet import packet
        from kp_agent_tooling_ops._impl.journey_registry import registered_slices
        from kp_agent_tooling_ops._impl.verification_adjacency import SLICES

        slice_id = reference["slice_id"]
        if slice_id not in SLICES and slice_id not in registered_slices(config):
            raise EvidenceReferenceError("unknown_verification_slice")
        try:
            result = packet(config, slice_id, mode="read", evidence_ids=[reference["id"]],
                            budget=100000, expected_packet_identity=reference["packet_identity"],
                            _internal=True)
        except (KeyError, ValueError, TypeError, OSError, SyntaxError, subprocess.SubprocessError) as exc:
            raise EvidenceReferenceError("packet_unavailable") from exc
        if result.get("gap", {}).get("reason") == "packet_identity_changed":
            raise EvidenceReferenceError("packet_identity_changed")
        if result["status"] == "invalid_selection":
            raise EvidenceReferenceError("unresolved_evidence_reference")
        if result["status"] != "ready":
            raise EvidenceReferenceError("packet_unavailable")
        item = result["evidence"][reference["id"]]
        return {"reference": dict(reference), "evidence_class": item["evidence_class"],
                "source_identity": {"slice_id": slice_id,
                                    "packet_identity": reference["packet_identity"]},
                "evidence": item}
    if kind == "observation":
        registry = config.get("observation_registry")
        if not registry:
            raise EvidenceReferenceError("observation_registry_unavailable")
        try:
            item = ObservationStore(registry).read(reference["id"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EvidenceReferenceError("observation_unavailable") from exc
        record = item["record"]
        evidence_class = {"static": "static-source", "source": "source",
                          "controlled-test": "controlled-test",
                          "runtime": "observed-runtime"}[record["kind"]]
        return {"reference": dict(reference), "evidence_class": evidence_class,
                "source_identity": {"id": reference["id"], "provider": record["provider"],
                                    "scope": record["scope"], "original_kind": record["kind"]},
                "evidence": item}
    if kind == "source":
        spec = config.get("repos", {}).get(reference["repo_key"])
        if not isinstance(spec, dict):
            raise EvidenceReferenceError("source_repository_not_configured")
        if spec.get("revision") != reference["revision"]:
            raise EvidenceReferenceError("source_revision_changed")
        try:
            blob, content = source(spec["path"], reference["revision"], reference["path"])
        except (KeyError, OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
            raise EvidenceReferenceError("source_unavailable") from exc
        if blob != reference["blob_sha"]:
            raise EvidenceReferenceError("source_blob_changed")
        if len(content.encode()) > 100_000:
            raise EvidenceReferenceError("source_exceeds_bound")
        return {"reference": dict(reference), "evidence_class": "source",
                "source_identity": {"repo_key": reference["repo_key"],
                                    "revision": reference["revision"],
                                    "path": reference["path"], "blob_sha": blob},
                "evidence": content}
    evidence_id = reference["evidence_id"]
    paths = config.get("evidence")
    if not isinstance(paths, dict) or evidence_id not in paths:
        raise EvidenceReferenceError("lifecycle_evidence_not_allowlisted")
    revision = config.get("evidence_revision")
    if revision != reference["revision"]:
        raise EvidenceReferenceError("lifecycle_revision_changed")
    try:
        blob, content = source(config["evidence_repo"], revision, paths[evidence_id])
    except (KeyError, OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        raise EvidenceReferenceError("lifecycle_evidence_unavailable") from exc
    if blob != reference["blob_sha"]:
        raise EvidenceReferenceError("lifecycle_blob_changed")
    if len(content.encode()) > 100_000:
        raise EvidenceReferenceError("lifecycle_evidence_exceeds_bound")
    return {"reference": dict(reference), "evidence_class": "retained-experiment",
            "source_identity": {"evidence_id": evidence_id, "revision": revision,
                                "path": paths[evidence_id], "blob_sha": blob},
            "evidence": content}
