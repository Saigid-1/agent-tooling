"""Cross-tool citation checks through the public finding validator."""
import copy
import subprocess

from kp_agent_tooling_ops._impl.evidence_references import lifecycle_reference, observation_reference
from kp_agent_tooling_ops._impl.observations import ObservationStore, make_record
from kp_agent_tooling_ops._impl.verification_finding import validate_finding


def _git(*args, cwd):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def _fixture(tmp_path):
    repo = tmp_path / "evidence"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "receipt.json").write_text('{"profile":"candidate","candidate_sha256":"' + "c" * 64 + '"}')
    subprocess.run(["git", "add", "receipt.json"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "receipt"], cwd=repo, check=True)
    revision = _git("rev-parse", "HEAD", cwd=repo)
    blob = _git("rev-parse", "HEAD:receipt.json", cwd=repo)
    scope = {"sources": {"ops": revision}, "binding_sha256": "b" * 64}
    config = {"repos": {"ops": {"revision": revision}}, "observation_registry": str(tmp_path / "observations"),
              "evidence_repo": str(repo), "evidence_revision": revision,
              "evidence": {"trial": "receipt.json"}}
    return config, scope, lifecycle_reference("trial", revision, blob)


def _observe(config, scope, *, kind="static", subject="module::symbol", original=None):
    original = original or {"symbol": "module::symbol"}
    record = make_record("serena" if kind == "static" else "retained-run", "invocation-1",
                         subject, kind, scope, original,
                         [{"predicate": "present", "value": True}], [],
                         ["Bounded retained observation; no semantic certification"])
    return ObservationStore(config["observation_registry"]).append(record)


def _finding(scope, refs, *, assessment=None, execution=None):
    return {"schema_version": "ops.verification-finding.v5",
            "claim": {"subject": "module::symbol", "target": "candidate",
                      "predicate": "present", "expected_value": True},
            "outcome": "supported",
            "scope": {"scenario": "retained evidence", "sources": scope["sources"],
                      "execution_evidence": execution},
            "packets": [],
            "evidence": [{"reference": ref, "establishes": "bounded evidence",
                          "limitation": "review required"} for ref in refs],
            "delivery": {"complete": list(refs), "gaps": []},
            "unresolved": [], "maintenance_trigger": ["source or evidence changes"],
            "memory_status": "not-promoted", "observation_assessment": assessment}


def _assessment(scope, *ids):
    return {"ids": list(ids), "expected_scope": scope,
            "declared_status": "compatible", "use": "joint_support"}


def _codes(result):
    return {row["code"] for row in result["errors"]}


def test_static_serena_observation_can_support_source_only_finding(tmp_path):
    config, scope, _ = _fixture(tmp_path)
    key = _observe(config, scope)
    finding = _finding(scope, [observation_reference(key)], assessment=_assessment(scope, key))
    result = validate_finding(config, finding)
    assert result["status"] == "valid", result["errors"]


def test_allowlisted_lifecycle_blob_requires_execution_identity(tmp_path):
    config, scope, lifecycle = _fixture(tmp_path)
    key = _observe(config, scope, kind="runtime",
                   original={"profile": "candidate", "candidate_sha256": "c" * 64})
    refs = [observation_reference(key), lifecycle]
    execution = {"profile": "candidate", "candidate_sha256": "c" * 64,
                 "execution_revision": config["evidence_revision"]}
    finding = _finding(scope, refs, assessment=_assessment(scope, key), execution=execution)
    assert "observation_applicability_missing" in _codes(validate_finding(config, finding))
    # This generic receipt lacks target/scenario/runtime-service provenance. It
    # remains usable as separate context while retaining runtime identity checks.
    finding["observation_assessment"]["use"] = "separate_context"
    assert validate_finding(config, finding)["status"] == "valid"
    finding["scope"]["execution_evidence"] = None
    assert "execution_evidence_identity_required" in _codes(validate_finding(config, finding))


def test_wrong_namespace_and_stale_observation_source_are_rejected(tmp_path):
    config, scope, lifecycle = _fixture(tmp_path)
    key = _observe(config, scope)
    finding = _finding(scope, [observation_reference(key)], assessment=_assessment(scope, key))
    wrong = copy.deepcopy(finding)
    wrong["evidence"][0]["reference"] = {"kind": "packet", "slice_id": "trial",
                                             "id": key, "packet_identity": "a" * 64}
    wrong["delivery"]["complete"] = [wrong["evidence"][0]["reference"]]
    assert validate_finding(config, wrong)["status"] == "invalid"
    stale_scope = {"sources": {"ops": "a" * 40}, "binding_sha256": scope["binding_sha256"]}
    stale_key = _observe(config, stale_scope)
    stale = _finding(scope, [observation_reference(stale_key)],
                     assessment=_assessment(stale_scope, stale_key))
    assert "observation_source_mismatch" in _codes(validate_finding(config, stale))
    wrong_lifecycle = _finding(scope, [{**lifecycle, "evidence_id": "unlisted"}])
    assert "lifecycle_evidence_not_allowlisted" in _codes(validate_finding(config, wrong_lifecycle))


def test_delivery_complete_must_resolve_original_body(tmp_path):
    config, scope, _ = _fixture(tmp_path)
    key = _observe(config, scope)
    finding = _finding(scope, [observation_reference(key)], assessment=_assessment(scope, key))
    finding["delivery"]["complete"].append(observation_reference("f" * 64))
    assert "observation_unavailable" in _codes(validate_finding(config, finding))
