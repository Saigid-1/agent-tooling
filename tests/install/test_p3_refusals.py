"""S3 P3: refusals.

Each of these is refused before any write: a missing or symlinked runtime root,
an image reference that is a tag, UID or GID 0, a board port below 1024, a path
containing `$` or a newline, a repository path that is not a Git repository, and
an unknown component.
Falsifier: any one accepted.

Every case first proves the unmutated baseline is accepted, then changes one
input. Interface-range cases beyond the P3 list (board port above 65535, a
repository with no commit) come from the plan argument definitions.
"""
from __future__ import annotations

import pytest

from s3_harness import explain, make_repo, snapshot, snapshot_diff


def _root(world, name, *, create=True, link=False):
    path = world.base / name
    if link:
        real = world.base / (name + "-target")
        real.mkdir()
        path.symlink_to(real, target_is_directory=True)
    elif create:
        path.mkdir()
    return {"root": path}


def _repo(world, name, *, git=True, commit=True):
    path = world.repos_dir / name
    if git:
        make_repo(path, commit=commit)
    else:
        path.mkdir()
        (path / "README.md").write_text("not a repository\n")
    return {"repos": {"fixture": path}}


CASES = {
    "runtime_root_missing": lambda w: _root(w, "absent-root", create=False),
    "runtime_root_symlink": lambda w: _root(w, "linked-root", link=True),
    "runtime_root_with_dollar": lambda w: _root(w, "root$HOME"),
    "runtime_root_with_newline": lambda w: _root(w, "root\nline"),
    "image_tag": lambda w: {"image": "agent-tooling:latest"},
    "image_registry_tag": lambda w: {"image": "ghcr.io/saigid-1/agent-tooling:0.4.0"},
    "image_bare_name": lambda w: {"image": "agent-tooling"},
    "uid_zero": lambda w: {"uid": 0},
    "gid_zero": lambda w: {"gid": 0},
    "board_port_1023": lambda w: {"board_port": 1023},
    "board_port_80": lambda w: {"board_port": 80},
    "board_port_65536": lambda w: {"board_port": 65536},
    "repository_with_dollar": lambda w: _repo(w, "repo$HOME"),
    "repository_with_newline": lambda w: _repo(w, "repo\nline"),
    "repository_not_git": lambda w: _repo(w, "plain", git=False),
    "repository_without_commit": lambda w: _repo(w, "uncommitted", commit=False),
    "component_unknown": lambda w: {"components": ("tooling", "board", "bogus")},
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_plan_refuses(world, case):
    world.plan_hash()  # control: the unmutated inputs are accepted
    argv = world.args(**CASES[case](world))
    before = snapshot(*world.surfaces())
    proc, _ = world.plan(argv)
    assert proc.returncode != 0, f"plan accepted {case}\n" + explain(proc)
    changed = snapshot_diff(before, snapshot(*world.surfaces()))
    assert not changed, f"plan wrote while refusing {case}:\n" + "\n".join(changed)


@pytest.mark.parametrize("case", sorted(CASES))
def test_apply_refuses_before_any_write(world, case):
    control_root = world.base / "control-root"
    control_root.mkdir()
    world.apply_ok(world.args(root=control_root))  # control: apply works at all
    reviewed = world.plan_hash()
    argv = world.args(**CASES[case](world))
    before = snapshot(*world.surfaces())
    proc = world.apply(argv, reviewed)
    assert proc.returncode != 0, f"apply accepted {case}\n" + explain(proc)
    changed = snapshot_diff(before, snapshot(*world.surfaces()))
    assert not changed, f"apply wrote while refusing {case}:\n" + "\n".join(changed)
