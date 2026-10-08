"""T9b P3, P4 (step 5) and P6: every pre-T9b store migrates once, losslessly (image-marked).

Order: docs/work/orders/T9b-memory-store-volume.md, Amendment 5 (the consolidated order): P3
(the migration, performed only by `kp-agent-install prepare`), P4 step 5 (refusals
`source_damaged`, `host_files_beside_populated_volume`, `copy_or_check_failed`, with both sides
unchanged; nothing created by a refused run) and P6 (pre-migration launches).
- Every store directory a pre-T9b root holds (`state/memory/*`, `state/registry`, the
  `state/assistant` store, `state/desk-memory`) migrates into the volume at its new path
  (/state/memory/, /state/memory/registry, /state/memory/assistant, /state/memory/desk-memory):
  sidecars are never copied; each `*.sqlite3` is copied with the backup API and is equal
  logically (quick_check ok; each ordinary table's rows in primary-key or rowid order digest
  the same); other files byte-equal with their mode; owned by AGENT_UID:AGENT_GID. Each host
  directory is renamed `<name>.migrated-<plan_sha256[:12]>` and never deleted; the receipt
  records the migration.
- Operator files naming an old path are never rewritten; `apply`'s report and `verify` list each
  one with its required new path.
- A re-apply when the volume already holds the store copies nothing. Host files beside a
  populated volume are refused loudly, naming both (not the renamed directories, not an empty
  mount point Docker recreated).
- A source database that fails quick_check refuses the apply, naming the database and the
  documented recovery, with both sides as they were. Any failure refuses the apply with both
  sides as they were.
- D3: after a migration that moves a store path, a hook for a launch made before the
  migration refuses and never writes outside /state/memory.
Falsifier: a byte, row or file missing or changed (a table whose content changed but whose
count matches); a sidecar copied; the host copy deleted; a second copy over a populated
volume; host files beside a populated volume ignored silently; a damaged source accepted; an
apply that reports success after a failed copy or check; a populated pre-T9b store directory
neither migrated nor reported; an operator file silently rewritten; a pre-migration launch's
hook that writes outside /state/memory or succeeds against an old path.

Setup: an empty root, `plan` and `apply` (never prepared: a pre-T9b root), then the test
writes the pre-T9b stores on the host bind (t9b_harness.build_store) and runs `prepare` with
Docker reachable and the image present. The volume is read by an inspection container (t9b_harness.volume_state),
never in place. Readings: see t9b_harness; also, a report and `verify` "name" an operator file
and its new path when both strings appear in their output, and "the documented recovery" is
named when the refusal points at docs/DOCKER.md. D3 runs the hook as a docker-mode
host launch does (`docker exec -i <project>-capture kp-agent-launch --receipt ... hook`), both
before the documented operator edits and after them. Before the edits the capture role is refused by its
store preflight. Under T12a (A2) it stays up and waits instead of exiting 3, so the exec'd hook runs in it
and must itself fail; before T12a the container had exited and the `docker exec` failed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import uuid

import pytest

from s3_harness import explain
from t7a_harness import write_private
from t9b_harness import (LEGACY, MIGRATED, STORE_TARGET, Project, apply_only, apply_plan, build_store,
                         host_copy_problems, host_tree, image_world, install_argv, migrated_directories, prepare,
                         prepare_refused, prepared, regular_files, result_json, store_volume_name, tree_diff,
                         verify, volume_problems, volume_state)

pytestmark = pytest.mark.image

COUNT = r'''
import json, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
print(json.dumps({t: connection.execute('select count(*) from "%s"' % t).fetchone()[0] for t in sys.argv[2:]}))
connection.close()
'''
# Operator files of a pre-T9b root that name old store paths: (key, old path, new path).
OLD_OPERATOR_PATHS = {
    "config/launch/registry.json": ("state_root", "/state/registry", f"{STORE_TARGET}/registry"),
    "config/launch/desks.json": ("roster_path", "/state/registry/roles.json", f"{STORE_TARGET}/registry/roles.json"),
}


def _inventory(project: Project, world):
    return volume_state(project, store_volume_name(world.project), image=world.image, uid=world.uid, gid=world.gid)


def _registry_files(world, *, old: bool) -> dict[str, bytes]:
    """The registry operator configuration and its descriptor, naming the old or the new store paths."""
    index = 1 if old else 2
    files = {
        "config/launch/desks.json": {"schema_version": "agent-tooling.desk-registry.v1", "tenant_id": "t9b",
                                     "roster_path": OLD_OPERATOR_PATHS["config/launch/desks.json"][index],
                                     "harness_profiles_path": "/config/launch/harness-profiles.json"},
        "config/launch/registry.json": {"schema_version": "ops.desk-memory.local.v1",
                                        "state_root": OLD_OPERATOR_PATHS["config/launch/registry.json"][index],
                                        "catalog_path": "/config/launch/desks.json",
                                        "workspace_root": "/config/launch", "provider_instance": "t9b",
                                        "provider_session_id": "t9b-operator"}}
    out = {}
    for relative, value in files.items():
        data = (json.dumps(value) + "\n").encode()
        write_private(world.root / relative, data)
        out[relative] = data
    return out


def _plan_sha(world) -> str:
    return json.loads((world.root / "receipt.json").read_text())["plan_sha256"]


def _migrate(world, project: Project, *, legacy=("memory",), crashed: bool = True):
    """A pre-T9b root (applied, never prepared) holding stores, then `prepare`: the migration."""
    apply_only(world)
    store = build_store(world.root, crashed=crashed, legacy=legacy)
    done = prepare(world)
    assert prepared(done) is None, f"prepare did not migrate the pre-T9b stores: {prepared(done)}\n" + explain(done)
    suffix = _plan_sha(world)[:12]
    for name in legacy:
        renamed = world.root / "state" / f"{name}{MIGRATED}{suffix}"
        assert renamed.is_dir(), (f"prepare reported prepared but state/{name} was not renamed to {renamed.name}; "
                                  f"state/ holds {sorted(p.name for p in (world.root / 'state').iterdir())}\n"
                                  + explain(done))
    return store


def _finish(remaining: list[str], world) -> None:
    assert not remaining, f"left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)  # kept for inspection when any step failed


def test_every_pre_t9b_store_migrates_once_losslessly():
    """GREEN-IF apply exits 0; the volume holds every store file at its new path, owned by the runtime
    user, with its mode, the same bytes (plain files) or a quick_check-ok database with the same rows in
    key order, and no copied sidecar; each host directory is renamed <name>.migrated-<plan_sha256[:12]>
    intact; the receipt names each; the operator files are unchanged and both apply and verify name each
    with its new path until it is edited; and the role reads the store at /state/memory."""
    world, docker = image_world("p3-migrate", ("tooling",))
    project = Project(docker, world)
    try:
        argv = install_argv(world)
        applied, digest = apply_plan(world, argv)
        assert applied.returncode == 0, explain(applied)
        store = build_store(world.root, crashed=True, legacy=tuple(LEGACY))
        operator = _registry_files(world, old=True)
        planned_again, digest = apply_plan(world, argv)  # the upgrade: plan and apply on the populated root
        assert planned_again.returncode == 0, explain(planned_again)
        done = prepare(world)
        assert prepared(done) is None, f"prepare did not migrate the pre-T9b stores: {prepared(done)}\n" + explain(done)
        receipt = (world.root / "receipt.json").read_text()
        assert json.loads(receipt)["plan_sha256"] == digest
        for name in LEGACY:
            renamed = world.root / "state" / f"{name}{MIGRATED}{digest[:12]}"
            assert renamed.is_dir(), f"state/{name} was not renamed to {renamed.name}\n" + explain(done)
            problems = host_copy_problems(renamed, store, name)
            assert not problems, f"the renamed {renamed.name} is not intact:\n" + "\n".join(problems)
            left = regular_files(world.root / "state" / name)
            assert not left, f"state/{name} was copied, not renamed: it still holds {left}"
            assert renamed.name in receipt, f"the receipt does not record the migration to {renamed.name}"

        problems = volume_problems(_inventory(project, world), store, world.uid, world.gid)
        assert not problems, "the volume does not hold the stores losslessly:\n" + "\n".join(problems)

        for relative, data in operator.items():
            assert (world.root / relative).read_bytes() == data, f"apply rewrote the operator file {relative}"
        report = planned_again.stdout + planned_again.stderr
        verified = verify(world)
        for relative, (key, old, new) in OLD_OPERATOR_PATHS.items():
            assert relative in report and new in report, (
                f"apply's report does not name {relative} ({key} {old}) with its new path {new}\n"
                + explain(planned_again))
            assert relative in verified.stdout and new in verified.stdout, (
                f"verify's readiness does not name {relative} with its new path {new}\n" + explain(verified))
        _registry_files(world, old=False)
        verified = verify(world)
        assert "/state/registry" not in verified.stdout.replace(STORE_TARGET + "/registry", ""), (
            "verify still names an old store path after the operator edits\n" + explain(verified))

        up = project.compose("up", "-d", "tooling")
        assert up.returncode == 0, explain(up)
        cid = project.wait_running(["tooling"])["tooling"]
        counted = project.docker_run("exec", "-i", cid, "python3", "-c", COUNT,
                                     f"{STORE_TARGET}/desk-history/episodes.sqlite3", "episodes", "cursors")
        assert counted.returncode == 0, explain(counted)
        expected = {t: v["rows"] for t, v in store.files["desk-history/episodes.sqlite3"]["tables"].items()}
        assert json.loads(counted.stdout) == expected, (
            f"the tooling role reads {counted.stdout.strip()} at {STORE_TARGET}, expected {expected}")
    finally:
        remaining = project.down()
    _finish(remaining, world)


def test_a_reapply_after_the_migration_copies_nothing():
    """GREEN-IF, after the migration, a role's write into the volume and `stop`, a re-plan, re-apply and
    re-prepare exit 0 (`prepared`), leave the volume exactly as it was, and rename nothing more."""
    world, docker = image_world("p3-reapply", ("tooling",))
    project = Project(docker, world)
    try:
        store = _migrate(world, project)
        up = project.compose("up", "-d", "tooling")
        assert up.returncode == 0, explain(up)
        cid = project.wait_running(["tooling"])["tooling"]
        wrote = project.docker_run("exec", "-i", cid, "python3", "-c", (
            "import sqlite3, sys\n"
            "c = sqlite3.connect(sys.argv[1] + '/desk-history/episodes.sqlite3')\n"
            "c.execute(\"insert into cursors values (999, 'after-migration', 1)\"); c.commit(); c.close()\n"
            "open(sys.argv[1] + '/after-migration.txt', 'w').write('written by the runtime')\n"), STORE_TARGET)
        assert wrote.returncode == 0, explain(wrote)
        assert project.compose("stop").returncode == 0
        before = _inventory(project, world)
        assert "after-migration.txt" in before["entries"], "precondition: the runtime's write is in the volume"
        renamed = migrated_directories(world.root)
        again, _ = apply_plan(world, install_argv(world))
        assert again.returncode == 0, "a re-apply after the migration was refused\n" + explain(again)
        repeated = prepare(world)
        assert prepared(repeated) is None, f"a re-run of prepare is not a no-op `prepared`\n" + explain(repeated)
        after = _inventory(project, world)
        assert after == before, ("the re-apply changed the populated volume:\n" +
                                 "\n".join(tree_diff(before["entries"], after["entries"])))
        assert migrated_directories(world.root) == renamed, (
            f"the re-apply renamed again: {renamed} -> {migrated_directories(world.root)}")
        problems = host_copy_problems(renamed[0], store, "memory")
        assert not problems, "the renamed host store changed:\n" + "\n".join(problems)
    finally:
        remaining = project.down()
    _finish(remaining, world)


@pytest.mark.parametrize("name", sorted(LEGACY))
def test_host_files_beside_a_populated_volume_are_refused_loudly(name):
    """GREEN-IF, with the store migrated and files again in the host's state/<name>, `prepare` refuses with
    `host_files_beside_populated_volume`, names state/<name> and the volume, and leaves both as they were."""
    world, docker = image_world(f"p3-beside-{name}", ("tooling",))
    project = Project(docker, world)
    try:
        _migrate(world, project, crashed=False)
        before = _inventory(project, world)
        host = world.root / "state" / name
        host.mkdir(mode=0o700, exist_ok=True)
        (host / "stray.txt").write_bytes(b"a host file beside the populated volume\n")
        (host / "desk-history").mkdir(mode=0o700, exist_ok=True)
        (host / "desk-history" / "README.txt").write_bytes(b"a different README on the host\n")
        host_before = host_tree(host)
        again = prepare(world)
        why = prepare_refused(again, "host_files_beside_populated_volume")
        assert why is None, f"host files beside a populated volume were not refused: {why}\n" + explain(again)
        output = again.stdout + again.stderr
        assert f"state/{name}" in output and store_volume_name(world.project) in output, (
            f"the refusal does not name both state/{name} and {store_volume_name(world.project)}\n" + explain(again))
        assert _inventory(project, world) == before, "the refused re-apply changed the volume"
        assert tree_diff(host_before, host_tree(host)) == [], (
            "the refused re-apply changed the host files:\n" + "\n".join(tree_diff(host_before, host_tree(host))))
    finally:
        remaining = project.down()
    _finish(remaining, world)


def _unchanged(world, project, store, volume_before, host_before, reason, proc) -> None:
    why = prepare_refused(proc, reason)
    assert why is None, f"{why}\n" + explain(proc)
    after = store.snapshot()
    assert after == host_before, "the host stores changed:\n" + "\n".join(
        f"state/{n}: {line}" for n in host_before for line in tree_diff(host_before[n], after[n]))
    assert migrated_directories(world.root) == [], f"renamed after a failure: {migrated_directories(world.root)}"
    volume_after = _inventory(project, world)
    if volume_before is None:
        assert volume_after is None, (f"the refused prepare created the volume {store_volume_name(world.project)} "
                                      f"(entries: {sorted((volume_after or {}).get('entries', {}))})")
    else:
        assert volume_after == volume_before, ("a failed migration changed the volume:\n" +
                                               "\n".join(tree_diff(volume_before["entries"],
                                                                   volume_after["entries"])))


DAMAGED = {"malformed-page": "zz-malformed.sqlite3", "not-a-database": "zz-not-a-database.sqlite3"}


@pytest.mark.parametrize("corruption", sorted(DAMAGED))
def test_a_damaged_source_database_refuses_naming_it_with_both_sides_unchanged(corruption):
    """GREEN-IF `prepare` refuses with `source_damaged`, its output names the damaged database and
    docs/DOCKER.md (the documented recovery), and the host stores and the volume are as they were;
    nothing is renamed."""
    world, docker = image_world(f"p3-{corruption}", ("tooling",))
    project = Project(docker, world)
    try:
        apply_only(world)
        store = build_store(world.root, crashed=False, corrupt=corruption, legacy=("memory", "registry"))
        volume_before = _inventory(project, world)
        host_before = store.snapshot()
        proc = prepare(world)
        _unchanged(world, project, store, volume_before, host_before, "source_damaged", proc)
        output = proc.stdout + proc.stderr
        assert DAMAGED[corruption] in output, f"the refusal does not name {DAMAGED[corruption]}\n" + explain(proc)
        assert "DOCKER.md" in output, "the refusal does not name the documented recovery\n" + explain(proc)
    finally:
        remaining = project.down()
    _finish(remaining, world)


def test_a_failed_copy_refuses_with_both_sides_unchanged():
    """GREEN-IF, when the copy cannot complete (the project's volume, empty and owned by the runtime user,
    is a 256 KiB tmpfs and the store holds over 1 MiB), `prepare` refuses with `copy_or_check_failed` and
    leaves the host stores and the volume as they were."""
    world, docker = image_world("p3-copyfail", ("tooling",))
    project = Project(docker, world)
    try:
        apply_only(world)
        name = store_volume_name(world.project)
        assert project.store_volume() == name, f"precondition (P1): no volume {name} at {STORE_TARGET}"
        created = project.docker_run(
            "volume", "create", "--driver", "local", "--opt", "type=tmpfs", "--opt", "device=tmpfs",
            "--opt", f"o=size=256k,uid={world.uid},gid={world.gid},mode=0700",
            "--label", f"com.docker.compose.project={world.project}", "--label", "com.docker.compose.volume=memory",
            name)
        assert created.returncode == 0, explain(created)
        store = build_store(world.root, crashed=False)
        volume_before = _inventory(project, world)
        assert volume_before is not None and not volume_before["entries"], volume_before
        host_before = store.snapshot()
        proc = prepare(world)
        _unchanged(world, project, store, volume_before, host_before, "copy_or_check_failed", proc)
    finally:
        remaining = project.down()
    _finish(remaining, world)


# ------------------------------------------------------------------- D3

LAUNCH = r'''
import json, subprocess, sys
config = ["--config", "/config/launch/registry.json"]
def run(argv, payload=None, ok=True):
    proc = subprocess.run(argv, input=None if payload is None else json.dumps(payload), capture_output=True, text=True)
    if ok and proc.returncode:
        raise SystemExit("%s failed: %s %s" % (argv, proc.stdout, proc.stderr))
    return proc
registry = ["kp-agent-desk-registry", *config]
run([*registry, "initialize"])
roles = json.loads(run([*registry, "roles"]).stdout)
role = (roles["roles"] if isinstance(roles, dict) else roles)[0]["role_id"]
desk = "desk:" + sys.argv[2]
run([*registry, "save"], {"desk_id": desk, "name": "T9b launch desk", "description": "Before the migration.",
                          "role": role, "repos": ["fixture"], "capture": True, "memory_write": True,
                          "expected_version": 0})
prepared = json.loads(run(["kp-agent-launch", *config, "prepare"], {
    "harness": "codex", "provider": "openai", "model": "t9b", "desk_id": desk, "workspace": sys.argv[1],
    "task_id": "t9b-task", "source": "host", "parent_session_id": None}).stdout)
hook = run(["kp-agent-launch", "--receipt", prepared["receipt_path"], "hook"], json.loads(sys.argv[3]), ok=False)
print(json.dumps({"receipt_path": prepared["receipt_path"], "hook": hook.returncode, "stderr": hook.stderr[-2000:]}))
'''


def _hook(project: Project, container: str, receipt: str, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run([project.docker, "exec", "-i", container, "kp-agent-launch", "--receipt", receipt, "hook"],
                          input=json.dumps(payload), capture_output=True, text=True, env=project.env, timeout=120)


def _old_store_area(world) -> dict:
    """The regular files where a pre-migration launch's paths point on the host: state/registry, its renamed
    copy, and the host state/memory (an empty mount point Docker creates there is not a write)."""
    state = world.root / "state"
    return {p.name: {k: v for k, v in host_tree(p).items() if v[0] != "dir"}
            for p in sorted(state.iterdir()) if p.name.startswith(("registry", "memory"))
            and any(v[0] != "dir" for v in host_tree(p).values())}


def test_a_pre_migration_launch_hook_refuses_and_writes_nothing_outside_the_volume():
    """GREEN-IF a Codex launch prepared on the pre-T9b registry (its first hook verified then), after the
    migration: (a) before the operator edits, its hook does not succeed in the capture role; (b) after the
    documented edits and a recreated capture, its hook by the old and by the migrated receipt path is
    refused; and neither writes on the host bind."""
    world, docker = image_world("p3-launch", ("tooling", "capture"))
    project = Project(docker, world)
    try:
        apply_only(world)
        (world.root / "state" / "registry").mkdir(mode=0o700)
        _registry_files(world, old=True)
        native = str(uuid.uuid4())
        rollout = (world.home / ".codex" / "sessions" / "2026" / "10" / "02" /
                   f"rollout-2026-10-02T00-00-00-{native}.jsonl")
        payload = {"hook_event_name": "UserPromptSubmit", "cwd": str(world.repo), "session_id": native,
                   "transcript_path": str(rollout), "prompt": "t9b"}
        launched = project.docker_run(
            "run", "--rm", "--name", f"t9b-test-launch-{uuid.uuid4().hex[:8]}", "--network", "none",
            "--user", f"{world.uid}:{world.gid}", "-e", "HOME=/state", "-e", "TMPDIR=/state/tmp",
            "--mount", f"type=bind,source={world.root / 'state'},target=/state",
            "--mount", f"type=bind,source={world.root / 'config'},target=/config,readonly",
            "--mount", f"type=bind,source={world.repo},target={world.repo},readonly",
            "--entrypoint", "python3", world.image, "-c", LAUNCH, str(world.repo), str(uuid.uuid4()),
            json.dumps(payload), timeout=300)
        assert launched.returncode == 0, "could not prepare the pre-T9b launch\n" + explain(launched)
        made = json.loads(launched.stdout.strip().splitlines()[-1])
        assert made["hook"] == 0, f"precondition: the launch's first hook verified before the migration: {made}"
        receipt = made["receipt_path"]
        assert receipt.startswith("/state/registry/launches/"), receipt

        done = prepare(world)
        assert prepared(done) is None, f"prepare did not migrate state/registry: {prepared(done)}\n" + explain(done)
        migrated_receipt = STORE_TARGET + "/registry" + receipt[len("/state/registry"):]
        container = f"{world.project}-capture"

        before = _old_store_area(world)
        up = project.compose("up", "-d", "capture")
        assert up.returncode == 0, explain(up)
        unedited = _hook(project, container, receipt, payload)
        assert unedited.returncode != 0, ("before the operator edits, the pre-migration launch's hook succeeded "
                                          "against its old path\n" + explain(unedited))
        assert _old_store_area(world) == before, "the hook wrote on the host bind before the operator edits"

        _registry_files(world, old=False)
        up = project.compose("up", "-d", "--force-recreate", "capture")
        assert up.returncode == 0, explain(up)
        project.wait_running(["capture"])
        before = _old_store_area(world)
        for path in (receipt, migrated_receipt):
            hooked = _hook(project, container, path, payload)
            assert hooked.returncode != 0, (f"after the operator edits, the pre-migration launch's hook by {path} "
                                            "succeeded\n" + explain(hooked))
        after = _old_store_area(world)
        assert after == before, "a pre-migration hook wrote on the host bind:\n" + "\n".join(
            f"{n}: {line}" for n in sorted(set(before) | set(after))
            for line in tree_diff(before.get(n, {}), after.get(n, {})))
    finally:
        remaining = project.down()
    _finish(remaining, world)
