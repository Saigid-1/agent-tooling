"""Contract harness for order T12a, A2 (docs/work/orders/T12a-capture-survives.md), image-marked tests.

Public surfaces only, as tests/install/t9b_harness.py: the `kp-agent-install` console script, the rendered
Compose project driven with `docker compose --project-directory "$root"`, `docker inspect`, `docker
exec` into the role containers, `docker kill` and `docker events`. No implementation module is imported.

Every Compose project, container and volume these tests create is named `t12a-test-...` (the arm's
Docker rule: `t12a-` only). Each project lives in a never-reused directory under TMPDIR and is taken
down with its volumes.

The role-refusal assertion is t9b_harness.assert_role_refused (Coordinator rule C2); this module adds only
the positive observations (the role command runs) and the restart observations.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
import yaml

from s3_harness import World, compose_files, docker_env, explain
from t7a_harness import Runtime, free_port, required_docker, resolve_image
import t9b_harness
from t9b_harness import exec_python, health_report, required_image, role_processes, role_programs

PREFIX = "t12a-test"
# T7a's not_configured wait re-checks its operator files every 10 s (NOT_CONFIGURED_POLL_SECONDS); the order
# says the refusal wait is "like T7a's not_configured" and names no other interval. Read: one re-check
# interval is 10 s, and the command must be running within that plus EXEC_SLACK for the exec and probe.
RECHECK_SECONDS = 10.0
EXEC_SLACK = 5.0
RESTART_WINDOW = 30.0
COMMAND_TIMEOUT = 90.0


def fresh_directory(label: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"{PREFIX}-{label}-")).resolve()


def image_world(label: str, components) -> tuple[World, str]:
    """t9b_harness.image_world with a `t12a-test-*` project."""
    docker = required_docker()
    ref = required_image()
    run_id = uuid.uuid4().hex[:8]
    world = World.create(fresh_directory(f"{label}-{run_id}"), components=tuple(components),
                         uid=os.getuid() or 10001, gid=os.getgid() or 10001, board_port=free_port(),
                         project=f"{PREFIX}-{label}-{run_id}")
    return replace(world, image=resolve_image(docker, ref, docker_env(world.home))), docker


class Project(t9b_harness.Project):
    """t9b_harness.Project for a `t12a-test-*` project."""

    def __init__(self, docker: str, world: World):
        Runtime.__init__(self, docker, world)
        assert self.project.startswith(PREFIX + "-"), self.project

    def all_containers(self) -> list[str]:
        label = f"label=com.docker.compose.project={self.project}"
        return self.docker_run("ps", "-aq", "--filter", label).stdout.split()


def remove_role(project: Project, role: str) -> None:
    ids = project.docker_run("ps", "-aq", "--filter", f"label=com.docker.compose.project={project.project}",
                             "--filter", f"label=com.docker.compose.service={role}").stdout.split()
    if ids:
        project.docker_run("rm", "-f", *ids)


def command_running(project: Project, cid: str) -> bool:
    """The role command of `cid` runs: a process runs one of its programs, or (tooling, whose `wait` runs
    inside tooling-container) health answers and is not `refused`."""
    programs = role_programs(project, cid)
    if programs:
        return bool(role_processes(project, cid, programs))
    health = health_report(project, cid)
    return health is not None and health.get("status") not in (None, "refused")


def wait_command(project: Project, service: str, timeout: float) -> float:
    """Seconds until `service`'s role command runs; fails after `timeout`."""
    cid = project.container(service)
    assert cid, f"no {service} container"
    start = time.monotonic()
    while True:
        if command_running(project, cid):
            return time.monotonic() - start
        if time.monotonic() - start > timeout:
            pytest.fail(f"{service}'s role command ({sorted(role_programs(project, cid))}) did not run within "
                        f"{timeout}s: state={json.dumps(project.state(cid))} health="
                        f"{json.dumps(health_report(project, cid))}\nlog:\n{project.logs(cid)[-2500:]}",
                        pytrace=False)
        time.sleep(0.5)


def main_process(project: Project, cid: str) -> dict:
    """The role's main process ({pid, ppid, argv}, PIDs in the container): the child of docker-init (PID 1)."""
    proc = exec_python(project, cid, t9b_harness.PROCESSES)
    assert proc.returncode == 0, explain(proc)
    rows = json.loads(proc.stdout.strip().splitlines()[-1])
    init = [row for row in rows if row["pid"] == 1]
    assert init and Path(init[0]["argv"][0]).name == "docker-init", f"precondition: PID 1 is docker-init: {init}"
    children = [row for row in rows if row["ppid"] == 1]
    assert len(children) == 1, f"precondition: one child of PID 1 (docker-init), found {children}"
    return children[0]


def compose_override(project: Project, override: dict, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """`docker compose` on the rendered root's own files plus one override file."""
    path = project.world.base / f"override-{uuid.uuid4().hex[:6]}.yaml"
    path.write_text(yaml.safe_dump(override, sort_keys=False, width=4096))
    argv = [project.docker, "compose", "--project-directory", str(project.root), "-p", project.project]
    for file in [*compose_files(project.root), path]:
        argv += ["-f", str(file)]
    return subprocess.run([*argv, *args], cwd=project.root, env=project.env, capture_output=True, text=True,
                          timeout=timeout)


def events(project: Project, cid: str, since: float) -> list[dict]:
    """`die` and `oom` events of `cid` from `since` (Unix time) until now."""
    proc = project.docker_run("events", "--since", f"{since:.3f}", "--until", f"{time.time():.3f}", "--filter",
                              f"container={cid}", "--filter", "event=die", "--filter", "event=oom",
                              "--format", "{{json .}}", timeout=60)
    assert proc.returncode == 0, explain(proc)
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def watch_stays_up(project: Project, service: str, window: float, *, refused: bool) -> None:
    """`service` keeps running for `window` s with no restart (RestartCount 0, the same StartedAt); while
    `refused`, its role command never runs during the window."""
    cid = project.container(service)
    first = project.state(cid)
    programs = role_programs(project, cid)
    deadline = time.monotonic() + window
    while True:
        state = project.state(cid)
        problem = None
        if not state.get("Running") or state.get("Status") != "running":
            problem = "is not running"
        elif state.get("Restarting") or state.get("RestartCount"):
            problem = "restarted"
        elif state.get("StartedAt") != first.get("StartedAt"):
            problem = "was restarted (StartedAt changed)"
        elif refused and programs and role_processes(project, cid, programs):
            problem = f"ran its role command {sorted(programs)} while refused"
        if problem:
            pytest.fail(f"{service} {problem}; state={json.dumps(state)}\nlog:\n{project.logs(cid)[-2500:]}",
                        pytrace=False)
        if time.monotonic() >= deadline:
            return
        time.sleep(2)


# ------------------------------------------- B57: the second source for an OOM death (d-ii)
#
# Order B57 (docs/work/orders/B57-oom-attribution.md), P1. The Docker daemon's OOM report (every `docker
# events` field, and State.OOMKilled, set from the same event) is ONE source. The second source is the
# kernel's own OOM record: for every OOM kill the kernel prints a one-line summary (mm/oom_kill.c,
# dump_oom_victim; mm/memcontrol.c, mem_cgroup_print_oom_context) of the form
#   oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=...,mems_allowed=0,
#   oom_memcg=/system.slice/docker-<id>.scope,task_memcg=/system.slice/docker-<id>.scope,
#   task=python3,pid=<host pid>,uid=<uid>
# (one line; wrapped here). `task_memcg=` is the victim's memory cgroup, which Docker names by the full
# container id: `docker-<id>.scope` under the systemd cgroup driver, `<id>` under the cgroupfs driver.
# The record is written by the kernel's OOM killer when it kills, and read from the host's kernel log
# (journald's copy with `journalctl -k`, or the ring buffer with `sudo -n dmesg`), never through the
# daemon, its events or its State: a missed daemon report does not remove it.

KERNEL_LOG_WAIT = 10.0
_TASK_MEMCG = re.compile(r"\btask_memcg=([^,\s]+)")
_HIDDEN_JOURNAL = "not seeing messages from other users and the system"


def container_id(project: Project, cid: str) -> str:
    """The full id of container `cid`: the id its cgroup is named by."""
    proc = project.docker_run("inspect", "--format", "{{.Id}}", cid)
    assert proc.returncode == 0, explain(proc)
    return proc.stdout.strip()


def daemon_oom_report(seen: list[dict]) -> list[dict]:
    """The daemon's OOM report among `events` output: `oom` actions and events whose oomKilled is true."""
    return [event for event in seen if event.get("Action") == "oom"
            or (event.get("Actor") or {}).get("Attributes", {}).get("oomKilled") == "true"]


def kernel_oom_lines(text: str, container: str) -> list[str]:
    """The kernel OOM-kill records in `text` whose victim is in container `container`'s memory cgroup:
    a `task_memcg=` path with a component `docker-<id>.scope` or `<id>` for its full id. A record of any
    other cgroup, and a bare count, are not this container's."""
    assert re.fullmatch(r"[0-9a-f]{64}", container), f"not a full container id: {container!r}"
    own = {f"docker-{container}.scope", container}
    return [line.strip() for line in text.splitlines()
            if any(own & set(path.split("/")) for path in _TASK_MEMCG.findall(line))]


def _journal_argv(since: float | None) -> list[str]:
    window = ["--since", f"@{int(since)}"] if since is not None else ["-n", "20"]
    return ["journalctl", "-k", "--no-pager", "-o", "short-precise", *window]


def _dmesg_argv(since: float | None) -> list[str]:
    return ["sudo", "-n", "dmesg"]


# Ranked: the journal first (no privilege), then the ring buffer through non-interactive sudo.
KERNEL_LOG_READERS = (("journalctl -k", _journal_argv), ("sudo -n dmesg", _dmesg_argv))


def _run_reader(argv: list[str]) -> subprocess.CompletedProcess | str:
    if not shutil.which(argv[0]):
        return f"{argv[0]} is not on PATH"
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=60,
                              env={**os.environ, "LC_ALL": "C"}, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"{type(error).__name__}: {error}"


def _reader_answer(name: str, proc: subprocess.CompletedProcess | str) -> str | None:
    """None when `proc` is a reading of the kernel's log; otherwise why it is not."""
    if isinstance(proc, str):
        return proc
    if proc.returncode != 0:
        return f"exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]!r}"
    if name.startswith("journalctl") and _HIDDEN_JOURNAL in (proc.stderr or ""):
        return "exit 0 without the system journal (the user cannot read kernel messages)"
    if not [line for line in proc.stdout.splitlines() if line.strip() and not line.startswith("-- ")]:
        return "exit 0 with no kernel message"
    return None


@dataclass
class Reading:
    """The second source's answer for one container in one window: `skipped` (its reason) when it could
    not be read; otherwise `lines`, this container's OOM-kill records (none: "no OOM for this container")."""
    source: str
    skipped: str | None = None
    lines: list[str] = field(default_factory=list)


@dataclass
class KernelLog:
    """The kernel's OOM record on this host, by the first reader that answers (`reader`), or `skipped` with
    the reason. It is this host's kernel log, so it is the containers' only when the Docker daemon runs on
    this host's kernel (Linux, the same kernel release); from Docker Desktop's VM it is skipped."""
    daemon: str
    reader: str | None = None
    skipped: str | None = None
    tried: list[str] = field(default_factory=list)

    @property
    def source(self) -> str:
        return f"kernel log ({self.reader})" if self.reader else "kernel log"

    def text(self, since: float) -> str:
        argv = dict(KERNEL_LOG_READERS)[self.reader](since)
        proc = _run_reader(argv)
        why = _reader_answer(self.reader, proc)
        if why is not None:
            raise RuntimeError(f"{self.reader} stopped answering: {why}")
        return proc.stdout

    def reading(self, container: str, since: float, *, before: Reading | None = None,
                wait: float = 0.0) -> Reading:
        """This container's OOM-kill records since `since` that `before` (a reading taken before the
        trigger) did not hold; polls up to `wait` s for the first one (the journal lags the kernel)."""
        if self.skipped:
            return Reading(self.source, skipped=self.skipped)
        old = set(before.lines) if before else set()
        deadline = time.monotonic() + wait
        while True:
            try:
                lines = [line for line in kernel_oom_lines(self.text(since), container) if line not in old]
            except RuntimeError as error:
                return Reading(self.source, skipped=str(error))
            if lines or time.monotonic() >= deadline:
                return Reading(self.source, lines=lines)
            time.sleep(1)


def kernel_log(project: Project) -> KernelLog:
    """The second source on this host: skipped unless the daemon runs on this host's kernel; then the first
    reader of KERNEL_LOG_READERS whose probe returns kernel messages. Every reader is probed, and `tried`
    records each one's answer, so a run's log shows which of them this host's user can read."""
    proc = project.docker_run("info", "--format", "{{json .}}", timeout=60)
    assert proc.returncode == 0, explain(proc)
    info = json.loads(proc.stdout)
    daemon = (f"{info.get('OperatingSystem')}, kernel {info.get('KernelVersion')}, cgroup "
              f"v{info.get('CgroupVersion')} ({info.get('CgroupDriver')} driver)")
    host = f"{platform.system()} {platform.release()}"
    if sys.platform != "linux" or info.get("KernelVersion") != platform.release():
        return KernelLog(daemon, skipped=f"the containers run on the Docker daemon's kernel ({daemon}), not on "
                                         f"this host's ({host}); this host cannot read that kernel's log")
    answers = {name: _reader_answer(name, _run_reader(argv(None))) for name, argv in KERNEL_LOG_READERS}
    tried = [f"{name}: {'reads kernel messages' if why is None else why}" for name, why in answers.items()]
    readers = [name for name, why in answers.items() if why is None]
    if readers:
        return KernelLog(daemon, reader=readers[0], tried=tried)
    return KernelLog(daemon, skipped="no reader of this host's kernel log answered: " + "; ".join(tried),
                     tried=tried)
