"""Contract harness for order T7a (docs/work/orders/T7a-role-readiness.md).

Public surfaces only:
- the `kp-agent-install` console script beside the test interpreter, through the
  S3 harness (tests/install/s3_harness.py);
- `docker compose --project-directory "$root" ...` on the rendered root, exactly as
  docs/DOCKER.md writes it (the rendered `.env` names the Compose files and profiles);
- `docker inspect` and `docker logs` of the role containers;
- console scripts run inside a role container with `docker exec -i`.
No implementation module is imported.

Image-marked tests read AGENT_TOOLING_TEST_IMAGE and FAIL (never skip) when it is
unset. Every Compose project they start is named `t7a-test-<test>-<random>`, lives in
a never-reused directory under TMPDIR and is taken down (`down --volumes`, then any
leftover container or network carrying its project label is removed).

Readings the order leaves open (repeated in the arm report under AMBIGUITY):
- "the missing operator files" of a role are, for `refresh`, `config/refresh.json`
  (absent or empty) and `secrets/github-token` (absent only: an empty token is valid
  for public remotes, docs/DOCKER.md "Optional roles"); for `capture`,
  `config/capture/session.json` and `config/capture/workspace-policy.json` (absent or
  empty). The approval record's path is chosen by the policy, so it is not required.
  `tooling` and `board` have no required operator file (the board's import config is
  optional: "Without the config, the import dialog reports that it is unconfigured").
- "naming ... by root-relative path": the path relative to the runtime root
  (`config/refresh.json`), not a container path (`/config/refresh.json`) or an
  absolute host path; a match must not be preceded by `/` or a path character.
- a "readiness entry" for a role is any JSON object in `verify`'s output that is keyed
  by the role's name or carries the role's name as a value.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from s3_harness import DIGEST_FORM, World, docker_env, explain

IMAGE_VARIABLE = "AGENT_TOOLING_TEST_IMAGE"

# Root-relative operator files per role (docs/DOCKER.md "Optional roles").
OPERATOR_FILES = {
    "refresh": ("config/refresh.json", "secrets/github-token"),
    "capture": ("config/capture/session.json", "config/capture/workspace-policy.json"),
}
# Files whose empty placeholder is itself a valid operator value.
EMPTY_IS_CONFIGURED = frozenset({"secrets/github-token"})
ALL_OPERATOR_FILES = tuple(path for paths in OPERATOR_FILES.values() for path in paths)

NOT_CONFIGURED = "not_configured"


# ------------------------------------------------------------------ readiness


def names_root_relative(text: str, relative: str) -> bool:
    """True when `text` names `relative` as a root-relative path (not /config/..., not $root/...)."""
    pattern = r"(?<![A-Za-z0-9_./$~-])" + re.escape(relative) + r"(?![A-Za-z0-9_/-])"
    return re.search(pattern, text) is not None


def unconfigured_files(root: Path, role: str) -> list[str]:
    """The operator files of `role` that are missing in `root` (absent, or empty where empty is not a value)."""
    missing = []
    for relative in OPERATOR_FILES.get(role, ()):
        path = root / relative
        if not path.is_file():
            missing.append(relative)
        elif path.stat().st_size == 0 and relative not in EMPTY_IS_CONFIGURED:
            missing.append(relative)
    return missing


def role_entries(document, role: str) -> list:
    """Every JSON value in `document` that is keyed by `role` or is an object carrying `role` as a value."""
    found = []

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == role:
                    found.append(child)
            if any(isinstance(child, str) and child == role for child in value.values()):
                found.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(document)
    return found


def entry_text(entry) -> str:
    return entry if isinstance(entry, str) else json.dumps(entry, sort_keys=True)


def not_configured_entries(document, role: str) -> list[str]:
    return [entry_text(e) for e in role_entries(document, role) if NOT_CONFIGURED in entry_text(e)]


def not_configured_claims(document, role: str) -> list[str]:
    """Each object under `role`'s entries that itself carries `not_configured` as a value, rendered
    from its own scalar and list fields only. A nested object (for example a sub-entry about a
    different file) is a claim of its own, so one sub-entry's state is never read as the role's.
    Meet refinement (T7a): the whole-entry text also held informational file lists."""
    claims = []

    def own_text(value: dict) -> str:
        return json.dumps({k: v for k, v in value.items() if not isinstance(v, dict)}, sort_keys=True)

    def walk(value):
        if isinstance(value, dict):
            if any(child == NOT_CONFIGURED for child in value.values()):
                claims.append(own_text(value))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    for entry in role_entries(document, role):
        if isinstance(entry, str):
            if NOT_CONFIGURED in entry:
                claims.append(entry)
        else:
            walk(entry)
    return claims


def write_private(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    path.chmod(0o600)
    return path


# ------------------------------------------------------------------- images


def required_image() -> str:
    ref = os.environ.get(IMAGE_VARIABLE, "").strip()
    if not ref:
        pytest.fail(f"{IMAGE_VARIABLE} is unset. This T7a test is image-marked and needs a built "
                    "product image; this is a failure, not a skip.", pytrace=False)
    return ref


def required_docker() -> str:
    docker = shutil.which("docker")
    if not docker:
        pytest.fail("the docker CLI is not on PATH; image-marked T7a tests need it", pytrace=False)
    return docker


def resolve_image(docker: str, ref: str, env: dict) -> str:
    """A digest reference as given; any other reference resolved to its local image ID (the
    installer accepts digests only)."""
    if DIGEST_FORM.fullmatch(ref):
        return ref
    proc = subprocess.run([docker, "image", "inspect", "--format", "{{.Id}}", ref],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, f"{IMAGE_VARIABLE}={ref!r} is not a local image\n" + explain(proc)
    return proc.stdout.strip()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fresh_directory(label: str) -> Path:
    """A never-reused directory under TMPDIR (Docker Desktop's file sharing has served a stale
    'bind source path does not exist' for a reused path; see tests/install/test_p5_empty_root_image.py)."""
    return Path(tempfile.mkdtemp(prefix=f"t7a-test-{label}-")).resolve()


def image_world(label: str, components, *, base: Path | None = None) -> World:
    """An S3 world for a real Compose run: the invoking user's IDs, a free loopback board port,
    a `t7a-test-*` project, and the resolved image."""
    docker = required_docker()
    ref = required_image()
    run_id = uuid.uuid4().hex[:8]
    base = base or fresh_directory(f"{label}-{run_id}")
    world = World.create(base, components=tuple(components), uid=os.getuid() or 10001,
                         gid=os.getgid() or 10001, board_port=free_port(),
                         project=f"t7a-test-{label}-{run_id}")
    return replace(world, image=resolve_image(docker, ref, docker_env(world.home)))


def transcript_roots(world: World) -> list[Path]:
    return [world.home / ".claude" / "projects", world.home / ".codex" / "sessions"]


def with_transcript_roots(world: World, argv: list[str]) -> list[str]:
    extra = []
    for root in transcript_roots(world):
        extra += ["--transcript-root", str(root)]
    return [*argv, *extra]


# ------------------------------------------------------------------ compose


class Runtime:
    """One rendered runtime root driven with the documented `docker compose --project-directory`."""

    def __init__(self, docker: str, world: World):
        self.docker, self.world = docker, world
        self.root = world.root
        self.project = world.project
        self.env = docker_env(world.home)
        self.started = False

    def compose(self, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
        self.started = True
        return subprocess.run([self.docker, "compose", "--project-directory", str(self.root), *args],
                              cwd=self.root, env=self.env, capture_output=True, text=True, timeout=timeout)

    def docker_run(self, *args: str, timeout: int = 180, input: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([self.docker, *args], env=self.env, capture_output=True, text=True,
                              timeout=timeout, input=input)

    def container(self, service: str) -> str:
        proc = self.compose("ps", "-aq", service, timeout=120)
        return proc.stdout.strip().splitlines()[0] if proc.returncode == 0 and proc.stdout.strip() else ""

    def services(self) -> list[str]:
        proc = self.compose("ps", "-a", "--format", "{{.Service}}", timeout=120)
        return sorted(set(proc.stdout.split())) if proc.returncode == 0 else []

    def state(self, cid: str) -> dict:
        proc = self.docker_run("inspect", "--format", "{{json .State}} {{.RestartCount}}", cid)
        assert proc.returncode == 0, explain(proc)
        raw, _, count = proc.stdout.strip().rpartition(" ")
        state = json.loads(raw)
        state["RestartCount"] = int(count)
        return state

    def logs(self, cid: str) -> str:
        proc = self.docker_run("logs", cid)
        return proc.stdout + proc.stderr

    def exec(self, container: str, *argv: str, stdin: str | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
        return self.docker_run("exec", "-i", container, *argv, input=stdin if stdin is not None else "",
                               timeout=timeout)

    def assert_stays_up(self, services, *, window: float = 40.0) -> dict:
        """Every service's container keeps running for `window` seconds without a restart."""
        cids = {service: self.container(service) for service in services}
        missing = [s for s, c in cids.items() if not c]
        assert not missing, f"no container was created for {missing}; compose ps: {self.services()}"
        deadline = time.monotonic() + window
        first = {s: self.state(c) for s, c in cids.items()}
        while True:
            for service, cid in cids.items():
                state = self.state(cid)
                problem = None
                if state.get("Status") != "running" or not state.get("Running"):
                    problem = "is not running"
                elif state.get("Restarting") or state["RestartCount"]:
                    problem = "restarted"
                elif state.get("StartedAt") != first[service].get("StartedAt"):
                    problem = "was restarted (StartedAt changed)"
                if problem:
                    logs = self.logs(cid)
                    pytest.fail(f"{service} {problem}; state={json.dumps(state)}\nlogs:\n{logs[-3000:]}",
                                pytrace=False)
            if time.monotonic() >= deadline:
                return cids
            time.sleep(2)

    def wait_healthy(self, services, *, timeout: float = 300.0) -> None:
        pending = {service: self.container(service) for service in services}
        missing = [s for s, c in pending.items() if not c]
        assert not missing, f"no container for {missing}"
        deadline = time.monotonic() + timeout
        states = {}
        while pending and time.monotonic() < deadline:
            for service, cid in list(pending.items()):
                state = self.state(cid)
                states[service] = state
                health = (state.get("Health") or {}).get("Status")
                if health == "healthy":
                    pending.pop(service)
                elif state.get("Status") in ("exited", "dead"):
                    pytest.fail(f"{service} exited while waiting for health; state={json.dumps(state)}\n"
                                f"logs:\n{self.logs(cid)[-3000:]}", pytrace=False)
            if pending:
                time.sleep(2)
        if pending:
            details = {s: states.get(s) for s in pending}
            logs = {s: self.logs(c)[-1500:] for s, c in pending.items()}
            pytest.fail(f"not healthy within {timeout}s: {sorted(pending)}; states={json.dumps(details)}\n"
                        f"logs={json.dumps(logs, indent=1)}", pytrace=False)

    def down(self) -> list[str]:
        if not self.started:
            return []
        self.compose("down", "--volumes", "--remove-orphans", "--timeout", "10", timeout=300)
        label = f"label=com.docker.compose.project={self.project}"
        leftovers = self.docker_run("ps", "-aq", "--filter", label).stdout.split()
        if leftovers:
            self.docker_run("rm", "-f", *leftovers)
        networks = self.docker_run("network", "ls", "-q", "--filter", label).stdout.split()
        if networks:
            self.docker_run("network", "rm", *networks)
        return self.docker_run("ps", "-aq", "--filter", label).stdout.split()
