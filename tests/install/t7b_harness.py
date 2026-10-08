"""Contract harness for order T7b (docs/work/orders/T7b-board-wiring.md).

Public surfaces only:
- the `kp-agent-install` console script beside the test interpreter (S3 harness);
- `docker compose --project-directory "$root" ...` on the rendered root, with the
  rendered `COMPOSE_FILE` list, plus (for container tests that launch agents) one
  test overlay that is not part of the product: it puts fake `claude`/`codex`
  executables ahead on the board's PATH and gives them a directory for their
  records (see FAKE_* below);
- the board's own HTTP interface: the tRPC procedures the Desks menu and task start
  call (`desks.*`, `projects.add`, `workspace.ensureWorktree|deleteWorktree`,
  `runtime.startTaskSession`), behind the board's passcode gate;
- console scripts run inside role containers with `docker exec -i`
  (`kp-agent-desk-registry`, and the memory MCP server a launch names);
- `docker inspect` / `docker logs` and the board process's environment.
No implementation module is imported.

Image-marked tests read AGENT_TOOLING_TEST_IMAGE_AGENTS and FAIL (never skip) when
it is unset. Every Compose project is named `t7b-test-<label>-<random>`, lives in a
never-reused directory under TMPDIR and is taken down (`down --volumes`, then any
leftover container or network carrying its project label is removed).

The fake CLIs never call a provider. They behave as the real CLIs do where the
contract can observe it: Claude writes its transcript to
`${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/<cwd with non-alphanumerics as '-'>/<session>.jsonl`
and runs the command hooks of every `--settings` file (stdin: the hook payload);
Codex writes its rollout to `${CODEX_HOME:-$HOME/.codex}/sessions/YYYY/MM/DD/rollout-...-<id>.jsonl`
(opening with a `session_meta` row) and runs the `-c hooks.<Event>` overrides when
`features.hooks` is true. Each records its argv, environment, hook runs and the
MCP servers it was given.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from s3_harness import StdioSession, World, docker_env, explain, read_dotenv
from t7a_harness import Runtime, free_port, required_docker, resolve_image, write_private

AGENTS_VARIABLE = "AGENT_TOOLING_TEST_IMAGE_AGENTS"
BOARD_AGENTS = "--board-agents"

REGISTRY_CONFIG = "/config/launch/registry.json"
REGISTRY_RELATIVE = "config/launch/registry.json"
DESK_REGISTRY_VARIABLE = "KANBAN_DESK_REGISTRY_COMMAND"
LAUNCH_BINDING_VARIABLE = "KANBAN_LAUNCH_BINDING_COMMAND"
# The order, P2: the board role's launch binding command, exactly.
LAUNCH_BINDING_ARGV = ["/usr/local/bin/kp-agent-launch", "--config", REGISTRY_CONFIG]

TENANT = "t7b-team"
# T9b: every store lives in the project volume at /state/memory, created in the runtime.
REGISTRY_STATE = "/state/memory/registry"
REGISTRY_INSTANCE = "agent-tooling"
REGISTRY_SESSION = "registry-operator"

FAKE_BIN = "/t7b/bin"
FAKE_RECORDS = "/t7b/records"
FAKE_WAIT = 300.0

UNCONFIGURED = re.compile(r"not configured|unconfigured|\"configured\"\s*:\s*false", re.IGNORECASE)
ROOT = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------------- worlds


def required_agents_image() -> str:
    ref = os.environ.get(AGENTS_VARIABLE, "").strip()
    if not ref:
        pytest.fail(f"{AGENTS_VARIABLE} is unset. This T7b test is image-marked and needs the `agents` image; "
                    "this is a failure, not a skip.", pytrace=False)
    return ref


def fresh_directory(label: str) -> Path:
    """A never-reused directory under TMPDIR (Docker Desktop has served a stale missing bind source for reused paths)."""
    return Path(tempfile.mkdtemp(prefix=f"t7b-test-{label}-")).resolve()


def agents_world(label: str, components) -> tuple[World, str]:
    """An S3 world for a real Compose run with the `agents` image: the invoking user's IDs, a free
    loopback board port and a `t7b-test-*` project."""
    docker = required_docker()
    ref = required_agents_image()
    run_id = uuid.uuid4().hex[:8]
    world = World.create(fresh_directory(f"{label}-{run_id}"), components=tuple(components),
                         uid=os.getuid() or 10001, gid=os.getgid() or 10001, board_port=free_port(),
                         project=f"t7b-test-{label}-{run_id}")
    return replace(world, image=resolve_image(docker, ref, docker_env(world.home))), docker


def install_argv(world: World, *, board_agents: bool) -> list[str]:
    """DOCKER.md first run, step 1: the planning user's home and both transcript roots; plus the
    order's new plan input when asked for."""
    argv = world.args() + ["--home", str(world.home),
                           "--transcript-root", str(world.home / ".claude" / "projects"),
                           "--transcript-root", str(world.home / ".codex" / "sessions")]
    if board_agents:
        argv.append(BOARD_AGENTS)
    return argv


def install(world: World, *, board_agents: bool) -> list[str]:
    argv = install_argv(world, board_agents=board_agents)
    proc, value = world.plan(argv)
    assert proc.returncode == 0 and isinstance(value, dict), (
        f"`kp-agent-install plan` refused the inputs (board_agents={board_agents})\n" + explain(proc))
    applied = world.apply(argv, value.get("plan_sha256"))
    assert applied.returncode == 0, "`kp-agent-install apply` refused its reviewed plan\n" + explain(applied)
    # Meet (Coordinator ruling, T9b Amendment 6): under P1b a role refuses an unprepared store,
    # so the documented sequence runs `prepare` between `apply` and `up`.
    from t9b_harness import prepare as _prepare, prepared as _prepared
    readied = _prepare(world)
    assert _prepared(readied) is None, explain(readied)
    return argv


def in_runtime(world: World, script: str, *, stdin: str | None = None) -> None:
    """T9b store creation: a one-off tooling container of the project (`docker compose run --rm -T
    tooling sh -c '<script>'`), which mounts the project volume at /state/memory."""
    proc = subprocess.run([required_docker(), "compose", "--project-directory", str(world.root), "run", "--rm",
                           "-T", "tooling", "sh", "-c", script], cwd=world.root, env=docker_env(world.home),
                          capture_output=True, text=True, timeout=600, input=stdin if stdin is not None else "")
    assert proc.returncode == 0, "the in-runtime store creation failed\n" + explain(proc)


def write_in_runtime(world: World, directories: list[str], files: dict[str, str]) -> None:
    """Create private store directories and files (0700/0600) at container paths, in the runtime."""
    script = ("import json, os, sys\n"
              "spec = json.load(sys.stdin)\n"
              "for d in spec['dirs']:\n"
              "    os.makedirs(d, exist_ok=True); os.chmod(d, 0o700)\n"
              "for path, text in spec['files'].items():\n"
              "    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n"
              "    os.write(fd, text.encode()); os.close(fd); os.chmod(path, 0o600)\n")
    in_runtime(world, "exec python3 -c " + shlex.quote(script),
               stdin=json.dumps({"dirs": directories, "files": files}))


def write_registry(world: World, tenant: str = TENANT) -> None:
    """DOCKER.md first run, step 3 (and HOST-ADAPTER.md "Docker runtime"): the registry operator
    config and its descriptor, and the existing private state_root (created in the runtime, under
    /state/memory). `initialize` runs later, in the tooling role (`registry_cli`)."""
    in_runtime(world, f"mkdir -p -m 700 {REGISTRY_STATE} && chmod 700 {REGISTRY_STATE}")
    write_private(world.root / "config" / "launch" / "desks.json", json.dumps({
        "schema_version": "agent-tooling.desk-registry.v1", "tenant_id": tenant,
        "roster_path": f"{REGISTRY_STATE}/roles.json",
        "harness_profiles_path": "/config/launch/harness-profiles.json"}) + "\n")
    write_private(world.root / REGISTRY_RELATIVE, json.dumps(registry_document()) + "\n")


def registry_document(**over) -> dict:
    value = {"schema_version": "ops.desk-memory.local.v1", "state_root": REGISTRY_STATE,
             "catalog_path": "/config/launch/desks.json", "workspace_root": "/config/launch",
             "provider_instance": REGISTRY_INSTANCE, "provider_session_id": REGISTRY_SESSION}
    value.update(over)
    return value


# ------------------------------------------------------------------- fake CLIs

FAKE_SOURCE = r'''#!/usr/local/bin/python3
"""T7b fake agent CLI. The flavor is the executable name (claude or codex). It never calls a provider."""
import datetime, json, os, re, subprocess, sys, time, uuid

FLAVOR = os.path.basename(sys.argv[0])
ARGV = sys.argv[1:]
RECORDS = os.environ.get("T7B_FAKE_RECORDS", "/t7b/records")
NAME = os.path.join(RECORDS, "%s-%d-%s.json" % (FLAVOR, os.getpid(), uuid.uuid4().hex[:8]))
KEEP = ("PATH", "HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "KANBAN_LAUNCH_BINDING_COMMAND")
record = {"flavor": FLAVOR, "argv": ARGV, "cwd": os.getcwd(), "pid": os.getpid(), "phase": "started",
          "env": {k: v for k, v in os.environ.items()
                  if k in KEEP or k.startswith(("KP_", "KANBAN_", "ASSISTANT_", "CLAUDE", "CODEX"))},
          "errors": [], "runs": [], "probes": [], "mcp_servers": {}, "settings": {}}


def save():
    tmp = NAME + ".tmp"
    with open(tmp, "w") as out:
        json.dump(record, out)
    os.replace(tmp, NAME)


def load_json_arg(value):
    text = value if value.lstrip().startswith("{") else open(value).read()
    return json.loads(text)


VALUE_FLAGS = {"--settings", "--session-id", "--permission-mode", "--append-system-prompt", "--system-prompt",
               "--setting-sources", "--model", "--resume", "--add-dir", "--allowedTools", "--disallowedTools"}


def claude_config():
    flags = {"--settings": [], "--session-id": []}
    mcp, positional, i = [], [], 0
    while i < len(ARGV):
        arg = ARGV[i]
        if arg == "--mcp-config":  # variadic, as in the real CLI
            i += 1
            while i < len(ARGV) and not ARGV[i].startswith("-"):
                mcp.append(ARGV[i]); i += 1
            continue
        if arg in VALUE_FLAGS and i + 1 < len(ARGV):
            flags.setdefault(arg, []).append(ARGV[i + 1]); i += 2; continue
        if arg.startswith("--") and "=" in arg and arg.split("=", 1)[0] in VALUE_FLAGS:
            key, value = arg.split("=", 1)
            flags.setdefault(key, []).append(value); i += 1; continue
        if not arg.startswith("-"):
            positional.append(arg)
        i += 1
    hooks, servers = {}, {}
    for value in flags["--settings"]:
        document = load_json_arg(value)
        record["settings"][value] = document
        for event, groups in (document.get("hooks") or {}).items():
            hooks.setdefault(event, []).extend(groups)
    for value in mcp:
        servers.update(load_json_arg(value).get("mcpServers") or {})
    return {"hooks": hooks, "servers": servers, "session_ids": flags["--session-id"], "mcp_configs": mcp,
            "prompt": positional[-1] if positional else ""}


def key_path(key):
    import tomllib
    probe, path = tomllib.loads("%s = 0" % key), []
    while isinstance(probe, dict):
        (name, probe), = probe.items()
        path.append(name)
    return path


def codex_config():
    import tomllib
    merged, positional, i = {}, [], 0
    while i < len(ARGV):
        arg, value = ARGV[i], None
        if arg in ("-c", "--config") and i + 1 < len(ARGV):
            value = ARGV[i + 1]; i += 1
        elif arg.startswith("--config="):
            value = arg[len("--config="):]
        elif arg in ("--sandbox", "--ask-for-approval", "--model", "-m", "-s", "-a") and i + 1 < len(ARGV):
            i += 2; continue
        elif not arg.startswith("-") and arg != "resume":
            positional.append(arg)
        i += 1
        if value is None:
            continue
        key, _, raw = value.partition("=")
        try:
            parsed = tomllib.loads("v = %s" % raw)["v"]
        except tomllib.TOMLDecodeError:
            parsed = raw
        target, path = merged, key_path(key)
        for name in path[:-1]:
            if not isinstance(target.get(name), dict):
                target[name] = {}
            target = target[name]
        target[path[-1]] = parsed
    hooks = merged.get("hooks") if isinstance(merged.get("hooks"), dict) else {}
    enabled = (merged.get("features") or {}).get("hooks") is True
    return {"hooks": {k: v for k, v in hooks.items() if isinstance(v, list)} if enabled else {},
            "hooks_enabled": enabled, "servers": merged.get("mcp_servers") or {},
            "prompt": positional[-1] if positional else ""}


def probe(label):
    raw = os.environ.get("T7B_FAKE_PROBE")
    if not raw:
        return
    try:
        done = subprocess.run(json.loads(raw), capture_output=True, text=True, timeout=120)
        record["probes"].append({"label": label, "code": done.returncode, "stdout": done.stdout[-200000:],
                                 "stderr": done.stderr[-4000:]})
    except Exception as error:
        record["probes"].append({"label": label, "error": "%s: %s" % (type(error).__name__, error)})


def run_hooks(config, event, payload, env):
    for group in config["hooks"].get(event, []) or []:
        if not isinstance(group, dict):
            continue
        for handler in group.get("hooks") or []:
            if not isinstance(handler, dict) or handler.get("type", "command") != "command":
                continue
            timeout = handler.get("timeout") or 60
            started = time.monotonic()
            try:
                done = subprocess.run(["/bin/sh", "-c", handler["command"]], input=json.dumps(payload),
                                      capture_output=True, text=True, cwd=payload["cwd"], env=env, timeout=timeout)
                code, out, err = done.returncode, done.stdout, done.stderr
            except subprocess.TimeoutExpired as expired:
                code, out, err = "timeout", str(expired.stdout or ""), str(expired.stderr or "")
            record["runs"].append({"event": event, "command": handler["command"], "timeout": timeout,
                                   "seconds": round(time.monotonic() - started, 2), "code": code,
                                   "stdout": out[-4000:], "stderr": err[-4000:]})
            save()


def append(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as out:
        for row in rows:
            out.write(json.dumps(row) + "\n")


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def read_terminal_message(timeout):
    """An interactive CLI started without a prompt takes the user's first message from its terminal."""
    import select
    if not sys.stdin.isatty():
        return ""
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    return sys.stdin.readline().strip() if ready else ""


def main():
    save()
    config = claude_config() if FLAVOR == "claude" else codex_config()
    record["mcp_servers"] = config["servers"]
    if not config["prompt"]:
        record["phase"] = "waiting-for-input"
        save()
        config["prompt"] = read_terminal_message(600)
        record["phase"] = "started"
    record["prompt"] = config["prompt"]
    cwd = os.getcwd()
    env = dict(os.environ)
    reply = "Recorded: %s" % config["prompt"]
    if FLAVOR == "claude":
        session = config["session_ids"][-1] if config["session_ids"] else str(uuid.uuid4())
        base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.environ.get("HOME", "/"), ".claude")
        transcript = os.path.join(base, "projects", re.sub(r"[^a-zA-Z0-9]", "-", cwd), session + ".jsonl")
        env["CLAUDE_PROJECT_DIR"] = cwd
        record.update(session_id=session, transcript=transcript)
        common = {"session_id": session, "transcript_path": transcript, "cwd": cwd, "permission_mode": "default"}
        probe("before-hooks")
        save()
        run_hooks(config, "SessionStart", dict(common, hook_event_name="SessionStart", source="startup"), env)
        append(transcript, [{"type": "user", "sessionId": session, "uuid": str(uuid.uuid4()), "cwd": cwd,
                             "timestamp": now(), "message": {"role": "user", "content": config["prompt"]}}])
        run_hooks(config, "UserPromptSubmit", dict(common, hook_event_name="UserPromptSubmit",
                                                   prompt=config["prompt"]), env)
        append(transcript, [{"type": "assistant", "sessionId": session, "uuid": str(uuid.uuid4()), "cwd": cwd,
                             "timestamp": now(), "message": {"role": "assistant",
                                                             "content": [{"type": "text", "text": reply}]}}])
        run_hooks(config, "Stop", dict(common, hook_event_name="Stop", stop_hook_active=False), env)
    else:
        session = str(uuid.uuid4())
        stamp = datetime.datetime.now(datetime.timezone.utc)
        base = os.environ.get("CODEX_HOME") or os.path.join(os.environ.get("HOME", "/"), ".codex")
        transcript = os.path.join(base, "sessions", stamp.strftime("%Y"), stamp.strftime("%m"), stamp.strftime("%d"),
                                  "rollout-%s-%s.jsonl" % (stamp.strftime("%Y-%m-%dT%H-%M-%S"), session))
        record.update(session_id=session, transcript=transcript, hooks_enabled=config.get("hooks_enabled"))
        append(transcript, [{"timestamp": now(), "type": "session_meta",
                             "payload": {"id": session, "timestamp": now(), "cwd": cwd, "originator": "codex_cli_rs",
                                         "cli_version": "0.0.0", "source": "cli"}},
                            {"timestamp": now(), "type": "response_item",
                             "payload": {"type": "message", "role": "user",
                                         "content": [{"type": "input_text", "text": config["prompt"]}]}}])
        common = {"session_id": session, "transcript_path": transcript, "cwd": cwd, "model": "fake-model",
                  "turn_id": "turn-1"}
        probe("before-hooks")
        save()
        run_hooks(config, "UserPromptSubmit", dict(common, hook_event_name="UserPromptSubmit",
                                                   prompt=config["prompt"]), env)
        append(transcript, [{"timestamp": now(), "type": "response_item",
                             "payload": {"type": "message", "role": "assistant",
                                         "content": [{"type": "output_text", "text": reply}]}}])
        run_hooks(config, "Stop", dict(common, hook_event_name="Stop", stop_hook_active=False,
                                       last_assistant_message=reply), env)
    probe("after-hooks")
    record["phase"] = "done"
    save()
    sys.stdout.write("fake %s session %s done\n" % (FLAVOR, session))
    sys.stdout.flush()
    time.sleep(1800)  # an interactive CLI stays up until its session is stopped


try:
    main()
except Exception as error:
    record["errors"].append("%s: %s" % (type(error).__name__, error))
    record["phase"] = "failed"
    save()
    raise
'''


def write_fakes(world: World) -> tuple[Path, Path]:
    """The fake executables (`<base>/t7b-fakes/bin/{claude,codex}`) and their record directory."""
    bin_dir = world.base / "t7b-fakes" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("claude", "codex"):
        path = bin_dir / name
        path.write_text(FAKE_SOURCE)
        path.chmod(0o755)
    bin_dir.chmod(0o755)
    (world.base / "t7b-fakes").chmod(0o755)
    records = world.base / "t7b-records"
    records.mkdir(mode=0o700)
    return bin_dir, records


def image_path(docker: str, image: str, env: dict) -> str:
    proc = subprocess.run([docker, "image", "inspect", "--format", "{{json .Config.Env}}", image],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, explain(proc)
    for item in json.loads(proc.stdout) or []:
        key, _, value = item.partition("=")
        if key == "PATH":
            return value
    return "/usr/local/bin:/usr/bin:/bin"


# ------------------------------------------------------------------- runtime


class BoardRuntime(Runtime):
    """A rendered root driven with the documented `docker compose --project-directory "$root"`.

    With `fakes`, the rendered COMPOSE_FILE list gets one more file, outside the runtime root:
    a test overlay that puts the fake CLIs ahead on the board's PATH (read-only) and binds their
    record directory, plus any `board_env` a test sets on the board (P4: the documented
    assistant memory variables). It changes nothing else about any role."""

    def __init__(self, docker: str, world: World, *, fakes: bool = False, board_env: dict | None = None):
        super().__init__(docker, world)
        self.records: Path | None = None
        self.overlay: Path | None = None
        self.board_env = dict(board_env or {})
        if fakes:
            self._add_fakes()

    def _add_fakes(self) -> None:
        bin_dir, self.records = write_fakes(self.world)
        rendered = read_dotenv(self.root / ".env").get("COMPOSE_FILE", "")
        assert rendered, "the rendered .env names no COMPOSE_FILE"
        board_env = (self.rendered_service("board").get("environment") or {})
        path = board_env.get("PATH") or image_path(self.docker, self.world.image, self.env)
        self.overlay = self.world.base / "t7b-fakes" / "compose.t7b-fakes.yaml"
        probe = json.dumps(["kp-agent-desk-registry", "--config", REGISTRY_CONFIG, "list"])
        self.overlay.write_text(yaml.safe_dump({"services": {"board": {
            "environment": {"PATH": f"{FAKE_BIN}:{path}", "T7B_FAKE_RECORDS": FAKE_RECORDS,
                            "T7B_FAKE_PROBE": probe, **self.board_env},
            "volumes": [
                {"type": "bind", "source": str(bin_dir), "target": FAKE_BIN, "read_only": True,
                 "bind": {"create_host_path": False}},
                {"type": "bind", "source": str(self.records), "target": FAKE_RECORDS,
                 "bind": {"create_host_path": False}}]}}}, sort_keys=False))
        self.env["COMPOSE_FILE"] = rendered + os.pathsep + str(self.overlay)

    def rendered_service(self, name: str) -> dict:
        """The service as `docker compose config` renders the rendered root (without any test overlay)."""
        env = dict(self.env)
        env.pop("COMPOSE_FILE", None)
        proc = subprocess.run([self.docker, "compose", "--project-directory", str(self.root), "config",
                               "--format", "json"], cwd=self.root, env=env, capture_output=True, text=True,
                              timeout=120)
        assert proc.returncode == 0, "docker compose config rejected the rendered root\n" + explain(proc)
        return (json.loads(proc.stdout).get("services") or {}).get(name) or {}

    def up(self, *services: str) -> None:
        up = self.compose("up", "-d", "--wait", "--wait-timeout", "300", *services, timeout=900)
        if up.returncode != 0:
            logs = {s: self.logs(c)[-2000:] for s in ("board", "tooling", "capture") if (c := self.container(s))}
            pytest.fail("`docker compose --project-directory \"$root\" up -d --wait` failed\n" + explain(up) +
                        f"\nlogs={json.dumps(logs, indent=1)}", pytrace=False)

    def board(self) -> str:
        cid = self.container("board")
        assert cid, f"no board container; compose ps: {self.services()}"
        return cid

    def tooling(self) -> str:
        return f"{self.project}-tooling"

    def capture(self) -> str:
        return f"{self.project}-capture"

    def registry_cli(self, container: str, action: str, payload=None, *, ok: bool = True):
        proc = self.exec(container, "kp-agent-desk-registry", "--config", REGISTRY_CONFIG, action,
                         stdin=None if payload is None else json.dumps(payload))
        if ok:
            assert proc.returncode == 0, f"kp-agent-desk-registry {action} in {container}\n" + explain(proc)
            return json.loads(proc.stdout)
        return proc

    def board_process_env(self) -> dict[str, str]:
        """The environment of the board server, the oldest `node /app/dist/cli.js` process that is not one of
        Kanban's own hook commands (`cli.js hooks ...`): what Kanban runs with."""
        proc = self.exec(self.board(), "/bin/sh", "-c", BOARD_PROCESS_SCRIPT)
        found, current, section = [], None, None
        for line in proc.stdout.split("\n"):
            if line.startswith("@@PID "):
                current, section = {"pid": int(line.split()[1]), "argv": [], "env": {}}, None
            elif line in ("@@ARGV", "@@ENV"):
                section = line
            elif line == "@@END":
                if current is not None:
                    found.append(current)
                current, section = None, None
            elif current is not None and line:
                if section == "@@ARGV":
                    current["argv"].append(line)
                elif section == "@@ENV" and "=" in line:
                    key, _, value = line.partition("=")
                    current["env"][key] = value
        servers = [p for p in found if "/app/dist/cli.js" in p["argv"]
                   and p["argv"][p["argv"].index("/app/dist/cli.js") + 1:][:1] != ["hooks"]]
        assert servers, f"no board server process (`node /app/dist/cli.js`) in the board container\n" + explain(proc)
        return min(servers, key=lambda p: p["pid"])["env"]

    # -- fake records ------------------------------------------------------
    def records_matching(self, flavor: str, marker: str) -> list[dict]:
        found = []
        for path in sorted(self.records.glob(f"{flavor}-*.json")):
            try:
                value = json.loads(path.read_text())
            except ValueError:
                continue
            if marker in json.dumps(value.get("argv")) or marker in str(value.get("prompt") or ""):
                found.append(value)
        return found

    def wait_record(self, flavor: str, marker: str, *, timeout: float = FAKE_WAIT) -> dict:
        deadline = time.monotonic() + timeout
        latest = None
        while time.monotonic() < deadline:
            matching = self.records_matching(flavor, marker)
            if matching:
                latest = matching[-1]
                if latest.get("phase") in ("done", "failed"):
                    return latest
            time.sleep(2)
        logs = self.logs(self.board())[-3000:]
        pytest.fail(f"the fake {flavor} launched for {marker!r} did not finish within {timeout}s; "
                    f"record={json.dumps(latest)[-4000:] if latest else None}\nboard logs:\n{logs}", pytrace=False)


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


def json_argv(raw: str | None) -> list | None:
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return None
    return value if isinstance(value, list) else None


# --------------------------------------------------------------- board HTTP


class Board:
    """The board's HTTP interface on its loopback port, past the passcode gate."""

    def __init__(self, runtime: BoardRuntime):
        self.runtime = runtime
        self.port = runtime.world.board_port
        self.headers = self._passcode()

    def request(self, method: str, path: str, body=None, headers=None, timeout: float = 120):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            payload = None if body is None else json.dumps(body)
            sent = dict(self.headers if hasattr(self, "headers") else {})
            sent.update(headers or {})
            if payload is not None:
                sent["Content-Type"] = "application/json"
            connection.request(method, path, body=payload, headers=sent)
            response = connection.getresponse()
            text = response.read().decode("utf-8", "replace")
            return response.status, {k.lower(): v for k, v in response.getheaders()}, text
        finally:
            connection.close()

    def _passcode(self) -> dict:
        self.headers = {}
        status, _, body = self.request("GET", "/api/passcode/status")
        if status == 200 and json.loads(body).get("required") is False:
            return {}
        match = None
        deadline = time.monotonic() + 30
        while match is None and time.monotonic() < deadline:
            match = re.search(r"passcode:\s*([A-Za-z0-9]+)", self.runtime.logs(self.runtime.board()))
            if match is None:
                time.sleep(1)
        assert match, "the board requires a passcode but printed none to its log"
        status, headers, body = self.request("POST", "/api/passcode/verify", {"passcode": match.group(1)})
        cookie = (headers.get("set-cookie") or "").split(";", 1)[0]
        assert status == 200 and cookie, f"passcode verification failed: {status} {body[:300]}"
        return {"Cookie": cookie}

    def call(self, kind: str, procedure: str, value=None, *, workspace: str | None = None):
        """One tRPC call (non-batched): (http status, parsed body or text)."""
        headers = {"x-kanban-workspace-id": workspace} if workspace else {}
        if kind == "query":
            path = f"/api/trpc/{procedure}"
            if value is not None:
                from urllib.parse import quote
                path += "?input=" + quote(json.dumps(value))
            status, _, text = self.request("GET", path, headers=headers)
        else:
            status, _, text = self.request("POST", f"/api/trpc/{procedure}", value if value is not None else {},
                                           headers=headers)
        try:
            return status, json.loads(text)
        except ValueError:
            return status, text

    def data(self, kind: str, procedure: str, value=None, *, workspace: str | None = None):
        status, body = self.call(kind, procedure, value, workspace=workspace)
        assert status == 200 and isinstance(body, dict) and "result" in body, (
            f"board {procedure} answered {status}: {json.dumps(body)[:1500] if not isinstance(body, str) else body[:1500]}")
        return body["result"].get("data")

    def serving(self) -> int:
        return self.request("GET", "/", timeout=15)[0]

    # -- the flows the UI runs ---------------------------------------------
    def add_project(self, path: Path) -> str:
        added = self.data("mutation", "projects.add", {"path": str(path)})
        assert added.get("ok") and added.get("project"), f"projects.add {path}: {added}"
        return added["project"]["id"]

    def save_desk(self, name: str = "Product", *, capture: bool = True, memory_write: bool = True) -> dict:
        roster = self.data("query", "desks.roles")
        role = roster["roles"][0]["role_id"]
        desk_id = "desk:" + str(uuid.uuid4())
        saved = self.data("mutation", "desks.save", {
            "desk_id": desk_id, "name": name, "description": f"{name}: a T7b contract desk.", "role": role,
            "repos": ["fixture"], "capture": capture, "memory_write": memory_write, "expected_version": 0})
        assert saved.get("desk_id") == desk_id, f"desks.save returned {saved}"
        return saved

    def start_task(self, workspace: str, task_id: str, prompt: str, *, agent: str = "claude",
                   desk_id: str | None = None, ensure: bool = True):
        """What the board's task start does (web-ui use-task-sessions): ensureWorktree, then startTaskSession."""
        if ensure:
            ensured = self.data("mutation", "workspace.ensureWorktree", {"taskId": task_id, "baseRef": "main"},
                                workspace=workspace)
            assert ensured.get("ok"), f"workspace.ensureWorktree for {task_id}: {ensured}"
        request = {"taskId": task_id, "prompt": prompt, "taskTitle": task_id, "baseRef": "main", "cols": 120,
                   "rows": 40, "agentId": agent}
        if desk_id is not None:
            request["deskId"] = desk_id
        return self.data("mutation", "runtime.startTaskSession", request, workspace=workspace)


# --------------------------------------------------------------- MCP in a role


def mcp_calls(runtime: BoardRuntime, container: str, server: dict, calls, *, timeout: float = 180) -> list[dict]:
    """Start `server` (an MCP server object: command, args, env) inside `container` over
    `docker exec -i`, initialize, and make each (tool, arguments) call; returns each tool result."""
    argv = [runtime.docker, "exec", "-i"]
    for key, value in (server.get("env") or {}).items():
        argv += ["-e", f"{key}={value}"]
    argv += [container, str(server["command"]), *[str(a) for a in server.get("args") or []]]
    session = StdioSession(argv, runtime.env)
    try:
        reply = session.request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                  "clientInfo": {"name": "t7b-contract", "version": "0"}},
                                timeout=timeout)
        assert "result" in reply, f"MCP initialize failed: {reply}"
        session.notify("notifications/initialized")
        results = []
        for index, (name, arguments) in enumerate(calls, start=2):
            reply = session.request(index, "tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
            assert "result" in reply, f"MCP {name} failed: {reply}"
            results.append(reply["result"])
        return results
    finally:
        session.close()


def tool_value(result: dict):
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    for item in result.get("content") or []:
        if item.get("type") == "text":
            try:
                return json.loads(item["text"])
            except ValueError:
                return item["text"]
    return None


def search_hits(result: dict, marker: str) -> list:
    value = tool_value(result)
    assert not result.get("isError") and isinstance(value, dict), f"memory.search failed: {result}"
    return [row for row in value.get("results") or [] if marker in json.dumps(row)]


def memory_server(servers: dict) -> tuple[str, dict]:
    """The desk memory server among the MCP servers a launch handed the CLI (LAUNCH-BINDING.md names it
    `kp_desk_memory`)."""
    if "kp_desk_memory" in servers:
        return "kp_desk_memory", servers["kp_desk_memory"]
    named = {k: v for k, v in servers.items() if "memory" in json.dumps(v)}
    assert len(named) == 1, f"no single desk memory MCP server among {sorted(servers)}"
    return next(iter(named.items()))


def bindings_for(listing: dict, session: str) -> list[dict]:
    return [b for b in listing.get("bindings") or [] if b.get("native_session_id") == session]


def desk_binding_key(listing: dict, desk_id: str) -> str:
    for desk in listing.get("desks") or []:
        if desk.get("desk_id") == desk_id:
            return desk.get("binding_key") or (desk.get("binding") or {}).get("binding_key")
    raise AssertionError(f"desk {desk_id} is not in the registry listing: {listing.get('desks')}")


def own_binding_keys(result: dict) -> list[str]:
    value = tool_value(result)
    assert isinstance(value, dict), f"memory.bindings failed: {result}"
    return [b.get("binding_key") for b in value.get("bindings") or [] if b.get("own")]


def probe_listing(record: dict, label: str) -> dict | None:
    for item in record.get("probes") or []:
        if item.get("label") == label and item.get("code") == 0:
            try:
                return json.loads(item["stdout"])
            except ValueError:
                return None
    return None


# --------------------------------------------------------------- documents


DOC_NAMES = ("DOCKER.md", "DESK-MENU.md", "HOST-ADAPTER.md", "LAUNCH-BINDING.md")


def documented_board_restarts() -> list[tuple[str, str]]:
    """Documented `docker compose ... restart` / `--force-recreate` commands that name `board` or no
    service, in code blocks or inline code of the four operator documents (file:line, command)."""
    restart = re.compile(r"\bdocker compose\b.*(\brestart\b|--force-recreate\b)")
    found = []
    for name in DOC_NAMES:
        fenced = False
        for number, line in enumerate((ROOT / "docs" / name).read_text().splitlines(), 1):
            if line.strip().startswith("```"):
                fenced = not fenced
                continue
            candidates = [line.strip()] if fenced else re.findall(r"`([^`]+)`", line)
            for command in candidates:
                command = command.split(" #", 1)[0].strip()
                if not restart.search(command):
                    continue
                tail = re.split(r"\brestart\b|\bup\b", command)[-1]
                services = [t for t in shlex.split(tail.replace('"$root"', "ROOT"))
                            if not t.startswith("-") and t != "ROOT"]
                if not services or "board" in services:
                    found.append((f"{name}:{number}", command))
    return found


def run_documented(world: World, command: str, env: dict) -> subprocess.CompletedProcess:
    run_env = dict(env, root=str(world.root), project=world.project)
    return subprocess.run(["/bin/bash", "-c", command], cwd=world.cwd, env=run_env, capture_output=True,
                          text=True, timeout=600)


def first_json_argv(candidate: str) -> list | None:
    """The first balanced `[...]` in `candidate` that parses as a non-empty JSON list of strings."""
    for start in [i for i, char in enumerate(candidate) if char == "["]:
        depth = 0
        for position in range(start, len(candidate)):
            depth += {"[": 1, "]": -1}.get(candidate[position], 0)
            if depth == 0:
                try:
                    value = json.loads(candidate[start:position + 1])
                except ValueError:
                    break
                if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
                    return value
                break
    return None


def documented_argvs(name: str, variable: str) -> list[list]:
    """Every JSON argv docs/<name> documents for `variable`: the first on the rest of its line, else, when
    the line ends at the variable, on the next non-empty line (the reading tests/docs/test_t7b_p5_* uses)."""
    lines = (ROOT / "docs" / name).read_text().splitlines()
    found = []
    for index, line in enumerate(lines):
        if variable not in line:
            continue
        rest = line[line.index(variable) + len(variable):]
        argv = first_json_argv(rest)
        if argv is None and not rest.strip(" \t`'\"=:\\"):
            following = next((l for l in lines[index + 1:index + 4] if l.strip()), "")
            argv = first_json_argv(following)
        if argv:
            found.append(argv)
    return found
