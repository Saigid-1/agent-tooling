"""T9b P5: `apply` and `verify`.

Order: docs/work/orders/T9b-memory-store-volume.md, Amendment 5 (the consolidated order), P5,
and the listing half of P3:
- `apply` never calls Docker and renders as before.
- `verify` reports the store as a readiness state like `not_configured`; its exit code and
  top-level `status` (`verified`/`drift`) are unchanged by `unprepared`. It names operator files
  that still point at old paths.
- P3: operator files that name an old path are never rewritten; `apply`'s report (no Docker
  needed) and `verify` list each one with its required new path.
- Definition: "prepared" is the volume `<project>_memory` existing, its root owned by
  AGENT_UID:AGENT_GID with mode 0700, and no pre-T9b store directory left to migrate on the bind;
  the single test `prepare`, `verify` and the role preflight use.
Falsifier: a default-suite `apply` that creates a volume or pulls; `verify` exiting non-zero or
reporting a different top-level status for an unprepared but otherwise clean root.

Amendment 7, J1 (with Amendment 6, H5): `verify`'s store state is the Definition as observed,
never the receipt's entry. A pre-T9b store directory left on the bind is `unprepared`, observed on
the host with no Docker needed. When the volume cannot be observed (no Docker, or the receipt's
image absent), the state is `not_observed`, never `unprepared` or `prepared`. Exit code and
top-level status stay unchanged in every case.
Falsifier (J1): with no `docker` executable on PATH and no store directory on the bind, `verify`
reports `unprepared` or `prepared`.

The default-suite tests run without Docker resources: the S3 `world`, whose image is a digest no
local image has. Each `verify` test runs under two PATH variants (Amendment 7, CI portability;
Verification K3):
- `empty-path`: PATH is one empty directory made per test, so no `docker` executable is found at
  all. It is never /usr/bin:/bin, because Linux runners keep `docker` in /usr/bin. The installer is
  an absolute console script and needs no tool on PATH.
- `inherited-path`: the test's own PATH and DOCKER_* settings, where `docker` may be present and
  reachable, but the world's image is absent.
Under both, the volume cannot be observed. The image-marked tests observe Docker around `apply`,
and `verify` before and after `prepare`.

Readings (repeated in the arm report under AMBIGUITY):
- the default-suite `verify` tests read the store state from verify's JSON report,
  `readiness.memory_store.status`, never from a substring of its output (a `not_observed` report
  may contain the word `unprepared`);
- in the image-marked test below, `verify` "reports the store unprepared" when its output says
  `unprepared`, and reports it prepared when its output no longer does;
- a report or `verify` "lists" an operator file with its new path when both strings appear in its
  output.
"""
from __future__ import annotations

import json
import shutil

import pytest

from s3_harness import explain
from t7a_harness import write_private
from t9b_harness import (STORE_TARGET, Project, apply_plan, build_store, image_world, install_argv, prepare, prepared,
                         result_json, run_installer, store_volume_name, verify)

OLD_PATHS = {
    "config/launch/registry.json": ("state_root", "/state/registry", f"{STORE_TARGET}/registry"),
    "config/launch/desks.json": ("roster_path", "/state/registry/roles.json", f"{STORE_TARGET}/registry/roles.json"),
}


def _old_operator_files(root) -> dict[str, bytes]:
    files = {
        "config/launch/desks.json": {"schema_version": "agent-tooling.desk-registry.v1", "tenant_id": "t9b",
                                     "roster_path": "/state/registry/roles.json",
                                     "harness_profiles_path": "/config/launch/harness-profiles.json"},
        "config/launch/registry.json": {"schema_version": "ops.desk-memory.local.v1", "state_root": "/state/registry",
                                        "catalog_path": "/config/launch/desks.json",
                                        "workspace_root": "/config/launch", "provider_instance": "t9b",
                                        "provider_session_id": "t9b-operator"}}
    out = {}
    for relative, value in files.items():
        data = (json.dumps(value) + "\n").encode()
        write_private(root / relative, data)
        out[relative] = data
    return out


def _status(proc) -> str | None:
    try:
        return json.loads(proc.stdout).get("status")
    except (ValueError, AttributeError):
        return None


def test_apply_and_verify_list_operator_files_naming_old_store_paths(world):
    """GREEN-IF, after the operator files name pre-T9b store paths, a re-plan and re-apply exits 0 with a
    report naming each file and its new path, rewrites neither, and `verify` names each with its new path."""
    world.apply_ok()
    operator = _old_operator_files(world.root)
    applied = world.apply_ok()
    report = applied.stdout + applied.stderr
    checked = world.run("verify", ["--runtime-root", str(world.root)])
    for relative, (key, old, new) in OLD_PATHS.items():
        assert (world.root / relative).read_bytes() == operator[relative], f"apply rewrote {relative}"
        assert relative in report and new in report, (
            f"apply's report does not list {relative} ({key} {old}) with its new path {new}\n" + explain(applied))
        assert relative in checked.stdout and new in checked.stdout, (
            f"verify does not list {relative} with its new path {new}\n" + explain(checked))


# Amendment 7, CI portability; Verification K3: the PATH variants under which `verify` cannot observe the volume.
PATH_VARIANTS = ("empty-path", "inherited-path")


def _verify_unobservable(world, variant: str):
    """`verify` on the world's root, under the PATH `variant` (see the module docstring)."""
    argv = ["--runtime-root", str(world.root)]
    if variant == "empty-path":
        empty = world.base / "path-without-docker"
        empty.mkdir()
        assert not any(empty.iterdir()), "precondition: the PATH directory is empty"
        return run_installer(world, "verify", argv, path=str(empty), docker=False)
    assert variant == "inherited-path", variant
    return run_installer(world, "verify", argv)


def _store_state(proc) -> tuple[object, object, dict]:
    """(exit code, top-level status, readiness.memory_store) of a `verify` run, from its JSON report."""
    value = result_json(proc)
    value = value if isinstance(value, dict) else {}
    store = (value.get("readiness") or {}).get("memory_store")
    return proc.returncode, value.get("status"), store if isinstance(store, dict) else {}


@pytest.mark.parametrize("variant", PATH_VARIANTS)
def test_verify_reports_a_clean_root_it_cannot_observe_not_observed(world, variant):
    """GREEN-IF, after a plain apply (no prepare) of a clean root with no store directory on the bind, and with
    the volume unobservable (PATH one empty directory, or the inherited PATH with the image absent), `verify`
    exits 0 with top-level status `verified` and readiness.memory_store.status `not_observed`."""
    world.apply_ok()
    checked = _verify_unobservable(world, variant)
    code, status, store = _store_state(checked)
    assert code == 0 and status == "verified", (
        "an unobserved store changed verify's exit code or top-level status\n" + explain(checked))
    assert store.get("status") == "not_observed", (
        f"verify reports the store {store.get('status')!r} without observing the volume, not `not_observed`; "
        f"reasons: {store.get('reasons')}\n" + explain(checked))


@pytest.mark.parametrize("variant", PATH_VARIANTS)
def test_verify_reports_an_unprepared_store_without_changing_its_status(world, variant):
    """GREEN-IF, after a plain apply (no prepare) of an otherwise clean root that holds a pre-T9b store
    directory on the bind (state/registry), under either PATH variant, `verify` exits 0 with top-level status
    `verified` and readiness.memory_store.status `unprepared` (observed on the host, no Docker needed)."""
    world.apply_ok()
    build_store(world.root, crashed=False, legacy=("registry",))
    checked = _verify_unobservable(world, variant)
    code, status, store = _store_state(checked)
    assert code == 0 and status == "verified", (
        "an unprepared store changed verify's exit code or top-level status\n" + explain(checked))
    assert store.get("status") == "unprepared", (
        f"verify reports the store {store.get('status')!r} with state/registry left on the bind, not "
        f"`unprepared`; reasons: {store.get('reasons')}\n" + explain(checked))


@pytest.mark.image
def test_apply_never_calls_docker():
    """P5 guard (passes at base by design). GREEN-IF plan and apply on a root holding pre-T9b stores, with
    Docker reachable, create no volume and no container of the project and leave the host stores alone."""
    world, docker = image_world("p5-apply", ("tooling",))
    project = Project(docker, world)
    try:
        argv = install_argv(world)
        first, _ = apply_plan(world, argv)
        assert first.returncode == 0, explain(first)
        store = build_store(world.root, crashed=True, legacy=("memory", "registry"))
        before = store.snapshot()
        again, _ = apply_plan(world, argv)
        assert again.returncode == 0, explain(again)
        label = f"label=com.docker.compose.project={world.project}"
        volumes = [n for n in project.docker_run("volume", "ls", "-q").stdout.split() if world.project in n]
        containers = project.docker_run("ps", "-aq", "--filter", label).stdout.split()
        assert not volumes and not containers, f"apply created Docker resources: {volumes} {containers}"
        assert store.snapshot() == before, "apply changed the pre-T9b stores"
    finally:
        remaining = project.down()
    assert not remaining, f"left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)


@pytest.mark.image
def test_verify_reports_prepared_only_while_the_definition_holds():
    """GREEN-IF `verify` (exit 0, status `verified` throughout) reports `unprepared` after apply, not after
    prepare, and again once a pre-T9b store directory appears on the bind to be migrated."""
    world, docker = image_world("p5-verify", ("tooling",))
    project = Project(docker, world)
    try:
        argv = install_argv(world)
        applied, _ = apply_plan(world, argv)
        assert applied.returncode == 0, explain(applied)
        steps = [("after apply", True)]
        checked = [verify(world)]
        done = prepare(world)
        assert prepared(done) is None, f"{prepared(done)}\n" + explain(done)
        assert project.volume_exists(store_volume_name(world.project))
        steps.append(("after prepare", False))
        checked.append(verify(world))
        build_store(world.root, crashed=False, legacy=("registry",))
        steps.append(("with state/registry left to migrate", True))
        checked.append(verify(world))
        for (label, unprepared), proc in zip(steps, checked):
            assert proc.returncode == 0 and _status(proc) == "verified", (
                f"{label}: verify's exit code or top-level status changed\n" + explain(proc))
            assert ("unprepared" in proc.stdout) == unprepared, (
                f"{label}: verify {'does not report' if unprepared else 'still reports'} the store unprepared\n"
                + explain(proc))
    finally:
        remaining = project.down()
    assert not remaining, f"left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)
