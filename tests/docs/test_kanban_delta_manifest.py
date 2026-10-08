"""Order D0a-3 (A4, round 2): the Kanban change record is current, checked offline.

apps/kanban/docs/ops-local-delta-manifest.md ends with the Apache-2.0 section 4(b) list of
the files this repository modified, added and removed under apps/kanban relative to the
upstream fork point. This test recomputes the three lists from the tree's own files, hashed
as Git blobs, and the committed upstream tree apps/kanban/docs/upstream-tree-abd4912c.json,
and compares them with the manifest. It never touches the network; Verification checks the
committed upstream tree against GitHub once, at the D0a-3 meet.

The files are the ones Git tracks plus untracked files it does not ignore (outside a Git
checkout: every file except build and dependency directories), so a file added under
apps/kanban without regenerating the manifest turns this test red.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KANBAN = ROOT / "apps" / "kanban"
MANIFEST = KANBAN / "docs" / "ops-local-delta-manifest.md"
UPSTREAM = KANBAN / "docs" / "upstream-tree-abd4912c.json"
GENERATOR = KANBAN / "scripts" / "kanban_delta.py"
FORK_POINT = "abd4912c27ce6b7f18b5a8106c145fd838e90cc4"
HEADING = "## Files changed relative to the fork point"
LISTS = {"Modified upstream files": "modified", "Added by this repository": "added",
         "Removed upstream files": "removed"}
_SKIP_DIRS = frozenset({".git", "node_modules", "dist", "dist-observer", ".worktrees", "__pycache__"})


def _blob(path: Path) -> str:
    data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _files() -> list[str]:
    top = subprocess.run(["git", "-C", str(KANBAN), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode == 0:
        out = subprocess.run(["git", "-C", str(KANBAN), "ls-files", "-z", "--cached", "--others",
                              "--exclude-standard", "--", "."], capture_output=True, check=True).stdout
        names = {n.decode() for n in out.split(b"\0") if n}
        return sorted(n for n in names if (KANBAN / n).is_file() or (KANBAN / n).is_symlink())
    found = []
    for directory, dirs, files in os.walk(KANBAN):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        found.extend(Path(directory, f).relative_to(KANBAN).as_posix() for f in files)
    return sorted(found)


def _manifest_lists() -> dict[str, list[str]]:
    section = MANIFEST.read_text()
    section = section[section.index(HEADING):]
    found, current = {}, None
    for line in section.splitlines():
        heading = re.fullmatch(r"\*\*(.+) \((\d+)\)\.\*\*", line)
        if heading:
            current = LISTS[heading.group(1)]
            found[current] = {"declared": int(heading.group(2)), "paths": []}
        elif current and line.startswith("- `") and line.endswith("`"):
            found[current]["paths"].append(line[3:-1])
    assert set(found) == set(LISTS.values()), found.keys()
    for key, value in found.items():
        assert value["declared"] == len(value["paths"]), f"{key}: heading says {value['declared']}"
    return {key: value["paths"] for key, value in found.items()}


def test_upstream_tree_is_the_fork_point_named_in_notice():
    data = json.loads(UPSTREAM.read_text())
    assert data["commit"] == FORK_POINT
    assert data["repository"] == "https://github.com/cline/kanban"
    assert data["files"] and all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in data["files"].values())
    for notice in (ROOT / "NOTICE", KANBAN / "NOTICE"):
        assert FORK_POINT in notice.read_text(), notice
    # NOTICE says the upstream LICENSE is kept unchanged.
    assert _blob(KANBAN / "LICENSE") == data["files"]["LICENSE"]


def test_manifest_lists_every_changed_file_and_nothing_else():
    theirs = json.loads(UPSTREAM.read_text())["files"]
    ours = {name: _blob(KANBAN / name) for name in _files()}
    expected = {"modified": sorted(p for p in ours if p in theirs and ours[p] != theirs[p]),
                "added": sorted(p for p in ours if p not in theirs),
                "removed": sorted(p for p in theirs if p not in ours)}
    listed = _manifest_lists()
    for key in expected:
        missing, extra = sorted(set(expected[key]) - set(listed[key])), sorted(set(listed[key]) - set(expected[key]))
        assert not (missing or extra), (f"{key}: not listed {missing}; listed but not {key} {extra}. "
                                        "Run python apps/kanban/scripts/kanban_delta.py --write")


def test_committed_generator_renders_the_manifest_section_exactly():
    spec = importlib.util.spec_from_file_location("kanban_delta", GENERATOR)
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    text = MANIFEST.read_text()
    assert generator.manifest_section(text) == generator.render(generator.delta())
