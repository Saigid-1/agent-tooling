"""T7b P4 (image-marked): the sidebar assistant's documented memory wiring works in the board container.

Order: docs/work/orders/T7b-board-wiring.md, P4: "DOCKER.md states the container form of the
assistant memory command, its binding file under `$root/config/board/` and the workspace
variable. With them set, the sidebar assistant's memory calls reach the registry desk named
in the binding; without them, its memory reports unconfigured."
Falsifier: a documented step that does not work verbatim, or assistant memory reaching another desk.
(The statements themselves are checked by tests/docs/test_t7b_p5_board_wiring_docs.py.)

What the test takes from DOCKER.md verbatim: the JSON argv documented for
KANBAN_ASSISTANT_MEMORY_COMMAND (its container form: absolute executable, `--binding
/config/board/<file>`) and, from it, the binding file `$root/config/board/<file>`.
What it supplies itself, from the formats the code fixes and the assistant documents state
(docs/memory/ASSISTANT-UI-CAPTURE.md, ASSISTANT-FIRST-SLICE.md; assistant_host_cli.py is outside
T7b's FEATURE write scope): the binding's content (`agent.assistant-host.v1`: `config_template`,
`transcript_root`), the isolated assistant store it names (an `ops.assistant-memory.local.v1`
config, its policy, and an `ops.imported-desk-catalog.v1` catalog with one `assistant` binding:
"the registry desk named in the binding"), and the workspace value.

Readings (repeated in the arm report under AMBIGUITY):
- "with them set": the board role runs with KANBAN_ASSISTANT_MEMORY_COMMAND equal to the
  documented argv and KANBAN_ASSISTANT_MEMORY_WORKSPACE equal to the sidebar's workspace, and
  the binding file exists. How an operator sets a board variable is the feature's to document;
  this test sets both through its Compose test overlay, so it tests the property and the
  documented values, not the mechanism;
- the sidebar workspace is the planned repository, added as a board project (so the board is
  planned with `--board-agents`); the sidebar session is started as the web UI starts it
  (Claude selected in settings, `__home_agent__:<workspace>:claude`, empty prompt), and the
  user's message is typed into its terminal (`runtime.sendTaskSessionInput`);
- the transcript root in the binding is where the Claude CLI writes in the board container:
  `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects` of the board process;
- "reach the registry desk named in the binding": the assistant-memory MCP server the launch
  hands the CLI reports exactly that binding as its own, and a desk-scope `memory.search` finds
  the captured turn after its Stop;
- "without them, its memory reports unconfigured": in a deployment where neither variable is
  set, the sidebar still launches, with no assistant memory attached (no assistant-memory MCP
  server, no `--strict-mcp-config`, no `ASSISTANT_MEMORY_LAUNCH_RECEIPT`). The code at base
  has no other "unconfigured" surface for the sidebar that this test could read.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import PurePosixPath

import pytest

from t7a_harness import write_private
from t12b_harness import bounded_wait, indexer_interval  # T12b B4: the bounded wait
from t7b_harness import (Board, BoardRuntime, agents_world, documented_argvs, install, mcp_calls, own_binding_keys,
                         search_hits, tool_value, write_in_runtime)

pytestmark = pytest.mark.image

COMPONENTS = ("tooling", "board")
TENANT = "t7b-assistant"
ROLE, REPO_KEY = "assistant", "workspace"
# T9b: the assistant store's location, in the project volume.
STORE = "/state/memory/assistant"
# T11b Q4 (regression-set exception, Coordinator 2026-10-04): the template is operator-written under /config.
TEMPLATE = "/config/board/assistant-memory.json"
ASSISTANT_KEY = "binding:" + hashlib.sha256(f"{TENANT}|{ROLE}|{REPO_KEY}".encode()).hexdigest()


def _documented_command() -> tuple[list[str], str]:
    for argv in documented_argvs("DOCKER.md", "KANBAN_ASSISTANT_MEMORY_COMMAND"):
        if argv[0].startswith("/") and "--binding" in argv[:-1]:
            binding = argv[argv.index("--binding") + 1]
            if binding.startswith("/config/board/") and PurePosixPath(binding).name:
                return argv, binding
    pytest.fail("DOCKER.md documents no container form of KANBAN_ASSISTANT_MEMORY_COMMAND (absolute executable, "
                "--binding /config/board/<file>)", pytrace=False)


def _assistant_store(world, binding: str, transcript_root: str) -> None:
    """The isolated assistant store (ASSISTANT-FIRST-SLICE.md) and the binding (ASSISTANT-UI-CAPTURE.md).
    T9b: the store lives in the project volume under /state/memory, created in the runtime."""
    base = STORE
    write_in_runtime(world, [base, f"{base}/store"], {
        f"{base}/catalog.json": json.dumps({
            "schema_version": "ops.imported-desk-catalog.v1", "approval_ref": "operator:t7b",
            "bindings": [{"binding_key": ASSISTANT_KEY, "tenant_id": TENANT, "role": ROLE, "repo_key": REPO_KEY,
                          "desk_label": "Workspace Assistant", "source": "operator:t7b",
                          "memory_write_allowed": True}]}),
        f"{base}/policy.json": json.dumps({"schema_version": "ops.assistant-summary-policy.v1",
                                           "focus_questions": ["What observed outcome supports this?"]})})
    write_private(world.root / TEMPLATE.lstrip("/"), json.dumps({
            "schema_version": "ops.assistant-memory.local.v1", "state_root": f"{base}/store",
            "catalog_path": f"{base}/catalog.json", "workspace_root": str(world.repo),
            "provider_instance": "kanban-claude-assistant", "provider_session_id": "assistant-template",
            "assistant_policy_path": f"{base}/policy.json"}))
    write_private(world.root / binding.lstrip("/"), json.dumps({
        "schema_version": "agent.assistant-host.v1", "config_template": TEMPLATE,
        "transcript_root": transcript_root}))


def _start_sidebar(board: Board, workspace: str) -> str:
    board.data("mutation", "runtime.saveConfig", {"selectedAgentId": "claude"}, workspace=workspace)
    task = f"__home_agent__:{workspace}:claude"
    started = board.data("mutation", "runtime.startTaskSession",
                         {"taskId": task, "prompt": "", "baseRef": "main", "cols": 120, "rows": 40}, workspace=workspace)
    assert isinstance(started, dict) and started.get("ok") is True, f"the sidebar session did not start: {started}"
    return task


def _type(board: Board, workspace: str, task: str, text: str) -> None:
    deadline = time.monotonic() + 60
    while True:
        sent = board.data("mutation", "runtime.sendTaskSessionInput", {"taskId": task, "text": text,
                                                                      "appendNewline": True}, workspace=workspace)
        if sent.get("ok") or time.monotonic() > deadline:
            break
        time.sleep(2)
    assert sent.get("ok"), f"typing into the sidebar terminal failed: {sent}"


def _teardown(runtime, world) -> None:
    remaining = runtime.down() if runtime is not None else []
    assert not remaining, f"the test left containers behind: {remaining}"
    if not os.environ.get("T7B_KEEP_WORLDS"):
        shutil.rmtree(world.base, ignore_errors=True)


def test_documented_assistant_memory_reaches_the_desk_named_in_the_binding():
    """GREEN-IF, with the documented command, its binding file and the workspace variable set, a sidebar Claude
    session gets the assistant-memory MCP server, its Stop is captured, and that server's own binding is exactly
    the binding's assistant desk, whose desk-scope search finds the turn."""
    argv, binding = _documented_command()
    world, docker = agents_world("p4-assistant", COMPONENTS)
    install(world, board_agents=True)
    runtime = None
    try:
        runtime = BoardRuntime(docker, world, fakes=True, board_env={
            "KANBAN_ASSISTANT_MEMORY_COMMAND": json.dumps(argv),
            "KANBAN_ASSISTANT_MEMORY_WORKSPACE": str(world.repo)})
        runtime.up()
        env = runtime.board_process_env()
        claude_dir = env.get("CLAUDE_CONFIG_DIR") or f"{env.get('HOME', '/state').rstrip('/')}/.claude"
        _assistant_store(world, binding, f"{claude_dir}/projects")
        board = Board(runtime)
        workspace = board.add_project(world.repo)
        task = _start_sidebar(board, workspace)
        marker = "T7b assistant juniper " + uuid.uuid4().hex[:10]
        _type(board, workspace, task, f"Remember {marker}.")
        record = runtime.wait_record("claude", marker)
        since = time.monotonic()  # T12b B4: the bounded wait starts once the hook has run
        assert record.get("phase") == "done", f"the fake claude failed: {record.get('errors')}"
        servers = record.get("mcp_servers") or {}
        assert "--strict-mcp-config" in record["argv"] and "assistant-memory" in servers, (
            f"the sidebar session got no isolated assistant memory: argv {record['argv']}, servers {sorted(servers)}")
        assert record.get("env", {}).get("ASSISTANT_MEMORY_LAUNCH_RECEIPT"), "no assistant launch receipt in the env"
        stops = [run for run in record.get("runs") or [] if run["event"] == "Stop" and "assistant_host_cli" in run["command"]]
        assert stops and all(run["code"] == 0 for run in stops), (
            f"the assistant's Stop hook did not run or failed: {json.dumps(stops)[-3000:]}")
        # T12b B4 (named exception): in a tooling+board install the indexer, not the hook, indexes; wait for the
        # search to answer, bounded by 2 x the indexer's interval, read from the rendered manifest's `indexer` command.
        interval = indexer_interval(runtime)

        def searched():
            replies = mcp_calls(runtime, runtime.board(), servers["assistant-memory"], [
                ("memory.connection_status", {}), ("memory.bindings", {}), ("memory.search", {"query": marker})])
            return bool(search_hits(replies[2], marker)), replies
        _, (status, bindings, search), attempts, waited = bounded_wait(searched, interval, since=since)
        assert (tool_value(status) or {}).get("status") == "ready", f"assistant memory is not ready: {status}"
        own = own_binding_keys(bindings)
        assert own == [ASSISTANT_KEY], (
            f"assistant memory reaches {own}, not the desk named in the binding ({ASSISTANT_KEY})")
        assert search_hits(search, marker), (f"the assistant's desk-scope memory.search does not find {marker!r} within "
                                             f"2 x the indexer interval ({2 * interval:.1f}s; {attempts} searches, "
                                             f"{waited:.1f}s)")
    finally:
        _teardown(runtime, world)


def test_sidebar_without_the_assistant_variables_launches_without_memory():
    """GREEN-IF, in a deployment that sets neither assistant variable, the sidebar Claude session launches and no
    assistant memory is attached to it."""
    world, docker = agents_world("p4-unwired", COMPONENTS)
    install(world, board_agents=True)
    runtime = None
    try:
        runtime = BoardRuntime(docker, world, fakes=True)
        runtime.up()
        board = Board(runtime)
        workspace = board.add_project(world.repo)
        task = _start_sidebar(board, workspace)
        marker = "T7b unwired juniper " + uuid.uuid4().hex[:10]
        _type(board, workspace, task, f"Remember {marker}.")
        record = runtime.wait_record("claude", marker)
        servers = record.get("mcp_servers") or {}
        assert "--strict-mcp-config" not in record["argv"] and "assistant-memory" not in servers, (
            f"a sidebar without the assistant variables got assistant memory: {record['argv']}, {sorted(servers)}")
        assert not record.get("env", {}).get("ASSISTANT_MEMORY_LAUNCH_RECEIPT"), "an assistant launch receipt appeared"
    finally:
        _teardown(runtime, world)
