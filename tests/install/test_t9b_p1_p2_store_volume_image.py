"""T9b P1, P2, the prepared state, and a P8 guard in real containers (image-marked).

Order: docs/work/orders/T9b-memory-store-volume.md, Amendment 5 (the consolidated order).
- P1: every role that mounts /state has the project volume `<project>_memory` at /state/memory;
  every store path a role opens resolves under /state/memory, so a store built by the documented
  first-run steps lands in the volume, not on the bind. Everything else under /state stays on the
  host bind (P8). Falsifier: a service whose /state/memory is the host bind; a store path a role
  opens outside /state/memory.
- "Prepared" (the definition, P4 step 4): measured after `apply`, then `prepare` with Docker
  reachable and the image present, then `up`: the volume's root, as every role sees it, is owned
  by AGENT_UID:AGENT_GID with mode 0700.
- P2: in two different role containers, `lockf` and `flock` on a file on the volume, and SQLite
  `BEGIN IMMEDIATE` on a database on the volume, are refused to the second holder. Falsifier: any
  of them granted.
- P8 (guard): a fresh empty root works through the documented sequence.

Setup (one project for the module): an empty root, `plan`, `apply`, `prepare` with components
tooling,refresh,capture,board and both transcript roots, then the documented
`docker compose --project-directory "$root" up -d`. No operator file is written before `up`.

Instruments: `docker inspect` (Mounts) of each role container; `os.stat` and file writes by
a process in each role container (`docker exec`); for P2, a holder process started in one
role with `docker exec -d` that takes the lock (or opens BEGIN IMMEDIATE) and then writes
`<path>.ready`, and a probe in each other role that tries the same without waiting. A probe
counts as refused only with EACCES or EAGAIN (locks) or "database is locked" (SQLite);
failing to open is an error, not a refusal. After the holder releases, one probe must be
granted (the control). The first-run registry: the registry operator configuration as
docs/DOCKER.md documents it (its `state_root` and `roster_path`), the state root created by a
one-off role container (`docker compose run --rm -T tooling`), and the documented
`kp-agent-desk-registry ... initialize` in the tooling role.

Measured before writing (Docker Desktop for macOS, 2026-10-02, image t9b-base:product): on a
host bind `lockf` is GRANTED across two containers and within one; on a named volume it is
REFUSED (errno 11). On a Linux host, bind mounts honour POSIX locks, so P2 does not fail at
base there; P1 does, on every host.
"""
from __future__ import annotations

import json
import posixpath
import re
import shutil
import uuid
from pathlib import Path, PurePosixPath

import pytest

from s3_harness import explain
from t7a_harness import write_private
from t9b_harness import (HOLD, MECHANISMS, STATE_ROLES, STATE_TARGET, STORE_TARGET, Project, exec_python,
                         image_world, install, mounts, probe_lock, regular_files, store_volume_name, verify,
                         wait_exists)

pytestmark = pytest.mark.image

DOC = Path(__file__).resolve().parents[2] / "docs" / "DOCKER.md"
STAT = r'''
import json, os, stat, sys
info = os.stat(sys.argv[1])
print(json.dumps({"uid": info.st_uid, "gid": info.st_gid, "mode": stat.S_IMODE(info.st_mode),
                  "dir": stat.S_ISDIR(info.st_mode)}))
'''
WRITE = "import sys; open(sys.argv[1], 'w').write(sys.argv[2])"


@pytest.fixture(scope="module")
def live():
    world, docker = image_world("p1", STATE_ROLES)
    project = Project(docker, world)
    try:
        install(world)
        up = project.compose("up", "-d")
        assert up.returncode == 0, "`docker compose --project-directory \"$root\" up -d` failed\n" + explain(up)
        cids = project.wait_running(STATE_ROLES)
        yield world, project, cids
    finally:
        remaining = project.down()
    assert not remaining, f"left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)


def _host_source(source: str) -> Path:
    """A bind's host path. Meet (Coordinator), portability: Docker Desktop sometimes reports a bind's Source
    with its VM-side `/host_mnt` prefix (`/host_mnt/Volumes/...` for `/Volumes/...`); Linux never does."""
    if source.startswith("/host_mnt/"):
        source = source[len("/host_mnt"):]
    return Path(source).resolve()


def _store_mount(project, cid):
    rows = mounts(project, cid)
    return rows, [m for m in rows if m.get("Destination") == STORE_TARGET]


@pytest.mark.parametrize("service", STATE_ROLES)
def test_store_mount_is_the_project_volume_and_the_rest_of_state_stays_on_the_bind(live, service):
    """GREEN-IF the role's /state/memory is the Docker volume `<project>_memory`; /state is the bind of
    `<root>/state` with no other mount below it; and a file the role writes elsewhere under /state
    appears on the host."""
    world, project, cids = live
    rows, store = _store_mount(project, cids[service])
    assert len(store) == 1, (f"{service}: nothing is mounted at {STORE_TARGET}; it resolves to the host bind. "
                             f"mounts: {json.dumps(rows)}")
    mount = store[0]
    assert mount.get("Type") == "volume", f"{service}: {STORE_TARGET} is a {mount.get('Type')} mount: {mount}"
    assert mount.get("Name") == store_volume_name(world.project), (
        f"{service}: the store volume is {mount.get('Name')!r}, not {store_volume_name(world.project)!r}")
    state = [m for m in rows if m.get("Destination") == STATE_TARGET]
    assert len(state) == 1 and state[0].get("Type") == "bind" and \
        _host_source(state[0].get("Source", "")) == (world.root / "state").resolve(), (
            f"{service}: {STATE_TARGET} is no longer the bind of the runtime root's state: {state}")
    below = sorted(m.get("Destination") for m in rows
                   if str(m.get("Destination", "")).startswith(STATE_TARGET + "/")
                   and m.get("Destination") != STORE_TARGET)
    assert not below, f"{service}: other state paths left the bind: {below}"
    marker = f"t9b-bind-{service}-{uuid.uuid4().hex[:8]}"
    wrote = exec_python(project, cids[service], WRITE, f"{STATE_TARGET}/tmp/{marker}", marker)
    assert wrote.returncode == 0, explain(wrote)
    on_host = world.root / "state" / "tmp" / marker
    assert on_host.is_file() and on_host.read_text() == marker, (
        f"{service}: a file written at {STATE_TARGET}/tmp is not on the host bind")
    on_host.unlink()


@pytest.mark.parametrize("service", STATE_ROLES)
def test_store_is_owned_by_the_runtime_user_with_mode_0700(live, service):
    """GREEN-IF /state/memory, as the role sees it, is a directory owned by AGENT_UID:AGENT_GID, mode 0700."""
    world, project, cids = live
    proc = exec_python(project, cids[service], STAT, STORE_TARGET)
    assert proc.returncode == 0, f"{service}: {STORE_TARGET} does not exist in the role\n" + explain(proc)
    seen = json.loads(proc.stdout)
    assert seen["dir"] and (seen["uid"], seen["gid"], seen["mode"]) == (world.uid, world.gid, 0o700), (
        f"{service}: {STORE_TARGET} is uid {seen['uid']} gid {seen['gid']} mode {oct(seen['mode'])}, "
        f"not {world.uid}:{world.gid} 0o700")


@pytest.mark.parametrize("holder", STATE_ROLES)
@pytest.mark.parametrize("mechanism", MECHANISMS)
def test_a_store_lock_held_in_one_role_is_refused_in_every_other_role(live, mechanism, holder):
    """GREEN-IF, while a process in `holder` holds `mechanism` on a file under /state/memory, the same
    attempt without waiting from each other role is refused, and is granted once the holder releases."""
    world, project, cids = live
    run = uuid.uuid4().hex[:8]
    suffix = ".sqlite3" if mechanism.startswith("sqlite") else ".lock"
    path = f"{STORE_TARGET}/t9b-lock-probe/{mechanism}-{holder}-{run}{suffix}"
    error_file = f"{STATE_TARGET}/tmp/t9b-holder-{run}.error"
    started = project.docker_run("exec", "-d", cids[holder], "python3", "-c", HOLD, mechanism, path, error_file)
    assert started.returncode == 0, explain(started)
    others = [service for service in STATE_ROLES if service != holder]
    try:
        if not wait_exists(project, cids[holder], path + ".ready"):
            error = world.root / "state" / "tmp" / f"t9b-holder-{run}.error"
            pytest.fail(f"the holder in {holder} did not take {mechanism} on {path}: "
                        f"{error.read_text() if error.is_file() else 'no error recorded'}", pytrace=False)
        results = {service: probe_lock(project, cids[service], mechanism, path) for service in others}
        errors = {s: r for s, r in results.items() if r["result"] not in ("granted", "refused")}
        assert not errors, f"the probe could not test {mechanism}: {errors}"
        granted = sorted(s for s, r in results.items() if r["result"] == "granted")
        assert not granted, (f"{mechanism} held in {holder} on {path} was granted again in {granted}: "
                             f"locking does not work for the store. results={results}")
    finally:
        exec_python(project, cids[holder], "import sys; open(sys.argv[1], 'w').close()", path + ".release")
    assert wait_exists(project, cids[holder], path + ".ready", present=False), "the holder did not release"
    control = probe_lock(project, cids[others[0]], mechanism, path)
    assert control["result"] == "granted", f"control: after release {mechanism} is still refused: {control}"


def _documented_registry() -> dict:
    """The registry operator configuration as docs/DOCKER.md's first run writes it."""
    text = DOC.read_text()
    for block in re.findall(r"\{[^{}]*\"schema_version\":\s*\"ops\.desk-memory\.local\.v1\"[^{}]*\}", text, re.S):
        if '"state_root"' in block and "registry" in block:
            values = dict(re.findall(r'"(\w+)":\s*"([^"$]*)"', block))
            if values.get("state_root", "").startswith("/"):
                return values
    pytest.fail("docs/DOCKER.md documents no registry operator configuration with a state_root", pytrace=False)


def _documented_roster() -> str | None:
    match = re.search(r'"roster_path":\s*"([^"$]+)"', DOC.read_text())
    return match.group(1) if match else None


def test_a_store_built_by_the_documented_first_run_lands_in_the_volume(live):
    """GREEN-IF the documented registry state_root (and roster) resolve under /state/memory, and after the
    documented `initialize` the registry's files exist in the role while none is on the host bind."""
    world, project, cids = live
    registry = _documented_registry()
    state_root, roster = registry["state_root"], _documented_roster()
    outside = [p for p in (state_root, roster) if p and not PurePosixPath(posixpath.normpath(p)).is_relative_to(STORE_TARGET)]
    assert not outside, f"docs/DOCKER.md documents store paths outside {STORE_TARGET}: {outside}"
    write_private(world.root / "config" / "launch" / "desks.json", json.dumps({
        "schema_version": "agent-tooling.desk-registry.v1", "tenant_id": "t9b-first-run",
        **({"roster_path": roster} if roster else {}),
        "harness_profiles_path": "/config/launch/harness-profiles.json"}) + "\n")
    write_private(world.root / "config" / "launch" / "registry.json", json.dumps({
        "schema_version": "ops.desk-memory.local.v1", "state_root": state_root,
        "catalog_path": "/config/launch/desks.json", "workspace_root": "/config/launch",
        "provider_instance": "agent-tooling", "provider_session_id": "registry-operator"}) + "\n")
    made = project.compose("run", "--rm", "-T", "tooling", "sh", "-c",
                           f"mkdir -p -m 700 {state_root} && chmod 700 {state_root}")
    assert made.returncode == 0, "the in-runtime creation of the state root failed\n" + explain(made)
    host_before = {p: regular_files(p) for p in (world.root / "state").iterdir() if p.is_dir()}
    initialized = project.docker_run("exec", "-i", f"{world.project}-tooling", "kp-agent-desk-registry", "--config",
                                     "/config/launch/registry.json", "initialize")
    assert initialized.returncode == 0, "the documented initialize failed\n" + explain(initialized)
    listed = exec_python(project, cids["tooling"], "import os, sys; print(len(os.listdir(sys.argv[1])))", state_root)
    assert listed.returncode == 0 and int(listed.stdout.strip() or 0) > 0, (
        f"initialize wrote nothing at {state_root}\n" + explain(listed))
    host_after = {p: regular_files(p) for p in (world.root / "state").iterdir() if p.is_dir()}
    landed = {str(p.relative_to(world.root)): sorted(set(files) - set(host_before.get(p, [])))
              for p, files in host_after.items() if set(files) - set(host_before.get(p, []))}
    assert not landed, f"the registry store landed on the host bind: {landed}"


def test_fresh_empty_root_install_still_works(live):
    """P8 guard (passes at base by design). GREEN-IF, after plan/apply/prepare/up from an empty root,
    tooling becomes healthy, every /state role keeps running, and `verify` exits 0."""
    world, project, cids = live
    project.wait_healthy(["tooling"])
    project.assert_stays_up(STATE_ROLES, window=4.0)
    verified = verify(world)
    assert verified.returncode == 0, explain(verified)
