"""T7a P1 (container part, image-marked): no role fails at creation.

Order: docs/work/orders/T7a-role-readiness.md, P1.
From an empty root, `plan`, `apply`, then `docker compose up -d` with any component
set creates every selected container. A role whose operator configuration is absent
stays up and reports `not_configured`, naming the missing operator files by
root-relative path, in its log (and in `verify`: test_t7a_p1_readiness_and_operator_files.py).
It does not exit, and it does not loop on restart.
Falsifier: an `up` that fails on a missing bind source; a role that exits or
restart-loops for missing operator files.

Each case renders an empty root with the image named by AGENT_TOOLING_TEST_IMAGE,
writes no operator file, runs the documented `docker compose --project-directory "$root"
up -d` (docs/DOCKER.md "Start, stop, status"), watches every selected role for 40 s,
reads the unconfigured roles' logs, then takes the project down.

Readings (repeated in the arm report under AMBIGUITY):
- "any component set" is sampled as each role with operator files alone (`refresh`,
  `capture`) and the rehearsal's set `tooling,refresh,capture,board`; `telemetry`
  runs upstream images this suite does not pull, so it is not sampled;
- "stays up": the container is running, has not restarted (RestartCount 0, the same
  StartedAt) throughout the window; its health is not asserted for an unconfigured role;
- the log must name each missing file root-relative (`config/refresh.json`, not only
  `/config/refresh.json`); see tests/install/t7a_harness.py.
"""
from __future__ import annotations

import shutil
import time

import pytest

from s3_harness import explain
from t7a_harness import (NOT_CONFIGURED, OPERATOR_FILES, Runtime, image_world, names_root_relative, required_docker,
                         unconfigured_files, with_transcript_roots)

pytestmark = pytest.mark.image

COMPONENT_SETS = {
    "refresh": ("tooling", "refresh"),
    "capture": ("tooling", "capture"),
    "all-roles": ("tooling", "refresh", "capture", "board", "indexer"),
}
# T12b (B2): the `indexer` role is selected whenever capture or board is; it is a role `up` creates and keeps up.
INDEXER = "indexer"


def _roles(components) -> tuple:
    extra = (INDEXER,) if {"capture", "board"} & set(components) and INDEXER not in components else ()
    return tuple(components) + extra
LOG_TIMEOUT = 60.0


def _wait_for_log(runtime: Runtime, cid: str, missing: list[str]) -> str:
    deadline = time.monotonic() + LOG_TIMEOUT
    logs = ""
    while time.monotonic() < deadline:
        logs = runtime.logs(cid)
        if NOT_CONFIGURED in logs and all(names_root_relative(logs, path) for path in missing):
            return logs
        time.sleep(2)
    return logs


@pytest.mark.parametrize("label", sorted(COMPONENT_SETS))
def test_up_creates_every_role_and_unconfigured_roles_stay_up(label):
    """GREEN-IF `up -d` exits 0, creates every selected role, every role keeps running without a
    restart for 40 s, and each unconfigured role logs `not_configured` naming its missing files."""
    components = COMPONENT_SETS[label]
    docker = required_docker()
    world = image_world(f"p1-{label}", components)
    argv = with_transcript_roots(world, world.args()) if "capture" in components else world.args()
    applied = world.apply(argv, world.plan_hash(argv))
    assert applied.returncode == 0, explain(applied)
    # Meet (Coordinator ruling, T9b Amendment 6): under P1b a role refuses an unprepared store,
    # so the documented sequence runs `prepare` between `apply` and `up`.
    from t9b_harness import prepare as _prepare, prepared as _prepared
    readied = _prepare(world)
    assert _prepared(readied) is None, explain(readied)
    roles = [role for role in components if role in OPERATOR_FILES]
    missing = {role: unconfigured_files(world.root, role) for role in roles}
    assert all(missing.values()), f"precondition: every role with operator files is unconfigured: {missing}"

    runtime = Runtime(docker, world)
    try:
        up = runtime.compose("up", "-d")
        assert up.returncode == 0, ("`docker compose --project-directory \"$root\" up -d` failed from an "
                                    "empty root with no operator files\n" + explain(up))
        created = runtime.services()
        expected = _roles(components)
        assert set(expected) <= set(created), f"up created {created}, not every selected role {expected}"
        cids = runtime.assert_stays_up(expected, window=40.0)
        for role in roles:
            logs = _wait_for_log(runtime, cids[role], missing[role])
            assert NOT_CONFIGURED in logs, f"{role}'s log does not report `{NOT_CONFIGURED}`:\n{logs[-3000:]}"
            unnamed = [path for path in missing[role] if not names_root_relative(logs, path)]
            assert not unnamed, f"{role}'s log does not name {unnamed} by root-relative path:\n{logs[-3000:]}"
        runtime.assert_stays_up(expected, window=4.0)
    finally:
        remaining = runtime.down()
    assert not remaining, f"the test left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)  # kept for inspection when any step failed
