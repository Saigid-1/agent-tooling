"""T12b B2 (image-marked): the indexer's health stays healthy during a catch-up whose watermark advances.

Order: docs/work/orders/T12b-one-indexer-outbox.md, B2, `index_lag` (T4): "the `indexer` role's health (r5):
unhealthy when the watermark has not advanced across 5 consecutive non-skipped drains while outbox rows exist. ...
Steady inflow with a moving watermark is healthy, so a post-deploy catch-up never reads as unhealthy." Falsifier
(r5): "an indexer whose health reads unhealthy during a catch-up in which the watermark advances".

Setup: a tooling + capture install (the `indexer` comes with capture), planned, applied, prepared and up. The probe
(tests/install/t12b_role_probe.py `setup`, in the capture container) builds a desk-memory store in the volume, and
its config is written as the capture role's session config and as every /config file the rendered `indexer` command
names after a `--...config...` option, so the indexer drains it however it finds its stores (a named config, or the
stores in the volume). Then: the indexer is stopped, a backlog of
BACKLOG seals is written (probe `seal`, capture container), the indexer is started, and for WINDOW seconds a steady
inflow (INFLOW seals every second, capture container) runs while the test samples, about once a second, the outbox
and indexed counts (probe `lag`) and the indexer's own health check (the rendered `indexer` healthcheck command, run
with `docker exec`; Docker's health status too).

GREEN-IF every health sample after the indexer started exits 0, its health record's `status` (the last output line,
JSON) is `healthy`, and Docker never reports `unhealthy`, while the samples show the catch-up (outbox rows present at
some health sample) and the watermark moving (the indexed count rises across the window). (Meet: the healthcheck is
CMD-SHELL, run through `sh -c`; the reading is the record's `status`, never a word search, because a healthy record
carries its own rule text.)
Readings (AMBIGUITY): "the indexer role's health" is its healthcheck command as the rendered manifest declares it (the
`x-health` default, `tooling-container health`, when it declares none); the store it drains is the capture role's
session config's store, or one its command names by a config option, or one it finds in the volume.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import PurePosixPath

import pytest

from s3_harness import explain
from t7a_harness import write_private
from t9b_harness import install
from t12b_harness import INDEXER, Project, image_world, indexer_service, run_probe

pytestmark = pytest.mark.image

COMPONENTS = ("tooling", "capture")
DIRECTORY = "/state/memory/t12b-health"
CAPTURE_CONFIG = "/config/capture/session.json"
BACKLOG = 1500
INFLOW = 5
WINDOW = 30.0


def _health_status(stdout: str):
    """The indexer health record's `status` (its last output line, JSON), or None when it does not parse.

    Meet seam fix: a healthy record carries its own rule text ("unhealthy while ...") and a `stalled` key, so the
    reading is the record's `status` field, never a word search over the output."""
    lines = [line for line in (stdout or "").splitlines() if line.strip()]
    try:
        record = json.loads(lines[-1]) if lines else None
    except ValueError:
        return None
    return record.get("status") if isinstance(record, dict) else None


def _operator_configs(command) -> list[str]:
    """Where the probe's desk-memory config is written, so the indexer drains the probe's store: every /config path
    the indexer's command names after a `--...config...` option, and the capture role's session config (the store a
    tooling+capture install seals into). An indexer that discovers the stores in the volume finds the probe's store
    there."""
    found = [CAPTURE_CONFIG]
    for position, token in enumerate(command[:-1]):
        value = command[position + 1]
        if str(token).startswith("--") and "config" in str(token) and str(value).startswith("/config/"):
            found.append(str(value))
    return sorted(set(found))


def _health_argv(service) -> list[str]:
    test = (service.get("healthcheck") or {}).get("test") or ["CMD", "tooling-container", "health"]
    if isinstance(test, str):
        return ["sh", "-c", test]
    if test[0] == "CMD-SHELL":
        # Docker runs a CMD-SHELL test through the container's shell (`/bin/sh -c`); its string is one shell command,
        # never an executable name (meet seam fix: the rendered indexer healthcheck is CMD-SHELL with `&&`).
        return ["sh", "-c", " ".join(test[1:])]
    return list(test[1:]) if test[0] == "CMD" else list(test)


def test_indexer_health_stays_healthy_through_a_catch_up_whose_watermark_advances():
    world, docker = image_world("health", COMPONENTS)
    project = Project(docker, world)
    stop = threading.Event()
    try:
        install(world)
        up = project.compose("up", "-d")
        assert up.returncode == 0, "`docker compose --project-directory \"$root\" up -d` failed\n" + explain(up)
        service = indexer_service(project)
        cids = project.wait_running(("tooling", "capture", INDEXER))
        ready = run_probe(project, cids["capture"], "setup", DIRECTORY)
        assert "error" not in ready, f"precondition: the probe store: {ready}"
        probe_config = json.loads(project.exec(cids["capture"], "cat", f"{DIRECTORY}/probe-a.json").stdout)
        for config in _operator_configs(service.get("command") or []):
            write_private(world.root / "config" / PurePosixPath(config).relative_to("/config"), json.dumps(probe_config))
        stopped = project.compose("stop", INDEXER)
        assert stopped.returncode == 0, explain(stopped)
        backlog = run_probe(project, cids["capture"], "seal", DIRECTORY, str(BACKLOG), timeout=900)
        assert "error" not in backlog and backlog["outbox"] >= BACKLOG, f"precondition: the backlog: {backlog}"
        started = project.compose("start", INDEXER)
        assert started.returncode == 0, explain(started)
        indexer = project.container(INDEXER)
        health_argv = _health_argv(service)

        def inflow():
            while not stop.is_set():
                run_probe(project, cids["capture"], "seal", DIRECTORY, str(INFLOW))
                stop.wait(1.0)
        feeder = threading.Thread(target=inflow, daemon=True)
        feeder.start()
        samples = []
        deadline = time.monotonic() + WINDOW
        while time.monotonic() < deadline:
            lag = run_probe(project, cids["capture"], "lag", DIRECTORY)
            health = subprocess.run([docker, "exec", indexer, *health_argv], env=project.env, capture_output=True,
                                    text=True, timeout=60)
            docker_health = project.docker_run("inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}"
                                               "{{end}}", indexer).stdout.strip()
            samples.append({"t": round(WINDOW - (deadline - time.monotonic()), 1), "outbox": lag.get("outbox"),
                            "indexed": lag.get("indexed"), "exit": health.returncode,
                            "status": _health_status(health.stdout),
                            "health": (health.stdout or health.stderr).strip()[-300:], "docker": docker_health})
            time.sleep(0.5)
        stop.set()
        feeder.join(120)
        shown = "\n".join(json.dumps(s) for s in samples)
        bad = [s for s in samples if s["exit"] != 0 or s["status"] != "healthy" or s["docker"] == "unhealthy"]
        catching_up = [s for s in samples if (s["outbox"] or 0) > 0]
        indexed = [s["indexed"] for s in samples if s["indexed"] is not None]
        print(f"T12b health: {len(samples)} samples, {len(catching_up)} with outbox rows, indexed "
              f"{indexed[:1]} -> {indexed[-1:]}, health exits {sorted({s['exit'] for s in samples})}, docker "
              f"{sorted({s['docker'] for s in samples})}")
        assert catching_up, f"precondition: no health sample saw outbox rows (no catch-up observed):\n{shown}"
        assert indexed and indexed[-1] > indexed[0], f"precondition: the watermark did not move:\n{shown}"
        assert not bad, (f"the indexer's health read unhealthy during a catch-up whose watermark advances "
                         f"({len(bad)} of {len(samples)} samples):\n{shown}\nindexer log:\n"
                         f"{project.logs(indexer)[-3000:]}")
    finally:
        stop.set()
        remaining = project.down()
    assert not remaining, f"left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)
