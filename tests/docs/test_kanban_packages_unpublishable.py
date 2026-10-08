"""Order D0f (meet, Verification's finding): no package under apps/kanban can be published to npm.

The board's own package.json declares the board's dependencies, and through @clinebot/llms they
reach ai-sdk-provider-claude-code and @anthropic-ai/claude-agent-sdk. An `npm publish` from
apps/kanban would therefore publish a package that pulls the SDK, which is the redistribution D0f
exists to prevent. Upstream's `"publishConfig": {"access": "public", "provenance": true}` made
that one command away. The board ships only inside this repository's images, so every
package.json Git tracks under apps/kanban (outside node_modules) must be `"private": true` and
carry no `publishConfig`. npm refuses to publish a private package.

GREEN-IF every tracked apps/kanban package.json is private and has no publishConfig, and at least
the board's own package.json was read (a control, so an empty file list cannot pass).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BOARD = "apps/kanban/package.json"


def _package_files() -> list[str]:
    try:
        listed = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", "apps/kanban"],
                                capture_output=True, check=True).stdout.decode().split("\0")
    except (OSError, subprocess.CalledProcessError):  # outside a Git checkout: every file on disk
        listed = [str(p.relative_to(ROOT)) for p in (ROOT / "apps" / "kanban").rglob("package.json")]
    return sorted(name for name in listed
                  if name.endswith("package.json") and "/node_modules/" not in f"/{name}")


def test_every_kanban_package_is_private_and_has_no_publish_config():
    files = _package_files()
    assert BOARD in files, f"control: {BOARD} was not among the files read: {files}"
    problems = []
    for name in files:
        manifest = json.loads((ROOT / name).read_text(encoding="utf-8"))
        if manifest.get("private") is not True:
            problems.append(f'{name}: "private" is {manifest.get("private")!r}, not true')
        if "publishConfig" in manifest:
            problems.append(f"{name}: carries publishConfig {manifest['publishConfig']!r}")
    assert not problems, "a Kanban package could be published to npm:\n" + "\n".join(problems)
