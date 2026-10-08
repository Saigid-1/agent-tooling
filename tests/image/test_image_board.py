"""S2 P2 (the board is self-contained) and the board command surface of `product`."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from image_harness import (
    DESK_REGISTRY, NON_ROOT_USER, OPERATOR_OVERRIDES, PASSTHROUGH_ARGS, argv_after_cli, assistant_host_summary,
    docker, has_pair, http_request, image_argv, image_for, passcode_session, sh, squash,
)

pytestmark = pytest.mark.image

UNCONFIGURED = re.compile(r"not configured|unconfigured|\"configured\"\s*:\s*false", re.IGNORECASE)
# Files Docker itself adds to a container (bind targets and the --init binary).
DOCKER_MANAGED = {"/etc/hosts", "/etc/hostname", "/etc/resolv.conf", "/etc/mtab", "/.dockerenv",
                  "/sbin/docker-init", "/usr/sbin/docker-init", "/usr/bin/docker-init"}

EXECUTABLE_SCRIPT = r"""
for p in "$@"; do
  if [ -f "$p" ] && [ -x "$p" ]; then echo "OK $p"; else echo "MISSING $p"; fi
done
"""

DOCKER_BINARY_SCRIPT = r"""
command -v kp-agent-session-import >/dev/null 2>&1 && echo "CONTROL kp-agent-session-import"
command -v docker 2>/dev/null | sed 's/^/ONPATH /'
find / -xdev \( -type f -o -type l \) \( -name docker -o -name kp-agent-session-import \) -perm /111 2>/dev/null \
  | sed 's/^/FOUND /'
echo SCAN-DONE
"""


def _docker_mentions(values: list[str]) -> list[str]:
    return [value for value in values if "docker" in value.lower()]


def _not_executable_in_image(board, paths: list[str]) -> list[str]:
    result = board.container.exec("/bin/sh", "-c", EXECUTABLE_SCRIPT, "sh", *paths)
    lines = result.stdout.splitlines()
    if len(lines) != len(paths):
        return [f"probe failed (exit {result.returncode}): {result.stderr.strip()[:300]}"]
    return [line for line in lines if not line.startswith("OK ")]


def test_board_answers_200_on_root_under_the_hardened_run(product_board):
    board = product_board.require()
    host = board.container.state().get("HostConfig", {})
    user = board.container.state().get("Config", {}).get("User")
    assert host.get("ReadonlyRootfs") is True and "ALL" in (host.get("CapDrop") or []) and user == NON_ROOT_USER, \
        f"instrument check: the board was not run as P2 states: {board.run_args}"
    assert board.root_status == 200, f"GET / on port 3486 answered {board.root_status}, expected 200"


def test_board_default_command_serves_the_contract_arguments_and_storage_root(product_board):
    process = product_board.require_process()
    args = argv_after_cli(process)
    wanted = [has_pair(args, "--host", "0.0.0.0"), has_pair(args, "--port", "3486"), "--no-open" in args]
    assert all(wanted), f"`kanban` without arguments ran node /app/dist/cli.js {args}; expected --host 0.0.0.0 --port 3486 --no-open"
    assert process.env.get("KANBAN_STORAGE_ROOT") == "/state/kanban", \
        f"KANBAN_STORAGE_ROOT={process.env.get('KANBAN_STORAGE_ROOT')!r}, expected '/state/kanban'"


def test_board_keeps_the_passcode_enabled_by_default(product_board):
    board = product_board.require()
    status, _, body = http_request(board.port, "GET", "/api/passcode/status")
    assert status == 200 and json.loads(body).get("required") is True, \
        f"/api/passcode/status answered {status} {body[:300]}; the default board must require its passcode"
    status, _, body = http_request(board.port, "GET", "/api/trpc/desks.list")
    assert status == 401, f"an unauthenticated API call answered {status} {body[:200]}, expected 401"


def test_board_reports_the_desk_registry_unconfigured_without_operator_config(product_board):
    board = product_board.require()
    headers = passcode_session(board)
    status, _, body = http_request(board.port, "GET", "/api/trpc/desks.list", headers=headers)
    assert UNCONFIGURED.search(body), f"desks.list answered {status} {body[:500]}; expected an unconfigured report"


def test_board_session_import_default_is_an_in_image_executable(product_board):
    board = product_board.require()
    value = board.require_process().env.get("KANBAN_SESSION_IMPORT_EXECUTABLE")
    assert value, "KANBAN_SESSION_IMPORT_EXECUTABLE has no default in the board's environment"
    assert value.startswith("/") and Path(value).name == "kp-agent-session-import", value
    assert not _docker_mentions([value]), f"default uses docker: {value}"
    assert not _not_executable_in_image(board, [value]), f"{value} is not an executable file in the image"


def test_board_desk_registry_default_is_the_in_image_registry():
    # The default lives in the image's configured environment. The board process
    # may withhold it while no operator --config names an existing file; that run
    # is covered by the unconfigured-report test, the configured run below.
    image = image_for("product")
    argv, raw = image_argv(image, DESK_REGISTRY)
    assert argv is not None, raw
    assert argv[0].startswith("/") and Path(argv[0]).name == "kp-agent-desk-registry", \
        f"image default {raw} does not begin with an absolute kp-agent-desk-registry"
    assert not _docker_mentions(argv), f"image default uses docker: {raw}"
    probe = sh(image, EXECUTABLE_SCRIPT, argv[0], timeout=120)
    assert probe.stdout.strip() == f"OK {argv[0]}", \
        f"{argv[0]} is not an executable file in the image: {(probe.stdout + probe.stderr).strip()[:300]}"


def test_configured_board_receives_the_in_image_desk_registry(configured_board):
    board = configured_board.require()
    raw = board.require_process().env.get(DESK_REGISTRY)
    assert raw, f"the board process lost the operator's configured {DESK_REGISTRY} ({board.configured})"
    argv = json.loads(raw)
    assert isinstance(argv, list) and argv[:1] == board.configured[:1], \
        f"the configured board runs {raw}; expected an argv beginning with {board.configured[0]}"


def test_board_assistant_memory_default_invokes_the_in_image_module(product_board):
    board = product_board.require()
    raw = board.require_process().env.get("KANBAN_ASSISTANT_MEMORY_COMMAND")
    assert raw, "KANBAN_ASSISTANT_MEMORY_COMMAND has no default in the board's environment"
    argv = json.loads(raw)
    assert isinstance(argv, list) and argv and all(isinstance(part, str) for part in argv), raw
    assert argv[0].startswith("/"), raw
    assert not _docker_mentions(argv), f"default uses docker: {raw}"
    assert not _not_executable_in_image(board, [argv[0]]), f"{argv[0]} is not an executable file in the image"
    result = board.container.exec(*argv, "--help")
    assert result.returncode == 0 and assistant_host_summary() in squash(result.stdout), \
        f"`{raw} --help` did not answer as kp_agent_tooling.assistant_host_cli: exit {result.returncode}: {(result.stdout + result.stderr)[:600]}"


def test_board_container_health_succeeds_without_navigation_configuration(product_board):
    board = product_board.require()
    result = board.container.exec("tooling-container", "health")
    assert result.returncode == 0, f"`tooling-container health` in the board container exited {result.returncode}: {(result.stdout + result.stderr)[-800:]}"


def test_board_writes_nothing_outside_state(writable_board):
    board = writable_board.require()
    http_request(board.port, "GET", "/api/passcode/status")
    diff = docker("diff", board.container.name, timeout=120)
    assert diff.returncode == 0, f"docker diff failed: {diff.stderr}"
    entries = [line.partition(" ")[::2] for line in diff.stdout.splitlines() if line.strip()]
    paths = [path for _, path in entries]
    outside = []
    for kind, path in entries:
        if path == "/state" or path.startswith("/state/") or path in DOCKER_MANAGED:
            continue
        if kind == "C" and any(other.startswith(path.rstrip("/") + "/") for other in paths):
            continue  # a directory changed only because something listed beneath it changed
        outside.append(f"{kind} {path}")
    assert not outside, "the board wrote outside /state (writable-root run, `docker diff`):\n" + "\n".join(outside[:40])


def test_kanban_passes_its_arguments_through(passthrough_board):
    board = passthrough_board.require()
    args = argv_after_cli(board.require_process())
    # The fixture already required an HTTP answer on the passed port.
    assert args == PASSTHROUGH_ARGS, f"`kanban {' '.join(PASSTHROUGH_ARGS)}` ran node /app/dist/cli.js {args}"


def test_operator_settings_override_the_integration_defaults(passthrough_board):
    env = passthrough_board.require_process().env
    differing = {key: env.get(key) for key, value in OPERATOR_OVERRIDES.items() if env.get(key) != value}
    assert not differing, f"operator settings were replaced: {differing}"


def test_product_contains_no_docker_binary():
    image = image_for("product")
    result = sh(image, DOCKER_BINARY_SCRIPT, user="0:0", timeout=600)
    lines = result.stdout.splitlines()
    assert "SCAN-DONE" in lines, f"scan did not finish: exit {result.returncode}: {result.stderr[-500:]}"
    assert "CONTROL kp-agent-session-import" in lines and any(
        line.startswith("FOUND ") and line.endswith("/kp-agent-session-import") for line in lines
    ), "positive control failed: the scan did not see kp-agent-session-import, so an absence would prove nothing"
    found = [line for line in lines if line.startswith("ONPATH ") or (line.startswith("FOUND ") and Path(line).name == "docker")]
    assert not found, "product contains a docker binary:\n" + "\n".join(found)
