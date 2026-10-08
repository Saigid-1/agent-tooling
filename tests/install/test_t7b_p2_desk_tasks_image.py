"""T7b P2 (image-marked): desk tasks bind and capture in the board container.

Order: docs/work/orders/T7b-board-wiring.md, P2:
- the board role sets KANBAN_LAUNCH_BINDING_COMMAND to
  `["/usr/local/bin/kp-agent-launch", "--config", "/config/launch/registry.json"]`;
- with the registry configured, a desk task started from the board in an `agents`-mode
  container binds with `source: "board"` (Claude at launch, Codex on its first hook);
- its Stop is captured under the desk, and `memory.search` from the bound session finds that turn;
- with the registry absent, a desk task does not start and the reason is shown. A task
  without a desk launches exactly as before.
Falsifier: a desk task launching unbound; a capture outside the desk; or a desk-less task changed.

Setup: the `agents` image (AGENT_TOOLING_TEST_IMAGE_AGENTS), components
tooling,capture,board, planned with `--board-agents` (P3: the board needs the repository to
create a task worktree) and DOCKER.md's first-run inputs (`--home`, both transcript roots).
The registry is configured as DOCKER.md's first run, step 3 writes it, before `up`, and
initialized in the tooling role. Fake `claude` and `codex` executables are put ahead on the
board's PATH by a test overlay (tests/install/t7b_harness.py); they never call a provider.
The desk is created, the project added and each task started through the board's HTTP
interface exactly as the web UI does it (`desks.save`, `projects.add`,
`workspace.ensureWorktree`, `runtime.startTaskSession` with the card's `deskId`).

Readings (repeated in the arm report under AMBIGUITY):
- "the board role sets KANBAN_LAUNCH_BINDING_COMMAND": the board server process runs with exactly
  that JSON argv once the registry is configured (whether the role's environment, the image default
  or the board's start sets it is the feature's choice);
- "binds with source board": the registry (`kp-agent-desk-registry ... list`, run in the
  tooling role) holds exactly one binding for the CLI's session, with `source: "board"`, the
  task's desk and the agent's harness id;
- "Claude at launch": the fake Claude receives `--session-id <id>` and that id is already bound
  before its first hook runs (the fake lists the registry from inside the board container
  before running any hook); "Codex on its first hook": the Codex session is not bound before
  its first hook and is bound after it;
- "captured under the desk ... memory.search from the bound session finds that turn": the
  memory MCP server the launch handed the CLI (`--mcp-config` for Claude, `-c
  mcp_servers.kp_desk_memory` for Codex), started in the board container as given, reports
  the desk's binding as its own and finds the turn's marker with a desk-scope `memory.search`;
  a session bound to a second desk does not find it ("a capture outside the desk");
- "the reason is shown": `runtime.startTaskSession` answers `ok: false` with a non-empty
  `error` (what the web UI shows), and the CLI never runs;
- "launches exactly as before": the CLI gets none of the launch binding's additions (no
  `--session-id`, no `--mcp-config`, no `KP_AGENT_LAUNCH_RECEIPT`, no hook that runs
  `kp-agent-launch`), and the registry gains no binding.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field

import pytest

from t7a_harness import write_private
from t12b_harness import bounded_wait, indexer_interval  # T12b B4: the bounded wait
from t7b_harness import (LAUNCH_BINDING_ARGV, LAUNCH_BINDING_VARIABLE, Board, BoardRuntime, agents_world,
                         bindings_for, desk_binding_key, install, json_argv, mcp_calls, memory_server,
                         own_binding_keys, probe_listing, registry_document, search_hits, tool_value, write_registry)

pytestmark = pytest.mark.image

COMPONENTS = ("tooling", "capture", "board")


@dataclass
class Deployment:
    error: str | None = None
    runtime: BoardRuntime | None = None
    board: Board | None = None
    workspace: str | None = None
    desk: dict = field(default_factory=dict)
    world: object = None

    def require(self) -> "Deployment":
        if self.error:
            pytest.fail(self.error, pytrace=False)
        return self


def _deploy(label: str, *, registry: bool) -> Deployment:
    deployment = Deployment()
    try:
        world, docker = agents_world(label, COMPONENTS)
        deployment.world = world
        install(world, board_agents=True)
        if registry:
            write_registry(world)
        deployment.runtime = BoardRuntime(docker, world, fakes=True)
        deployment.runtime.up()
        if registry:
            deployment.runtime.registry_cli(deployment.runtime.tooling(), "initialize")
        deployment.board = Board(deployment.runtime)
        deployment.workspace = deployment.board.add_project(world.repo)
        if registry:
            deployment.desk = deployment.board.save_desk("Product")
    except (AssertionError, pytest.fail.Exception) as error:
        deployment.error = f"{label} deployment failed: {error}"
    return deployment


def _teardown(deployment: Deployment) -> None:
    if deployment.runtime is not None:
        remaining = deployment.runtime.down()
        assert not remaining, f"the test left containers behind: {remaining}"
    if deployment.world is not None and not os.environ.get("T7B_KEEP_WORLDS"):
        shutil.rmtree(deployment.world.base, ignore_errors=True)


@pytest.fixture(scope="module")
def configured():
    deployment = _deploy("p2-registry", registry=True)
    try:
        yield deployment
    finally:
        _teardown(deployment)


@pytest.fixture(scope="module")
def unconfigured():
    deployment = _deploy("p2-no-registry", registry=False)
    try:
        yield deployment
    finally:
        _teardown(deployment)


def _task(prefix: str) -> tuple[str, str]:
    token = uuid.uuid4().hex[:10]
    return f"t7b-{prefix}-{token}", f"T7b {prefix} juniper {token}"


def _started(result: dict, what: str) -> None:
    assert isinstance(result, dict) and result.get("ok") is True, f"{what}: startTaskSession answered {result}"


LAUNCH_HOOK = re.compile(r"(kp-agent-launch|kp_agent_tooling\.launch_cli)\b.*--receipt")


def _launch_hook_runs(record: dict, event: str) -> list[dict]:
    """The runs of the launch's own hook (`kp-agent-launch --receipt <launch.json> hook`, LAUNCH-BINDING.md)."""
    return [run for run in record.get("runs") or [] if run["event"] == event and LAUNCH_HOOK.search(run["command"])]


def _search_from(deployment: Deployment, server: dict, marker: str):
    runtime = deployment.runtime
    status, bindings, search = mcp_calls(runtime, runtime.board(), server, [
        ("memory.connection_status", {}), ("memory.bindings", {}), ("memory.search", {"query": marker})])
    return tool_value(status), own_binding_keys(bindings), search_hits(search, marker)


def _second_desk_session(deployment: Deployment) -> str:
    """A session bound (by the operator, through the Desks menu) to a second desk, and its memory config."""
    board, world = deployment.board, deployment.world
    other = board.save_desk("Elsewhere")
    session = "t7b-other-" + uuid.uuid4().hex[:12]
    bound = board.data("mutation", "desks.bind", {
        "harness": "operator", "provider": "none", "model": "none", "native_session_id": session,
        "desk_id": other["desk_id"], "source": "operator", "workspace": str(world.repo), "parent_session_id": None})
    assert bound.get("status") == "bound", bound
    directory = world.root / "state" / "t7b"
    directory.mkdir(mode=0o700, exist_ok=True)
    write_private(directory / f"{session}.json", json.dumps(registry_document(provider_session_id=session)))
    return f"/state/t7b/{session}.json"


def _assert_desk_capture(deployment: Deployment, server: dict, session: str, marker: str, harness: str) -> None:
    since = time.monotonic()  # T12b B4: the bounded wait starts once the hook has run
    listing = deployment.runtime.registry_cli(deployment.runtime.tooling(), "list")
    rows = bindings_for(listing, session)
    desk_id = deployment.desk["desk_id"]
    assert len(rows) == 1, f"the {harness} session {session} is not bound exactly once: {listing.get('bindings')}"
    assert (rows[0].get("source"), rows[0].get("desk_id"), rows[0].get("harness")) == ("board", desk_id, harness), (
        f"the {harness} task's binding is not (board, its desk, {harness}): {rows[0]}")
    # T12b B4 (named exception): the indexer, not the hook, indexes; wait for the search to answer, bounded by
    # 2 x the indexer's interval, read from the rendered manifest's `indexer` command.
    interval = indexer_interval(deployment.runtime)
    _, (status, own, hits), attempts, waited = bounded_wait(
        lambda: (lambda found: (bool(found[2]), found))(_search_from(deployment, server, marker)), interval,
        since=since)
    assert isinstance(status, dict) and status.get("status") == "ready", (
        f"the bound session's memory server is not ready: {status}")
    assert own == [desk_binding_key(listing, desk_id)], (
        f"the bound session's own memory binding is {own}, not its desk's {desk_binding_key(listing, desk_id)}")
    assert hits, (f"desk-scope memory.search from the bound {harness} session does not find {marker!r} within 2 x "
                  f"the indexer interval ({2 * interval:.1f}s; {attempts} searches, {waited:.1f}s)")
    other_config = _second_desk_session(deployment)
    elsewhere = mcp_calls(deployment.runtime, deployment.runtime.tooling(),
                          {"command": "kp-agent-memory", "args": ["--config", other_config, "serve"]},
                          [("memory.search", {"query": marker})])
    leaked = search_hits(elsewhere[0], marker)
    assert not leaked, f"a session of another desk finds the {harness} desk task's turn: {leaked}"


# ------------------------------------------------------------------- the role


def test_board_role_sets_the_launch_binding_command(configured):
    """GREEN-IF, with the registry configured, the board server process runs with
    KANBAN_LAUNCH_BINDING_COMMAND = ["/usr/local/bin/kp-agent-launch","--config","/config/launch/registry.json"]."""
    deployment = configured.require()
    running = json_argv(deployment.runtime.board_process_env().get(LAUNCH_BINDING_VARIABLE))
    assert running == LAUNCH_BINDING_ARGV, f"the board process runs with {LAUNCH_BINDING_VARIABLE}={running!r}"


# ------------------------------------------------------------------ desk tasks


def test_claude_desk_task_binds_at_launch_and_its_stop_is_captured_under_the_desk(configured):
    """GREEN-IF a Claude desk task started from the board gets a minted --session-id that is bound (source board,
    its desk) before any hook, its Stop hook captures, and the launch's memory MCP finds the turn in desk scope,
    while another desk's session does not."""
    deployment = configured.require()
    task, marker = _task("claude")
    _started(deployment.board.start_task(deployment.workspace, task, f"Remember {marker}.", agent="claude",
                                         desk_id=deployment.desk["desk_id"]), "claude desk task")
    record = deployment.runtime.wait_record("claude", marker)
    assert record.get("phase") == "done", f"the fake claude failed: {record.get('errors')}"
    session_ids = [record["argv"][i + 1] for i, a in enumerate(record["argv"][:-1]) if a == "--session-id"]
    assert len(session_ids) == 1 and session_ids[0] == record["session_id"], (
        f"a Claude desk task must launch with one minted --session-id: argv {record['argv']}")
    session = record["session_id"]
    before = probe_listing(record, "before-hooks")
    assert before is not None and len(bindings_for(before, session)) == 1, (
        f"the Claude session {session} was not bound at launch (before any hook): probes {record.get('probes')}")
    stops = _launch_hook_runs(record, "Stop")
    assert stops and all(run["code"] == 0 for run in stops), (
        f"the launch's Stop hook did not run or failed: {json.dumps(stops)[-3000:]}")
    _, server = memory_server(record.get("mcp_servers") or {})
    _assert_desk_capture(deployment, server, session, marker, "claude")


def test_codex_desk_task_binds_on_its_first_hook_and_its_stop_is_captured_under_the_desk(configured):
    """GREEN-IF a Codex desk task started from the board is unbound before its first hook, bound (source board, its
    desk) after it, its Stop hook captures, and the launch's memory MCP finds the turn in desk scope."""
    deployment = configured.require()
    task, marker = _task("codex")
    _started(deployment.board.start_task(deployment.workspace, task, f"Remember {marker}.", agent="codex",
                                         desk_id=deployment.desk["desk_id"]), "codex desk task")
    record = deployment.runtime.wait_record("codex", marker)
    assert record.get("phase") == "done", f"the fake codex failed: {record.get('errors')}"
    assert record.get("hooks_enabled") is True, f"the Codex launch did not enable hooks: argv {record['argv']}"
    session = record["session_id"]
    before = probe_listing(record, "before-hooks")
    assert before is not None and not bindings_for(before, session), (
        f"the Codex session was bound before its first hook: {record.get('probes')}")
    firsts = _launch_hook_runs(record, "UserPromptSubmit")
    assert firsts and all(run["code"] == 0 for run in firsts), (
        f"the launch's first Codex hook did not run or failed: {json.dumps(firsts)[-3000:]}")
    stops = _launch_hook_runs(record, "Stop")
    assert stops and all(run["code"] == 0 for run in stops), (
        f"the launch's Codex Stop hook did not run or failed: {json.dumps(stops)[-3000:]}")
    _, server = memory_server(record.get("mcp_servers") or {})
    _assert_desk_capture(deployment, server, session, marker, "codex")


def _assert_unbound_launch(deployment: Deployment, record: dict) -> None:
    argv = record.get("argv") or []
    added = [flag for flag in ("--session-id", "--mcp-config", "--strict-mcp-config") if flag in argv]
    assert not added, f"a task without a desk got launch binding arguments {added}: {argv}"
    assert "KP_AGENT_LAUNCH_RECEIPT" not in (record.get("env") or {}), "a desk-less task got a launch receipt"
    launch_hooks = [path for path, document in (record.get("settings") or {}).items()
                    if LAUNCH_HOOK.search(json.dumps(document)) or "kp_agent_tooling" in json.dumps(document)]
    assert not launch_hooks, f"a desk-less task got launch binding hooks in {launch_hooks}"
    assert not record.get("mcp_servers"), f"a desk-less task got MCP servers {record.get('mcp_servers')}"


def test_task_without_a_desk_launches_unbound_as_before(configured):
    """GREEN-IF, with the registry configured, a task without a desk launches the CLI with none of the launch
    binding's additions and the registry gains no binding."""
    deployment = configured.require()
    runtime = deployment.runtime
    before = runtime.registry_cli(runtime.tooling(), "list").get("bindings") or []
    task, marker = _task("nodesk")
    _started(deployment.board.start_task(deployment.workspace, task, f"Remember {marker}.", agent="claude"),
             "desk-less claude task")
    record = runtime.wait_record("claude", marker)
    assert record.get("phase") == "done", f"the fake claude failed: {record.get('errors')}"
    _assert_unbound_launch(deployment, record)
    after = runtime.registry_cli(runtime.tooling(), "list").get("bindings") or []
    assert len(after) == len(before) and not bindings_for({"bindings": after}, record["session_id"]), (
        f"a desk-less task changed the registry's bindings: {len(before)} -> {len(after)}")


def test_desk_task_without_registry_does_not_start_and_shows_the_reason(unconfigured):
    """GREEN-IF, with no registry, a desk-less task still launches (control), while a desk task answers
    ok: false with a non-empty reason and its CLI never runs."""
    deployment = unconfigured.require()
    runtime = deployment.runtime
    control_task, control_marker = _task("control")
    _started(deployment.board.start_task(deployment.workspace, control_task, f"Remember {control_marker}.",
                                         agent="claude"), "desk-less control task")
    control = runtime.wait_record("claude", control_marker)
    _assert_unbound_launch(deployment, control)

    for agent in ("claude", "codex"):
        task, marker = _task(f"nodesk-registry-{agent}")
        desk_id = "desk:" + str(uuid.uuid4())
        result = deployment.board.start_task(deployment.workspace, task, f"Remember {marker}.", agent=agent,
                                             desk_id=desk_id)
        assert isinstance(result, dict) and result.get("ok") is False, (
            f"a {agent} desk task started without a registry: {result}")
        assert isinstance(result.get("error"), str) and result["error"].strip(), (
            f"a refused {agent} desk task shows no reason: {result}")
        time.sleep(10)
        assert not runtime.records_matching(agent, marker), f"the {agent} CLI ran for a refused desk task"
