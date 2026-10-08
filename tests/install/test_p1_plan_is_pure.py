"""S3 P1: plan is pure.

`plan` writes nothing anywhere. Identical inputs give identical `plan_sha256`,
and any input change changes it.
Falsifier: any file created or modified by `plan`, or a hash that is unstable or
insensitive to an input.

"Anywhere" is observed on these surfaces: the runtime root, the repositories
(including .git), the installer's HOME, working directory and TMPDIR.
"""
from __future__ import annotations

import pytest

from s3_harness import DIGEST_BARE, PLAN_HASH, explain, snapshot, snapshot_diff


def test_plan_prints_json_with_plan_sha256(world):
    proc, value = world.plan()
    assert proc.returncode == 0, explain(proc)
    assert isinstance(value, dict), "plan must print the plan as a JSON object\n" + explain(proc)
    assert isinstance(value.get("plan_sha256"), str) and PLAN_HASH.fullmatch(value["plan_sha256"]), (
        "plan JSON must include plan_sha256\n" + explain(proc))


def test_plan_writes_nothing(world):
    before = snapshot(*world.surfaces())
    world.plan_hash()  # a plan that printed its plan_sha256, not merely an exit 0
    changed = snapshot_diff(before, snapshot(*world.surfaces()))
    assert not changed, "plan wrote:\n" + "\n".join(changed)
    assert list(world.root.iterdir()) == []


def test_plan_hash_is_stable(world):
    first = world.plan_hash()
    second = world.plan_hash()
    assert first == second, "identical inputs gave different plan_sha256"


def _other_root(world):
    other = world.base / "root-b"
    other.mkdir()
    return {"root": other}


VARIATIONS = {
    "runtime_root": _other_root,
    "image": lambda w: {"image": DIGEST_BARE},
    "repository_key": lambda w: {"repos": {"renamed": w.repo}},
    "repository_path": lambda w: {"repos": {"fixture": w.repo_b}},
    "repository_added": lambda w: {"repos": {"fixture": w.repo, "second": w.repo_b}},
    "components": lambda w: {"components": ("tooling", "board", "refresh")},
    "uid": lambda w: {"uid": w.uid + 1},
    "gid": lambda w: {"gid": w.gid + 1},
    "board_port": lambda w: {"board_port": w.board_port + 1},
    "project_name": lambda w: {"project": w.project + "-other"},
}


@pytest.mark.parametrize("variation", sorted(VARIATIONS))
def test_plan_hash_changes_with_each_input(world, variation):
    baseline = world.plan_hash()
    changed = world.plan_hash(world.args(**VARIATIONS[variation](world)))
    assert changed != baseline, f"plan_sha256 is insensitive to {variation}"
