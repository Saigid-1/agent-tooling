"""T7a P6: the home directory is an explicit plan input.

Order: docs/work/orders/T7a-role-readiness.md, P6.
`kp-agent-install plan|apply --home <absolute existing directory>` sets `inputs.home`.
Without the flag, `$HOME` applies as today. `verify` does not treat a different
invoking `$HOME` as drift.
Falsifier: `--home` ignored, a relative or missing directory accepted, or a `$HOME`
change reported as drift.

Readings (repeated in the arm report under AMBIGUITY):
- `inputs.home` is compared with the given directory's physical path (the S3 worlds
  live on physical paths, so the two are the same string);
- "accepted" is falsified only by a reasoned refusal: exit 1 with the installer's
  refusal JSON (`status: refused`, no writes) that names the home directory. An
  argparse usage error (exit 2) is not that refusal: it is what an installer without
  the option prints;
- `--home` names a directory that need not be the invoking user's home; the rendered
  launch profiles (docs/HOST-ADAPTER.md "Docker runtime") expand `~/` against it;
- `verify` is run with only `--runtime-root` (the documented form), under another `$HOME`.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from s3_harness import INSTALLER, explain, hermetic_env, snapshot, snapshot_diff


def _run(world, action, argv, *, home: Path) -> subprocess.CompletedProcess:
    return subprocess.run([str(INSTALLER), action, *argv], cwd=world.cwd, env=hermetic_env(home, world.itmp),
                          capture_output=True, text=True, timeout=180, umask=0o022, stdin=subprocess.DEVNULL)


def _json(proc):
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def _homes(world) -> tuple[Path, Path]:
    """An operator-named home (with native transcript roots) and an unrelated invoking home."""
    named = world.base / "operator-home"
    (named / ".claude" / "projects").mkdir(parents=True)
    (named / ".codex" / "sessions").mkdir(parents=True)
    other = world.base / "other-invoking-home"
    other.mkdir()
    return named, other


def _plan(world, argv, *, home):
    proc = _run(world, "plan", argv, home=home)
    value = _json(proc)
    assert proc.returncode == 0 and isinstance(value, dict), "plan refused\n" + explain(proc)
    return value


def test_plan_home_option_sets_inputs_home(world):
    """GREEN-IF `plan --home D` records D as inputs.home, whatever the invoking $HOME is."""
    named, other = _homes(world)
    argv = world.args() + ["--home", str(named)]
    first = _plan(world, argv, home=world.home)
    assert first["inputs"].get("home") == str(named.resolve()), (
        f"inputs.home is {first['inputs'].get('home')!r}, not the --home directory {named}")
    second = _plan(world, argv, home=other)
    assert second["inputs"].get("home") == str(named.resolve())
    assert first["plan_sha256"] == second["plan_sha256"], (
        "with --home given, the invoking $HOME still changed plan_sha256")
    default = _plan(world, world.args(), home=world.home)
    assert default["plan_sha256"] != first["plan_sha256"], "plan_sha256 is insensitive to --home"


def test_without_home_option_the_invoking_home_applies(world):
    """GREEN-IF a plan without --home records the invoking $HOME as inputs.home (as today)."""
    value = _plan(world, world.args(), home=world.home)
    assert value["inputs"].get("home") == str(world.home.resolve())


def test_apply_home_option_expands_launch_profiles_against_it(world):
    """GREEN-IF apply --home D renders config/launch/harness-profiles.json with `~/` capture roots
    under D, enabled because they lie under the planned transcript roots, and nothing under $HOME."""
    named, _ = _homes(world)
    roots = [named / ".claude" / "projects", named / ".codex" / "sessions"]
    argv = world.args(components=("tooling", "capture")) + ["--home", str(named)]
    for root in roots:
        argv += ["--transcript-root", str(root)]
    plan = _run(world, "plan", argv, home=world.home)
    value = _json(plan)
    assert plan.returncode == 0 and isinstance(value, dict), explain(plan)
    applied = _run(world, "apply", argv + ["--expected-plan-sha256", value["plan_sha256"]], home=world.home)
    assert applied.returncode == 0, explain(applied)
    profiles = json.loads((world.root / "config" / "launch" / "harness-profiles.json").read_text())
    capture_roots = {p["harness"]: (p["capture"].get("root"), p.get("enabled"))
                     for p in profiles["profiles"] if p.get("capture", {}).get("mode") == "transcript"}
    assert capture_roots, f"no transcript profile was rendered: {profiles}"
    for harness, (root, enabled) in capture_roots.items():
        assert root and Path(root).is_relative_to(named.resolve()), (
            f"{harness} capture root {root!r} is not expanded against --home {named}")
        assert not Path(root).is_relative_to(world.home.resolve()), f"{harness} root uses the invoking $HOME"
        assert enabled is True, f"{harness} is rendered disabled although its root {root} is a planned transcript root"


@pytest.mark.parametrize("kind", ["relative", "missing", "file"])
@pytest.mark.parametrize("action", ["plan", "apply"])
def test_home_option_refuses_anything_but_an_absolute_existing_directory(world, kind, action):
    """GREEN-IF a relative, missing or non-directory --home is refused with the installer's refusal
    JSON (exit 1, nothing written); plan's refusal names the home directory, and apply refuses even
    the digest that the same inputs without --home would have."""
    (world.cwd / "relative-home").mkdir()  # exists relative to the installer's cwd: refused for being relative
    (world.base / "a-file").write_text("not a directory\n")
    value = {"relative": "relative-home", "missing": str(world.base / "no-such-home"),
             "file": str(world.base / "a-file")}[kind]
    argv = world.args() + ["--home", value]
    if action == "apply":
        # The digest of the same plan without --home: an installer that ignores --home applies it.
        argv += ["--expected-plan-sha256", world.plan_hash(world.args())]
    before = snapshot(*world.surfaces())
    proc = _run(world, action, argv, home=world.home)
    after = snapshot(*world.surfaces())
    refusal = _json(proc)
    assert proc.returncode == 1 and isinstance(refusal, dict) and refusal.get("status") == "refused", (
        f"--home {value!r} ({kind}) was not refused with the installer's refusal JSON\n" + explain(proc))
    if action == "plan":  # apply's digest is not of these inputs, so its refusal may name the mismatch
        named = " ".join(str(refusal.get(key, "")) for key in ("reason", "detail")).lower()
        assert "home" in named, f"the refusal does not name the home directory: {refusal}"
    assert not snapshot_diff(before, after), "a refused --home wrote files"
    assert list(world.root.iterdir()) == []


def _apply_and_verify_under(world, argv, *, apply_home, verify_home):
    plan = _run(world, "plan", argv, home=apply_home)
    value = _json(plan)
    assert plan.returncode == 0 and isinstance(value, dict), explain(plan)
    applied = _run(world, "apply", argv + ["--expected-plan-sha256", value["plan_sha256"]], home=apply_home)
    assert applied.returncode == 0, explain(applied)
    proc = _run(world, "verify", ["--runtime-root", str(world.root)], home=verify_home)
    return proc, _json(proc)


def test_verify_ignores_a_different_invoking_home_after_home_option(world):
    """GREEN-IF a root applied with --home verifies clean (exit 0, no drift) under another $HOME."""
    named, other = _homes(world)
    argv = world.args(components=("tooling", "capture")) + ["--home", str(named)]
    proc, value = _apply_and_verify_under(world, argv, apply_home=world.home, verify_home=other)
    assert isinstance(value, dict) and proc.returncode == 0 and value.get("status") != "drift" \
        and not value.get("drift"), "verify treats a different invoking $HOME as drift\n" + explain(proc)


def test_verify_ignores_a_different_invoking_home(world):
    """GREEN-IF a root applied without --home verifies clean under another $HOME."""
    _, other = _homes(world)
    proc, value = _apply_and_verify_under(world, world.args(components=("tooling", "capture")),
                                          apply_home=world.home, verify_home=other)
    assert isinstance(value, dict) and proc.returncode == 0 and value.get("status") != "drift" \
        and not value.get("drift"), "verify treats a different invoking $HOME as drift\n" + explain(proc)
