"""T7b P3 (container part, image-marked): with `--board-agents`, Kanban creates and removes task
worktrees in a planned repository.

Order: docs/work/orders/T7b-board-wiring.md, P3: `--board-agents` "mounts each planned
repository read-write at its host path into the `board` role only. Kanban can then create and
remove task worktrees there." Falsifier: "a board task worktree that cannot be created with
the flag". (The rendered mounts, with and without the flag, are checked by
test_t7b_p3_board_agents_rendered.py.)

The project is added and the worktree created and removed through the board's HTTP interface,
as the web UI does it (`projects.add`, `workspace.ensureWorktree`, `workspace.deleteWorktree`).

Readings (repeated in the arm report under AMBIGUITY):
- "create ... there": `ensureWorktree` answers `ok` with a path, the worktree holds the
  repository's committed files, and the planned repository (on the host) records it as one
  of its worktrees (`git worktree list --porcelain` names that path);
- "remove": `deleteWorktree` answers `ok` with `removed: true`, and afterwards the repository no
  longer records that worktree.
"""
from __future__ import annotations

import shutil
import subprocess
import uuid

import pytest

from t7b_harness import Board, BoardRuntime, agents_world, install

pytestmark = pytest.mark.image


def _worktrees(repo) -> list[str]:
    done = subprocess.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return [line.split(" ", 1)[1] for line in done.stdout.splitlines() if line.startswith("worktree ")]


def test_board_creates_and_removes_a_task_worktree_in_a_planned_repository():
    """GREEN-IF, installed with --board-agents, the board adds the planned repository as a project, creates a
    task worktree with its files that the repository records, and removes it again."""
    world, docker = agents_world("p3-worktrees", ("tooling", "board"))
    install(world, board_agents=True)
    runtime = BoardRuntime(docker, world)
    try:
        runtime.up()
        board = Board(runtime)
        workspace = board.add_project(world.repo)
        task = "t7b-worktree-" + uuid.uuid4().hex[:10]
        ensured = board.data("mutation", "workspace.ensureWorktree", {"taskId": task, "baseRef": "main"},
                             workspace=workspace)
        assert ensured.get("ok") is True and ensured.get("path"), f"ensureWorktree failed: {ensured}"
        path = ensured["path"]
        assert path in _worktrees(world.repo), (
            f"the repository does not record the board's task worktree {path}: {_worktrees(world.repo)}")
        listing = runtime.exec(runtime.board(), "/bin/sh", "-c", 'cat "$1/README.md"', "sh", path)
        assert listing.returncode == 0 and "kp-s3-fixture-needle-5d1c" in listing.stdout, (
            f"the task worktree {path} does not hold the repository's files: {listing.stderr[-800:]}")
        deleted = board.data("mutation", "workspace.deleteWorktree", {"taskId": task}, workspace=workspace)
        assert deleted.get("ok") is True and deleted.get("removed") is True, f"deleteWorktree failed: {deleted}"
        assert path not in _worktrees(world.repo), f"the repository still records the removed worktree {path}"
    finally:
        remaining = runtime.down()
    assert not remaining, f"the test left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)
