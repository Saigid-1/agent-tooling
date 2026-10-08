"""Harness for the S2 product-image contract suite (``tests/image``).

Contract: ``docs/work/orders/S2-product-image.md``. The suite tests that
contract against images named by environment variables; it never builds an
image and never reads an implementation. Every container it starts carries the
label ``agent-tooling-image-test=s2`` and is force-removed by name afterwards.

Environment (a test that needs an unset variable FAILS; it never skips):

- ``AGENT_TOOLING_TEST_IMAGE``          product image (required)
- ``AGENT_TOOLING_TEST_IMAGE_RUNTIME``  runtime image (required by runtime tests)
- ``AGENT_TOOLING_TEST_IMAGE_AGENTS``   agents image (required by agents tests)
- ``AGENT_TOOLING_TEST_SOURCE_REVISION`` the ``SOURCE_REVISION`` build arg (P4)
- ``AGENT_TOOLING_TEST_CLAUDE_VERSION`` / ``AGENT_TOOLING_TEST_CODEX_VERSION``
  the versions pinned for the ``agents`` target (P3)
- ``AGENT_TOOLING_TEST_REFERENCE_BYTES`` size of the current ``full`` build,
  default 2330000000 (the measured 2.33 GB), P5
- ``AGENT_TOOLING_TEST_SOURCE_ROOT``    source tree for the static checks,
  default: the checkout that contains this file
- ``AGENT_TOOLING_TEST_BOARD_TIMEOUT``  seconds to wait for a board, default 120
"""
from __future__ import annotations

import ast
import http.client
import json
import os
import re
import socket
import subprocess
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

PRODUCT = "AGENT_TOOLING_TEST_IMAGE"
RUNTIME = "AGENT_TOOLING_TEST_IMAGE_RUNTIME"
AGENTS = "AGENT_TOOLING_TEST_IMAGE_AGENTS"
REVISION = "AGENT_TOOLING_TEST_SOURCE_REVISION"
CLAUDE_VERSION = "AGENT_TOOLING_TEST_CLAUDE_VERSION"
CODEX_VERSION = "AGENT_TOOLING_TEST_CODEX_VERSION"
REFERENCE_BYTES = "AGENT_TOOLING_TEST_REFERENCE_BYTES"
DEFAULT_REFERENCE_BYTES = 2_330_000_000
SOURCE_ROOT = "AGENT_TOOLING_TEST_SOURCE_ROOT"
BOARD_TIMEOUT = "AGENT_TOOLING_TEST_BOARD_TIMEOUT"

TARGET_VARIABLE = {"product": PRODUCT, "runtime": RUNTIME, "agents": AGENTS}

LABEL = "agent-tooling-image-test=s2"
BOARD_PORT = 3486
# The run the compose manifest uses for every role: a fixed non-root identity.
NON_ROOT_USER = "10001:10001"
STATE_MARKER = ("/state/.ops-tooling-volume", "ops-tooling-state-v1")
REVISION_LABEL = "org.opencontainers.image.revision"

# Operator overrides for the three board integrations; the paths need not exist.
OPERATOR_OVERRIDES = {
    "KANBAN_SESSION_IMPORT_EXECUTABLE": "/opt/operator/bin/kp-agent-session-import",
    "KANBAN_DESK_REGISTRY_COMMAND": '["/opt/operator/bin/kp-agent-desk-registry","--config","/config/desks.json"]',
    "KANBAN_ASSISTANT_MEMORY_COMMAND": '["/opt/operator/bin/assistant-memory","--binding","/config/assistant.json"]',
}
PASSTHROUGH_PORT = 4111
PASSTHROUGH_ARGS = ["--host", "0.0.0.0", "--port", str(PASSTHROUGH_PORT), "--no-open"]


# --------------------------------------------------------------------------
# Environment


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(
            f"{name} is unset. The image suite requires it and fails rather than skips "
            f"(docs/work/orders/S2-product-image.md, TEST write scope).",
            pytrace=False,
        )
    return value


def image_for(target: str) -> str:
    return required(TARGET_VARIABLE[target])


def reference_bytes() -> int:
    raw = os.environ.get(REFERENCE_BYTES, "").strip()
    if not raw:
        return DEFAULT_REFERENCE_BYTES
    try:
        return int(raw)
    except ValueError:
        pytest.fail(f"{REFERENCE_BYTES} must be an integer byte count, got {raw!r}", pytrace=False)


def source_root() -> Path:
    raw = os.environ.get(SOURCE_ROOT, "").strip()
    return Path(raw).resolve() if raw else REPO_ROOT


def board_timeout() -> float:
    raw = os.environ.get(BOARD_TIMEOUT, "").strip()
    return float(raw) if raw else 120.0


def declared_entry_points() -> list[str]:
    """Every ``[project.scripts]`` name in the package this suite belongs to."""
    data = tomllib.loads((REPO_ROOT / "packages/tooling/pyproject.toml").read_text())
    names = sorted(data["project"]["scripts"])
    assert names, "packages/tooling/pyproject.toml declares no [project.scripts]"
    return names


def assistant_host_summary() -> str:
    """First docstring line of ``kp_agent_tooling.assistant_host_cli`` (its ``--help`` header)."""
    source = REPO_ROOT / "packages/tooling/src/kp_agent_tooling/assistant_host_cli.py"
    doc = ast.get_docstring(ast.parse(source.read_text()))
    assert doc, "assistant_host_cli has no module docstring"
    return squash(doc.splitlines()[0])


def squash(text: str) -> str:
    return " ".join(text.split())


# --------------------------------------------------------------------------
# Docker


def docker(*args: str, timeout: float = 300, input: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, input=input
        )
    except FileNotFoundError:
        pytest.fail("docker CLI not found; the image suite requires Docker", pytrace=False)


def _name(kind: str) -> str:
    return f"agent-tooling-image-test-{kind}-{uuid.uuid4().hex[:10]}"


def remove(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=120)


def run_once(image: str, command: list[str], *, entrypoint: str | None = None, user: str | None = None,
             run_args: list[str] | tuple = (), timeout: float = 300, network: str = "none") -> subprocess.CompletedProcess:
    """``docker run --rm`` one command; the container is removed even on timeout."""
    name = _name("run")
    args = ["run", "--rm", "--pull", "never", "--name", name, "--label", LABEL, "--network", network]
    if entrypoint is not None:
        args += ["--entrypoint", entrypoint]
    if user is not None:
        args += ["--user", user]
    args += [*run_args, image, *command]
    try:
        return docker(*args, timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.fail(f"`docker {' '.join(args)}` did not finish within {timeout}s", pytrace=False)
    finally:
        remove(name)


def sh(image: str, script: str, *args: str, user: str | None = None, timeout: float = 300) -> subprocess.CompletedProcess:
    """Inspect image content with ``/bin/sh``; the image's own entry is bypassed."""
    return run_once(image, ["-c", script, "sh", *args], entrypoint="/bin/sh", user=user, timeout=timeout)


def inspect_image(image: str) -> dict:
    result = docker("image", "inspect", image, timeout=120)
    if result.returncode:
        pytest.fail(f"image {image!r} is not present locally: {result.stderr.strip()}", pytrace=False)
    return json.loads(result.stdout)[0]


def image_env(image: str) -> dict[str, str]:
    env = {}
    for item in inspect_image(image)["Config"].get("Env") or []:
        key, _, value = item.partition("=")
        env[key] = value
    return env


DESK_REGISTRY = "KANBAN_DESK_REGISTRY_COMMAND"


def image_argv(image: str, name: str) -> tuple[list[str] | None, str]:
    """A JSON argv from the image's configured environment (``Config.Env``), or why there is none."""
    raw = image_env(image).get(name)
    if not raw:
        return None, f"{name} is not set in the image's configured environment"
    try:
        argv = json.loads(raw)
    except ValueError:
        return None, f"{name}={raw!r} in the image environment is not JSON"
    if not (isinstance(argv, list) and argv and all(isinstance(part, str) and part for part in argv)):
        return None, f"{name}={raw!r} in the image environment is not a JSON argv"
    return argv, raw


def image_labels(image: str) -> dict[str, str]:
    return inspect_image(image)["Config"].get("Labels") or {}


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=60)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(60)
        sock.connect(self._path)
        self.sock = sock


_UNITS = {"B": 1, "kB": 1000, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}


def unpacked_size(image: str) -> tuple[int, str]:
    """The unpacked size ``docker images`` reports (Engine API ``/images/json`` ``Size``).

    This is the instrument behind the measured 2.33 GB. With the containerd
    image store ``docker image inspect .Size`` is the compressed content size,
    a different quantity, so it is not used.
    """
    image_id = inspect_image(image)["Id"]
    host = os.environ.get("DOCKER_HOST", "").strip()
    if not host:
        host = docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}", timeout=60).stdout.strip()
    if host.startswith("unix://"):
        connection = _UnixHTTPConnection(host[len("unix://"):])
        try:
            connection.request("GET", "/images/json")
            rows = json.loads(connection.getresponse().read())
        finally:
            connection.close()
        for row in rows:
            if row.get("Id") == image_id:
                return int(row["Size"]), "Engine API /images/json Size (exact bytes)"
    listing = docker("image", "ls", "--no-trunc", "--format", "{{.ID}} {{.Size}}", timeout=120)
    for line in listing.stdout.splitlines():
        ident, _, human = line.partition(" ")
        if ident == image_id:
            match = re.fullmatch(r"([0-9.]+)\s*([kKMGT]?B)", human.strip())
            if match:
                return int(float(match.group(1)) * _UNITS[match.group(2)]), "docker image ls Size (3 significant digits)"
    pytest.fail(f"could not measure the unpacked size of {image!r}", pytrace=False)


# --------------------------------------------------------------------------
# Probes shared by several tests

# Resolves each argument on PATH and reports what it found; a script whose
# `#!` interpreter is missing does not resolve.
RESOLVE_SCRIPT = r"""
for n in "$@"; do
  p=$(command -v "$n" 2>/dev/null) || { echo "MISSING $n"; continue; }
  case "$p" in /*) ;; *) echo "NOTFILE $n $p"; continue ;; esac
  if [ ! -f "$p" ] || [ ! -x "$p" ]; then echo "NOTEXEC $n $p"; continue; fi
  first=$(head -n 1 "$p" 2>/dev/null | head -c 512)
  case "$first" in
    '#!'*)
      interp=${first#??}
      interp=${interp# }
      interp=${interp%% *}
      if [ ! -x "$interp" ]; then echo "BADINTERP $n $p $interp"; continue; fi ;;
  esac
  echo "OK $n $p"
done
"""


def resolve_on_path(image: str, names: list[str]) -> dict[str, str]:
    """Map each name to ``OK <path>`` or the failure reason, as the image's default user sees it."""
    result = sh(image, RESOLVE_SCRIPT, *names, timeout=180)
    seen = {}
    for line in result.stdout.splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2:
            seen[parts[1]] = line
    return {name: seen.get(name, f"NOPROBE {name} (exit {result.returncode}: {result.stderr.strip()[:300]})")
            for name in names}


def unresolved(image: str, names: list[str]) -> list[str]:
    return [line for line in resolve_on_path(image, names).values() if not line.startswith("OK ")]


def entrypoint_names(image: str) -> list[str]:
    return [Path(part).name for part in (inspect_image(image)["Config"].get("Entrypoint") or [])]


# --------------------------------------------------------------------------
# Containers and the board


@dataclass
class Container:
    name: str

    def exec(self, *command: str, timeout: float = 120, user: str | None = None) -> subprocess.CompletedProcess:
        args = ["exec"]
        if user:
            args += ["--user", user]
        return docker(*args, self.name, *command, timeout=timeout)

    def logs(self) -> str:
        result = docker("logs", self.name, timeout=60)
        return result.stdout + result.stderr

    def state(self) -> dict:
        result = docker("inspect", self.name, timeout=60)
        return json.loads(result.stdout)[0] if result.returncode == 0 else {}

    def running(self) -> bool:
        return bool(self.state().get("State", {}).get("Running"))

    def host_port(self, port: int) -> int | None:
        result = docker("port", self.name, f"{port}/tcp", timeout=60)
        for line in result.stdout.splitlines():
            if line.startswith("127.0.0.1:"):
                return int(line.rsplit(":", 1)[1])
        return None

    def remove(self) -> None:
        remove(self.name)


def start(image: str, command: list[str], *, run_args: list[str], kind: str) -> tuple[Container | None, str | None]:
    name = _name(kind)
    result = docker("run", "-d", "--pull", "never", "--name", name, "--label", LABEL, *run_args, image, *command,
                    timeout=180)
    container = Container(name)
    if result.returncode:
        container.remove()
        return None, f"`docker run` failed ({result.returncode}): {result.stderr.strip()}"
    return container, None


def hardened_args(state_dir: Path, *, read_only: bool = True) -> list[str]:
    """P2 run: read-only root, all capabilities dropped, non-root, writable /state bind."""
    args = ["--init", "--cap-drop", "ALL", "--user", NON_ROOT_USER, "-v", f"{state_dir.resolve()}:/state"]
    if read_only:
        args.insert(0, "--read-only")
    return args


def prepare_state(path: Path, *, board: bool = False, navigation: bool = False) -> Path:
    """A writable /state root; the board's storage root exists because Kanban never creates it."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o777)
    marker = path / Path(STATE_MARKER[0]).name
    marker.write_text(STATE_MARKER[1] + "\n")
    marker.chmod(0o644)
    if board:
        (path / "kanban").mkdir(exist_ok=True)
        (path / "kanban").chmod(0o777)
    if navigation:
        for name in ("snapshots", "search", "tmp"):
            (path / name).mkdir(exist_ok=True)
            (path / name).chmod(0o777)
    return path


def navigation_config(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "navigation.json").write_text(json.dumps({
        "schema_version": "ops.agent-tooling.v1",
        "repos": {},
        "snapshot_registry": "/state/snapshots",
        "navigation_registry_path": "/state/search",
        "enabled_tools": ["tooling.identity"],
    }))
    (path / "navigation.json").chmod(0o644)
    path.chmod(0o755)
    return path


def http_request(port: int, method: str, path: str, *, body: dict | None = None,
                 headers: dict | None = None, timeout: float = 10) -> tuple[int, dict, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        payload = None if body is None else json.dumps(body)
        all_headers = dict(headers or {})
        if payload is not None:
            all_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=all_headers)
        response = connection.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read().decode("utf-8", "replace")
    finally:
        connection.close()


BOARD_PROCESS_SCRIPT = r"""
for d in /proc/[0-9]*; do
  [ -r "$d/cmdline" ] || continue
  if tr '\000' '\n' < "$d/cmdline" 2>/dev/null | grep -qx '/app/dist/cli.js'; then
    printf '@@PID %s\n' "${d#/proc/}"
    printf '@@ARGV\n'; tr '\000' '\n' < "$d/cmdline"; printf '\n'
    printf '@@ENV\n'; tr '\000' '\n' < "$d/environ" 2>/dev/null; printf '\n'
    printf '@@END\n'
  fi
done
"""


@dataclass
class BoardProcess:
    pid: int
    argv: list[str]
    env: dict[str, str]


def board_processes(container: Container) -> tuple[list[BoardProcess], str]:
    result = container.exec("/bin/sh", "-c", BOARD_PROCESS_SCRIPT)
    found, current, section = [], None, None
    for line in result.stdout.split("\n"):
        if line.startswith("@@PID "):
            current, section = BoardProcess(int(line.split()[1]), [], {}), None
        elif line in ("@@ARGV", "@@ENV"):
            section = line
        elif line == "@@END":
            if current:
                found.append(current)
            current, section = None, None
        elif current and line:
            if section == "@@ARGV":
                current.argv.append(line)
            elif section == "@@ENV":
                key, _, value = line.partition("=")
                current.env[key] = value
    return found, f"exit {result.returncode}; stderr: {result.stderr.strip()[:500]}"


@dataclass
class Board:
    """A board container, or the reason there is none (tests fail on it)."""

    error: str | None = None
    container: Container | None = None
    port: int | None = None
    root_status: int | None = None
    process: BoardProcess | None = None
    process_error: str | None = None
    logs: str = ""
    run_args: list[str] = field(default_factory=list)
    configured: list[str] | None = None  # the operator's desk-registry argv, in the configured run

    def require(self) -> "Board":
        if self.error:
            pytest.fail(self.error, pytrace=False)
        return self

    def require_process(self) -> BoardProcess:
        self.require()
        if self.process is None:
            pytest.fail(f"no `node /app/dist/cli.js` process found in the board container: {self.process_error}",
                        pytrace=False)
        return self.process


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_board(image: str, state_dir: Path, *, command: list[str] | None = None, port: int = BOARD_PORT,
                env: dict[str, str] | None = None, read_only: bool = True, kind: str = "board",
                extra_args: list[str] | tuple = ()) -> Board:
    """Run the board as the contract states and wait until its HTTP port answers.

    The board is published on loopback only, with the browser origin the
    deployment manifest gives a port-mapped board (``KANBAN_PUBLIC_ORIGIN``,
    as ``deploy/compose.yaml`` sets it); the board rejects other Host headers.
    """
    prepare_state(state_dir, board=True)
    for _ in range(3):
        host_port = free_loopback_port()
        run_args = hardened_args(state_dir, read_only=read_only) + [
            "-p", f"127.0.0.1:{host_port}:{port}", "-e", f"KANBAN_PUBLIC_ORIGIN=http://127.0.0.1:{host_port}"]
        for key, value in (env or {}).items():
            run_args += ["-e", f"{key}={value}"]
        run_args += list(extra_args)
        container, error = start(image, command if command is not None else ["kanban"], run_args=run_args, kind=kind)
        if not (error and "already allocated" in error):
            break
    board = Board(container=container, run_args=run_args)
    if error:
        board.error = error
        return board
    deadline = time.monotonic() + board_timeout()
    last = "no answer yet"
    while time.monotonic() < deadline:
        if not container.running():
            board.logs = container.logs()
            board.error = f"board container exited before answering HTTP; logs:\n{board.logs[-4000:]}"
            return board
        board.port = board.port or container.host_port(port)
        if board.port:
            try:
                board.root_status = http_request(board.port, "GET", "/", timeout=5)[0]
                break
            except (OSError, http.client.HTTPException) as exc:
                last = repr(exc)
        time.sleep(0.5)
    else:
        board.logs = container.logs()
        board.error = f"board did not answer HTTP on {port} within {board_timeout()}s ({last}); logs:\n{board.logs[-4000:]}"
        return board
    time.sleep(1.0)  # let startup output reach the log
    board.logs = container.logs()
    processes, detail = board_processes(container)
    nodes = [p for p in processes if p.argv and Path(p.argv[0]).name.startswith("node")]
    if nodes:
        board.process = sorted(nodes, key=lambda p: p.pid)[0]
    else:
        board.process_error = f"{detail}; candidates: {[p.argv for p in processes]}"
    return board


def argv_after_cli(process: BoardProcess) -> list[str]:
    index = process.argv.index("/app/dist/cli.js")
    return process.argv[index + 1:]


def has_pair(args: list[str], flag: str, value: str) -> bool:
    return any(args[i] == flag and args[i + 1] == value for i in range(len(args) - 1)) or f"{flag}={value}" in args


def passcode_session(board: Board) -> dict[str, str]:
    """Headers that authenticate to the board: none when no passcode is required."""
    status, _, body = http_request(board.port, "GET", "/api/passcode/status")
    if status == 200 and json.loads(body).get("required") is False:
        return {}
    match = re.search(r"passcode:\s*([A-Za-z0-9]+)", board.container.logs())
    if not match:
        pytest.fail("the board requires a passcode but printed none to its log", pytrace=False)
    status, headers, body = http_request(board.port, "POST", "/api/passcode/verify", body={"passcode": match.group(1)})
    cookie = (headers.get("set-cookie") or "").split(";", 1)[0]
    if status != 200 or not cookie:
        pytest.fail(f"passcode verification failed: {status} {body[:300]}", pytrace=False)
    return {"Cookie": cookie}
