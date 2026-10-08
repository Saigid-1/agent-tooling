"""T12a A2: services restart, and store-preflight refusals wait (image-marked).

Order: docs/work/orders/T12a-capture-survives.md, A2, frozen at its merge commit:
- `restart: unless-stopped` for tooling, refresh, capture and board; `stop` still stops them.
- Store-preflight refusals no longer exit 3: the role stays up and logs the refusal as JSON, reports health
  `refused`, re-checks on an interval, and never execs the role command until the refusal clears.
- Recovery from an unprepared store: `docker compose stop` -> `prepare` -> `up`. T9b P4's `writers_running`
  is unchanged: a waiting role is a running container of the project, so `prepare` refuses while it waits.
- "Clears within one re-check interval" applies only to `operator_file_names_outside_store`, which the
  operator clears by editing the file, without `prepare` and without a stop.
Falsifiers: a role started on an unprepared store exits, restart-loops, or runs its command before
`prepare`; after the operator edits a file named by `operator_file_names_outside_store` the waiting role
does not start its command within one re-check interval; a killed capture container is not running again
within 30 s (RestartCount >= 1); `docker compose stop` leaves anything restarting.

Cases (the arm's dispatch letters):
  (a) a role on an unprepared store waits, refused, without exiting, restarting or running its command;
  (b) recovery: stop -> prepare -> up runs every role's command;
  (c) an operator edit clears `operator_file_names_outside_store` within one re-check interval;
  (d) a capture SIGKILLed from inside is running again within 30 s with RestartCount >= 1, and
      `docker kill --signal KILL` leaves it stopped;
  (d-ii) a real OOM of a role's main process (tooling, under a mem_limit override) restarts it within 30 s
      (Verification's falsifier of record), and the death is attributed to OOM from the daemon's report or
      the kernel's record (B57); its attribution falsifiers F1-F3 run in their own worlds (B57 P4);
  (e) `docker compose stop` leaves nothing restarting (a guard: GREEN at base);
  (f) T9b P4: `prepare` still refuses `writers_running` while a role waits.

Every refusal assertion goes through t9b_harness.assert_role_refused (Coordinator rule C2), whose readings
are in t9b_harness. Further readings (repeated in the arm report under AMBIGUITY):
- "one re-check interval" is 10 s, T7a's not_configured poll, since the order makes the refusal wait "like
  T7a's not_configured" and names no other interval; the command must run within 10 s + 5 s (exec, probe).
- (d) The order's falsifier names `docker kill --signal KILL`. Measured on this arm's Docker (29.5.3), and
  ruled at the meet (Coordinator): `docker kill` is a deliberate stop to the daemon, and no restart policy
  (`always`, `unless-stopped`, `on-failure`) restarts it. Per the ruling, (d) measures what an OOM kill
  does, a SIGKILL of the container's main process from inside it, and asserts that `docker kill` leaves
  the container stopped. The main process is the role's process, the child of docker-init (PID 1): a
  SIGKILL of PID 1 sent from inside its own PID namespace is ignored by the kernel (measured: still
  running, RestartCount 0), so the role's PID is killed, with `docker exec` as the role's own user.
- (d-ii) The OOM is real: a Compose override (on the rendered root's own files) sets tooling's mem_limit and
  memswap_limit to 96m and its command to a Python program that, as the main process, records its PID,
  waits for a trigger file, then allocates and touches memory without bound. "Which process died": the
  recorded PID is the child of docker-init, and after the restart a new main process logs that it
  restarted. The death is read from `docker events` (`die` with exitCode 137), because the restart replaces
  the container's State before a poll can read it.
- (d-ii) attribution, order B57 (docs/work/orders/B57-oom-attribution.md). The death is attributed to OOM
  by either of two sources:
  1. the Docker daemon's report: an `oom` event or the `die` event's `oomKilled` attribute. State.OOMKilled
     is the daemon's own flag, set from the same OOM event, so it is never a second source;
  2. the kernel's own OOM record, the first of the order's ranked candidates, and the one chosen: the
     one-line summary the kernel prints for every OOM kill (`oom-kill:...,task_memcg=<cgroup>,task=...,
     pid=<host pid>,...`), whose `task_memcg=` path names THIS container's cgroup by its full id
     (`docker-<id>.scope` under systemd's cgroup driver, `<id>` under cgroupfs), and which was not in a
     reading taken before the trigger. It is read from this host's kernel log by the first reader that
     answers, `journalctl -k --since <trigger time>` then `sudo -n dmesg` (t12a_harness, B57 section).
     Independence: the kernel's OOM killer writes it when it kills, and it is read without the daemon,
     its events or its State, so a missed daemon report does not remove it. A bare count is not used.
     The other candidates are not used: the container cgroup's own `memory.events` races the restart that
     re-creates the cgroup, and a parent cgroup's `oom_kill` moves for any descendant.
  Skip rule: this host's kernel log is the containers' only when the daemon runs on this host's kernel
  (Linux, the same kernel release). Elsewhere, for example Docker Desktop's VM read from the Mac, and when
  no reader answers, the SOURCE is skipped with a printed reason and the daemon's report is the only
  source, so a missed report fails the test there as before B57. The skip is of the source, never of the
  test. The test prints `t12a (d-ii): OOM attributed by <daemon|kernel log> (<the event or the kernel
  line>)` and the second source's reading, past pytest's capture, so a passing run's log carries both.
  `test_a2d_ii_attribution_falsifiers` runs the same world with a test-only shape: F1 (a real OOM, the
  daemon's report dropped) is attributed by the kernel log, or skipped where that source is; F2 (a
  SIGKILL below the limit, the report dropped) and F3 (a real OOM, the report dropped, the kernel log
  blinded) are not attributed.
- (e) "leaves anything restarting": for 15 s after `docker compose stop` returns, no container of the
  project is running or restarting and none has started again; measured once with every role running and
  once with every role refused (an operator file under /config naming a store path outside the volume is
  read by every role's preflight), so that under a restart policy the stop meets waiting or
  restart-looping roles.
"""
from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass

import pytest

from s3_harness import explain
from t7a_harness import write_private
from t9b_harness import (STATE_ROLES, STORE_TARGET, apply_only, assert_role_refused, health_report, install,
                         prepare, prepare_refused, prepared)
from t12a_harness import (COMMAND_TIMEOUT, EXEC_SLACK, KERNEL_LOG_WAIT, RECHECK_SECONDS, RESTART_WINDOW, Project,
                          Reading, compose_override, container_id, daemon_oom_report, events, image_world,
                          kernel_log, kernel_oom_lines, main_process, remove_role, wait_command, watch_stays_up)

pytestmark = pytest.mark.image

WAIT_WINDOW = 20.0
STOP_WINDOW = 15.0
CAPTURE_FILE = "config/capture/session.json"


def _capture_operator_file(world, state_root: str) -> None:
    path = world.root / CAPTURE_FILE
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(path, json.dumps({
        "schema_version": "ops.desk-memory.local.v1", "state_root": state_root,
        "catalog_path": "/config/launch/desks.json", "workspace_root": "/config/launch",
        "provider_instance": "t12a", "provider_session_id": "t12a-preflight"}) + "\n")


def _start(project: Project, role: str) -> None:
    remove_role(project, role)
    up = project.compose("up", "-d", "--no-deps", role)
    assert up.returncode == 0 and project.container(role), f"`up` did not create {role}\n" + explain(up)


def _down(project: Project, world) -> None:
    remaining = project.down()
    assert not remaining, f"left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)  # only once the project is down with nothing left


@pytest.fixture(scope="module")
def unprepared():
    """An applied root that was never prepared; `up` creates its volume afresh (root-owned)."""
    world, docker = image_world("a2-unprepared", STATE_ROLES)
    project = Project(docker, world)
    try:
        apply_only(world)
        yield world, project
    finally:
        _down(project, world)


@pytest.mark.parametrize("role", STATE_ROLES)
def test_a2a_a_role_on_an_unprepared_store_waits_refused(unprepared, role):
    """GREEN-IF the role, started alone on a root that was never prepared, reports the refusal
    `store_unprepared` (t9b_harness.assert_role_refused: running, not restarting, health `refused`, a JSON
    log line naming the reason, its command not exec'd), then keeps running for 20 s with RestartCount 0,
    the same StartedAt and its command never running, and is still refused at the end."""
    world, project = unprepared
    _start(project, role)
    try:
        assert_role_refused(project, role, "store_unprepared")
        watch_stays_up(project, role, WAIT_WINDOW, refused=True)
        assert_role_refused(project, role, "store_unprepared")
    finally:
        remove_role(project, role)


def test_a2f_prepare_refuses_writers_running_while_a_role_waits(unprepared):
    """GREEN-IF, with capture waiting refused on the never-prepared root, `prepare` refuses
    `writers_running` naming capture, and capture is afterwards still waiting refused (`store_unprepared`)."""
    world, project = unprepared
    _start(project, "capture")
    try:
        assert_role_refused(project, "capture", "store_unprepared")
        done = prepare(world)
        why = prepare_refused(done, "writers_running")
        assert why is None, f"{why}\n" + explain(done)
        assert re.search(r"\bcapture\b", done.stdout + done.stderr), "the refusal does not name capture\n" + explain(done)
        assert_role_refused(project, "capture", "store_unprepared")
    finally:
        remove_role(project, "capture")


def test_a2b_stop_prepare_up_runs_every_role_command():
    """GREEN-IF, on a never-prepared root, `up -d` leaves every role waiting refused (`store_unprepared`);
    then `docker compose stop`, `prepare` (reports `prepared`) and `up -d` run every role's command within
    90 s, and no role's health says `refused`."""
    world, docker = image_world("a2-recover", STATE_ROLES)
    project = Project(docker, world)
    try:
        apply_only(world)
        up = project.compose("up", "-d")
        assert up.returncode == 0, explain(up)
        for role in STATE_ROLES:
            assert_role_refused(project, role, "store_unprepared")
        stopped = project.compose("stop")
        assert stopped.returncode == 0, explain(stopped)
        done = prepare(world)
        assert prepared(done) is None, f"{prepared(done)}\n" + explain(done)
        up = project.compose("up", "-d")
        assert up.returncode == 0, explain(up)
        for role in STATE_ROLES:
            wait_command(project, role, COMMAND_TIMEOUT)
            health = health_report(project, project.container(role))
            assert (health or {}).get("status") != "refused", f"{role} still reports refused: {health}"
    finally:
        _down(project, world)


def test_a2c_an_operator_edit_clears_the_refusal_within_one_recheck_interval():
    """GREEN-IF capture, on a prepared root whose config/capture/session.json names state_root /state/registry,
    waits refused (`operator_file_names_outside_store`); after the operator edits that file to a path under
    /state/memory (no prepare, no stop), capture's command runs within 10 s + 5 s and its health is no
    longer `refused`."""
    world, docker = image_world("a2-edit", ("tooling", "capture"))
    project = Project(docker, world)
    try:
        install(world)
        _capture_operator_file(world, "/state/registry")
        _start(project, "capture")
        assert_role_refused(project, "capture", "operator_file_names_outside_store")
        _capture_operator_file(world, f"{STORE_TARGET}/t12a-capture")
        elapsed = wait_command(project, "capture", RECHECK_SECONDS + EXEC_SLACK)
        health = health_report(project, project.container("capture"))
        assert (health or {}).get("status") != "refused", f"capture still reports refused after {elapsed:.1f}s: {health}"
        state = project.state(project.container("capture"))
        assert state.get("Running") and not state.get("RestartCount"), (
            f"capture cleared the refusal by restarting, not by re-checking: {json.dumps(state)}")
    finally:
        _down(project, world)


def test_a2d_a_sigkilled_capture_is_running_again_within_30s_and_docker_kill_stops_it():
    """GREEN-IF capture, running its command on a prepared root: (1) after a SIGKILL of its main process
    from inside the container (what an OOM kill does) it is running again within 30 s with RestartCount >= 1
    and runs its command again; (2) after `docker kill --signal KILL` it is exited (137) and stays exited,
    not restarting, for 15 s (Docker's deliberate stop; the Coordinator's meet ruling)."""
    world, docker = image_world("a2-kill", ("tooling", "capture"))
    project = Project(docker, world)
    try:
        install(world)
        _start(project, "capture")
        wait_command(project, "capture", COMMAND_TIMEOUT)
        cid = project.container("capture")
        before = project.state(cid)
        main = main_process(project, cid)
        pid = main["pid"]
        print(f"t12a (d): SIGKILL of capture's main process, PID {pid} (child of docker-init): {main['argv']}")
        # The exec dies with the container; its own exit status is not an observation. PID 1 (docker-init)
        # cannot be SIGKILLed from inside its own PID namespace, so the role's process (its child) is.
        project.docker_run("exec", cid, "python3", "-c", f"import os, signal; os.kill({pid}, signal.SIGKILL)")
        deadline = time.monotonic() + RESTART_WINDOW
        while True:
            state = project.state(cid)
            back = state.get("Running") and state["RestartCount"] >= 1 and state.get("StartedAt") != before.get("StartedAt")
            if back:
                break
            if time.monotonic() > deadline:
                pytest.fail(f"capture is not running again within {RESTART_WINDOW}s of a SIGKILL of its main "
                            f"process (PID {pid}, {main['argv'][:3]}): {json.dumps(state)}\nlog:\n{project.logs(cid)[-2000:]}", pytrace=False)
            time.sleep(1)
        wait_command(project, "capture", COMMAND_TIMEOUT)

        restarts = project.state(cid)["RestartCount"]
        killed = project.docker_run("kill", "--signal", "KILL", cid)
        assert killed.returncode == 0, explain(killed)
        deadline = time.monotonic() + STOP_WINDOW
        while True:
            state = project.state(cid)
            if state.get("Running") or state.get("Restarting") or state["RestartCount"] != restarts:
                pytest.fail(f"`docker kill --signal KILL` did not leave capture stopped: {json.dumps(state)}",
                            pytrace=False)
            if time.monotonic() >= deadline:
                break
            time.sleep(1)
        assert state.get("ExitCode") == 137, f"capture exited {state.get('ExitCode')} on docker kill, not 137"
    finally:
        _down(project, world)


OOM_SCRIPT = r'''
import os, sys, time
marker, go = sys.argv[1], sys.argv[2]
if os.path.exists(marker):
    print("t12a-oom: restarted, main process", os.getpid(), flush=True)
    while True:
        time.sleep(3600)
with open(marker, "w") as out:
    out.write(str(os.getpid()))
print("t12a-oom: main process", os.getpid(), "waits for the trigger", flush=True)
while not os.path.exists(go):
    time.sleep(0.2)
print("t12a-oom: allocating in the main process", os.getpid(), flush=True)
blocks = []
while True:
    block = bytearray(8 << 20)
    block[::4096] = b"\x01" * len(range(0, 8 << 20, 4096))
    blocks.append(block)
'''
OOM_LIMIT = "96m"


# B57 P4: the falsifier shapes of the attribution step. A test-only parameter of _oom_death, which both
# (d-ii) tests call; it never reaches the product or the workflows. Each shape is its own OOM world.
#   F1  a real OOM, the daemon's report dropped: attributed by the second source (skipped where that source is);
#   F2  no OOM: the trigger is never written, the main process is SIGKILLed with `docker exec` (`kill -KILL
#       <pid>`) and dies 137 below its limit; the daemon's report dropped as well: NOT attributed;
#   F3  a real OOM, the daemon's report dropped and the second source blinded ("no OOM"): NOT attributed.
SHAPES = ("F1", "F2", "F3")
# The one-line OOM-kill summary the kernel prints (t12a_harness, B57 section), for the matcher's own check.
KERNEL_RECORD = ("oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=docker-{id}.scope,mems_allowed=0,"
                 "oom_memcg=/system.slice/docker-{id}.scope,task_memcg=/system.slice/docker-{id}.scope,"
                 "task=python3,pid=4242,uid=1001")


@dataclass
class OomDeath:
    """One (d-ii) world's death and its attribution: `by` is `daemon`, `kernel log` or None."""
    container: str
    events: list[dict]
    second: Reading
    by: str | None
    evidence: str


def _say(capsys, line: str) -> None:
    """Print `line` past pytest's capture, so a passing run's log carries it (CI runs `-q -rfE`)."""
    with capsys.disabled():
        print(f"\n{line}", flush=True)


def _second_source(reading: Reading) -> str:
    if reading.skipped is not None:
        return f"{reading.source}: skipped ({reading.skipped}); the daemon's report is the only source"
    if not reading.lines:
        return f"{reading.source}: no OOM-kill record for this container in the window"
    return f"{reading.source}: {len(reading.lines)} OOM-kill record(s) for this container: {reading.lines[0]}"


def _probes(kernel) -> str:
    """Each kernel-log reader's probe answer (none where the source is skipped before any reader)."""
    return f" [daemon on {kernel.daemon}; readers: {'; '.join(kernel.tried)}]" if kernel.tried else ""


def _without_daemon_report(seen: list[dict]) -> list[dict]:
    """`seen` with the daemon's OOM report dropped: no `oom` action and no `oomKilled` attribute."""
    kept = []
    for event in seen:
        if event.get("Action") == "oom":
            continue
        actor = event.get("Actor") or {}
        attributes = {k: v for k, v in (actor.get("Attributes") or {}).items() if k != "oomKilled"}
        kept.append({**event, "Actor": {**actor, "Attributes": attributes}})
    return kept


def _attribute(seen: list[dict], second: Reading) -> tuple[str | None, str]:
    """The attribution step: the daemon's report, else the second source's OOM-kill record of this
    container. A skipped second source attributes nothing."""
    report = daemon_oom_report(seen)
    if report:
        event = report[0]
        attributes = (event.get("Actor") or {}).get("Attributes", {})
        fields = "".join(f", {key} {attributes[key]}" for key in ("exitCode", "oomKilled") if key in attributes)
        return "daemon", f"`{event.get('Action')}` event at {event.get('time')}{fields}"
    if second.skipped is None and second.lines:
        return "kernel log", second.lines[0]
    return None, ""


def _oom_death(shape: str | None, capsys) -> OomDeath:
    """The (d-ii) world: tooling under the packaged compose plus a 96m override whose main process allocates
    once triggered (F2: is SIGKILLed instead). Asserts the precondition, exit 137 and the restart (P2), then
    attributes the death (P1) with `shape` applied (None: the real test)."""
    name = "t12a (d-ii)" if shape is None else f"t12a (d-ii) {shape}"
    world, docker = image_world("a2-oom" if shape is None else f"a2-oom-{shape.lower()}", ("tooling",))
    project = Project(docker, world)
    try:
        kernel = kernel_log(project)
        if shape == "F1" and kernel.skipped:
            _say(capsys, f"{name}: skipped, the second source is skipped on this host: {kernel.skipped}"
                         f"{_probes(kernel)}")
            pytest.skip(f"F1 needs the second source, which is skipped on this host: {kernel.skipped}")
        install(world)
        tag = uuid.uuid4().hex[:8]
        marker, go = f"/state/tmp/t12a-oom-{tag}.pid", f"/state/tmp/t12a-oom-{tag}.go"
        host_marker, host_go = world.root / marker.lstrip("/"), world.root / go.lstrip("/")
        override = {"services": {"tooling": {"mem_limit": OOM_LIMIT, "memswap_limit": OOM_LIMIT,
                                             "command": ["python3", "-c", OOM_SCRIPT, marker, go]}}}
        up = compose_override(project, override, "up", "-d", "--no-deps", "tooling")
        assert up.returncode == 0, explain(up)
        cid = project.container("tooling")
        deadline = time.monotonic() + COMMAND_TIMEOUT
        while not host_marker.exists():
            assert time.monotonic() < deadline, (f"the allocating command never started: "
                                                 f"{json.dumps(project.state(cid))}\n{project.logs(cid)[-2000:]}")
            time.sleep(0.5)
        main = main_process(project, cid)
        assert str(main["pid"]) == host_marker.read_text().strip() and main["argv"][:2] == ["python3", "-c"], (
            f"precondition: the allocating process is not the main process: main={main} "
            f"marker={host_marker.read_text()!r}")
        container = container_id(project, cid)
        death, cause = ("SIGKILL", "SIGKILL") if shape == "F2" else ("OOM", "allocation")
        print(f"{name}: {death} in tooling's main process, PID {main['pid']} (child of docker-init): "
              f"{main['argv'][:2]}; container {container[:12]}")
        since = time.time() - 1
        before = kernel.reading(container, since)
        if shape == "F2":
            # Below the limit: the trigger is never written. The exec dies with the container; its own exit
            # status is not an observation.
            project.docker_run("exec", cid, "sh", "-c", f"kill -KILL {main['pid']}")
        else:
            host_go.write_text("go\n")
        deadline = time.monotonic() + 60
        while True:
            seen = events(project, cid, since)
            died = [e for e in seen if e.get("Action") == "die"]
            if died:
                break
            assert time.monotonic() < deadline, f"tooling did not die of its {cause}: {json.dumps(project.state(cid))}"
            time.sleep(1)
        died_at = time.monotonic()
        code = (died[0].get("Actor") or {}).get("Attributes", {}).get("exitCode")
        assert code == "137", f"the main process's death is not exit 137: {died[0]}"
        while True:
            state = project.state(cid)
            if state.get("Running") and state["RestartCount"] >= 1 and "t12a-oom: restarted" in project.logs(cid):
                break
            if time.monotonic() - died_at > RESTART_WINDOW:
                pytest.fail(f"tooling is not running again within {RESTART_WINDOW}s of its {death} death: "
                            f"{json.dumps(state)}\nlog:\n{project.logs(cid)[-2000:]}", pytrace=False)
            time.sleep(1)
        seen = events(project, cid, since)
        second = kernel.reading(container, since, before=before, wait=KERNEL_LOG_WAIT)
        _say(capsys, f"{name}: second source {_second_source(second)}{_probes(kernel)}")
        if shape is not None:
            seen = _without_daemon_report(seen)
        if shape == "F3":
            second = Reading(second.source, lines=[])
            _say(capsys, f"{name}: second source blinded: its reading is forced to no OOM-kill record")
        by, evidence = _attribute(seen, second)
        return OomDeath(container, seen, second, by, evidence)
    finally:
        _down(project, world)


def test_a2d_ii_a_real_oom_of_the_main_process_restarts_the_role(capsys):
    """GREEN-IF tooling, on a prepared root, under the packaged compose plus an override that sets
    mem_limit/memswap_limit 96m and makes its command allocate in its MAIN process (the child of docker-init,
    whose PID it records and the test confirms) past the limit: the container dies with exit 137 (a `die`
    event with exitCode 137) and is running again within 30 s of that death with RestartCount >= 1, its new
    main process logging that it restarted (Verification's falsifier of record); and the death is attributed
    to OOM (B57 P1) by the daemon's report (an `oom` event or the `die` event's `oomKilled`) OR by the
    kernel's OOM-kill record of this container (skipped, with its reason, where this host cannot read the
    containers' kernel). It prints `t12a (d-ii): OOM attributed by <daemon|kernel log> (<evidence>)`."""
    death = _oom_death(None, capsys)
    assert death.by, (f"no OOM was reported for tooling's death: the daemon's report has none (events: "
                      f"{death.events}); second source {_second_source(death.second)}")
    _say(capsys, f"t12a (d-ii): OOM attributed by {death.by} ({death.evidence})")


@pytest.mark.parametrize("shape", SHAPES)
def test_a2d_ii_attribution_falsifiers(shape, capsys):
    """GREEN-IF, in the (d-ii) world with the daemon's OOM report dropped (no `oom` action, no `oomKilled`;
    B57 P4): F1, a real OOM, is attributed by the kernel log (skipped, never passed, where the second source
    is skipped); F2, a SIGKILL of the main process below its limit (the trigger never written), is NOT
    attributed, the kernel log answering "no OOM-kill record for this container" where it is read, and the
    matcher refusing another container's record while it accepts this container's; F3, a real OOM with the
    second source blinded, is NOT attributed."""
    death = _oom_death(shape, capsys)
    _say(capsys, f"t12a (d-ii) {shape}: attributed by {death.by or 'nothing'}"
                 + (f" ({death.evidence})" if death.by else ""))
    if shape == "F1":
        assert death.by == "kernel log", (f"F1: a real OOM with the daemon's report dropped is attributed by "
                                          f"{death.by!r}, not by the second source: {death.evidence}; "
                                          f"second source {_second_source(death.second)}")
        return
    assert death.by is None, f"{shape}: the death is attributed by {death.by} ({death.evidence}); expected none"
    if shape == "F2":
        foreign = uuid.uuid4().hex + uuid.uuid4().hex
        assert not kernel_oom_lines(KERNEL_RECORD.format(id=foreign), death.container), (
            "F2: the matcher attributes another container's OOM-kill record to this container")
        assert kernel_oom_lines(KERNEL_RECORD.format(id=death.container), death.container), (
            "F2: the matcher does not recognise this container's OOM-kill record, so its 'no' is no answer")
        if death.second.skipped is None:
            assert not death.second.lines, (f"F2: the second source records an OOM kill for a container that "
                                             f"was SIGKILLed below its limit: {death.second.lines}")


def _assert_stop_leaves_nothing_restarting(project: Project, phase: str) -> None:
    stopped = project.compose("stop")
    assert stopped.returncode == 0, explain(stopped)
    cids = project.all_containers()
    assert cids, "precondition: the project has containers"
    first = {cid: project.state(cid) for cid in cids}
    deadline = time.monotonic() + STOP_WINDOW
    while True:
        for cid in cids:
            state = project.state(cid)
            if state.get("Running") or state.get("Restarting") or \
                    state.get("StartedAt") != first[cid].get("StartedAt") or \
                    state["RestartCount"] != first[cid]["RestartCount"]:
                pytest.fail(f"{phase}: after `docker compose stop` a container is running or restarting: {cid} "
                            f"{json.dumps(state)}\nlog:\n{project.logs(cid)[-1500:]}", pytrace=False)
        if time.monotonic() >= deadline:
            return
        time.sleep(2)


def test_a2e_compose_stop_leaves_nothing_restarting():
    """GREEN-IF `docker compose stop` exits 0 and for 15 s afterwards no container of the project is running
    or restarting and none starts again, both (1) with every role running its command on a prepared root and
    (2) with every role refused: an operator file under /config (config/capture/session.json) naming a store
    path outside the volume, which every role's preflight reads, and 10 s for the roles to reach the refusal
    (waiting, restart-looping or exited)."""
    world, docker = image_world("a2-stop", STATE_ROLES)
    project = Project(docker, world)
    try:
        install(world)
        up = project.compose("up", "-d")
        assert up.returncode == 0, explain(up)
        project.wait_running(STATE_ROLES)
        _assert_stop_leaves_nothing_restarting(project, "running roles")
        _capture_operator_file(world, "/state/registry")
        up = project.compose("up", "-d")
        assert up.returncode == 0, explain(up)
        time.sleep(10)
        _assert_stop_leaves_nothing_restarting(project, "refused roles")
    finally:
        _down(project, world)
