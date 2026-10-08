"""O3 A1: the default plan never selects OpenCode (a guard, GREEN at the order's base).

Order: docs/work/orders/O3-opencode-optional-image.md, Q2 ("never selected unless asked") and A1
(Verification): "A plan with the default components, and one with the live-like set (tooling, refresh,
capture, board, indexer), render no `opencode` service or profile and the same `COMPOSE_PROFILES` as
today." The census found no default-plan golden; this file is that pin. It also serves the later
summarizer slice's "never selected unless asked": a role that a plan selects without being asked
changes the pinned `COMPOSE_PROFILES`.

How the plan is made. `kp-agent-install plan` runs in-process (`kp_agent_tooling.install_cli.main`),
with a temporary runtime root, home and committed fixture repository under the test's temporary
directory, a synthetic digest, and uid/gid 10001. "Default" passes no `--components` (the CLI
default); "live-like" passes `--components tooling,refresh,capture,board,indexer`. The rendered
bytes are re-derived with the function the plan renders with (`runtime_install.render` over
`runtime_install.manifest()`), and each one must hash to the `sha256` the printed plan records for
it, so the bytes read here are the bytes the plan commits to.

Base measurement (this order's base, main after T12c and the O1 freeze; run in-process as above):

    default    exit 0; inputs.components ['tooling']; compose.profiles [];
               .env COMPOSE_PROFILES=''
    live-like  exit 0; inputs.components ['tooling', 'refresh', 'capture', 'board', 'indexer'];
               compose.profiles ['refresh', 'capture', 'board', 'indexer'];
               .env COMPOSE_PROFILES='refresh,capture,board,indexer'
    both       rendered compose files compose.yaml, compose.workspaces.yaml, compose.capture.yaml;
               services tooling, refresh, capture, board, indexer, tempo, collector; declared
               profiles refresh, capture, board, indexer, telemetry; no `opencode` anywhere in them.

GREEN-IF, for each of the two plans:
- `test_plan_renders_no_opencode_service_or_profile`: the plan exits 0 (a manifest the installer
  refuses is not a plan), and no service of any rendered compose file, and no profile any of them
  declares, is named with `opencode` (any case); nor does `COMPOSE_PROFILES` or the plan's
  `compose.profiles` name it.
- `test_plan_compose_profiles_are_todays`: the plan exits 0, its `inputs.components` and
  `compose.profiles` equal the pinned lists, and the rendered `.env` sets `COMPOSE_PROFILES`
  exactly once, to the pinned value.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from kp_agent_tooling import install_cli
from kp_agent_tooling._impl import runtime_install

# The two plans A1 names, and today's values (measured at the order's base; see the docstring).
DEFAULT = "default"
LIVE_LIKE = "live-like"
COMPONENTS_ARGUMENT = {DEFAULT: None, LIVE_LIKE: "tooling,refresh,capture,board,indexer"}
TODAY_COMPONENTS = {
    DEFAULT: ["tooling"],
    LIVE_LIKE: ["tooling", "refresh", "capture", "board", "indexer"],
}
TODAY_PROFILES = {
    DEFAULT: [],
    LIVE_LIKE: ["refresh", "capture", "board", "indexer"],
}
TODAY_COMPOSE_PROFILES = {
    DEFAULT: "",
    LIVE_LIKE: "refresh,capture,board,indexer",
}

OPENCODE = re.compile(r"opencode", re.IGNORECASE)
DIGEST = "sha256:" + hashlib.sha256(b"o3-a1-default-plan-pin").hexdigest()
CONTAINER_ID = "10001"
_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "O3 A1",
    "GIT_AUTHOR_EMAIL": "o3-a1@example.invalid",
    "GIT_COMMITTER_NAME": "O3 A1",
    "GIT_COMMITTER_EMAIL": "o3-a1@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], env=dict(os.environ, **_GIT_ENV),
                   capture_output=True, text=True, check=True)


def _committed_repository(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("O3 A1 fixture repository.\n")
    _git(path, "add", "-A")
    _git(path, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "O3 A1 fixture")
    return path.resolve()


def _plan(base: Path, which: str) -> tuple[int, dict | None, str]:
    """`kp-agent-install plan` in-process: its exit code, its JSON (or None) and its raw output."""
    root, home = base / "root", base / "home"
    root.mkdir()
    home.mkdir()
    repository = _committed_repository(base / "repos" / "fixture")
    argv = ["plan", "--runtime-root", str(root.resolve()), "--image", DIGEST,
            "--repository", f"fixture={repository}", "--home", str(home.resolve()),
            "--uid", CONTAINER_ID, "--gid", CONTAINER_ID]
    if COMPONENTS_ARGUMENT[which] is not None:
        argv += ["--components", COMPONENTS_ARGUMENT[which]]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = install_cli.main(argv)
    text = out.getvalue()
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    return code, document, text


def _rendered(document: dict) -> dict[str, bytes]:
    """The plan's files as bytes, each checked against the sha256 the printed plan records."""
    files, *_ = runtime_install.render(document["inputs"], runtime_install.manifest()[0])
    recorded = document["files"]
    assert sorted(files) == sorted(recorded), (
        f"re-rendered files {sorted(files)} differ from the plan's {sorted(recorded)}")
    for name, data in files.items():
        assert hashlib.sha256(data).hexdigest() == recorded[name]["sha256"], (
            f"re-rendered {name} does not hash to the plan's sha256; the bytes read would not be the plan's")
    return files


def _ok_plan(tmp_path: Path, which: str) -> dict:
    code, document, text = _plan(tmp_path, which)
    assert code == 0 and isinstance(document, dict) and "plan_sha256" in document, (
        f"the {which} plan did not succeed (exit {code}):\n{text[-3000:]}")
    return document


def _compose_profiles_lines(env_bytes: bytes) -> list[str]:
    return [line for line in env_bytes.decode().splitlines() if line.startswith("COMPOSE_PROFILES=")]


def _unquote(value: str) -> str:
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"" else value


@pytest.mark.parametrize("which", [DEFAULT, LIVE_LIKE])
def test_plan_renders_no_opencode_service_or_profile(tmp_path, which):
    document = _ok_plan(tmp_path, which)
    files = _rendered(document)
    root = document["inputs"]["runtime_root"]
    compose_files = [str(Path(path).relative_to(root)) for path in document["compose"]["files"]]
    assert compose_files and all(name in files for name in compose_files), (
        f"the plan's compose files {document['compose']['files']} are not among its rendered files")
    found = []
    for name in compose_files:
        model = yaml.safe_load(files[name]) or {}
        for service, body in sorted((model.get("services") or {}).items()):
            if OPENCODE.search(str(service)):
                found.append(f"{name}: service {service!r}")
            for profile in (body or {}).get("profiles") or ():
                if OPENCODE.search(str(profile)):
                    found.append(f"{name}: service {service!r} declares profile {profile!r}")
    for line in _compose_profiles_lines(files[".env"]):
        if OPENCODE.search(line):
            found.append(f".env: {line}")
    for profile in document["compose"]["profiles"]:
        if OPENCODE.search(profile):
            found.append(f"plan compose.profiles: {profile!r}")
    assert not found, f"the {which} plan renders OpenCode:\n" + "\n".join(found)


@pytest.mark.parametrize("which", [DEFAULT, LIVE_LIKE])
def test_plan_compose_profiles_are_todays(tmp_path, which):
    document = _ok_plan(tmp_path, which)
    files = _rendered(document)
    assert document["inputs"]["components"] == TODAY_COMPONENTS[which], (
        f"the {which} plan selects {document['inputs']['components']}; today it selects "
        f"{TODAY_COMPONENTS[which]}")
    assert document["compose"]["profiles"] == TODAY_PROFILES[which], (
        f"the {which} plan's compose.profiles is {document['compose']['profiles']}; today it is "
        f"{TODAY_PROFILES[which]}")
    lines = _compose_profiles_lines(files[".env"])
    assert len(lines) == 1, f"the {which} plan's .env sets COMPOSE_PROFILES {len(lines)} times: {lines}"
    value = _unquote(lines[0].split("=", 1)[1])
    assert value == TODAY_COMPOSE_PROFILES[which], (
        f"the {which} plan renders COMPOSE_PROFILES={value!r}; today it is {TODAY_COMPOSE_PROFILES[which]!r}")
