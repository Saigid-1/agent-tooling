"""D0f F4: the runtime constraints on the install action (image-marked).

Order: docs/work/orders/D0f-board-without-agent-sdk.md, F4 (Verification), frozen at D0f's base:
- every role image is read-only with capabilities dropped, so the SDK installs under /state, the one
  writable mount, and the board resolves it from there;
- the board role needs outbound network for the action; `capture` has none and never runs it;
- the action does not restart the board: T7a's stays-up contract, no RestartCount change;
- the installed SDK survives a board restart, because it lives on /state.
Falsifiers: an install writing outside /state; the board restarting because of the action; `capture`
attempting the install, or needing network; the install not surviving a board restart.

One rendered root runs the scenario once, in this order, and each test asserts its own part of the record:
1. capture alone, on the fresh /state (network_mode none, as rendered), watched for 20 s;
2. the board and the stand-in registry (tests/install/d0f_harness.py), then the acknowledged action
   (status -> install naming the shown notice's sha256, the lockfile's pin), then 20 s of the board
   watched as T7a watches a role (RestartCount 0, the same StartedAt, running), with capture still up;
3. `docker compose restart board`, then the status and the registry's log again.
Every role keeps the manifest's own settings (read-only root, cap_drop ALL, its user, its mounts); the
overlay changes only the board's network and adds `npm_config_registry` (see d0f_harness).

Readings (repeated in the arm report under AMBIGUITY):
- "writing outside /state": a writable mount of the board container whose destination is not /state or
  under it, or a `docker diff` entry outside /state that appeared during the action (entries already there
  before it, such as Docker's mount points and init binary, are not the action's); with the manifest's
  read-only root, an install that writes elsewhere also fails, which the action's result shows;
- "the board restarting because of the action": RestartCount above 0, StartedAt changed, the container not
  running, or the board server process (`node /app/dist/cli.js`) replaced, during the action and for 20 s
  after it;
- "capture attempting the install": an npm process in the capture container, or a claude-agent-sdk
  package or npm cache appearing under /state, while capture runs on a /state the board has not installed
  into; "needing network": capture, rendered with network_mode none, not running its role or restarting;
- "survives a board restart": after `docker compose restart board`, the status reads installed at the
  same version, the same package is under /state, and the registry received nothing at or after the
  restart.
"""
from __future__ import annotations

import shutil
import subprocess
import time

import pytest

from s3_harness import explain
from d0f_harness import (Board, Project, diff_outside_state, install_ok, installed_sdks, installed_world,
                         npm_processes, tree, writable_mounts_outside_state)

pytestmark = pytest.mark.image

COMPONENTS = ("tooling", "capture", "board")
WINDOW = 20.0


def _state_dict(project: Project, cid: str) -> dict:
    state = project.state(cid)
    return {key: state.get(key) for key in ("Running", "Status", "Restarting", "RestartCount", "StartedAt")}


def _watch(project: Project, cid: str, window: float, extra=None) -> list[str]:
    """T7a's stays-up observation of one container for `window` seconds; problems found (empty: none)."""
    first = _state_dict(project, cid)
    problems = []
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        state = _state_dict(project, cid)
        if not state["Running"] or state["Status"] != "running":
            problems.append(f"not running: {state}")
        elif state["Restarting"] or state["RestartCount"]:
            problems.append(f"restarted: {state}")
        elif state["StartedAt"] != first["StartedAt"]:
            problems.append(f"StartedAt changed: {first['StartedAt']} -> {state['StartedAt']}")
        if extra:
            problems.extend(extra())
        if problems:
            return problems
        time.sleep(2)
    return problems


@pytest.fixture(scope="module")
def scenario():
    world, project = installed_world("f4", COMPONENTS)
    state_dir = world.root / "state"
    record: dict = {"errors": {}, "rendered": {}, "install_ok": False}
    try:
        for service in ("board", "capture"):
            record["rendered"][service] = project.rendered_service(service)

        # 1. capture alone on a fresh /state.
        up = project.compose("up", "-d", "--no-deps", "capture", timeout=600)
        record["capture_up"] = up.returncode
        capture = project.container("capture")
        record["capture_cid"] = capture
        before = tree(state_dir)

        def capture_attempts() -> list[str]:
            found = npm_processes(project, capture)
            record.setdefault("capture_npm_probe_failed", found is None)
            return [f"npm process in capture: {argv}" for argv in (found or [])]

        record["capture_watch"] = _watch(project, capture, WINDOW, capture_attempts) if capture else ["no container"]
        appeared = sorted(tree(state_dir) - before)
        record["capture_appeared"] = [p for p in appeared if "claude-agent-sdk" in p or "/.npm" in f"/{p}"]

        # 2. the board, the stand-in registry and the acknowledged action. The board is waited for by its
        # own HTTP answer (not Compose's health wait, whose retries a slow first start can exhaust).
        up = project.compose("up", "-d", "--no-deps", "d0f-registry", "board", timeout=600)
        assert up.returncode == 0, "`docker compose up d0f-registry board` failed\n" + explain(up)
        board = Board(project)
        cid = board.cid()
        record["board_cid"] = cid
        record["serving"] = board.serving(timeout=300)
        assert record["serving"] == 200, f"the board did not serve: GET / {record['serving']}\n{project.logs(cid)[-2000:]}"
        record["board_before"] = _state_dict(project, cid)
        record["pids_before"] = board.server_pids()
        try:
            record["diff_before_action"] = diff_outside_state(project, cid)
        except (AssertionError, subprocess.TimeoutExpired) as error:
            record["diff_before_action"] = None
            record["errors"]["diff_before_action"] = f"{type(error).__name__}: {error}"
        requests_before = len(project.requests())
        try:
            record["status_before"] = board.status()
        except AssertionError as error:
            record["errors"]["status_before"] = str(error)
        try:
            record["install"] = board.install_acknowledged()
        except AssertionError as error:
            record["errors"]["install"] = str(error)
            record["install"] = {}
        record["install_ok"] = install_ok(record["install"])
        record["requests_by_action"] = project.requests()[requests_before:]
        record["board_watch"] = _watch(project, cid, WINDOW)
        record["board_after"] = _state_dict(project, cid)
        try:
            record["pids_after"] = board.server_pids()
        except AssertionError as error:  # the board container is not there to probe (restarting)
            record["pids_after"] = []
            record["errors"]["pids_after"] = str(error)
        record["capture_after_install_npm"] = npm_processes(project, capture)
        record["capture_after_install"] = _state_dict(project, capture)
        record["sdk_after_action"] = installed_sdks(state_dir)
        record["mounts_outside_state"] = writable_mounts_outside_state(project, cid)
        # The container's own layer as the action left it, less what was there before it (Docker's mount
        # points and init binary): what the action wrote outside /state.
        try:
            before = set(record["diff_before_action"] or ())
            record["diff_outside_state"] = [line for line in diff_outside_state(project, cid) if line not in before]
        except (AssertionError, subprocess.TimeoutExpired) as error:
            record["diff_outside_state"] = None
            record["errors"]["diff_after_action"] = f"{type(error).__name__}: {error}"
        try:
            record["status_after_action"] = board.status()
        except AssertionError as error:
            record["errors"]["status_after_action"] = str(error)

        # 3. a board restart.
        requests_before_restart = len(project.requests())
        restarted = project.compose("restart", "board", timeout=600)
        record["restart_exit"] = restarted.returncode
        record["serving_after_restart"] = board.serving(timeout=300)
        try:
            record["status_after_restart"] = board.status()
        except AssertionError as error:
            record["errors"]["status_after_restart"] = str(error)
        record["sdk_after_restart"] = installed_sdks(state_dir)
        record["requests_at_restart"] = project.requests()[requests_before_restart:]
        record["board_logs"] = project.logs(cid)[-3000:]
    except (Exception, pytest.fail.Exception) as error:  # recorded once; every test then fails on it
        record["setup_error"] = f"{type(error).__name__}: {error}"
    try:
        yield record
    finally:
        remaining = project.down()
        shutil.rmtree(world.base, ignore_errors=True)
        assert not remaining, f"the scenario left containers behind: {remaining}"


def _setup(record: dict) -> None:
    assert "setup_error" not in record, f"the scenario did not complete: {record['setup_error']}"


def _action_ran(record: dict) -> None:
    _setup(record)
    assert record["install_ok"], (
        f"the acknowledged action did not install (so there is no install to constrain): "
        f"{record.get('install')} errors={record['errors']}\nboard log:\n{record.get('board_logs', '')[-1500:]}")


def test_f4a_the_action_installs_under_state_and_writes_nowhere_else(scenario):
    """GREEN-IF the board role is rendered read-only with every capability dropped, the acknowledged action
    succeeds, the SDK package is then under /state (the host bind), the board container has no writable
    mount outside /state, and `docker diff` gains no entry outside /state during the action."""
    _setup(scenario)
    board = scenario["rendered"]["board"]
    assert board.get("read_only") is True and "ALL" in (board.get("cap_drop") or []), (
        f"precondition: the board role is not rendered read-only with cap_drop ALL: "
        f"read_only={board.get('read_only')} cap_drop={board.get('cap_drop')}")
    _action_ran(scenario)
    assert scenario["sdk_after_action"], "the action reported success but no SDK package is under /state"
    assert scenario["mounts_outside_state"] == [], (
        f"the board can write outside /state through: {scenario['mounts_outside_state']}")
    assert scenario["diff_before_action"] is not None and scenario["diff_outside_state"] is not None, (
        f"instrument: `docker diff` did not answer: {scenario['errors']}")
    assert scenario["diff_outside_state"] == [], f"writes outside /state: {scenario['diff_outside_state']}"


def test_f4b_the_action_does_not_restart_the_board(scenario):
    """GREEN-IF the board role is rendered with a network (not network_mode none), the acknowledged action
    succeeds, and during it and for 20 s after it the board container keeps running with RestartCount 0
    and the same StartedAt, and the board server process is the same process."""
    _setup(scenario)
    assert scenario["rendered"]["board"].get("network_mode") != "none", (
        "the board role is rendered without a network; the action needs outbound network")
    _action_ran(scenario)
    assert scenario["board_watch"] == [], f"the board restarted because of the action: {scenario['board_watch']}"
    before, after = scenario["board_before"], scenario["board_after"]
    assert after["RestartCount"] == before["RestartCount"] == 0 and after["StartedAt"] == before["StartedAt"], (
        f"before {before}, after {after}")
    assert scenario["pids_before"] and scenario["pids_after"] == scenario["pids_before"], (
        f"the board server process changed: {scenario['pids_before']} -> {scenario['pids_after']}")


def test_f4c_the_installed_sdk_survives_a_board_restart(scenario):
    """GREEN-IF after the acknowledged action and `docker compose restart board`, the board serves, its
    status reads installed at the version it read after the action, the same SDK package is under /state,
    and the registry received no request at or after the restart."""
    _action_ran(scenario)
    after_action = scenario.get("status_after_action") or {}
    assert after_action.get("installed") is True, f"control: the status after the action: {after_action}"
    assert scenario["restart_exit"] == 0 and scenario["serving_after_restart"] == 200, (
        f"the board did not come back: restart exit {scenario['restart_exit']}, GET / "
        f"{scenario['serving_after_restart']}")
    restarted = scenario.get("status_after_restart") or {}
    assert restarted.get("installed") is True and restarted.get("version") == after_action.get("version"), (
        f"after the restart the status reads {restarted} (errors {scenario['errors']}); before it, {after_action}")
    assert scenario["sdk_after_restart"] == scenario["sdk_after_action"], (
        f"the SDK under /state changed across the restart: {scenario['sdk_after_action']} -> "
        f"{scenario['sdk_after_restart']}")
    assert scenario["requests_at_restart"] == [], f"fetched at the restart: {scenario['requests_at_restart']}"


def test_f4d_capture_never_runs_the_install_and_needs_no_network(scenario):
    """GREEN-IF capture is rendered with network_mode none, runs for 20 s on a fresh /state without
    restarting and without an npm process or a claude-agent-sdk package or npm cache appearing under /state,
    and after the board's install still runs no npm process and has not restarted. Control: the board's
    action, on the same /state, does install, so the instrument sees an install there."""
    _setup(scenario)
    assert scenario["rendered"]["capture"].get("network_mode") == "none", (
        f"capture is rendered with network_mode {scenario['rendered']['capture'].get('network_mode')!r}")
    assert scenario.get("capture_up") == 0 and scenario.get("capture_cid"), "capture did not start"
    assert not scenario.get("capture_npm_probe_failed"), "the process probe could not run in capture"
    assert scenario["capture_watch"] == [], f"capture on a fresh /state: {scenario['capture_watch']}"
    assert scenario["capture_appeared"] == [], f"appeared under /state while capture ran: {scenario['capture_appeared']}"
    _action_ran(scenario)
    assert scenario["sdk_after_action"], "control: the board's install is not visible under /state"
    assert scenario["capture_after_install_npm"] == [], (
        f"npm processes in capture after the install: {scenario['capture_after_install_npm']}")
    after = scenario["capture_after_install"]
    assert after["Running"] and not after["RestartCount"], f"capture after the install: {after}"
