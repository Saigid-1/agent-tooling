"""S2 interface surface: one build file, and the two old Dockerfiles retired.

These read the source tree (``AGENT_TOOLING_TEST_SOURCE_ROOT``, default: this
checkout). Run them from the merged checkout the images were built from.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from image_harness import source_root

pytestmark = pytest.mark.image

OLD_DOCKERFILES = ("deploy/kanban/Dockerfile", "deploy/tooling/Dockerfile")
OLD_REFERENCE = re.compile(r"deploy/(?:kanban|tooling)/Dockerfile")
# Historical records and a file the order forbids S2 to edit (S3 replaces it):
# frozen orders, ADR context, docs/DOCKER.md, and this suite itself.
# tests/docs/ holds the T6b documentation guard, which must name the retired paths to forbid them.
REFERENCE_EXEMPT_PREFIXES = ("docs/work/orders/", "docs/adr/", "tests/image/", "tests/docs/")
REFERENCE_EXEMPT_FILES = {"docs/DOCKER.md", *OLD_DOCKERFILES}


def _tree_files(root: Path) -> list[str]:
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True)
    if listed.returncode == 0 and listed.stdout:
        return [path for path in listed.stdout.split("\0") if path]
    files = []
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not any(part in {".git", "node_modules"} for part in path.parts):
            files.append(rel)
    return files


def test_build_file_declares_the_four_targets_and_the_revision_arg():
    dockerfile = source_root() / "deploy/Dockerfile"
    assert dockerfile.is_file(), f"{dockerfile} does not exist"
    text = dockerfile.read_text()
    stages = {match.lower() for match in re.findall(r"(?im)^\s*FROM\s+\S+(?:\s+--\S+)*\s+AS\s+([A-Za-z0-9_.-]+)", text)}
    missing = {"runtime", "product", "agents", "acceptance"} - stages
    assert not missing, f"deploy/Dockerfile lacks target(s) {sorted(missing)}; declared: {sorted(stages)}"
    assert re.search(r"(?m)^\s*ARG\s+SOURCE_REVISION\b", text), "deploy/Dockerfile declares no ARG SOURCE_REVISION"


@pytest.mark.parametrize("old", OLD_DOCKERFILES)
def test_old_dockerfile_is_removed_or_a_pointer(old):
    path = source_root() / old
    if not path.exists():
        return
    lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    instructions = [line for line in lines if not line.startswith("#")]
    assert not instructions, f"{old} still has build instructions: {instructions[:3]}"
    assert "deploy/Dockerfile" in path.read_text(), f"{old} is reduced to comments but does not point at deploy/Dockerfile"


def test_no_reference_to_the_old_dockerfiles_remains():
    root = source_root()
    stale = []
    for rel in _tree_files(root):
        if rel in REFERENCE_EXEMPT_FILES or rel.startswith(REFERENCE_EXEMPT_PREFIXES):
            continue
        try:
            text = (root / rel).read_text(errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if OLD_REFERENCE.search(line):
                stale.append(f"{rel}:{number}: {line.strip()[:160]}")
    assert not stale, "stale references to the retired Dockerfiles:\n" + "\n".join(stale)
