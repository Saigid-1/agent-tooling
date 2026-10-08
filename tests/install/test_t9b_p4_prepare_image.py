"""T9b P4: `kp-agent-install prepare` (image-marked).

Order: docs/work/orders/T9b-memory-store-volume.md, Amendment 5 (the consolidated order), P4:
  0. if the root is already "prepared", report `prepared` and change nothing, with no stop required;
  1. refuse `writers_running` if any project container runs, one-off `compose run` containers included;
  2. refuse `docker_unreachable`;
  3. refuse `image_absent` if the runtime image is not present locally (inspect, never pull);
  4. create the volume if absent (a one-off root container that only chowns and chmods its root);
  5. migrate under P3 (tests/install/test_t9b_p3_migration_image.py);
  6. record the outcome in the receipt.
  Nothing is created before steps 1-3 pass.
Falsifier: a volume created by a refused run; a refusal under the wrong condition; a pull; a
re-run on a prepared root that requires a stop or changes anything.
(The prepared state itself, owner AGENT_UID:AGENT_GID and mode 0700 measured after `prepare`
then `up`, is in test_t9b_p1_p2_store_volume_image.py.)

Setup: an empty root, `plan` and `apply` (never prepared), the pre-T9b stores on the host bind
(t9b_harness.build_store), then the condition, then `prepare`. Running writers on a root that is
not prepared (whose roles would refuse to start) are the project's own containers with a
long-running test entrypoint: service containers from `up` with a test Compose override, or a
one-off `docker compose run -d`. "Docker unreachable" is DOCKER_HOST naming a socket that does
not exist, or no `docker` executable on PATH. "Image absent" is a digest reference that no local
image has.

Readings (repeated in the arm report under AMBIGUITY): a refusal exits non-zero and prints
`refused` and its reason; `writers_running` also names each running service and a
`docker compose ... stop|down` command (the original P4); "changes nothing" means the volume's
contents, the host stores and receipt.json are unchanged; a pull is not observed (NOT VERIFIED).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import replace

import pytest
import yaml

from s3_harness import DIGEST_REGISTRY, compose_files, explain
from t9b_harness import (Project, apply_only, build_store, image_world, install, prepare, prepare_refused, prepared,
                         store_volume_name, tree_diff, volume_state)

pytestmark = pytest.mark.image

ROLES = ("tooling", "capture")
DEAD_SOCKET = "unix:///nonexistent/t9b-test-docker.sock"
# Meet (Coordinator), CI portability: Linux runners keep `docker` in /usr/bin, so "no docker on PATH" is a
# PATH of one empty directory, made per test (the installer is an absolute console script and needs no tool).
UNREACHABLE = {"dead-socket": {"env": {"DOCKER_HOST": DEAD_SOCKET}},
               "no-docker-cli": {"path": "<empty directory>"}}


def _unreachable(how, world):
    options = dict(UNREACHABLE[how])
    if "path" in options:
        empty = world.base / "path-without-docker"
        empty.mkdir(exist_ok=True)
        options["path"] = str(empty)
    return options


def _inventory(project, world):
    return volume_state(project, store_volume_name(world.project), image=world.image, uid=world.uid, gid=world.gid)


def _sleepers(project: Project, world, services) -> subprocess.CompletedProcess:
    """`up -d` of `services` with a test override whose entrypoint only sleeps (the project's own containers)."""
    override = world.base / "t9b-sleepers.yaml"
    override.write_text(yaml.safe_dump({"services": {s: {"entrypoint": ["sleep"], "command": ["600"],
                                                         "healthcheck": {"disable": True}} for s in services}}))
    argv = [project.docker, "compose", "--project-directory", str(world.root), "-p", world.project]
    for path in [*compose_files(world.root), override]:
        argv += ["-f", str(path)]
    return subprocess.run([*argv, "up", "-d", "--no-deps", *services], cwd=world.root, env=project.env,
                          capture_output=True, text=True, timeout=300)


def _assert_writers_refused(proc, services, project, world, store, host_before, volume_before) -> None:
    why = prepare_refused(proc, "writers_running")
    assert why is None, f"{why}\n" + explain(proc)
    output = proc.stdout + proc.stderr
    unnamed = [service for service in services if not re.search(rf"\b{service}\b", output)]
    assert not unnamed, f"the refusal does not name the running services {unnamed}\n" + explain(proc)
    assert re.search(r"docker compose\b[^\n]{0,400}?\s(stop|down)\b", output.replace('\\"', '"')), (
        "the refusal does not name the documented stop command (`docker compose ... stop`)\n" + explain(proc))
    assert store.snapshot() == host_before, "the host stores changed"
    assert _inventory(project, world) == volume_before, "the refused prepare changed the volume"


def test_prepare_refuses_writers_running_service_containers():
    """GREEN-IF, on a root that is not prepared, with service containers of the project running, prepare
    refuses `writers_running`, names `tooling` and `capture` and the stop command, and changes neither the
    host stores nor the volume."""
    world, docker = image_world("p4-live", ROLES)
    project = Project(docker, world)
    try:
        apply_only(world)
        store = build_store(world.root, crashed=False)
        up = _sleepers(project, world, ROLES)
        assert up.returncode == 0, explain(up)
        project.wait_running(ROLES)
        host_before, volume_before = store.snapshot(), _inventory(project, world)
        _assert_writers_refused(prepare(world), ROLES, project, world, store, host_before, volume_before)
    finally:
        remaining = project.down()
    assert not remaining, f"left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)  # kept for inspection when any step failed


def test_prepare_refuses_writers_running_one_off_container():
    """GREEN-IF, with only a one-off `docker compose run -d` container of the project running, prepare
    refuses `writers_running` naming `tooling` and the stop command, and changes nothing."""
    world, docker = image_world("p4-oneoff", ("tooling",))
    project = Project(docker, world)
    try:
        apply_only(world)
        store = build_store(world.root, crashed=False)
        started = project.compose("run", "-d", "--no-deps", "--entrypoint", "sleep", "tooling", "600")
        assert started.returncode == 0, explain(started)
        label = f"label=com.docker.compose.project={world.project}"
        oneoff = project.docker_run("ps", "-q", "--filter", label, "--filter", "label=com.docker.compose.oneoff=True")
        assert oneoff.stdout.split(), f"precondition: a one-off container runs\n{explain(oneoff)}"
        host_before, volume_before = store.snapshot(), _inventory(project, world)
        _assert_writers_refused(prepare(world), ("tooling",), project, world, store, host_before, volume_before)
    finally:
        remaining = project.down()
    assert not remaining, f"left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)


@pytest.mark.parametrize("how", sorted(UNREACHABLE))
def test_prepare_refuses_docker_unreachable_and_creates_nothing(how):
    """GREEN-IF, with Docker unreachable, prepare refuses `docker_unreachable`, no volume exists afterwards,
    and the host stores are unchanged."""
    world, docker = image_world(f"p4-nodocker-{how}", ("tooling",))
    project = Project(docker, world)
    try:
        apply_only(world)
        store = build_store(world.root, crashed=False)
        host_before = store.snapshot()
        assert not project.volume_exists(store_volume_name(world.project)), "precondition: no volume yet"
        proc = prepare(world, **_unreachable(how, world))
        why = prepare_refused(proc, "docker_unreachable")
        assert why is None, f"{why}\n" + explain(proc)
        assert not project.volume_exists(store_volume_name(world.project)), "a refused prepare created the volume"
        assert store.snapshot() == host_before, "the host stores changed"
    finally:
        remaining = project.down()
    assert not remaining, f"left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)


def test_prepare_refuses_an_absent_image_and_creates_nothing():
    """GREEN-IF, with an image digest that no local image has, prepare refuses `image_absent`, and no volume
    exists afterwards."""
    world, docker = image_world("p4-noimage", ("tooling",))
    world = replace(world, image=DIGEST_REGISTRY)
    project = Project(docker, world)
    try:
        absent = project.docker_run("image", "inspect", DIGEST_REGISTRY)
        assert absent.returncode != 0, "precondition: the digest names no local image"
        apply_only(world)
        build_store(world.root, crashed=False)
        proc = prepare(world)
        why = prepare_refused(proc, "image_absent")
        assert why is None, f"{why}\n" + explain(proc)
        assert not project.volume_exists(store_volume_name(world.project)), "a refused prepare created the volume"
    finally:
        remaining = project.down()
    assert not remaining, f"left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)


def test_prepare_on_a_prepared_root_is_a_no_op_while_roles_run():
    """GREEN-IF, on a prepared root whose roles run (one of them having written into the volume), prepare
    reports `prepared`, exits 0, and changes neither the volume, the receipt nor the running roles."""
    world, docker = image_world("p4-noop", ROLES)
    project = Project(docker, world)
    try:
        install(world)
        up = project.compose("up", "-d")
        assert up.returncode == 0, explain(up)
        cids = project.wait_running(ROLES)
        wrote = project.docker_run("exec", "-i", cids["tooling"], "python3", "-c",
                                   "open('/state/memory/t9b-noop.txt', 'w').write('kept')")
        assert wrote.returncode == 0, explain(wrote)
        volume_before = _inventory(project, world)
        receipt_before = (world.root / "receipt.json").read_bytes()
        states_before = {s: project.state(c).get("StartedAt") for s, c in cids.items()}
        again = prepare(world)
        why = prepared(again)
        assert why is None, f"prepare on a prepared root with roles running: {why}\n" + explain(again)
        after = _inventory(project, world)
        assert after == volume_before, "prepare on a prepared root changed the volume:\n" + "\n".join(
            tree_diff(volume_before["entries"], after["entries"]))
        assert (world.root / "receipt.json").read_bytes() == receipt_before, "prepare on a prepared root changed the receipt"
        assert {s: project.state(c).get("StartedAt") for s, c in cids.items()} == states_before, (
            "prepare on a prepared root stopped or restarted a role")
        assert all(project.state(c).get("Running") for c in cids.values()), "a role stopped"
    finally:
        remaining = project.down()
    assert not remaining, f"left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)
