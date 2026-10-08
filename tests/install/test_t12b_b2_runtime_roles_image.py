"""T12b B2 (image-marked): inside the runtime, only the `indexer` role opens the index or deletes outbox rows.

Order: docs/work/orders/T12b-one-indexer-outbox.md, B2, frozen r5. "Inside Compose, sealers never drain. The
`indexer` role is the only drainer. Inside Compose means `AGENT_MEMORY_VOLUME` is set ... it also covers `docker
exec` and one-off runs. No hook, capture pass, board process or gateway in the runtime opens the index."
Falsifier (r5): "inside the runtime, any process other than the `indexer` role opening the index, or deleting outbox
rows (statement trace, per role)".

Setup: a tooling + capture + board install (the `indexer` is selected with them), planned, applied, prepared and up
(docs/DOCKER.md's order, t9b_harness.install). Per role (capture, board, tooling), the probe
(tests/install/t12b_role_probe.py) runs in that role's container through `docker exec` (so with the role's
environment: AGENT_MEMORY_VOLUME set, the store volume at /state/memory): `setup` builds a desk-memory state root in
the volume (its own directory per role) with an empty index beside the store; then `flows`, recorded, runs the
sealing flows a role hosts through the product's entry points (capture, register, import, claim, link, the operator
import, a session-import job's advance; the order's sealers: "No hook, capture pass, board process or gateway in the
runtime opens the index", so operator verbs are not run). The record: every
`sqlite3.connect` and Python `open` of the index or a build file beside it, and every `DELETE FROM index_outbox` on
the store. Then, in the `indexer` container, the product's drain over each role's store (the positive control: the
indexer does open the index and delete the outbox rows it applied, and every sealed episode is indexed).

GREEN-IF no non-indexer role's recorded flows connect to or open the index or a build file, or delete outbox rows,
while they sealed (outbox rows appended); and in the indexer container a drain empties each role's outbox (by
deleting its rows) and indexes every sealed episode.
Reading (AMBIGUITY): the probe runs the library entry points the roles' commands call, in each role's container;
the long-running role processes themselves (kanban's hooks, the capture watch) are not traced.
"""
from __future__ import annotations

import json
import shutil

import pytest

from s3_harness import explain
from t9b_harness import install
from t12b_harness import INDEXER, Project, image_world, run_probe

pytestmark = pytest.mark.image

COMPONENTS = ("tooling", "capture", "board")
SEALERS = ("capture", "board", "tooling")


def _directory(role: str) -> str:
    return f"/state/memory/t12b-probe-{role}"


def test_inside_the_runtime_only_the_indexer_opens_the_index_or_deletes_outbox_rows():
    world, docker = image_world("roles", COMPONENTS)
    project = Project(docker, world)
    try:
        install(world)
        up = project.compose("up", "-d")
        assert up.returncode == 0, "`docker compose --project-directory \"$root\" up -d` failed\n" + explain(up)
        cids = project.wait_running(SEALERS)
        problems, observed = [], {}
        for role in SEALERS:
            ready = run_probe(project, cids[role], "setup", _directory(role))
            assert "error" not in ready and ready.get("volume"), f"precondition: probe setup in {role}: {ready}"
            flows = run_probe(project, cids[role], "flows", _directory(role))
            observed[role] = flows
            assert "error" not in flows, f"precondition: the probe's flows ran in {role}: {json.dumps(flows)[:3000]}"
            if flows["index_opens"]:
                problems.append(f"{role}: opened the index {len(flows['index_opens'])} times: {flows['index_opens'][:4]}")
            if flows["outbox_deletes"]:
                problems.append(f"{role}: deleted outbox rows: {flows['outbox_deletes'][:3]}")
            if flows["store_connects"] < 1 or not (flows["after"]["sealed"] > flows["before"]["sealed"]):
                problems.append(f"{role}: positive control: the flows sealed nothing ({flows['before']} -> "
                                f"{flows['after']})")
            if flows["errors"]:
                problems.append(f"{role}: flow errors: {flows['errors']}")
            print(f"T12b roles: {role}: index opens {len(flows['index_opens'])}, outbox deletes "
                  f"{len(flows['outbox_deletes'])}, store connects {flows['store_connects']}, {flows['before']} -> "
                  f"{flows['after']}")
        assert not problems, "inside the runtime, a non-indexer role:\n" + "\n".join(problems)
        indexer = project.container(INDEXER)
        assert indexer, f"no `{INDEXER}` container is running; compose ps: {project.services()}"
        for role in SEALERS:
            drained = run_probe(project, indexer, "drain", _directory(role))
            assert "error" not in drained, f"the indexer's drain of {role}'s store: {json.dumps(drained)[:3000]}"
            after = drained["after"]
            print(f"T12b roles: indexer drain of {role}: index opens {len(drained['index_opens'])}, outbox deletes "
                  f"{len(drained['outbox_deletes'])}, {drained['before']} -> {after}")
            assert after["indexed"] == after["sealed"] and not after["outbox"], (
                f"positive control: after the indexer's drain, {role}'s store: {after} (before {drained['before']})")
    finally:
        remaining = project.down()
    assert not remaining, f"left containers behind: {remaining}"
    shutil.rmtree(world.base, ignore_errors=True)
