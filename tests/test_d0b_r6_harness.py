"""Instrument self-tests of the D0b R6 harness (tests/d0b_r6_harness.py, wired by tests/conftest.py).

These prove the harness on synthetic environments, without Docker. They test the TEST arm's own
instrument, not FEATURE's work, so they hold at D0b's base too.
"""
from __future__ import annotations

import hashlib

import d0b_r6_harness as r6
import d0b_seams as seams

AGENTS, UNREVISIONED = seams.AGENTS_VARIABLE, seams.UNREVISIONED_VARIABLE


def _digest(seed: str, registry: str = "localhost:5000/agent-tooling") -> str:
    return f"{registry}@sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def _pushed(**overrides) -> dict:
    env = {seams.IMAGE_VARIABLES[t]: _digest(t) for t in seams.PUBLISHED_TARGETS}
    env.update({AGENTS: "agent-tooling-ci:agents", UNREVISIONED: "agent-tooling-ci:product-unrevisioned"})
    env.update(overrides)
    return env


NAMED_AGENTS = "tests/image/test_image_product_roles.py::test_agents_carries_the_product_roles"
NAMED_MODULE = "tests/install/test_t7b_p2_desk_tasks_image.py::test_board_role_sets_the_launch_binding_command"
NAMED_UNREVISIONED = "tests/image/test_t7a_p4_image_identity.py::test_unlabelled_image_reports_unknown"
UNNAMED = "tests/image/test_image_identity_and_size.py::test_revision_label_equals_the_build_arg[product]"


def test_local_tags_leave_every_run_as_before():
    """GREEN-IF, with every image variable a local tag (images.yml, a developer's run), the harness is not in
    pushed-digest mode: no failure, nothing withheld, no report header."""
    env = {seams.IMAGE_VARIABLES[t]: f"agent-tooling-ci:{t}" for t in seams.PUBLISHED_TARGETS}
    env[AGENTS] = "agent-tooling-ci:agents"
    assert not r6.pushed_mode(env)
    assert r6.failure(env) is None
    assert r6.withheld(env, UNNAMED) == ()
    assert r6.header(env) == []


def test_pushed_digests_beside_local_builds_hold():
    """GREEN-IF four registry digests with local agents and unrevisioned builds raise no failure, and the header
    names the mode."""
    env = _pushed()
    assert r6.pushed_mode(env)
    assert r6.failure(env) is None
    assert r6.header(env)[0].startswith("D0b R6")


def test_a_tag_where_r6_says_the_pushed_digest_fails():
    """GREEN-IF, in pushed-digest mode, a published target named by a tag (or unset) is a failure naming its
    variable."""
    variable = seams.IMAGE_VARIABLES["ops"]
    reason = r6.failure(_pushed(**{variable: "agent-tooling-ci:ops"}))
    assert reason and variable in reason and "local build or a tag" in reason
    unset = r6.failure(_pushed(**{seams.IMAGE_VARIABLES["opencode"]: ""}))
    assert unset and seams.IMAGE_VARIABLES["opencode"] in unset and "unset" in unset


def test_a_registry_digest_for_agents_fails():
    """GREEN-IF, in pushed-digest mode, AGENT_TOOLING_TEST_IMAGE_AGENTS naming a registry digest is a failure (R2:
    agents is never pushed, so R6 takes a local build)."""
    reason = r6.failure(_pushed(**{AGENTS: _digest("agents")}))
    assert reason and AGENTS in reason


def test_only_the_named_tests_see_the_local_builds():
    """GREEN-IF, in pushed-digest mode, a test the order names keeps the local build it is named for (by node id,
    or by module for a `module::` entry) and a test outside the list has both local-build variables withheld."""
    env = _pushed()
    assert AGENTS not in r6.withheld(env, NAMED_AGENTS)
    assert AGENTS not in r6.withheld(env, NAMED_MODULE)
    assert UNREVISIONED in r6.withheld(env, NAMED_AGENTS)
    assert r6.withheld(env, NAMED_UNREVISIONED) == (AGENTS,)
    assert set(r6.withheld(env, UNNAMED)) == {AGENTS, UNREVISIONED}


def test_node_id_is_relative_to_the_repository_root():
    """GREEN-IF a test file's node id is `tests/...::name` relative to the repository root."""
    path = r6.REPO_ROOT / "tests" / "image" / "test_image_product_roles.py"
    assert r6.node_id(path, "test_agents_carries_the_product_roles") == NAMED_AGENTS
