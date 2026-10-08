"""S3 P5: an empty root works end to end (image-marked).

With a real image (the S2 `product` target, or today's `full` image) and one
small committed repository:
- `plan`, then `apply`, then `docker compose up -d tooling` reaches healthy;
- over `docker exec -i … kp-agent-tooling --config /config/navigation.json serve`,
  MCP `initialize` succeeds;
- `tools/list` includes `navigation.search`;
- a `navigation.search` call on the repository at its HEAD commit returns at
  least one result for a string known to be in it.
Falsifier: any step fails from an empty root.

AGENT_TOOLING_TEST_IMAGE names the image: a digest reference is passed to the
installer as given; any other reference is resolved to its local image ID
(`sha256:…`), because the installer accepts digests only. The test works in a
fresh directory under TMPDIR (point TMPDIR at the external volume for the meet), uses its
own Compose project name, runs only the `tooling` role, and removes its project.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from dataclasses import replace

import pytest

from s3_harness import (DIGEST_FORM, StdioSession, World, command_vectors, compose_files,
                        docker_env, explain, read_snippets, serve_exec)

NEEDLE = "kp-s3-fixture-needle-5d1c"
HEALTH_TIMEOUT = 300


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _resolve_image(docker: str, ref: str, env: dict) -> str:
    if DIGEST_FORM.fullmatch(ref):
        return ref
    proc = subprocess.run([docker, "image", "inspect", "--format", "{{.Id}}", ref],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, f"AGENT_TOOLING_TEST_IMAGE={ref!r} is not a local image\n" + explain(proc)
    return proc.stdout.strip()


class Project:
    def __init__(self, docker: str, world: World):
        self.docker, self.world = docker, world
        self.env = docker_env(world.home)
        self.base = [docker, "compose", "--project-directory", str(world.root), "-p", world.project]
        for f in compose_files(world.root):
            self.base += ["-f", str(f)]

    def compose(self, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
        return subprocess.run([*self.base, *args], cwd=self.world.root, env=self.env,
                              capture_output=True, text=True, timeout=timeout)

    def docker_run(self, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run([self.docker, *args], env=self.env, capture_output=True,
                              text=True, timeout=timeout)

    def down(self) -> None:
        self.compose("down", "--volumes", "--remove-orphans", "--timeout", "10", timeout=300)
        label = f"label=com.docker.compose.project={self.world.project}"
        leftovers = self.docker_run("ps", "-aq", "--filter", label).stdout.split()
        if leftovers:
            self.docker_run("rm", "-f", *leftovers)
        networks = self.docker_run("network", "ls", "-q", "--filter", label).stdout.split()
        if networks:
            self.docker_run("network", "rm", *networks)

    def wait_healthy(self, container: str) -> None:
        deadline = time.monotonic() + HEALTH_TIMEOUT
        state: dict = {}
        while time.monotonic() < deadline:
            proc = self.docker_run("inspect", "--format", "{{json .State}}", container)
            assert proc.returncode == 0, explain(proc)
            state = json.loads(proc.stdout)
            health = state.get("Health")
            if health is None:
                pytest.fail(f"tooling container has no health check; state={state}")
            if health.get("Status") == "healthy":
                return
            if state.get("Status") in ("exited", "dead"):
                break
            time.sleep(2)
        logs = self.docker_run("logs", "--tail", "50", container)
        pytest.fail(f"tooling did not reach healthy within {HEALTH_TIMEOUT}s; state={state}\n"
                    f"logs={logs.stdout[-2000:]}{logs.stderr[-2000:]}")


@pytest.mark.image
def test_empty_root_end_to_end(request):
    ref = os.environ.get("AGENT_TOOLING_TEST_IMAGE", "").strip()
    if not ref:
        pytest.fail("AGENT_TOOLING_TEST_IMAGE is unset. P5 was selected (-m image) and needs a "
                    "built product image (or today's full image); this is a failure, not a skip.")
    request.getfixturevalue("installer")
    docker = shutil.which("docker")
    if not docker:
        pytest.fail("P5 needs the docker CLI; none is on PATH")

    uid = os.getuid() or 10001
    gid = os.getgid() or 10001
    run_id = uuid.uuid4().hex[:10]
    # A never-reused directory under TMPDIR rather than under pytest's basetemp: Docker
    # Desktop's file sharing served a stale "bind source path does not exist" for a
    # directory that a reused --basetemp had deleted and recreated at the same path.
    parent = Path(tempfile.mkdtemp(prefix=f"s3-p5-{run_id}-")).resolve()
    print(f"P5 working directory: {parent}")
    world = World.create(parent, components=("tooling",), uid=uid, gid=gid,
                         board_port=_free_port(), project=f"s3p5-{run_id}")
    world = replace(world, image=_resolve_image(docker, ref, docker_env(world.home)))
    assert list(world.root.iterdir()) == [], "P5 must start from an empty root"

    digest = world.plan_hash()
    applied = world.apply(expected=digest)
    assert applied.returncode == 0, explain(applied)
    # Meet (Coordinator ruling, T9b Amendment 6): under P1b a role refuses an unprepared store,
    # so the documented sequence runs `prepare` between `apply` and `up`.
    from t9b_harness import prepare as _prepare, prepared as _prepared
    readied = _prepare(world)
    assert _prepared(readied) is None, explain(readied)

    match = serve_exec(command_vectors(read_snippets(world.root)["host/claude-mcp.json"]))
    assert match, "host/claude-mcp.json has no docker exec -i … serve command"
    argv, container = match

    project = Project(docker, world)
    session = None
    try:
        up = project.compose("up", "-d", "tooling")
        assert up.returncode == 0, "docker compose up -d tooling failed\n" + explain(up)
        cid = project.compose("ps", "-q", "tooling").stdout.strip()
        assert cid, "compose reports no tooling container"
        project.wait_healthy(cid)

        named = project.docker_run("inspect", "--format", "{{.Id}}", container)
        assert named.returncode == 0 and named.stdout.strip().startswith(cid), (
            f"the rendered snippet's container {container!r} is not this project's tooling "
            f"container {cid}\n" + explain(named))

        session = StdioSession([docker, *argv[1:]], project.env)
        init = session.request(1, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "s3-p5-contract", "version": "1"}})
        assert "result" in init and init["result"].get("serverInfo"), f"initialize failed: {init}"
        session.notify("notifications/initialized")

        listed = session.request(2, "tools/list", {})
        names = [t.get("name") for t in (listed.get("result") or {}).get("tools", [])]
        assert "navigation.search" in names, f"tools/list lacks navigation.search: {names}"

        called = session.request(3, "tools/call", {"name": "navigation.search", "arguments": {
            "repo_key": "fixture", "target_revision": world.head, "query": NEEDLE, "mode": "literal"}})
        result = called.get("result") or {}
        assert not result.get("isError") and "error" not in called, f"navigation.search failed: {called}"
        payload = result.get("structuredContent")
        if payload is None:
            payload = json.loads(result["content"][0]["text"])
        rows = payload.get("results") or []
        assert any(NEEDLE in str(row.get("excerpt", "")) for row in rows), (
            f"navigation.search at HEAD {world.head} found no result for {NEEDLE!r}: {payload}")
    finally:
        if session is not None:
            session.close()
        project.down()
    remaining = project.docker_run("ps", "-aq", "--filter",
                                   f"label=com.docker.compose.project={world.project}").stdout.split()
    assert not remaining, f"P5 left containers behind: {remaining}"
    shutil.rmtree(parent, ignore_errors=True)  # kept for inspection when any step failed
