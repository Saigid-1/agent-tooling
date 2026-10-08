"""S3 P2: apply is exact and idempotent, plus the rendered runtime-root layout.

- `apply` with a mismatched `--expected-plan-sha256` writes nothing.
- A second `apply` with identical inputs changes no bytes and reports no change.
- A pre-existing differing file, or a symlink in the root, is refused with no writes.
Falsifier: any partial write on refusal, or any byte change on re-apply.

Layout (order, Installer CLI / apply): `.env`, `compose.yaml` (a copy of the
manifest), the overlays, `config/navigation.json` (schema ops.agent-tooling.v1,
container paths), `state/.ops-tooling-volume`, host registration snippets that
run `docker exec -i <container> kp-agent-tooling --config /config/navigation.json
serve` (never -t), and `receipt.json` with the sha256 of every written file.
Directories 0700, files 0600. `verify` re-hashes against the receipt and reports drift.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from s3_harness import (MANIFEST, STATE_MARKER, command_vectors, explain, has_tty_flag,
                        json_strings, read_snippets, receipt_pairs, serve_exec, sha256_file,
                        snapshot, snapshot_diff, tree_entries)


def _assert_refused_without_writes(world, proc, before, what):
    assert proc.returncode != 0, f"apply accepted {what}\n" + explain(proc)
    changed = snapshot_diff(before, snapshot(*world.surfaces()))
    assert not changed, f"apply wrote while refusing {what}:\n" + "\n".join(changed)


# ------------------------------------------------------------ hash mismatch

def _mismatched(world, digest, mismatch):
    if mismatch == "wrong_hash":
        return "0" * 64 if digest.removeprefix("sha256:") != "0" * 64 else "1" * 64
    if mismatch == "other_plan_hash":  # a genuine plan hash, for other inputs
        return world.plan_hash(world.args(uid=world.uid + 7))
    return None  # --expected-plan-sha256 omitted


@pytest.mark.parametrize("mismatch", ["wrong_hash", "other_plan_hash", "missing_hash"])
def test_apply_refuses_mismatched_plan_hash_without_writes(world, mismatch):
    digest = world.plan_hash()
    expected = _mismatched(world, digest, mismatch)
    assert expected != digest
    before = snapshot(*world.surfaces())
    proc = world.apply(expected=expected)
    _assert_refused_without_writes(world, proc, before, f"a {mismatch.replace('_', ' ')}")
    assert list(world.root.iterdir()) == []


# ------------------------------------------------------------------ layout

REQUIRED = (".env", "compose.yaml", "compose.workspaces.yaml", "config/navigation.json",
            "state/.ops-tooling-volume", "host/claude-mcp.json", "host/codex-mcp.toml",
            "receipt.json")


def test_apply_renders_runtime_root_layout(world):
    world.apply_ok()
    missing = [rel for rel in REQUIRED if not (world.root / rel).is_file()]
    assert not missing, f"apply did not write {missing}"
    assert (world.root / "compose.yaml").read_bytes() == MANIFEST.read_bytes(), (
        "runtime compose.yaml is not a copy of deploy/compose.yaml")
    assert (world.root / "state/.ops-tooling-volume").read_text().strip() == STATE_MARKER


def test_apply_modes_dirs_0700_files_0600(world):
    world.apply_ok()
    files, dirs, links = tree_entries(world.root)
    assert files, "apply wrote no files"
    bad = [f"{p.relative_to(world.root)} {oct(stat.S_IMODE(os.lstat(p).st_mode))}"
           for p in dirs if stat.S_IMODE(os.lstat(p).st_mode) != 0o700]
    bad += [f"{p.relative_to(world.root)} {oct(stat.S_IMODE(os.lstat(p).st_mode))}"
            for p in files if stat.S_IMODE(os.lstat(p).st_mode) != 0o600]
    assert not bad, "modes differ from dirs 0700 / files 0600:\n" + "\n".join(bad)
    assert not links, f"apply left symlinks: {links}"


def test_receipt_records_sha256_of_every_written_file(world):
    world.apply_ok()
    receipt = json.loads((world.root / "receipt.json").read_text())
    files, _, _ = tree_entries(world.root)
    written = {p: p.relative_to(world.root).as_posix() for p in files
               if p != world.root / "receipt.json"}
    names = set(written.values()) | {str(p) for p in written}
    pairs = receipt_pairs(receipt, names)
    missing = []
    for path, rel in written.items():
        digest = sha256_file(path)
        recorded = pairs.get(rel, set()) | pairs.get(str(path), set())
        if digest not in recorded and f"sha256:{digest}" not in recorded:
            missing.append(rel)
    assert not missing, f"receipt.json lacks the sha256 of: {missing}"


def test_navigation_config_uses_container_paths(world):
    world.apply_ok()
    nav = json.loads((world.root / "config/navigation.json").read_text())
    assert nav.get("schema_version") == "ops.agent-tooling.v1"
    repos = nav.get("repos")
    assert isinstance(repos, dict) and set(world.repos) <= set(repos), (
        f"navigation.json repos {sorted(repos or {})} lack {sorted(world.repos)}")
    for key, host_path in world.repos.items():
        path = repos[key].get("path") if isinstance(repos[key], dict) else None
        assert isinstance(path, str) and path.startswith("/"), f"repo {key} has no absolute path"
        assert path != str(host_path), f"repo {key} uses the host path, not a container path"
    host_values = [str(world.base), str(world.root), str(world.home), str(world.repo)]
    leaks = [s for s in json_strings(nav) if any(h in s for h in host_values)]
    assert not leaks, f"navigation.json carries host paths: {leaks}"


def test_host_snippets_exec_interactive_without_tty(world):
    world.apply_ok()
    containers = {}
    for name, parsed in read_snippets(world.root).items():
        vectors = command_vectors(parsed)
        match = serve_exec(vectors)
        assert match, (f"{name} has no `docker exec -i <container> kp-agent-tooling --config "
                       f"/config/navigation.json serve` command; found {vectors}")
        argv, container = match
        assert not has_tty_flag(argv), f"{name} allocates a TTY: {argv}"
        containers[name] = container
    assert len(set(containers.values())) == 1, f"snippets name different containers: {containers}"


# -------------------------------------------------------------- idempotence

def test_plan_accepts_root_matching_receipt_with_same_hash(world):
    digest = world.plan_hash()
    proc = world.apply(expected=digest)
    assert proc.returncode == 0, explain(proc)
    assert world.plan_hash() == digest, "identical inputs gave a different plan_sha256 after apply"


def test_reapply_identical_changes_no_bytes(world):
    digest = world.plan_hash()
    first = world.apply(expected=digest)
    assert first.returncode == 0, explain(first)
    before = snapshot(*world.surfaces())
    second = world.apply(expected=digest)
    assert second.returncode == 0, "second identical apply failed\n" + explain(second)
    changed = snapshot_diff(before, snapshot(*world.surfaces()))
    assert not changed, "second identical apply changed:\n" + "\n".join(changed)


def test_reapply_reports_no_change(world):
    digest = world.plan_hash()
    first = world.apply(expected=digest)
    assert first.returncode == 0, explain(first)
    second = world.apply(expected=digest)
    assert second.returncode == 0, explain(second)
    assert (second.stdout, second.stderr) != (first.stdout, first.stderr), (
        "the no-op re-apply reports exactly what the writing apply reported; it cannot be "
        "reporting no change\n" + explain(second))


# ------------------------------------------------- differing files, symlinks

@pytest.mark.parametrize("case", ["preexisting_in_empty_root", "tampered_after_apply"])
def test_apply_refuses_differing_file_without_writes(world, case):
    digest = world.plan_hash()
    if case == "tampered_after_apply":
        proc = world.apply(expected=digest)
        assert proc.returncode == 0, explain(proc)
        with open(world.root / ".env", "a") as handle:
            handle.write("OPERATOR_EDIT=1\n")
    else:
        (world.root / ".env").write_text("OPERATOR_EDIT=1\n")
    before = snapshot(*world.surfaces())
    proc = world.apply(expected=digest)
    _assert_refused_without_writes(world, proc, before, f"a differing .env ({case})")


@pytest.mark.parametrize("case", ["dangling_file_link", "directory_link", "identical_content_link"])
def test_apply_refuses_symlink_without_writes(world, case):
    digest = world.plan_hash()
    victim = world.base / "victim"
    victim.mkdir()
    if case == "dangling_file_link":
        (world.root / ".env").symlink_to(victim / "env-target")
    elif case == "directory_link":
        (world.root / "state").symlink_to(victim, target_is_directory=True)
    else:
        proc = world.apply(expected=digest)
        assert proc.returncode == 0, explain(proc)
        original = world.root / "config" / "navigation.json"
        moved = victim / "navigation.json"
        os.replace(original, moved)
        original.symlink_to(moved)
    before = snapshot(*world.surfaces())
    proc = world.apply(expected=digest)
    _assert_refused_without_writes(world, proc, before, f"a symlink in the root ({case})")


# ------------------------------------------------------------------- verify

def test_verify_clean_root_then_reports_drift(world):
    world.apply_ok()
    clean = world.run("verify", ["--runtime-root", str(world.root)])
    assert clean.returncode == 0, "verify reports drift on an untouched root\n" + explain(clean)
    with open(world.root / "config" / "navigation.json", "a") as handle:
        handle.write("\n")
    drift = world.run("verify", ["--runtime-root", str(world.root)])
    assert drift.returncode != 0, "verify exits 0 on a drifted root\n" + explain(drift)
    assert "config/navigation.json" in drift.stdout + drift.stderr, (
        "verify does not name the drifted file\n" + explain(drift))
