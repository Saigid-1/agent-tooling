import pytest
import hashlib

from kp_agent_tooling_ops._impl.review_ledger import specification, validate_review_ledger


def test_ledger_preserves_requirements_and_checks_source_range():
    spec = specification([{"id": "R1", "text": "Every edge attribute is compared."},
                          {"id": "R2", "text": "Endpoint multiplicity is preserved."}])
    content = "alpha\nbeta\n"
    citation = {"reference": {"kind": "source", "repo_key": "core", "path": "x.py",
                              "revision": "a" * 40, "blob_sha": "b" * 40},
                "start_line": 2, "end_line": 2,
                "excerpt_sha256": hashlib.sha256(b"beta").hexdigest()}
    def resolve(_config, _ref):
        return {"evidence_class": "source", "evidence": content}
    def row(rid, text):
        return {"requirement_id": rid, "requirement_text": text, "decision": "supported",
                "oracle": "Compare exact source and scenario output", "evidence": [citation],
                "normalization_rule_ids": []}
    ledger = {"schema_version": "ops.review-ledger.v1", "specification": spec,
              "approved_normalization_rules": [],
              "reviews": [row("R1", spec["requirements"][0]["text"]), row("R2", spec["requirements"][1]["text"])]}
    assert validate_review_ledger({}, spec, ledger, resolver=resolve)["status"] == "valid"
    ledger["reviews"].pop()
    assert any(e["code"] == "missing_requirement:R2" for e in validate_review_ledger({}, spec, ledger, resolver=resolve)["errors"])
    ledger["reviews"].append(row("R2", "renamed"))
    assert any(e["code"] == "requirement_text_changed" for e in validate_review_ledger({}, spec, ledger, resolver=resolve)["errors"])
    ledger["reviews"][-1]["requirement_text"] = spec["requirements"][1]["text"]
    citation["excerpt_sha256"] = "0" * 64
    assert any(e["code"] == "source_excerpt_changed" for e in validate_review_ledger({}, spec, ledger, resolver=resolve)["errors"])


def test_ledger_does_not_accept_self_approved_normalization():
    spec = specification([{"id": "R", "text": "Exact text"}])
    rule = {"id": "N1", "requirement_id": "R", "rule": "Ignore timestamps", "approval_ref": "person-1"}
    ledger = {"schema_version": "ops.review-ledger.v1", "specification": spec,
              "approved_normalization_rules": [rule],
              "reviews": [{"requirement_id": "R", "requirement_text": "Exact text", "decision": "unverified",
                           "oracle": "Review", "evidence": [], "normalization_rule_ids": ["N1"]}]}
    result = validate_review_ledger({}, spec, ledger)
    assert result["status"] == "invalid"
    assert {e["code"] for e in result["errors"]} >= {"approval_registry_mismatch", "normalization_not_approved_for_requirement"}
    assert validate_review_ledger({}, spec, ledger, approved_rules=[rule])["status"] == "valid"


def test_malformed_review_fields_return_validation_errors():
    spec = specification([{"id": "D8", "text": "Preserve every endpoint."}])
    row = {"requirement_id": {"bad": "id"}, "requirement_text": [], "decision": [],
           "oracle": "Review", "evidence": [], "normalization_rule_ids": [{}]}
    ledger = {"schema_version": "ops.review-ledger.v1", "specification": spec,
              "approved_normalization_rules": [], "reviews": [row]}
    result = validate_review_ledger({}, spec, ledger)
    assert result["status"] == "invalid"
    assert any(e["code"] == "missing_requirement:D8" for e in result["errors"])
    row["requirement_id"] = "renamed-D8"
    assert any(e["code"] == "unknown_or_renamed_requirement" for e in validate_review_ledger({}, spec, ledger)["errors"])


def test_source_citation_rechecks_git_blob_and_exact_lines(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "demo.py").write_text("first\nsecond\nthird\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "demo.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "fixture"], check=True)
    revision = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    blob = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD:demo.py"], text=True).strip()
    spec = specification([{"id": "D8", "text": "Third line exists"}])
    citation = {"reference": {"kind": "source", "repo_key": "demo", "path": "demo.py",
                              "revision": revision, "blob_sha": blob}, "start_line": 3, "end_line": 3,
                "excerpt_sha256": hashlib.sha256(b"third").hexdigest()}
    ledger = {"schema_version": "ops.review-ledger.v1", "specification": spec,
              "approved_normalization_rules": [],
              "reviews": [{"requirement_id": "D8", "requirement_text": "Third line exists",
                           "decision": "supported", "oracle": "Inspect third line", "evidence": [citation],
                           "normalization_rule_ids": []}]}
    config = {"repos": {"demo": {"path": str(repo), "revision": revision}}}
    assert validate_review_ledger(config, spec, ledger)["status"] == "valid"
    citation["end_line"] = 4
    assert any(e["code"] == "invalid_source_range" for e in validate_review_ledger(config, spec, ledger)["errors"])


@pytest.mark.skip(reason='S5 excluded: needs OPS-only scripts/opencode_trial_capture.py')
def test_capture_publisher_keeps_transcript_separate(tmp_path):
    from scripts.opencode_trial_capture import publish_answer
    import json
    from pathlib import Path
    messages = json.loads(Path(__file__).with_name("fixtures_opencode_trial66_sanitized.json").read_text())
    transcript = tmp_path / "messages.json"
    transcript.write_text(json.dumps(messages))
    answer = tmp_path / "answer.md"
    assert publish_answer(messages, answer)["status"] == "published"
    assert answer.read_text() == "sanitized text\n"
    assert json.loads(transcript.read_text()) == messages
