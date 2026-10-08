"""T9b P8 guards: `plan` stays pure on a root whose pre-T9b host stores hold files.

Order: docs/work/orders/T9b-memory-store-volume.md, Amendment 5 (the consolidated order), P8
("`plan` stays pure").
A root that P3 would migrate (an installation whose `state/memory` holds files) is the
new input; `plan` must still write nothing anywhere and give a stable hash.
- Without Docker: nothing on the host changes (runtime root, repositories, HOME, cwd, TMPDIR).
- With Docker (image-marked): no Docker volume or container of the project appears either.

These are guards: they pass at base by design (base `plan` is pure). Each fails against
the null stub (an installer that prints no plan).

Reading (repeated in the arm report under AMBIGUITY): `plan` succeeds on such a root
without starting a container or reaching the image (the non-Docker test plans with an
image digest that names no local image, as every S3 test does).
"""
from __future__ import annotations

import shutil

import pytest

from s3_harness import snapshot, snapshot_diff
from t9b_harness import Project, build_store, image_world, install, result_json, run_installer


def test_plan_writes_nothing_on_a_root_with_a_populated_store(world):
    """GREEN-IF plan prints the same plan_sha256 twice and no surface changes."""
    world.apply_ok()
    build_store(world.root, crashed=True, legacy=("memory", "registry"))
    before = snapshot(*world.surfaces())
    first, second = world.plan_hash(), world.plan_hash()
    changed = snapshot_diff(before, snapshot(*world.surfaces()))
    assert not changed, "plan wrote on a root with a populated store:\n" + "\n".join(changed)
    assert first == second, "identical inputs gave different plan_sha256 on a root with a populated store"


@pytest.mark.image
def test_plan_creates_no_docker_resource_on_a_root_with_a_populated_store():
    """GREEN-IF plan leaves the project's Docker volumes and containers, and the host, as they were."""
    world, docker = image_world("p6-plan", ("tooling",))
    project = Project(docker, world)
    try:
        argv = install(world)
        build_store(world.root, crashed=True, legacy=("memory", "registry"))

        def docker_state():
            label = f"label=com.docker.compose.project={world.project}"
            volumes = sorted(n for n in project.docker_run("volume", "ls", "-q").stdout.split()
                             if world.project in n)
            labelled = sorted(project.docker_run("volume", "ls", "-q", "--filter", label).stdout.split())
            containers = sorted(project.docker_run("ps", "-aq", "--filter", label).stdout.split())
            return volumes, labelled, containers

        before_docker, before_host = docker_state(), snapshot(*world.surfaces())

        def plan_sha():
            proc = run_installer(world, "plan", argv)  # with Docker reachable
            assert proc.returncode == 0, proc.stdout + proc.stderr
            return (result_json(proc) or {}).get("plan_sha256")

        first, second = plan_sha(), plan_sha()
        assert first == second, "plan_sha256 is unstable on a root with a populated store"
        assert docker_state() == before_docker, (
            f"plan created Docker resources: {before_docker} -> {docker_state()}")
        changed = snapshot_diff(before_host, snapshot(*world.surfaces()))
        assert not changed, "plan wrote on the host:\n" + "\n".join(changed)
    finally:
        remaining = project.down()
    assert not remaining, f"left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)  # kept for inspection when any step failed
