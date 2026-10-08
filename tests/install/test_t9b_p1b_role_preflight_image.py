"""T9b P1b: a role refuses to start on a store that is not the project volume (image-marked).

Order: docs/work/orders/T9b-memory-store-volume.md, Amendment 5 (the consolidated order), P1b:
the role preflight (`deploy/image/container.py`, at role start) refuses when `/state/memory` is
not the project volume (`/proc/self/mountinfo`, not a marker file); when the store is not
"prepared" (the volume `<project>_memory` exists, its root is owned by AGENT_UID:AGENT_GID with
mode 0700, and no pre-T9b store directory is left to migrate on the bind); or when an operator
file present at start names a store path outside `/state/memory`. Each refusal has a named reason.
Falsifier: remove the mount; mount a host directory holding a copied marker or a restored
`memory.migrated-<...>`; leave the volume unprepared; start with an operator file naming an
outside path. In each case the role must refuse and does not.
Open-time refusal by `docker exec`'d tools is T11's, not tested here.

Setup: an empty root, `plan`, `apply` and `prepare` (Docker reachable, image present), components
tooling,refresh,capture,board and both transcript roots. Each case starts one
role with `docker compose ... up -d --no-deps <role>`:
- mount removed / host directory: from a copy of the rendered compose.yaml in which only that
  role's /state/memory mount is removed, or replaced by a bind of the host directory (a stale
  compose file, or a `compose run` from an old root);
- the copied marker: a host directory holding a copy of everything in the prepared volume,
  made after the roles had run on it;
- the restored copy: a host directory `state/memory.migrated-<12 hex>` holding a store;
- unprepared: the project volume removed, so that Compose creates it afresh at `up` (root-owned);
- left to migrate: a pre-T9b `state/registry` holding files on the bind beside the prepared
  volume (visible to every role through the /state bind);
- operator file: the role's own documented operator file naming a `state_root` outside
  /state/memory (tooling: `config/sessions/<session>.json`, docs/DOCKER.md "Register a
  harness"; capture: `config/capture/session.json`; board: `config/launch/registry.json`,
  its Desks registry). The same file naming a path under /state/memory is the control: the
  role starts and keeps running. Refresh has no operator file that names a store.

Readings (repeated in the arm report under AMBIGUITY): "the role refuses" is read as T12a reads it
(docs/work/orders/T12a-capture-survives.md, A2, the named regression-set exception: "does not exec the
role command, and health says refused" instead of "exit 3"), through the one helper
t9b_harness.assert_role_refused (Coordinator rule C2): the container stays running and does not restart,
`tooling-container health` says `refused`, a JSON log line names the preflight's reason, and the role
command is not exec'd. `store_not_mounted` waits like the four refusals the order names (Coordinator meet
ruling: every store-preflight refusal of a long-running role command waits). "A named reason" is that JSON
reason (store_not_mounted, store_not_project_volume, store_unprepared or
operator_file_names_outside_store), and for an unprepared store the log also names
the preparation (`prepar...`); "never creates a fresh empty store on the bind" means the host directory
mounted at /state/memory (or the host state/memory) holds no new file after the attempt.
"""
from __future__ import annotations

import json
import shutil
import uuid

import pytest

from s3_harness import explain
from t7a_harness import write_private
from t9b_harness import (STATE_ROLES, STORE_TARGET, Project, assert_role_refused, build_store, compose_with,
                         host_tree, image_world, install, stale_compose, store_volume_name, tree_diff, wait_exit)

pytestmark = pytest.mark.image

OUTSIDE = ("/state/registry", "/state/memory/../registry")
OPERATOR_FILES = {"tooling": "config/sessions/t9b-session.json", "capture": "config/capture/session.json",
                  "board": "config/launch/registry.json"}


def _remove_role(project: Project, role: str) -> None:
    ids = project.docker_run("ps", "-aq", "--filter", f"label=com.docker.compose.project={project.project}",
                             "--filter", f"label=com.docker.compose.service={role}").stdout.split()
    if ids:
        project.docker_run("rm", "-f", *ids)


def _start_refused(project: Project, role: str, reason: str, manifest=None) -> str:
    """Start `role` alone; assert that its store preflight refused it with `reason` (T12a's reading, through
    t9b_harness.assert_role_refused, while the container is up); return its log."""
    _remove_role(project, role)
    args = ("up", "-d", "--no-deps", role)
    up = compose_with(project, manifest, *args) if manifest is not None else project.compose(*args)
    cid = project.container(role)
    assert cid, f"`up` created no {role} container\n" + explain(up)
    try:
        return assert_role_refused(project, role, reason)["logs"]
    finally:
        _remove_role(project, role)


@pytest.fixture(scope="module")
def prepared():
    """A prepared install whose roles ran once on the volume; then stopped and removed."""
    world, docker = image_world("p1b", STATE_ROLES)
    project = Project(docker, world)
    try:
        install(world)
        up = project.compose("up", "-d")
        assert up.returncode == 0, explain(up)
        project.wait_running(STATE_ROLES)
        assert project.compose("stop").returncode == 0
        copied = world.base / "copied-volume"
        copied.mkdir(mode=0o700)
        copy = project.docker_run(
            "run", "--rm", "--name", f"t9b-test-copy-{uuid.uuid4().hex[:8]}", "--network", "none",
            "--user", f"{world.uid}:{world.gid}", "--mount",
            f"type=volume,source={store_volume_name(world.project)},target=/src,readonly",
            "--mount", f"type=bind,source={copied},target=/dst", "--entrypoint", "sh", world.image,
            "-c", "cp -a /src/. /dst/")
        assert copy.returncode == 0, "could not copy the prepared volume\n" + explain(copy)
        scratch = world.base / "scratch-root"
        (scratch / "state").mkdir(parents=True)
        build_store(scratch, crashed=False)
        restored = world.root / "state" / "memory.migrated-0123456789ab"
        shutil.move(str(scratch / "state" / "memory"), restored)
        assert project.compose("rm", "-f").returncode == 0
        yield world, project, copied, restored
    finally:
        remaining = project.down()
    assert not remaining, f"left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)


@pytest.mark.parametrize("role", STATE_ROLES)
def test_a_role_refuses_to_start_without_the_store_volume(prepared, role):
    """GREEN-IF the role, started from a compose file without its /state/memory mount, is refused
    (`store_not_mounted`) and leaves the host state/memory without a new file."""
    world, project, _, _ = prepared
    before = host_tree(world.root / "state" / "memory")
    _start_refused(project, role, "store_not_mounted", stale_compose(world, role))
    created = tree_diff(before, host_tree(world.root / "state" / "memory"))
    assert not created, f"{role} created a store on the host bind: {created}"


@pytest.mark.parametrize("role", STATE_ROLES)
def test_a_role_refuses_a_host_directory_holding_a_copied_marker(prepared, role):
    """GREEN-IF the role, with a host copy of the prepared volume bind-mounted at /state/memory, is
    refused (`store_not_project_volume`) and adds no file to that directory."""
    world, project, copied, _ = prepared
    before = host_tree(copied)
    _start_refused(project, role, "store_not_project_volume", stale_compose(world, role, bind=copied))
    assert not tree_diff(before, host_tree(copied)), f"{role} wrote into the host copy: {tree_diff(before, host_tree(copied))}"


@pytest.mark.parametrize("role", STATE_ROLES)
def test_a_role_refuses_a_restored_migrated_directory(prepared, role):
    """GREEN-IF the role, with a restored state/memory.migrated-<...> bind-mounted at /state/memory,
    is refused (`store_not_project_volume`) and adds no file to it."""
    world, project, _, restored = prepared
    before = host_tree(restored)
    _start_refused(project, role, "store_not_project_volume", stale_compose(world, role, bind=restored))
    assert not tree_diff(before, host_tree(restored)), f"{role} wrote into {restored.name}"


@pytest.mark.parametrize("role", STATE_ROLES)
def test_a_role_refuses_an_unprepared_volume_naming_the_reason(prepared, role):
    """GREEN-IF, with the project volume removed so that Compose creates it afresh at `up`, the role is
    refused (`store_unprepared`) and its log names the missing preparation."""
    world, project, _, _ = prepared
    for service in STATE_ROLES:
        _remove_role(project, service)
    removed = project.docker_run("volume", "rm", "-f", store_volume_name(world.project))
    assert removed.returncode == 0, explain(removed)
    logs = _start_refused(project, role, "store_unprepared")
    assert "prepar" in logs.lower(), f"{role} refused an unprepared volume without naming the reason:\n{logs[-2000:]}"


@pytest.fixture(scope="module")
def configured():
    world, docker = image_world("p1b-cfg", STATE_ROLES)
    project = Project(docker, world)
    try:
        install(world)
        yield world, project
    finally:
        remaining = project.down()
    assert not remaining, f"left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)


def _operator_file(world, role: str, state_root: str) -> None:
    (world.root / OPERATOR_FILES[role]).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(world.root / OPERATOR_FILES[role], json.dumps({
        "schema_version": "ops.desk-memory.local.v1", "state_root": state_root,
        "catalog_path": "/config/launch/desks.json", "workspace_root": "/config/launch",
        "provider_instance": "t9b", "provider_session_id": "t9b-preflight"}) + "\n")


@pytest.mark.parametrize("role", sorted(OPERATOR_FILES))
def test_a_role_refuses_an_operator_file_naming_a_store_outside_the_volume(configured, role):
    """GREEN-IF the role keeps running with its operator file naming a state_root under /state/memory
    (the control), and is refused (`operator_file_names_outside_store`) with the same file naming each path
    outside it."""
    world, project = configured
    _operator_file(world, role, f"{STORE_TARGET}/t9b-preflight")
    _remove_role(project, role)
    up = project.compose("up", "-d", "--no-deps", role)
    cid = project.container(role)
    assert cid, explain(up)
    state = wait_exit(project, cid, 8.0)
    assert state.get("Running"), (f"control: {role} does not start with its operator file under {STORE_TARGET}: "
                                  f"{json.dumps(state)}\n{project.logs(cid)[-2000:]}")
    for outside in OUTSIDE:
        _operator_file(world, role, outside)
        _start_refused(project, role, "operator_file_names_outside_store")
    (world.root / OPERATOR_FILES[role]).unlink()


@pytest.mark.parametrize("role", STATE_ROLES)
def test_a_role_refuses_while_a_pre_t9b_store_is_left_to_migrate(configured, role):
    """GREEN-IF, with the volume prepared but a pre-T9b state/registry holding files on the bind, the role
    is refused (`store_unprepared`) naming the preparation (the store is not "prepared")."""
    world, project = configured
    legacy = world.root / "state" / "registry"
    legacy.mkdir(mode=0o700, exist_ok=True)
    (legacy / "roles.json").write_text('{"roles": []}\n')
    try:
        logs = _start_refused(project, role, "store_unprepared")
        assert "prepar" in logs.lower() or "migrat" in logs.lower(), (
            f"{role} refused without naming the unmigrated store:\n{logs[-2000:]}")
    finally:
        shutil.rmtree(legacy, ignore_errors=True)
