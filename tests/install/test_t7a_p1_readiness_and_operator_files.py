"""T7a P1 (non-container part): `verify` reports readiness; operator files stay the operator's.

Order: docs/work/orders/T7a-role-readiness.md, P1.

- A role whose operator configuration is absent reports `not_configured`, naming the
  missing operator files by root-relative path, in `kp-agent-install verify`.
  Falsifier: "a `verify` that reports `verified` with no readiness entry for an
  unconfigured role".
- An operator-written file (a token, `refresh.json`, `config/capture/*`) is never
  created with content, replaced or hashed by the installer. A placeholder may be
  created only where absent, empty, mode 0600, and must be recorded as operator-owned.
  Falsifier: "an operator file overwritten or re-hashed".

The container half of P1 (`docker compose up -d`, roles staying up, their logs) is in
test_t7a_p1_roles_start_image.py (image-marked).

Readings (see tests/install/t7a_harness.py and the arm report):
- the readiness entry is read from `verify`'s JSON stdout, whatever its exit code; the
  order does not say whether `not_configured` changes verify's exit status, so the
  exit status is not asserted, but a not-configured role must not appear as drift;
- once the operator has written a role's files, verify must no longer name them as
  missing (a report naming present files as missing would be false);
- "recorded as operator-owned": a placeholder's root-relative path appears in
  `receipt.json` outside its hashed `files` and `directories` maps.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from s3_harness import explain, sha256_file
from t7a_harness import (ALL_OPERATOR_FILES, OPERATOR_FILES, names_root_relative, not_configured_claims,
                         not_configured_entries, unconfigured_files, write_private)

# T12b: every tooling-image role, the `indexer` (selected whenever capture or board is) included.
FULL = ("tooling", "refresh", "capture", "board", "indexer")
COMPONENT_SETS = {
    "refresh": ("tooling", "refresh"),
    "capture": ("tooling", "capture"),
    "all-roles": FULL,
}

TOKEN = b"t7a-fixture-token-not-a-credential\n"


def _verify(world) -> tuple[object, dict]:
    proc = world.run("verify", ["--runtime-root", str(world.root)])
    try:
        document = json.loads(proc.stdout)
    except ValueError:
        pytest.fail("verify did not print a JSON document\n" + explain(proc), pytrace=False)
    return proc, document


def _operator_contents(world) -> dict[str, bytes]:
    """Plausible operator files, written the way docs/DOCKER.md describes them."""
    approval = json.dumps({"tenant_id": "t7a-tenant", "repositories": ["fixture"]}).encode()
    request = {
        "repositories": {"fixture": {
            "repository": "/workspaces/fixture", "python_scope": ["src"],
            "workspace_observation_roots": ["/workspaces/fixture"], "analysis_dependencies": [],
            "semantic_deadline_seconds": 900}},
        "output_root": "/state/refresh", "publication": "/state/refresh/profile.json",
        "check_status": "/state/refresh/status.json", "toolchain": "/opt/toolchain",
        "node": "/usr/local/bin/node", "semantic": False}
    session = {"schema_version": "ops.desk-memory.local.v1", "state_root": "/state/memory/desk-memory",
               "catalog_path": "/config/launch/desks.json", "workspace_root": "/config/launch",
               "provider_instance": "claude", "provider_session_id": "t7a-capture-session"}
    policy = {
        "schema_version": "ops.workspace-capture.v1",
        "approval_record": "/config/capture/workspace-approval.json",
        "approval_sha256": hashlib.sha256(approval).hexdigest(), "tenant_id": "t7a-tenant",
        "approved_repo_keys": ["fixture"], "repos": {"fixture": [str(world.repo)]},
        "native_roots": {"claude": str(world.home / ".claude" / "projects"),
                         "codex": str(world.home / ".codex" / "sessions")},
        "excluded_sessions": {"claude": [], "codex": []},
        "max_candidates": 64, "max_batch_bytes": 2000000, "max_batch_rows": 500}
    return {
        "config/refresh.json": (json.dumps(request, indent=2) + "\n").encode(),
        "secrets/github-token": TOKEN,
        "config/capture/session.json": (json.dumps(session, indent=2) + "\n").encode(),
        "config/capture/workspace-policy.json": (json.dumps(policy, indent=2) + "\n").encode(),
        "config/capture/workspace-approval.json": approval,
    }


def _write_operator_files(world, roles) -> dict[str, bytes]:
    contents = _operator_contents(world)
    wanted = {path for role in roles for path in OPERATOR_FILES.get(role, ())}
    if "capture" in roles:
        wanted.add("config/capture/workspace-approval.json")
    written = {}
    for relative in sorted(wanted):
        write_private(world.root / relative, contents[relative])
        written[relative] = contents[relative]
    return written


@pytest.mark.parametrize("label", sorted(COMPONENT_SETS))
def test_verify_reports_not_configured_roles_by_root_relative_path(world, label):
    """GREEN-IF verify carries, for each selected role with absent operator files, a
    `not_configured` entry naming exactly those files by root-relative path; once the operator
    writes them, no `not_configured` entry for that role names them any longer."""
    components = COMPONENT_SETS[label]
    world.apply_ok(world.args(components=components))
    roles = [role for role in components if role in OPERATOR_FILES]

    proc, document = _verify(world)
    assert document.get("status") != "drift" and not document.get("drift"), (
        "a fresh installation with absent operator files must not report drift\n" + explain(proc))
    for role in roles:
        missing = unconfigured_files(world.root, role)
        assert missing, f"precondition: {role}'s operator files are absent after apply"
        entries = not_configured_entries(document, role)
        assert entries, (f"verify has no `not_configured` readiness entry for unconfigured role {role!r} "
                         f"(missing {missing})\n" + explain(proc))
        unnamed = [path for path in missing if not any(names_root_relative(e, path) for e in entries)]
        assert not unnamed, (f"verify's `not_configured` entry for {role!r} does not name {unnamed} by "
                             f"root-relative path: {entries}")

    written = _write_operator_files(world, roles)
    proc, document = _verify(world)
    for role in roles:
        stale = [path for path in OPERATOR_FILES[role] for e in not_configured_claims(document, role)
                 if names_root_relative(e, path)]
        assert not stale, (f"after the operator wrote {sorted(written)}, verify still reports {role!r} "
                           f"`not_configured` naming {sorted(set(stale))}\n" + explain(proc))
    drifted = [row for row in document.get("drift") or [] if row.get("path") in written]
    assert not drifted, f"operator files are reported as installer drift: {drifted}"


def test_unselected_roles_have_no_readiness_failure(world):
    """GREEN-IF a tooling-only installation reports no `not_configured` refresh or capture entry."""
    world.apply_ok(world.args(components=("tooling",)))
    _, document = _verify(world)
    for role in OPERATOR_FILES:
        flagged = [e for e in not_configured_entries(document, role)
                   if any(names_root_relative(e, p) for p in OPERATOR_FILES[role])]
        assert not flagged, f"{role} was not selected, yet verify reports it not configured: {flagged}"


def _receipt(world) -> dict:
    return json.loads((world.root / "receipt.json").read_text())


def _assert_placeholders_only(world, stage: str) -> None:
    receipt = _receipt(world)
    hashed = set(receipt.get("files") or {})
    unhashed = {k: v for k, v in receipt.items() if k not in ("files", "directories")}
    for relative in ALL_OPERATOR_FILES:
        path = world.root / relative
        assert relative not in hashed, f"{stage}: the installer hashes operator file {relative} in receipt.json"
        if not path.exists():
            continue
        assert path.is_file() and not path.is_symlink(), f"{stage}: {relative} is not a regular file"
        assert path.stat().st_size == 0, f"{stage}: the installer created {relative} with content"
        assert path.stat().st_mode & 0o777 == 0o600, (
            f"{stage}: placeholder {relative} has mode {oct(path.stat().st_mode & 0o777)}, not 0600")
        assert relative in json.dumps(unhashed), (
            f"{stage}: placeholder {relative} exists but receipt.json does not record it as operator-owned "
            f"(outside its hashed files): {sorted(unhashed)}")


def test_operator_files_are_never_written_replaced_or_hashed(world):
    """GREEN-IF apply creates operator files only as empty 0600 placeholders recorded as
    operator-owned, and neither a re-apply nor an upgrade plan replaces or hashes what the
    operator wrote; verify does not report them as drift."""
    argv = world.args(components=FULL)
    world.apply_ok(argv)
    _assert_placeholders_only(world, "first apply")

    written = _write_operator_files(world, ("refresh", "capture"))
    before = {rel: (sha256_file(world.root / rel), (world.root / rel).stat().st_mode & 0o777) for rel in written}

    again = world.apply(argv, world.plan_hash(argv))
    assert again.returncode == 0, "re-applying the same plan refused once operator files exist\n" + explain(again)
    upgraded = world.args(components=FULL, board_port=world.board_port + 1)
    moved = world.apply(upgraded, world.plan_hash(upgraded))
    assert moved.returncode == 0, "an upgrade plan refused once operator files exist\n" + explain(moved)

    after = {rel: (sha256_file(world.root / rel), (world.root / rel).stat().st_mode & 0o777) for rel in written}
    assert after == before, f"apply replaced or re-moded operator files: {before} -> {after}"
    receipt_text = (world.root / "receipt.json").read_text()
    hashed = set(_receipt(world).get("files") or {})
    rehashed = [rel for rel in written if rel in hashed or hashlib.sha256(written[rel]).hexdigest() in receipt_text]
    assert not rehashed, f"receipt.json hashes operator-written files: {rehashed}"
    proc, document = _verify(world)
    drifted = [row for row in document.get("drift") or [] if row.get("path") in written]
    assert not drifted, f"verify reports operator files as drift: {drifted}\n" + explain(proc)
