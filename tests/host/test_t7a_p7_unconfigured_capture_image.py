"""T7a P7 (image-marked): host launches survive an unconfigured capture.

Order: docs/work/orders/T7a-role-readiness.md, P7.
With `capture` selected but `config/capture/*` absent, the capture container stays
up. Spool ingestion keeps running and `kp-agent-host launch` works in docker mode.
Falsifier: the capture container exits, or a docker-mode launch fails, only because
workspace capture is unconfigured.

The test renders a real runtime root (`kp-agent-install`, components tooling and
capture, the two native transcript roots under a scratch HOME) for the image named by
AGENT_TOOLING_TEST_IMAGE and starts it with the documented `docker compose
--project-directory "$root" up -d`. It writes no `config/capture/*` file. It then
follows docs/HOST-ADAPTER.md "Docker runtime (installer)" steps 2 and 3 (the registry
operator config at `$root/config/launch/registry.json`, a descriptor naming
`/config/launch/harness-profiles.json`), creates one desk through
`docker exec -i <project>-capture kp-agent-desk-registry`, and runs
`kp-agent-host --config $root/host/kp-agent-host.json launch claude --desk ...` from the
repository with a fake `claude` first on PATH (tests/host/host_harness.py; the real
CLI is never run). The fake fires one `Stop` hook after writing a transcript turn.
The capture container's ingestion must then make that turn searchable through the
memory MCP server the launch generated (`docker exec -i <project>-capture kp-agent-memory ...`).

Readings (repeated in the arm report under AMBIGUITY):
- "config/capture/* absent" also admits empty placeholders the installer may create (P1);
- the registry state directory is created by the test before `initialize` (P2 owns
  whether an operator must do that); the roster file is left absent, so the default
  roster applies (docs/DESK-MENU.md);
- "spool ingestion keeps running" is observed as the launch's hook event being
  ingested and captured under the desk within 180 s.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from host_harness import BIN, claude_payload, claude_rows, claude_transcript, step, write_fake
from s3_harness import docker_env, explain
from t3_harness import mcp_session, search_hits
from t7a_harness import (Runtime, image_world, required_docker, transcript_roots, unconfigured_files,
                         with_transcript_roots, write_private)

pytestmark = pytest.mark.image

# T9b: stores live in the project volume under /state/memory, created in the runtime.
STATE_ROOT = "/state/memory/launch-registry"
INGEST_TIMEOUT = 180.0


def _launch_registry(world) -> None:
    """docs/HOST-ADAPTER.md "To use docker mode", steps 2-3; docs/DESK-MENU.md "Portable registry"."""
    launch = world.root / "config" / "launch"
    write_private(launch / "desks.json", json.dumps({
        "schema_version": "agent-tooling.desk-registry.v1", "tenant_id": "t7a-p7",
        "roster_path": f"{STATE_ROOT}/roles.json",  # T9b: the roster is a store, under /state/memory
        "harness_profiles_path": "/config/launch/harness-profiles.json"}))
    write_private(launch / "registry.json", json.dumps({
        "schema_version": "ops.desk-memory.local.v1", "state_root": STATE_ROOT,
        "catalog_path": "/config/launch/desks.json", "workspace_root": "/config/launch",
        "provider_instance": "host-launcher", "provider_session_id": "operator-console"}))
    made = subprocess.run([required_docker(), "compose", "--project-directory", str(world.root), "run", "--rm", "-T",
                           "tooling", "sh", "-c", f"mkdir -p -m 700 {STATE_ROOT} && chmod 700 {STATE_ROOT}"],
                          cwd=world.root, env=docker_env(world.home), capture_output=True, text=True, timeout=600)
    assert made.returncode == 0, "the in-runtime creation of the registry state failed\n" + explain(made)


def _registry(runtime: Runtime, container: str, action: str, payload=None):
    proc = runtime.exec(container, "kp-agent-desk-registry", "--config", "/config/launch/registry.json", action,
                        stdin=None if payload is None else json.dumps(payload))
    assert proc.returncode == 0, f"kp-agent-desk-registry {action} failed in {container}\n" + explain(proc)
    return json.loads(proc.stdout)


def _search(server: dict, env: dict, marker: str) -> int:
    try:
        _, replies = mcp_session(server["command"], server.get("args", []), env,
                                 calls=[("memory.search", {"query": marker})], list_tools=False)
        return len(search_hits(replies[0], marker))
    except Exception:  # polled while ingestion runs; the final assertion reports the outcome
        return 0


def test_docker_mode_launch_and_ingestion_survive_unconfigured_capture():
    """GREEN-IF the capture container stays up without config/capture/*, a docker-mode Claude launch
    execs the CLI, and its Stop hook event is ingested and captured under the desk."""
    docker = required_docker()
    world = image_world("p7", ("tooling", "capture"))
    argv = with_transcript_roots(world, world.args())
    applied = world.apply(argv, world.plan_hash(argv))
    assert applied.returncode == 0, explain(applied)
    # Meet (Coordinator ruling, T9b Amendment 6): under P1b a role refuses an unprepared store,
    # so the documented sequence runs `prepare` between `apply` and `up`.
    from t9b_harness import prepare as _prepare, prepared as _prepared
    readied = _prepare(world)
    assert _prepared(readied) is None, explain(readied)
    assert unconfigured_files(world.root, "capture"), "precondition: config/capture/* is absent or empty"
    _launch_registry(world)
    adapter = world.root / "host" / "kp-agent-host.json"
    container = json.loads(adapter.read_text())["runtime"]["container"]

    runtime = Runtime(docker, world)
    try:
        up = runtime.compose("up", "-d")
        assert up.returncode == 0, "up -d failed with capture unconfigured\n" + explain(up)
        runtime.assert_stays_up(("tooling", "capture"), window=20.0)

        _registry(runtime, container, "initialize")
        roles = _registry(runtime, container, "roles")
        role = (roles.get("roles") if isinstance(roles, dict) else roles)[0]["role_id"]
        desk_id = "desk:" + str(uuid.uuid4())
        _registry(runtime, container, "save", {
            "desk_id": desk_id, "name": "P7 desk", "description": "Host launches under an unconfigured capture.",
            "role": role, "repos": ["fixture"], "capture": True, "memory_write": True, "expected_version": 0})

        fakes = world.base / "fakes"
        write_fake(fakes, "claude")
        records = world.base / "records"
        records.mkdir()
        marker = "Juniper p7 marker " + uuid.uuid4().hex[:8]
        claude_root = transcript_roots(world)[0]
        transcript = claude_transcript(claude_root, world.repo, "@SESSION@")
        plan = [step("stop", "Stop", claude_payload("@SESSION@", transcript, world.repo, "Stop"),
                     write=(transcript, claude_rows("@SESSION@", world.repo, f"Remember {marker}.",
                                                    f"Recorded {marker}.")))]
        (records / "plan.json").write_text(json.dumps(plan))
        env = dict(runtime.env)
        env["PATH"] = os.pathsep.join([str(fakes), str(BIN), env.get("PATH", "/usr/bin:/bin")])
        env.update(T4_FAKE_PLAN=str(records / "plan.json"), T4_FAKE_RECORD=str(records / "record.json"))
        done = subprocess.run([str(BIN / "kp-agent-host"), "--config", str(adapter), "launch", "claude",
                               "--desk", desk_id, "--", "--verbose"], cwd=world.repo, env=env,
                              capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
        assert done.returncode == 0, ("kp-agent-host launch failed in docker mode with capture unconfigured\n"
                                      f"exit={done.returncode}\nstdout={done.stdout[-3000:]}\n"
                                      f"stderr={done.stderr[-3000:]}")
        record_path = records / "record.json"
        assert record_path.exists(), "launch exited 0 without executing the claude CLI"
        record = json.loads(record_path.read_text())
        assert not record["errors"], record["errors"]
        hooks = [(run["event"], run["code"], run["stderr"][-500:]) for run in record["runs"]]
        assert hooks and all(code == 0 for _, code, _ in hooks), f"the Stop hook did not append to the spool: {hooks}"

        servers = [s for s in (record["config"].get("mcp_servers") or {}).values()
                   if Path(str(s.get("command", ""))).name == "docker"]
        assert servers, f"the launch generated no docker-mode memory server: {record['config'].get('mcp_servers')}"
        deadline = time.monotonic() + INGEST_TIMEOUT
        hits = 0
        while time.monotonic() < deadline:
            hits = _search(servers[0], env, marker)
            if hits:
                break
            time.sleep(3)
        assert hits >= 1, (f"the Stop event was not ingested and captured within {INGEST_TIMEOUT}s: spool "
                           f"ingestion is not running in {container}\ncapture logs:\n"
                           f"{runtime.logs(runtime.container('capture'))[-3000:]}")
        runtime.assert_stays_up(("tooling", "capture"), window=4.0)
    finally:
        remaining = runtime.down()
    assert not remaining, f"the test left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)  # kept for inspection when any step failed
