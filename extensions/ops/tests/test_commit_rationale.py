"""The pilot uses synthetic commits; live snooze SHAs remain an acceptance gap."""

import hashlib
import json
import subprocess
from datetime import datetime
from types import SimpleNamespace

import pytest

from kp_agent_tooling_ops._impl.commit_rationale import MAX_MANIFEST_BYTES, RationaleCatalog, RationaleError
from kp_agent_tooling_ops._impl.tool_discovery import GitSource


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args]).decode().strip()


def commit(repo, path, text, subject, body):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git(repo, "add", path)
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test",
        "commit", "-m", subject, "-m", body)
    return git(repo, "rev-parse", "HEAD")


def entry(sha, body, **overrides):
    result = {"id": "snooze-limit", "repo_key": "ops", "commit": sha,
              "body_lines": [1, 1], "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
              "owner": "OPS", "reviewer": "Reviewer A", "review_state": "reviewed",
              "lifecycle": "current", "artifact_refs": ["docs/snooze.md"],
              "questions": ["Why is snooze capped?"]}
    result.update(overrides)
    return result


def catalog(tmp_path, repo, entries):
    path = tmp_path / "rationale.json"
    path.write_text(json.dumps({"schema_version": "ops.commit-rationale.v1", "entries": entries}))
    return path, RationaleCatalog(path, {"ops": repo})


def test_reviewed_alias_cites_exact_commit_body_and_abstains_on_loose_match(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    body = "Snooze is capped because reminders must return.\nThe cap is seven days."
    sha = commit(repo, "docs/snooze.md", "snooze policy\n", "Bound snooze", body)
    _, source = catalog(tmp_path, repo, [entry(sha, body)])

    found = source.search("ops", "Why is snooze capped?", sha)
    assert found["status"] == "evidence"
    assert found["answerability"] == "source_passage_only"
    assert found["results"][0]["excerpt"] == "Snooze is capped because reminders must return."
    assert found["results"][0]["body_lines"] == [1, 1]
    assert found["results"][0]["commit"] == sha
    assert found["results"][0]["artifact_refs"] == ["docs/snooze.md"]
    assert found["results"][0]["source_lineage"] == "ancestor"

    loose = source.search("ops", "Are snooze reminders capped at seven days?", sha)
    compound = source.search("ops", "Why is snooze capped? Does websocket delivery work?", sha)
    assert compound["results"] and compound["question_coverage"]["status"] == "unverified"
    assert compound["question_coverage"]["gap"] == "subquestion_coverage_not_established"
    assert loose["status"] == "unverified"
    assert loose["results"][0]["id"] == "snooze-limit"
    assert loose["fallback"]["max_commits"] == 50
    absent = source.search("ops", "Who owns the invoice flow?", sha)
    assert absent["status"] == "missing" and absent["results"] == []


def test_unreviewed_superseded_and_future_branch_do_not_serve(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    body = "Snooze is capped because reminders must return."
    sha = commit(repo, "docs/snooze.md", "policy\n", "Policy", body)
    original_branch = git(repo, "branch", "--show-current")
    git(repo, "checkout", "-b", "other")
    future = commit(repo, "docs/snooze.md", "different\n", "Future", body)
    git(repo, "checkout", original_branch)
    for altered in (entry(sha, body, review_state="unreviewed"),
                    entry(sha, body, lifecycle="historical"),
                    entry(sha, body, lifecycle="superseded", superseded_by=future),
                    entry(future, body)):
        _, source = catalog(tmp_path, repo, [altered])
        assert source.search("ops", "Why is snooze capped?", sha)["status"] == "missing"


def test_source_digest_references_and_manifest_change_fail_closed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    body = "Snooze is capped because reminders must return."
    sha = commit(repo, "docs/snooze.md", "policy\n", "Policy", body)
    with pytest.raises(RationaleError, match="body changed"):
        catalog(tmp_path, repo, [entry(sha, body, body_sha256="0" * 64)])
    with pytest.raises(RationaleError, match="referenced artifact"):
        catalog(tmp_path, repo, [entry(sha, body, artifact_refs=["docs/absent.md"])])
    path, source = catalog(tmp_path, repo, [entry(sha, body)])
    path.write_text(json.dumps({"schema_version": "ops.commit-rationale.v1", "entries": []}))
    with pytest.raises(RationaleError, match="changed during response"):
        source.search("ops", "Why is snooze capped?", sha)


def test_incremental_plan_is_stable_and_revocation_is_explicit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    body = "Snooze is capped because reminders must return."
    sha = commit(repo, "docs/snooze.md", "policy\n", "Policy", body)
    _, source = catalog(tmp_path, repo, [entry(sha, body)])
    first = source.refresh_plan({})
    assert first["upsert"] == ["snooze-limit"]
    replay = source.refresh_plan(first["snapshot"])
    assert replay["upsert"] == replay["remove"] == []
    assert replay["unchanged"] == ["snooze-limit"]
    _, withdrawn = catalog(tmp_path, repo, [entry(sha, body, lifecycle="withdrawn")])
    assert withdrawn.refresh_plan(first["snapshot"])["remove"] == ["snooze-limit"]


def test_alias_collision_and_alias_update_are_reviewed_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    body = "Snooze is capped because reminders must return."
    sha = commit(repo, "docs/snooze.md", "policy\n", "Policy", body)
    with pytest.raises(RationaleError, match="ambiguous reviewed question"):
        catalog(tmp_path, repo, [entry(sha, body), entry(sha, body, id="second")])
    _, first = catalog(tmp_path, repo, [entry(sha, body)])
    _, changed = catalog(tmp_path, repo, [entry(sha, body, questions=["Why limit snooze?"])])
    plan = changed.refresh_plan(first.snapshot())
    assert plan["upsert"] == ["snooze-limit"]
    assert plan["unchanged"] == []


def test_supersession_requires_replacement_and_git_failure_is_not_a_miss(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    body = "Snooze is capped because reminders must return."
    sha = commit(repo, "docs/snooze.md", "policy\n", "Policy", body)
    with pytest.raises(RationaleError, match="supersession requires"):
        catalog(tmp_path, repo, [entry(sha, body, lifecycle="superseded")])
    _, source = catalog(tmp_path, repo, [entry(sha, body)])
    monkeypatch.setattr("kp_agent_tooling_ops._impl.commit_rationale.subprocess.run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=2))
    with pytest.raises(RationaleError, match="ancestry unavailable"):
        source._ancestor(SimpleNamespace(repository=repo), sha, sha)


def test_manifest_byte_limit_is_checked_before_decoding(tmp_path):
    path = tmp_path / "too-large.json"
    path.write_bytes(b" " * (MAX_MANIFEST_BYTES + 1))
    with pytest.raises(RationaleError, match="64 KiB"):
        RationaleCatalog(path, {})


def test_body_citations_preserve_carriage_returns():
    from kp_agent_tooling_ops._impl.commit_rationale import _body
    raw = b"tree " + b"a" * 40 + b"\n\nSubject\n\nFirst\r\nSecond\r\n"

    class Source:
        def _git(self, *args):
            if args[1] == "-t":
                return b"commit\n"
            if args[1] == "-s":
                return str(len(raw)).encode()
            return raw

    lines, digest = _body(Source(), "b" * 40)
    assert lines == ["First\r", "Second\r"]
    assert digest == hashlib.sha256(b"First\r\nSecond\r").hexdigest()


def test_reviewed_cross_repository_artifact_excerpt_has_separate_identity(tmp_path):
    ats = tmp_path / "ats"
    core = tmp_path / "core"
    ats.mkdir(); core.mkdir()
    git(ats, "init"); git(core, "init")
    body = "The task snooze limit lets reminders return."
    ats_sha = commit(ats, "docs/snooze.md", "historical app source\n", "Explain snooze", body)
    implementation = "def snooze(days):\n    return min(days, 7)\n"
    core_sha = commit(core, "kp_core/api/routers/tasks.py", implementation,
                      "Implement task snooze", "Bound the days argument.")
    row = entry(ats_sha, body, current_artifact_refs=[{
        "repo_key": "core", "revision": core_sha, "path": "kp_core/api/routers/tasks.py",
        "start_line": 1, "end_line": 2}])
    manifest = tmp_path / "rationale.json"
    manifest.write_text(json.dumps({"schema_version": "ops.commit-rationale.v1", "entries": [row]}))
    source = RationaleCatalog(manifest, {"ops": ats, "core": core})
    result = source.search("ops", "Why is snooze capped?", ats_sha)
    assert result["status"] == "evidence"
    cited = result["results"][0]
    assert cited["excerpt"] == body
    assert cited["excerpt_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    source_dates = git(ats, "show", "-s", "--format=%aI%n%cI", ats_sha).splitlines()
    assert [cited["author_date"], cited["committer_date"]] == source_dates
    assert all(datetime.fromisoformat(value).utcoffset() is not None for value in source_dates)
    current = cited["current_artifact_refs"][0]
    assert current["repo_key"] == "core" and current["revision"] == core_sha
    assert current["blob_sha"] == git(core, "rev-parse", f"{core_sha}:kp_core/api/routers/tasks.py")
    assert current["excerpt"] == implementation.rstrip("\n")
    assert current["excerpt_sha256"] == hashlib.sha256(current["excerpt"].encode()).hexdigest()
    assert current["relationship"] == "reviewer_declared"
    assert current["semantic_equivalence"] == "not-established"
    assert current["runtime_execution"] == "not-assessed"


def test_distinct_slices_of_one_current_file_are_valid_but_duplicates_fail(tmp_path):
    ats = tmp_path / "ats"
    core = tmp_path / "core"
    ats.mkdir(); core.mkdir()
    git(ats, "init"); git(core, "init")
    body = "Original design intent belongs to the app commit."
    ats_sha = commit(ats, "docs/design.md", "historic\n", "Explain", body)
    core_sha = commit(core, "tasks.py", "first = 1\nsecond = 2\n", "Implement", "Code body.")
    first = {"repo_key": "core", "revision": core_sha, "path": "tasks.py",
             "start_line": 1, "end_line": 1}
    second = {**first, "start_line": 2, "end_line": 2}
    manifest = tmp_path / "rationale.json"
    manifest.write_text(json.dumps({"schema_version": "ops.commit-rationale.v1", "entries": [
        entry(ats_sha, body, artifact_refs=["docs/design.md"],
              current_artifact_refs=[first, second])]}))
    source = RationaleCatalog(manifest, {"ops": ats, "core": core})
    result = source.search("ops", "Why is snooze capped?", ats_sha)["results"][0]
    assert result["excerpt"] == body
    assert result["excerpt_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert [ref["excerpt"] for ref in result["current_artifact_refs"]] == ["first = 1", "second = 2"]
    assert [ref["excerpt_sha256"] for ref in result["current_artifact_refs"]] == [
        hashlib.sha256(b"first = 1").hexdigest(), hashlib.sha256(b"second = 2").hexdigest()]
    manifest.write_text(json.dumps({"schema_version": "ops.commit-rationale.v1", "entries": [
        entry(ats_sha, body, artifact_refs=["docs/design.md"],
              current_artifact_refs=[first, first])]}))
    with pytest.raises(RationaleError, match="duplicate current artifact"):
        RationaleCatalog(manifest, {"ops": ats, "core": core})


def test_cross_repository_refs_fail_closed_on_bad_identity_and_span(tmp_path):
    ats = tmp_path / "ats"
    core = tmp_path / "core"
    ats.mkdir(); core.mkdir()
    git(ats, "init"); git(core, "init")
    body = "The task snooze limit lets reminders return."
    ats_sha = commit(ats, "docs/snooze.md", "source\n", "Explain", body)
    core_sha = commit(core, "tasks.py", "one\ntwo\n", "Implement", "Implementation body.")
    manifest = tmp_path / "rationale.json"
    baseline = {"repo_key": "core", "revision": core_sha, "path": "tasks.py"}
    variants = [
        ({**baseline, "repo_key": "unknown"}, "configured repository"),
        ({**baseline, "revision": "0" * 40}, "pinned current artifact unavailable"),
        ({**baseline, "path": "missing.py"}, "regular file"),
        ({**baseline, "start_line": 1}, "both endpoints"),
        ({**baseline, "start_line": 1, "end_line": 81}, "1..80 lines"),
        ({**baseline, "start_line": 1, "end_line": 3}, "range unavailable"),
    ]
    for artifact_ref, expected in variants:
        manifest.write_text(json.dumps({"schema_version": "ops.commit-rationale.v1",
                                        "entries": [entry(ats_sha, body, current_artifact_refs=[artifact_ref])]}))
        with pytest.raises(RationaleError, match=expected):
            RationaleCatalog(manifest, {"ops": ats, "core": core})


def test_detached_history_requires_exact_reviewed_target_and_current_link(tmp_path):
    ats = tmp_path / "ats"
    core = tmp_path / "core"
    ats.mkdir(); core.mkdir()
    git(ats, "init"); git(core, "init")
    base = commit(ats, "docs/base.md", "base\n", "Base", "Base rationale.")
    source_branch = git(ats, "branch", "--show-current")
    rationale = "A bounded snooze lets reminders return."
    old_sha = commit(ats, "docs/snooze.md", "old source\n", "Explain", rationale)
    git(ats, "checkout", "-b", "detached-target", base)
    target_sha = commit(ats, "docs/base.md", "new baseline\n", "New baseline", "New branch.")
    other_target = commit(ats, "docs/base.md", "later baseline\n", "Later", "Later branch.")
    git(ats, "checkout", source_branch)
    core_sha = commit(core, "tasks.py", "def snooze():\n    return 7\n", "Implement", "Code body.")
    current = {"repo_key": "core", "revision": core_sha, "path": "tasks.py",
               "start_line": 1, "end_line": 2}
    approved = entry(old_sha, rationale, current_artifact_refs=[current],
                     detached_history_review={"target_revision": target_sha,
                                              "reason": "Reviewed pre-split app rationale linked to pinned lib source."})
    manifest = tmp_path / "rationale.json"
    manifest.write_text(json.dumps({"schema_version": "ops.commit-rationale.v1", "entries": [approved]}))
    source = RationaleCatalog(manifest, {"ops": ats, "core": core})
    exact = source.search("ops", "Why is snooze capped?", target_sha)
    assert exact["status"] == "evidence"
    assert exact["results"][0]["source_lineage"] == "reviewer_declared_detached_history"
    assert exact["results"][0]["applicability"] == "not-established"
    assert exact["results"][0]["detached_history_reason"] == approved["detached_history_review"]["reason"]
    assert exact["results"][0]["excerpt"] == rationale
    assert source.search("ops", "Why is snooze capped?", other_target)["status"] == "missing"
    assert source.search("ops", "Why is snooze capped?", old_sha)["results"][0]["source_lineage"] == "ancestor"

    invalid = [
        entry(old_sha, rationale, detached_history_review=approved["detached_history_review"]),
        entry(old_sha, rationale, current_artifact_refs=[current], review_state="unreviewed",
              detached_history_review=approved["detached_history_review"]),
        entry(old_sha, rationale, current_artifact_refs=[current],
              detached_history_review={"target_revision": target_sha, "reason": " "}),
    ]
    for row in invalid:
        manifest.write_text(json.dumps({"schema_version": "ops.commit-rationale.v1", "entries": [row]}))
        with pytest.raises(RationaleError, match="detached history"):
            RationaleCatalog(manifest, {"ops": ats, "core": core})


def test_git_replace_refs_cannot_substitute_pinned_body_or_ancestry(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init')
    base = commit(repo, 'docs/snooze.md', 'base\n', 'Base', 'Base body.')
    rationale = 'The actual pinned rationale is this body.'
    pinned = commit(repo, 'docs/snooze.md', 'bounded\n', 'Pinned', rationale)
    target = commit(repo, 'docs/other.md', 'later\n', 'Later', 'Later body.')
    # Git normally replaces the bytes and parent chain returned under the old SHA.
    git(repo, 'replace', pinned, base)
    _, source = catalog(tmp_path, repo, [entry(pinned, rationale)])
    result = source.search('ops', 'Why is snooze capped?', target)
    assert result['status'] == 'evidence'
    assert result['results'][0]['commit'] == pinned
    assert result['results'][0]['excerpt'] == rationale
    assert source._ancestor(GitSource(repo, target), base, target)


def _compound_fixture(tmp_path):
    ats, core = tmp_path / "ats", tmp_path / "core"
    ats.mkdir(); core.mkdir()
    git(ats, "init"); git(core, "init")
    snooze_body = "Snooze changes due_at.\nNo lifecycle event is emitted."
    snooze = commit(ats, "docs/snooze.md", "snooze\n", "Ruling", snooze_body)
    foundation_body = "The bridge handles notifications.\nVerified: login to candidate modal."
    foundation = commit(ats, "docs/bridge.md", "bridge\n", "Foundation", foundation_body)
    core_body = "Production login had no tenant context.\nDeployment manifests still need DB environment."
    core_commit = commit(core, "service/ws.js", "const bridge = true;\n", "Production gap", core_body)
    snooze_row = entry(snooze, snooze_body, id="snooze", questions=["Why is snooze due_at?"],
                        evidence_scope={"observations": [{"kind": "historical_intent", "body_lines": [1, 1]}],
                                        "not_established": ["Current runtime behavior"]})
    foundation_row = entry(foundation, foundation_body, id="foundation",
                           artifact_refs=["docs/bridge.md"], questions=["What was verified in foundation?"],
                           body_lines=[1, 2],
                           evidence_scope={"observations": [{"kind": "author_reported_verification", "body_lines": [2, 2]}],
                                           "not_established": ["Live card moves", "Real-login production behavior"]})
    core_row = entry(core_commit, core_body, id="production-gap", repo_key="core",
                     artifact_refs=["service/ws.js"], questions=["What production gap was reported?"],
                     body_lines=[1, 2],
                     current_artifact_refs=[{"repo_key": "ops", "revision": foundation, "path": "docs/bridge.md"}],
                     evidence_scope={"observations": [
                         {"kind": "reported_defect", "body_lines": [1, 1]},
                         {"kind": "deployment_prerequisite", "body_lines": [2, 2]}],
                         "not_established": ["Production fix after deployment"]})
    route = {"repo_key": "ops", "target_revision": foundation,
             "questions": ["Why snooze due_at and were WebSocket card moves proven?"],
             "groups": [
                 {"id": "snooze", "repo_key": "ops", "target_revision": foundation, "entry_ids": ["snooze"]},
                 {"id": "foundation", "repo_key": "ops", "target_revision": foundation, "entry_ids": ["foundation"]},
                 {"id": "production", "repo_key": "core", "target_revision": core_commit, "entry_ids": ["production-gap"]}],
             "not_established": ["Date of Core split", "Running production behavior"]}
    rows = [snooze_row, foundation_row, core_row]
    manifest = tmp_path / "rationale.json"

    def load(*, changed_rows=None, changed_route=None):
        manifest.write_text(json.dumps({"schema_version": "ops.commit-rationale.v1",
                                        "entries": rows if changed_rows is None else changed_rows,
                                        "compound_questions": [route if changed_route is None else changed_route]}))
        return RationaleCatalog(manifest, {"ops": ats, "core": core},
                                {"ops": foundation, "core": core_commit})

    return load, rows, route, foundation


def test_compound_route_keeps_three_pinned_evidence_groups_and_unverified_claims(tmp_path):
    load, _, route, target = _compound_fixture(tmp_path)
    source = load()
    found = source.search("ops", route["questions"][0], target)
    assert found["status"] == "unverified"
    assert found["answerability"] == "claim_scope_unverified"
    assert found["coverage_basis"] == "reviewed_compound_route"
    assert found["not_established"] == ["Date of Core split", "Running production behavior"]
    assert [group["id"] for group in found["groups"]] == ["snooze", "foundation", "production"]
    assert [item["id"] for item in found["results"]] == ["snooze", "foundation", "production-gap"]
    assert found["omitted"] == 0
    assert found["groups"][1]["result_ids"] == ["foundation"]
    foundation = found["results"][1]
    assert foundation["evidence_scope"]["coverage_basis"] == "reviewer_annotation"
    verification = foundation["evidence_scope"]["observations"][0]
    assert verification["kind"] == "author_reported_verification"
    assert verification["excerpt"] == "Verified: login to candidate modal."
    assert verification["proof_scope"] == "author_stated_only"
    assert foundation["evidence_scope"]["not_established"] is None
    assert foundation["evidence_scope"]["entry_not_established"] == ["Live card moves", "Real-login production behavior"]
    assert foundation["evidence_scope"]["gap_scope"] == "not_assessed_for_this_question"
    prerequisite = found["results"][2]["evidence_scope"]["observations"][1]
    assert prerequisite["kind"] == "deployment_prerequisite"
    assert prerequisite["excerpt"] == "Deployment manifests still need DB environment."
    assert found["groups"][2]["target_revision"] == found["results"][2]["commit"]

    bounded = source.search("ops", route["questions"][0], target, top_k=1)
    assert [item["id"] for item in bounded["results"]] == ["snooze"]
    assert bounded["omitted"] == 2
    assert bounded["groups"][1]["omitted_entry_ids"] == ["foundation"]
    assert bounded["groups"][2]["omitted_entry_ids"] == ["production-gap"]
    assert bounded["fallback"]["method"] == "read_omitted_reviewed_groups"
    recovered = source.search("ops", route["questions"][0], target, top_k=5)
    assert recovered["omitted"] == 0
    assert [item["id"] for item in recovered["results"]] == ["snooze", "foundation", "production-gap"]


def test_compound_route_rejects_unlinked_or_unadmitted_cross_repository_evidence(tmp_path):
    load, rows, route, target = _compound_fixture(tmp_path)
    with pytest.raises(RationaleError, match="pinned anchor link"):
        load(changed_rows=[*rows[:2], {**rows[2], "current_artifact_refs": []}])
    with pytest.raises(RationaleError, match="not admitted"):
        load(changed_rows=[*rows[:2], {**rows[2], "lifecycle": "withdrawn"}])
    with pytest.raises(RationaleError, match="configured pinned anchor"):
        load(changed_route={**route, "target_revision": "bad"})
    with pytest.raises(RationaleError, match="collides"):
        load(changed_route={**route, "questions": ["Why is snooze due_at?"]})
    source = load()
    assert source.search("ops", route["questions"][0], target)["omitted"] == 0
    assert source.search("ops", route["questions"][0], rows[0]["commit"])["status"] != "evidence"


def test_reviewed_scope_and_compound_route_changes_enter_refresh_plan(tmp_path):
    load, rows, route, target = _compound_fixture(tmp_path)
    original = load()
    checkpoint = original.snapshot()
    route_keys = [key for key in checkpoint if key.startswith("compound:")]
    assert len(route_keys) == 1
    changed_route = {**route, "not_established": ["Date of Core split", "Runtime not observed"]}
    updated = load(changed_route=changed_route)
    assert updated.refresh_plan(checkpoint)["upsert"] == route_keys
    scope = {**rows[0]["evidence_scope"], "not_established": ["Runtime not observed"]}
    changed = load(changed_rows=[{**rows[0], "evidence_scope": scope}, *rows[1:]])
    assert "snooze" in changed.refresh_plan(updated.snapshot())["upsert"]
    assert changed.search("ops", route["questions"][0], target)["status"] == "unverified"


def test_evidence_scope_requires_bounded_cited_lines_and_known_kind(tmp_path):
    load, rows, _, _ = _compound_fixture(tmp_path)
    invalid = [
        {"observations": [{"kind": ["author_reported_verification"], "body_lines": [1, 1]}], "not_established": []},
        {"observations": [{"kind": "runtime_verified", "body_lines": [1, 1]}], "not_established": []},
        {"observations": [{"kind": "historical_intent", "body_lines": [3, 3]}], "not_established": []},
        {"observations": [{"kind": "historical_intent", "body_lines": [1, 1]}], "not_established": [" "]},
    ]
    for scope in invalid:
        with pytest.raises(RationaleError):
            load(changed_rows=[{**rows[0], "evidence_scope": scope}, *rows[1:]])


def test_disputed_relayed_claim_is_never_evidence_and_compact_preserves_counter_source(tmp_path):
    load, rows, route, target = _compound_fixture(tmp_path)
    # The counter-statement is independently attributed source text, not an adjudication.
    rows[2]['contested_by'] = [{'entry_id': 'foundation', 'body_lines': [2, 2]}]
    rows[2]['review_provenance'] = {'kind': 'agent', 'reviewer_id': 'agent:test', 'source': 'fixture review'}
    rows[2]['evidence_scope']['observations'][0] = {
        'kind': 'relayed_observation', 'body_lines': [1, 1],
        'attribution': {'reported_by': 'commit author', 'attributed_to': 'requester'}}
    full = load(changed_rows=rows).search('ops', route['questions'][0], target)
    assert full['status'] == 'unverified' and full['answerability'] == 'claim_scope_unverified'
    assert full['retrieval_status'] == 'unverified' and full['dispute_status'] == 'disputed'
    assert full['evidence_kind'] == 'reviewed_source_passages'
    claim = full['results'][2]
    assert claim['review_label'] == 'agent-reviewed'
    assert claim['review_state'] == 'reviewed'
    assert claim['claim_status'] == 'disputed'
    assert claim['source_authorship']['identity_authenticated'] is False
    assert claim['evidence_scope']['observations'][0]['direct_observation'] is False
    assert claim['contested_by'][0]['source']['commit'] == rows[1]['commit']
    assert claim['contested_by'][0]['counter_statement'] == 'Verified: login to candidate modal.'
    compact = load(changed_rows=rows).search('ops', route['questions'][0], target, response_mode='compact')
    assert compact['status'] == 'unverified'
    assert compact['retrieval_status'] == 'unverified'
    assert 'excerpt' not in compact['results'][2]
    assert compact['results'][2]['body_sha256'] == claim['body_sha256']
    assert compact['results'][2]['contested_by'] == claim['contested_by']
    assert compact['expand_call']['arguments']['response_mode'] == 'full'


def test_exact_dispute_is_not_evidence_but_candidate_retrieval_stays_unverified(tmp_path):
    load, rows, _, target = _compound_fixture(tmp_path)
    rows[2]['contested_by'] = [{'entry_id': 'foundation', 'body_lines': [2, 2]}]
    source = load(changed_rows=rows)
    exact = source.search('core', rows[2]['questions'][0], rows[2]['commit'])
    assert exact['retrieval_status'] == 'evidence'
    assert exact['status'] == 'disputed'
    assert exact['answerability'] == 'conflicting_testimony'
    assert exact['results'][0]['claim_status'] == 'disputed'
    assert exact['results'][0]['review_state'] == 'reviewed'
    assert exact['claim_status'] == 'disputed'
    counter_source = exact['results'][0]['contested_by'][0]['source']
    assert counter_source['target_revision'] == target
    assert counter_source['source_lineage'] == 'ancestor'

    candidate = source.search('core', 'Was the production login gap reported?', rows[2]['commit'])
    assert candidate['retrieval_status'] == candidate['status'] == 'unverified'
    assert candidate['evidence_kind'] == 'candidate_only'
    assert candidate['answerability'] == 'unverified'
    assert candidate['dispute_status'] == 'disputed'
    assert candidate['results'][0]['claim_status'] == 'disputed'


def test_counter_statement_requires_lineage_at_requested_revision_or_detached_review(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init')
    base = commit(repo, 'docs/snooze.md', 'base\n', 'Base', 'Base rationale.')
    main_branch = git(repo, 'branch', '--show-current')
    claim_body = 'Snooze has a seven day cap.'
    claim_sha = commit(repo, 'docs/snooze.md', 'cap\n', 'Claim', claim_body)
    git(repo, 'checkout', '-b', 'counter-branch', base)
    counter_body = 'Snooze has a fourteen day cap.'
    counter_sha = commit(repo, 'docs/snooze.md', 'counter\n', 'Counter', counter_body)
    git(repo, 'checkout', main_branch)
    claim = entry(claim_sha, claim_body, id='claim', contested_by=[{'entry_id': 'counter', 'body_lines': [1, 1]}])
    counter = entry(counter_sha, counter_body, id='counter', questions=['What did the counter say?'])
    _, source = catalog(tmp_path, repo, [claim, counter])
    with pytest.raises(RationaleError, match='counter-statement is not admitted'):
        source.search('ops', claim['questions'][0], claim_sha)

    counter['current_artifact_refs'] = [{'repo_key': 'ops', 'revision': claim_sha, 'path': 'docs/snooze.md'}]
    counter['detached_history_review'] = {'target_revision': claim_sha, 'reason': 'Reviewed pre-split counter statement.'}
    _, source = catalog(tmp_path, repo, [claim, counter])
    found = source.search('ops', claim['questions'][0], claim_sha)
    assert found['status'] == 'disputed'
    assert found['results'][0]['contested_by'][0]['source']['commit'] == counter_sha


def test_question_gaps_do_not_leak_across_aliases_or_mutate_cached_entries(tmp_path):
    load, rows, route, target = _compound_fixture(tmp_path)
    rows[0]['questions'].append('What is the runtime evidence for snooze?')
    rows[0]['question_gaps'] = {rows[0]['questions'][0]: ['Reason applicability today'],
                              rows[0]['questions'][1]: ['Current runtime execution']}
    source = load(changed_rows=rows)
    one = source.search('ops', rows[0]['questions'][0], target)
    assert one['results'][0]['evidence_scope']['not_established'] == ['Reason applicability today']
    two = source.search('ops', rows[0]['questions'][1], target)
    assert two['results'][0]['evidence_scope']['not_established'] == ['Current runtime execution']
    assert one['results'][0]['evidence_scope']['not_established'] == ['Reason applicability today']
    compound = source.search('ops', route['questions'][0], target)
    assert compound['results'][0]['evidence_scope']['not_established'] is None
    assert compound['results'][0]['evidence_scope']['entry_not_established'] == ['Current runtime behavior']


def test_contest_and_relay_attribution_fail_closed(tmp_path):
    load, rows, _, _ = _compound_fixture(tmp_path)
    rows[2]['contested_by'] = [{'entry_id': 'absent', 'body_lines': [1, 1]}]
    with pytest.raises(RationaleError, match='counter-statement'):
        load(changed_rows=rows)
    rows[2].pop('contested_by')
    rows[2]['evidence_scope']['observations'][0]['kind'] = 'relayed_observation'
    with pytest.raises(RationaleError, match='reporter'):
        load(changed_rows=rows)


def test_no_match_does_not_run_ancestry_and_warm_lookup_reuses_sources(tmp_path, monkeypatch):
    load, rows, route, target = _compound_fixture(tmp_path)
    source = load()
    source.search('ops', rows[0]['questions'][0], target)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('warm immutable lookup invoked Git'))
    assert source.search('ops', rows[0]['questions'][0], target)['status'] == 'evidence'
    assert source.search('ops', 'encrypted invoices to Mars', target)['status'] == 'missing'
