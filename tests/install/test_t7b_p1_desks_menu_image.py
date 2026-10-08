"""T7b P1 (container part, image-marked): the Desks menu works in Docker.

Order: docs/work/orders/T7b-board-wiring.md, P1:
- the `board` role's desk registry command is `kp-agent-desk-registry --config
  /config/launch/registry.json`, run in the board container;
- with that registry configured (DOCKER.md/HOST-ADAPTER.md steps), the Desks menu lists,
  saves a desk and binds a session through the board's own HTTP interface, writing the
  same registry the `tooling` and `capture` roles read;
- without it, the menu reports that the registry is not configured, and the board keeps serving.
Falsifier: a menu action that cannot reach a configured registry; a board that fails or
exits for an absent registry; or a write the other roles cannot read.

The registry is configured exactly as DOCKER.md's first run, step 3 writes it (and
HOST-ADAPTER.md "Docker runtime" steps 2 and 3): `/state/memory/registry` (0700, created in the runtime; T9b),
`$root/config/launch/desks.json` (with `harness_profiles_path`), `$root/config/launch/registry.json`,
then `kp-agent-desk-registry --config /config/launch/registry.json initialize` in the tooling role.
The menu is driven through the tRPC procedures the Desks menu calls (`desks.list`,
`desks.roles`, `desks.save`, `desks.bind`) on the published loopback port, past the passcode gate.

Readings (repeated in the arm report under AMBIGUITY):
- "the board role's desk registry command" is the KANBAN_DESK_REGISTRY_COMMAND the board server
  process runs with once the registry exists (whether the role's environment, the image default
  or the board's start sets it is the feature's choice): an absolute in-image
  `kp-agent-desk-registry` followed by exactly `--config /config/launch/registry.json`;
- "the other roles read" the write when `kp-agent-desk-registry --config
  /config/launch/registry.json list`, run in the tooling and in the capture container, shows
  the saved desk and the bound session;
- the order of steps is read two ways, each its own test: the registry written before `up`
  (no restart question), and DOCKER.md's own first-run order, which writes it after `up`.
  In the second, the board is restarted or recreated only if one of the operator documents
  states such a command (`docker compose ... restart|--force-recreate` naming board or no
  service; t7b_harness.documented_board_restarts), as in the T7a P2 rehearsal;
- "the board keeps serving": its container keeps running without a restart, and GET / on
  the loopback port answers, for 20 s after the unconfigured report.
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import PurePosixPath

import pytest

from s3_harness import explain
from t7b_harness import (DESK_REGISTRY_VARIABLE, REGISTRY_CONFIG, TENANT, UNCONFIGURED, Board, BoardRuntime,
                         agents_world, bindings_for, documented_board_restarts, install, json_argv, run_documented,
                         write_registry)

pytestmark = pytest.mark.image

COMPONENTS = ("tooling", "capture", "board")


def _assert_registry_command(argv, where: str) -> None:
    assert (isinstance(argv, list) and len(argv) == 3 and isinstance(argv[0], str) and argv[0].startswith("/")
            and PurePosixPath(argv[0]).name == "kp-agent-desk-registry" and argv[1:] == ["--config", REGISTRY_CONFIG]), (
        f"{where}: {DESK_REGISTRY_VARIABLE} is {argv!r}; the order states "
        f"`kp-agent-desk-registry --config {REGISTRY_CONFIG}` run in the board container")


def _menu_reaches_the_registry(runtime: BoardRuntime, board: Board) -> None:
    """List, save a desk and bind a session through the board; the tooling and capture roles read both."""
    listing = board.data("query", "desks.list")
    assert isinstance(listing.get("desks"), list) and listing.get("tenant_id") == TENANT, (
        f"desks.list did not answer from the configured registry (tenant {TENANT}): {json.dumps(listing)[:1500]}")
    desk = board.save_desk("Desks menu desk")
    session = "t7b-p1-" + uuid.uuid4().hex[:12]
    bound = board.data("mutation", "desks.bind", {
        "harness": "operator", "provider": "none", "model": "none", "native_session_id": session,
        "desk_id": desk["desk_id"], "source": "operator", "workspace": "/config/launch", "parent_session_id": None})
    assert bound.get("status") == "bound" and bound["binding"]["native_session_id"] == session, bound
    relisted = board.data("query", "desks.list")
    assert desk["desk_id"] in [d.get("desk_id") for d in relisted.get("desks") or []], (
        f"desks.list does not list the desk the menu saved: {relisted.get('desks')}")
    for role, container in (("tooling", runtime.tooling()), ("capture", runtime.capture())):
        seen = runtime.registry_cli(container, "list")
        assert desk["desk_id"] in [d.get("desk_id") for d in seen.get("desks") or []], (
            f"the {role} role does not read the desk the board saved: {seen.get('desks')}")
        rows = bindings_for(seen, session)
        assert len(rows) == 1 and rows[0].get("desk_id") == desk["desk_id"], (
            f"the {role} role does not read the session the board bound: {seen.get('bindings')}")


def test_configured_registry_desks_menu_lists_saves_and_binds_through_the_board():
    """GREEN-IF, with the registry written before `up`, the board process's desk registry command is
    `kp-agent-desk-registry --config /config/launch/registry.json`, and desks.list / desks.save / desks.bind over
    HTTP reach the registry that the tooling and capture roles read."""
    world, docker = agents_world("p1-configured", COMPONENTS)
    install(world, board_agents=False)
    write_registry(world)
    runtime = BoardRuntime(docker, world)
    try:
        runtime.up()
        runtime.registry_cli(runtime.tooling(), "initialize")
        _assert_registry_command(json_argv(runtime.board_process_env().get(DESK_REGISTRY_VARIABLE)),
                                 "the board process")
        _menu_reaches_the_registry(runtime, Board(runtime))
        runtime.assert_stays_up(COMPONENTS + ("indexer",), window=4.0)  # T12b: indexer comes with capture/board
    finally:
        remaining = runtime.down()
    assert not remaining, f"the test left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)


def test_unconfigured_registry_is_reported_then_the_documented_steps_reach_the_menu():
    """GREEN-IF without the registry the Desks menu reports it not configured while the board keeps serving, and
    after DOCKER.md's step 3 (plus only a documented board restart, if any) the menu reaches the registry."""
    world, docker = agents_world("p1-unconfigured", COMPONENTS)
    install(world, board_agents=False)
    runtime = BoardRuntime(docker, world)
    try:
        runtime.up()
        board = Board(runtime)
        for kind, procedure in (("query", "desks.list"), ("query", "desks.roles")):
            status, body = board.call(kind, procedure)
            assert UNCONFIGURED.search(json.dumps(body)), (
                f"without the registry, {procedure} answered {status} {json.dumps(body)[:800]}; expected a "
                f"'not configured' report")
        runtime.assert_stays_up(("board",), window=20.0)
        assert board.serving() < 500, "the board stopped answering GET / without a registry"

        # DOCKER.md first run, step 3, after the roles are up (its own order).
        write_registry(world)
        runtime.registry_cli(runtime.tooling(), "initialize")
        restarts = documented_board_restarts()
        for cite, command in restarts:
            done = run_documented(world, command, runtime.env)
            assert done.returncode == 0, f"documented restart ({cite}) failed\n" + explain(done)
        if restarts:
            runtime.up()
        _menu_reaches_the_registry(runtime, Board(runtime))
    finally:
        remaining = runtime.down()
    assert not remaining, f"the test left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)
