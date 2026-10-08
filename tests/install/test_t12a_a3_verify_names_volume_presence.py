"""T12a A3 (K2 legibility): with the image absent, `verify` names whether the store volume is present.

Order: docs/work/orders/T12a-capture-survives.md, A3, frozen at its merge commit:
"When the image is absent, `verify`'s `memory_store.reasons` also lists whether the volume is present.
Its status is unchanged."

In-process: the `kp-agent-install` entry point (`kp_agent_tooling.install_cli.main`) runs `verify` on an
applied S3 world (tests/install/s3_harness.py), with PATH holding only a fake `docker` executable. The fake
answers as a reachable daemon on which the receipt's image is absent and the volume `<project>_memory`
either exists (carrying the Compose labels `prepare` gives it) or does not. It never creates, runs, pulls
or removes anything; every call is logged and the test checks that.

Readings (repeated in the arm report under AMBIGUITY):
- "lists whether the volume is present": a clause of an entry of `readiness.memory_store.reasons` (entries
  split at `;`, `, ` and `. `) names the volume `<project>_memory` and says it is present (`exists` or
  `present`, without a negation) when it exists, or absent (`does not exist`, `absent`, `not present`,
  `missing` or `no such volume`) when it does not;
- "its status is unchanged": `readiness.memory_store.status` stays `not_observed`, as at base for an absent
  image, and `verify` still exits 0 with top-level status `verified`;
- the fake answers `version`, `image inspect`, `volume inspect` (with or without `--format`), `volume ls`
  and `ps`, which covers the ways the installer observes Docker at base; any other call fails (exit 1),
  is logged, and fails the test only if it is a write (`create`, `run`, `pull`, `rm`, `volume create`).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

from s3_harness import World

FAKE_DOCKER = r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["T12A_FAKE_DOCKER_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
volume = os.environ["T12A_FAKE_VOLUME"]
present = os.environ["T12A_FAKE_VOLUME_PRESENT"] == "1"
labels = {"com.docker.compose.project": os.environ["T12A_FAKE_PROJECT"], "com.docker.compose.volume": "memory"}
fmt = args[args.index("--format") + 1] if "--format" in args else None
names = [a for a in args[2:] if not a.startswith("-") and a != fmt]

def fail(message):
    sys.stderr.write(message + "\n")
    sys.exit(1)

if args[:1] == ["version"]:
    print("29.5.3")
elif args[:2] == ["image", "inspect"]:
    fail("Error response from daemon: No such image: " + (names[0] if names else ""))
elif args[:2] == ["volume", "inspect"]:
    if not present or (names and volume not in names):
        fail(f"Error response from daemon: get {names[0] if names else volume}: no such volume")
    body = {"Name": volume, "Driver": "local", "Labels": labels, "Scope": "local",
            "Mountpoint": f"/var/lib/docker/volumes/{volume}/_data"}
    if fmt is None:
        print(json.dumps([body]))
    elif ".Labels" in fmt:
        print(json.dumps(labels))
    elif "{{json .}}" in fmt:
        print(json.dumps(body))
    elif ".Name" in fmt:
        print(volume)
    else:
        print(json.dumps(body))
elif args[:2] == ["volume", "ls"]:
    if present:
        print(volume if (fmt or "-q" in args or "--quiet" in args) else f"local     {volume}")
elif args[:1] == ["ps"]:
    pass
else:
    fail("fake docker: unsupported call " + json.dumps(args))
'''

WRITES = (("run",), ("create",), ("pull",), ("rm",), ("volume", "create"), ("volume", "rm"), ("container",),
          ("compose",), ("start",), ("kill",), ("stop",))
PRESENT = re.compile(r"\b(exists|present)\b", re.IGNORECASE)
ABSENT = re.compile(r"does not exist|doesn't exist|\babsent\b|not present|\bmissing\b|no such volume",
                    re.IGNORECASE)
CLAUSE = re.compile(r";|\.\s|,\s")


def _clauses(reasons, volume):
    """The clauses (of each reason, split at `;`, `, ` and `. `) that name the volume."""
    return [part for reason in reasons for part in CLAUSE.split(reason) if volume in part]


def _fake_docker(base: Path, project: str, present: bool, monkeypatch) -> Path:
    bin_dir = base / "fake-docker-bin"
    bin_dir.mkdir()
    program = base / "fake_docker.py"
    program.write_text(FAKE_DOCKER)
    docker = bin_dir / "docker"
    docker.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{program}" "$@"\n')
    docker.chmod(0o755)
    log = base / "fake-docker.log"
    log.write_text("")
    monkeypatch.setenv("PATH", str(bin_dir))
    for key in [k for k in os.environ if k.startswith("DOCKER_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("T12A_FAKE_DOCKER_LOG", str(log))
    monkeypatch.setenv("T12A_FAKE_VOLUME", f"{project}_memory")
    monkeypatch.setenv("T12A_FAKE_PROJECT", project)
    monkeypatch.setenv("T12A_FAKE_VOLUME_PRESENT", "1" if present else "0")
    return log


def _verify(world: World, present: bool, monkeypatch, capsys):
    from kp_agent_tooling import install_cli

    world.apply_ok()
    log = _fake_docker(world.base, world.project, present, monkeypatch)
    capsys.readouterr()
    code = install_cli.main(["verify", "--runtime-root", str(world.root)])
    out = capsys.readouterr().out
    report = json.loads(out)
    calls = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return code, report, calls, out


def _check_unchanged(code, report, calls, out):
    store = (report.get("readiness") or {}).get("memory_store") or {}
    story = f"\nexit={code} memory_store={json.dumps(store, indent=1)}\ndocker calls={calls}"
    assert code == 0 and report.get("status") == "verified", "verify's exit code or status changed" + story
    assert store.get("status") == "not_observed", "the store's status changed from `not_observed`" + story
    writes = [call for call in calls if any(call[:len(w)] == list(w) for w in WRITES)]
    assert not writes, f"verify wrote to Docker: {writes}" + story
    assert any(call[:2] == ["image", "inspect"] for call in calls), (
        "precondition: verify did not look for the image through the fake docker" + story)
    reasons = store.get("reasons")
    assert isinstance(reasons, list) and all(isinstance(r, str) for r in reasons), "reasons is not a list" + story
    return reasons, story


def test_a3_verify_names_a_present_volume_when_the_image_is_absent(world, monkeypatch, capsys):
    """GREEN-IF, with the image absent and the volume present, `verify` exits 0, status `verified`, the store
    stays `not_observed`, and one reason names `<project>_memory` as present."""
    code, report, calls, out = _verify(world, True, monkeypatch, capsys)
    reasons, story = _check_unchanged(code, report, calls, out)
    volume = f"{world.project}_memory"
    named = [c for c in _clauses(reasons, volume) if PRESENT.search(c) and not ABSENT.search(c)]
    assert named, f"no reason says that the volume {volume} is present" + story


def test_a3_verify_names_an_absent_volume_when_the_image_is_absent(world, monkeypatch, capsys):
    """GREEN-IF, with the image absent and no volume, `verify` exits 0, status `verified`, the store stays
    `not_observed`, and one reason names `<project>_memory` as absent."""
    code, report, calls, out = _verify(world, False, monkeypatch, capsys)
    reasons, story = _check_unchanged(code, report, calls, out)
    volume = f"{world.project}_memory"
    named = [c for c in _clauses(reasons, volume) if ABSENT.search(c)]
    assert named, f"no reason says that the volume {volume} is absent" + story
