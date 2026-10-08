import subprocess

import pytest

from kp_agent_tooling_ops._impl.evidence_references import (
    EvidenceReferenceError, lifecycle_reference, observation_reference,
    packet_reference, reference_schema, resolve_reference, source_reference, _valid,
)
from kp_agent_tooling_ops._impl.observations import ObservationStore, make_record


def test_reference_schema_rejects_foreign_namespace_and_extra_locator():
    assert len(reference_schema()["oneOf"]) == 4
    assert _valid(packet_reference("slice", "entry", "a" * 64))
    assert _valid(observation_reference("b" * 64))
    assert not _valid({"kind": "url", "url": "https://example.invalid"})
    assert not _valid({**observation_reference("b" * 64), "path": "/tmp/other"})


def test_observation_resolves_preserved_original_without_provider_invocation(tmp_path):
    store = ObservationStore(tmp_path)
    scope = {"sources": {"ops": "a" * 40}, "binding_sha256": "b" * 64}
    record = make_record("serena", "call-1", "module::symbol", "static", scope,
                         {"symbol": "module::symbol"},
                         [{"predicate": "declaration", "value": True}], [],
                         ["Static result; no execution evidence"])
    key = store.append(record)
    resolved = resolve_reference({"observation_registry": str(tmp_path)}, observation_reference(key))
    assert resolved["evidence_class"] == "static-source"
    assert resolved["source_identity"]["original_kind"] == "static"
    assert resolved["evidence"]["original"] == {"symbol": "module::symbol"}
    with pytest.raises(EvidenceReferenceError, match="observation_unavailable"):
        resolve_reference({"observation_registry": str(tmp_path)}, observation_reference("c" * 64))


def test_packet_requires_original_packet_identity_and_existing_id(monkeypatch):
    from kp_agent_tooling_ops._impl import verification_packet

    calls = []
    def fake_packet(config, slice_id, **kwargs):
        calls.append((slice_id, kwargs))
        if kwargs["expected_packet_identity"] != "a" * 64:
            return {"status": "unverified", "gap": {"reason": "packet_identity_changed"}}
        if kwargs["evidence_ids"] != ["entry"]:
            return {"status": "invalid_selection"}
        return {"status": "ready", "evidence": {"entry": {"evidence_class": "source"}}}
    monkeypatch.setattr(verification_packet, "packet", fake_packet)
    ref = packet_reference("ats-pool-lifecycle", "entry", "a" * 64)
    assert resolve_reference({}, ref)["evidence_class"] == "source"
    assert calls[-1][1]["expected_packet_identity"] == "a" * 64
    with pytest.raises(EvidenceReferenceError, match="packet_identity_changed"):
        resolve_reference({}, {**ref, "packet_identity": "b" * 64})
    with pytest.raises(EvidenceReferenceError, match="unresolved_evidence_reference"):
        resolve_reference({}, {**ref, "id": "missing"})


def test_lifecycle_is_allowlisted_and_pinned_to_revision_and_blob(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "receipt.json").write_text('{"ok":true}')
    subprocess.run(["git", "-C", str(tmp_path), "add", "receipt.json"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "receipt"], check=True)
    rev = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    blob = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD:receipt.json"], text=True).strip()
    config = {"evidence": {"receipt": "receipt.json"}, "evidence_repo": str(tmp_path),
              "evidence_revision": rev}
    ref = lifecycle_reference("receipt", rev, blob)
    resolved = resolve_reference(config, ref)
    assert resolved["source_identity"]["blob_sha"] == blob
    with pytest.raises(EvidenceReferenceError, match="lifecycle_evidence_not_allowlisted"):
        resolve_reference(config, {**ref, "evidence_id": "../receipt.json"})
    with pytest.raises(EvidenceReferenceError, match="lifecycle_revision_changed"):
        resolve_reference(config, {**ref, "revision": "a" * 40})
    with pytest.raises(EvidenceReferenceError, match="lifecycle_blob_changed"):
        resolve_reference(config, {**ref, "blob_sha": "b" * 40})


def test_source_is_configured_regular_git_blob_with_exact_pins(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "module.py").write_text("value = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "module.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "source"], check=True)
    rev = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True).strip()
    blob = subprocess.check_output(["git", "-C", str(tmp_path), "rev-parse", "HEAD:module.py"], text=True).strip()
    config = {"repos": {"ops": {"path": str(tmp_path), "revision": rev}}}
    ref = source_reference("ops", "module.py", rev, blob)
    result = resolve_reference(config, ref)
    assert result["evidence_class"] == "source"
    assert result["evidence"] == "value = 1\n"
    with pytest.raises(EvidenceReferenceError, match="source_repository_not_configured"):
        resolve_reference(config, {**ref, "repo_key": "other"})
    with pytest.raises(EvidenceReferenceError, match="source_revision_changed"):
        resolve_reference(config, {**ref, "revision": "a" * 40})
    with pytest.raises(EvidenceReferenceError, match="source_blob_changed"):
        resolve_reference(config, {**ref, "blob_sha": "b" * 40})
    with pytest.raises(EvidenceReferenceError, match="source_unavailable"):
        resolve_reference(config, {**ref, "path": "../module.py"})
