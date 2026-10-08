"""Contract harness for order T12b (docs/work/orders/T12b-one-indexer-outbox.md), image-marked tests.

Public surfaces only, as tests/install/t9b_harness.py and t12a_harness.py: the `kp-agent-install` console script,
the rendered Compose project driven with `docker compose --project-directory "$root"`, `docker inspect`, `docker
exec` into the role containers. Every Compose project, container and volume these tests create is named
`t12b-test-...` (the arm's Docker rule) and is taken down with its volumes.

The indexer's interval is read from ONE place, the rendered manifest's `indexer` command (the order, B4: "The
interval is read from one place: the rendered manifest's `indexer` command"), by `indexer_interval` below; the
spelling of the interval option is the seam in tests/t12b_seams.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from s3_harness import World, compose_files, docker_env, explain
from t7a_harness import Runtime, free_port, required_docker, resolve_image
import t9b_harness
from t9b_harness import required_image

import t12b_seams as seams

PREFIX = "t12b-test"
INDEXER = seams.INDEXER_SERVICE
PROBE = Path(__file__).resolve().parent / "t12b_role_probe.py"


def fresh_directory(label: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"{PREFIX}-{label}-")).resolve()


def image_world(label: str, components) -> tuple[World, str]:
    """t9b_harness.image_world with a `t12b-test-*` project."""
    docker = required_docker()
    ref = required_image()
    run_id = uuid.uuid4().hex[:8]
    world = World.create(fresh_directory(f"{label}-{run_id}"), components=tuple(components),
                         uid=os.getuid() or 10001, gid=os.getgid() or 10001, board_port=free_port(),
                         project=f"{PREFIX}-{label}-{run_id}")
    return replace(world, image=resolve_image(docker, ref, docker_env(world.home))), docker


class Project(t9b_harness.Project):
    """t9b_harness.Project for a `t12b-test-*` project."""

    def __init__(self, docker: str, world: World):
        Runtime.__init__(self, docker, world)
        assert self.project.startswith(PREFIX + "-"), self.project


def rendered_services(runtime) -> dict:
    """The services `docker compose config` renders for the root's own files (no test overlay)."""
    env = dict(runtime.env)
    env.pop("COMPOSE_FILE", None)
    argv = [runtime.docker, "compose", "--project-directory", str(runtime.root)]
    for path in compose_files(runtime.root):
        argv += ["-f", str(path)]
    proc = subprocess.run(argv + ["config", "--format", "json"], cwd=runtime.root, env=env, capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, "docker compose config rejected the rendered root\n" + explain(proc)
    return json.loads(proc.stdout).get("services") or {}


def indexer_service(runtime) -> dict:
    services = rendered_services(runtime)
    if INDEXER not in services:
        pytest.fail(f"the rendered manifest has no `{INDEXER}` service (roles: {sorted(services)}); T12b B2: "
                    "`indexer` is selected whenever capture or board is", pytrace=False)
    return services[INDEXER]


def indexer_interval(runtime) -> float:
    """THE one place the indexer's interval is read: the rendered manifest's `indexer` command."""
    command = indexer_service(runtime).get("command") or []
    interval = seams.indexer_interval(command)
    if interval is None or interval <= 0:
        pytest.fail(f"the rendered `{INDEXER}` command names no interval: {command}", pytrace=False)
    return interval


def bounded_wait(probe, interval: float, *, since: float | None = None):
    """The B4 bounded wait: call `probe()` (it returns (answered, value)) until it answers, starting calls only until
    2 x `interval` seconds after `since` (default: now), the order's bound. Returns (answered, value, attempts,
    seconds since `since` at the last call's end)."""
    started = time.monotonic() if since is None else since
    deadline = started + 2 * interval
    attempts = 0
    while True:
        attempts += 1
        answered, value = probe()
        if answered or time.monotonic() >= deadline:
            return answered, value, attempts, time.monotonic() - started
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def run_probe(runtime, container: str, *args: str, timeout: int = 300) -> dict:
    """tests/install/t12b_role_probe.py inside `container` (`docker exec -i ... python3 - <args>`); its JSON."""
    proc = subprocess.run([runtime.docker, "exec", "-i", container, "python3", "-", *args],
                          input=PROBE.read_text(), env=runtime.env, capture_output=True, text=True, timeout=timeout)
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            value.setdefault("exit", proc.returncode)
            value.setdefault("stderr", proc.stderr[-3000:])
            return value
    return {"exit": proc.returncode, "error": "no JSON result", "stdout": proc.stdout[-3000:],
            "stderr": proc.stderr[-3000:]}
