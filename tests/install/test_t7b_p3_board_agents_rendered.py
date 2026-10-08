"""T7b P3 (default, rendered configuration): `--board-agents` gives the board, and only the
board, read-write repositories at their host paths.

Order: docs/work/orders/T7b-board-wiring.md, P3:
- a new plan input, `--board-agents`, mounts each planned repository read-write at its host
  path into the `board` role only;
- without the flag, the board gets no repository mount, as today;
- every other role keeps its current mounts (tests/install/test_p4_rendered_isolation.py
  stays unmodified and green);
- `--board-agents` without the `board` component is refused at `plan`.
Falsifier: a read-write repository mount in any role but `board`; a repository mount
without the flag. (A board task worktree that cannot be created with the flag is the
container half: test_t7b_p3_task_worktrees_image.py.)

The configuration comes from `docker compose config` when the docker CLI with the compose
plugin exists, otherwise from the S3 harness's labelled YAML model (s3_harness.rendered_config).

Readings (repeated in the arm report under AMBIGUITY):
- "at its host path": the bind's source and its container target are both the planned
  repository path (the physical path `plan` records under `inputs.repositories`);
- "read-write": the bind is not `read_only: true`;
- "every other role keeps its current mounts": each other service's binds, compared as
  (source, target, read_only) with sources inside the runtime root written root-relative,
  are the same set with and without the flag, for the same inputs; the board keeps its
  other binds too. The order does not forbid other additions to the board, so only the
  repository binds are required of them; the flagged rendering must still keep the Docker
  socket and home directory roots out of every role (DOCKER.md, Roles);
- "a new plan input": the flag is covered by `plan_sha256` (identical inputs otherwise
  give a different digest, and `apply` refuses the other plan's digest), as every
  installer input is (DOCKER.md, "Identical inputs give the same hash");
- "refused at plan": `plan` exits non-zero with the installer's JSON refusal
  (`"status": "refused"`, `"writes": "none"`) naming the board, and writes nothing,
  while the same inputs with `board` are accepted.
"""
from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest

from s3_harness import ALL_COMPONENTS, World, binds, explain, home_roots, rendered_config, snapshot

FLAG = "--board-agents"


def _norm(path) -> str:
    return os.path.normpath(str(path))


def _bind_set(service: dict, root=None) -> set[tuple[str, str, bool]]:
    """(source, target, read_only) of each bind; a source inside `root` is written `<root>/...` so two
    renderings into different runtime roots compare."""
    def source(raw):
        path = _norm(raw)
        if root is not None and (path == _norm(root) or path.startswith(_norm(root) + "/")):
            return "<root>" + path[len(_norm(root)):]
        return path
    return {(source(b.get("source", "")), str(b.get("target", "")), b.get("read_only") is True)
            for b in binds(service)}


@pytest.fixture(scope="module")
def rendered_pair(tmp_path_factory, installer):
    """The same inputs rendered twice, into two roots: without and with `--board-agents`. A failure to
    render is handed to each test, which then fails (rather than erroring in setup)."""
    try:
        return _render_pair(tmp_path_factory)
    except AssertionError as error:
        return error


def _required(pair):
    if isinstance(pair, AssertionError):
        pytest.fail(str(pair), pytrace=False)
    return pair


def _render_pair(tmp_path_factory):
    world = World.create(tmp_path_factory.mktemp("t7b-p3"), components=ALL_COMPONENTS)
    world.repos = {"fixture": world.repo, "second": world.repo_b}
    flagged_root = world.base / "root-board-agents"
    flagged_root.mkdir()
    plain_argv = world.args()
    flagged_argv = world.args(root=flagged_root) + [FLAG]
    plan, plan_value = world.plan(flagged_argv)
    assert plan.returncode == 0 and isinstance(plan_value, dict), (
        f"plan refused `{FLAG}` with every component selected\n" + explain(plan))
    world.apply_ok(plain_argv)
    world.apply_ok(flagged_argv)
    plain, instrument = rendered_config(world)
    flagged, _ = rendered_config(replace(world, root=flagged_root))
    repos = {key: _norm(row["path"]) for key, row in (plan_value.get("inputs") or {}).get("repositories", {}).items()}
    assert sorted(repos) == ["fixture", "second"], f"plan inputs.repositories: {plan_value.get('inputs')}"
    return world, plain.get("services") or {}, flagged.get("services") or {}, repos, instrument, flagged_root


def test_board_agents_binds_each_repository_read_write_at_its_host_path_into_board(rendered_pair):
    """GREEN-IF with the flag the board holds, for each planned repository, a bind whose source and target
    are the repository's host path and which is not read-only."""
    _, _, flagged, repos, instrument, _ = _required(rendered_pair)
    board = flagged.get("board")
    assert board, f"{instrument}: no board service with {FLAG}"
    found = _bind_set(board)
    missing = [key for key, path in repos.items() if (path, path, False) not in found]
    assert not missing, (f"{instrument}: with {FLAG} the board lacks a read-write bind at the host path for "
                         f"{missing}; board binds: {sorted(found)}")


def test_other_roles_keep_their_mounts_and_only_the_board_binds_repositories_writable(rendered_pair):
    """GREEN-IF the flag changes no other service's binds, keeps the board's existing binds and adds the
    repository binds to it, and no role but board binds a repository writable; without the flag the board
    binds no repository."""
    world, plain, flagged, repos, instrument, flagged_root = _required(rendered_pair)
    sources = set(repos.values())
    assert sorted(plain) == sorted(flagged), f"{instrument}: services differ: {sorted(plain)} vs {sorted(flagged)}"

    def before(name):
        return _bind_set(plain[name], world.root)

    def after(name):
        return _bind_set(flagged[name], flagged_root)

    changed = {name: sorted(after(name) ^ before(name)) for name in plain if name != "board" and after(name) != before(name)}
    assert not changed, f"{instrument}: {FLAG} changed the mounts of other roles: {changed}"
    plain_board, flagged_board = before("board"), after("board")
    assert plain_board <= flagged_board, (
        f"{instrument}: {FLAG} removed board mounts: {sorted(plain_board - flagged_board)}")
    added = flagged_board - plain_board
    assert {(path, path, False) for path in sources} <= added, (
        f"{instrument}: {FLAG} must add the read-write repository binds to the board; added {sorted(added)}")
    unflagged = [b for b in plain_board if b[0] in sources or any(b[0] == p or p.startswith(b[0].rstrip('/') + '/')
                                                                    for p in sources)]
    assert not unflagged, f"{instrument}: without {FLAG} the board binds a repository: {unflagged}"
    writable = [(name, b) for name, svc in flagged.items() if name != "board"
                for b in _bind_set(svc) if b[0] in sources and not b[2]]
    assert not writable, f"{instrument}: roles other than board bind a repository writable: {writable}"


def test_board_agents_keeps_the_docker_socket_and_home_roots_out(rendered_pair):
    """GREEN-IF, with the flag, no service binds the Docker socket or a home directory root (DOCKER.md, Roles:
    "No role mounts the Docker socket or a home directory")."""
    world, _, flagged, _, instrument, _ = _required(rendered_pair)
    homes = home_roots(world.home)
    offenders = []
    for name, svc in flagged.items():
        for b in binds(svc):
            source = _norm(b.get("source", ""))
            if source.endswith("docker.sock") or str(b.get("target", "")).endswith("docker.sock"):
                offenders.append((name, "docker socket", source))
            if any(source == h or h.startswith(source.rstrip("/") + "/") for h in homes):
                offenders.append((name, "home directory root", source))
    assert not offenders, f"{instrument}: with {FLAG}: {offenders}"


def test_board_agents_is_a_reviewed_plan_input(tmp_path, installer):
    """GREEN-IF the flag changes `plan_sha256` for otherwise identical inputs, and `apply` with the flag refuses
    the digest of the plan without it (and the reverse), writing nothing."""
    world = World.create(tmp_path / "w", components=("tooling", "board"))
    plain_argv, flagged_argv = world.args(), world.args() + [FLAG]
    plain = world.plan_hash(plain_argv)
    flagged = world.plan_hash(flagged_argv)
    assert plain != flagged, f"{FLAG} does not change plan_sha256 ({plain}); it is not a reviewed plan input"
    before = snapshot(world.root)
    for argv, digest in ((flagged_argv, plain), (plain_argv, flagged)):
        refused = world.apply(argv, digest)
        assert refused.returncode != 0, f"apply accepted the other plan's digest\n" + explain(refused)
        assert snapshot(world.root) == before, "a refused apply wrote into the runtime root"
    applied = world.apply(flagged_argv, flagged)
    assert applied.returncode == 0, f"apply refused its own reviewed {FLAG} plan\n" + explain(applied)


@pytest.mark.parametrize("components", [("tooling",), ("tooling", "capture"), ("tooling", "refresh", "capture")],
                         ids=lambda c: ",".join(c))
def test_board_agents_without_board_is_refused_at_plan(tmp_path, installer, components):
    """GREEN-IF `plan --board-agents` without the board component exits non-zero with the installer's JSON
    refusal (status refused, writes none) naming the board and writes nothing, while the same inputs with
    board are accepted."""
    world = World.create(tmp_path / "w", components=components)
    before = snapshot(*world.surfaces())
    proc, value = world.plan(world.args() + [FLAG])
    assert proc.returncode != 0, f"plan accepted {FLAG} without the board component\n" + explain(proc)
    assert isinstance(value, dict) and value.get("status") == "refused" and value.get("writes") == "none", (
        "the refusal is not the installer's JSON refusal on stdout (status: refused, writes: none)\n" + explain(proc))
    assert "board" in json.dumps(value).lower(), f"the refusal does not name the board component: {value}"
    assert snapshot(*world.surfaces()) == before, "a refused plan wrote something"
    accepted, _ = world.plan(world.args(components=(*components, "board")) + [FLAG])
    assert accepted.returncode == 0, (f"the same inputs with board are refused too, so the refusal is not "
                                      f"about the board component\n" + explain(accepted))
