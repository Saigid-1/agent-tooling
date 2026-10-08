"""T9b J1: `verify` observes the store, the receipt only records it (image-marked).

Order: docs/work/orders/T9b-memory-store-volume.md, Amendment 7, J1 (additive to Amendments 5 and 6):
- `verify`'s readiness.memory_store is the Definition ("prepared") as observed, never the receipt's
  `prepared` entry. With Docker reachable and the receipt's image present locally, `verify` observes the
  volume and reports `prepared` or `unprepared`; the receipt entry, present or absent, does not change the
  result.
- `prepare`'s step 0 is unchanged: on a prepared root it changes nothing, receipt.json included.
- Exit code and top-level status stay unchanged in every case.
Falsifier: two roots with the same project name. Root A is applied and prepared, then root B is applied
and prepared (step 0, nothing changed). A `verify` on B with Docker reachable reports anything but
`prepared`.

Setup: two never-reused roots under TMPDIR rendered for one `t9b-test-*` Compose project
(t9b_harness.sharing_world). Each is planned, applied and prepared with Docker reachable and the image
present. The shared project and its volume are taken down once, after both
(`docker compose --project-directory <root A> down --volumes --remove-orphans`, then the project's
leftovers by label and name, t9b_harness.Project.down).

Readings (repeated in the arm report under AMBIGUITY):
- "prepared with `changed` false" is prepare's JSON report: `status` `prepared` and `changed` false;
- "changes nothing" includes receipt.json byte for byte (J1);
- verify's store state is its JSON report's `readiness.memory_store.status`, never a substring of its
  output.
"""
from __future__ import annotations

import shutil

import pytest

from s3_harness import explain
from t9b_harness import (PREFIX, Project, apply_only, image_world, prepare, prepared, result_json, sharing_world,
                         store_volume_name, verify)

pytestmark = pytest.mark.image


def _store_state(proc) -> tuple[object, object, dict]:
    """(exit code, top-level status, readiness.memory_store) of a `verify` run."""
    value = result_json(proc)
    value = value if isinstance(value, dict) else {}
    store = (value.get("readiness") or {}).get("memory_store")
    return proc.returncode, value.get("status"), store if isinstance(store, dict) else {}


def _memory_containers(project: Project) -> list[str]:
    """Containers left by prepare's one-off steps (`<project>-memory-<step>-<hex>`), running or not."""
    proc = project.docker_run("ps", "-a", "--filter", f"name={project.project}-memory-", "--format", "{{.Names}}")
    assert proc.returncode == 0, explain(proc)
    return sorted(name for name in proc.stdout.split() if name.startswith(f"{project.project}-memory-"))


def test_verify_on_a_second_root_of_a_prepared_project_reports_prepared():
    """GREEN-IF, with root A of a project applied and prepared, and root B of the same project then applied
    and prepared (step 0: `prepared`, `changed` false, receipt.json byte-identical), `verify` on B with
    Docker reachable exits 0 with status `verified` and readiness.memory_store.status `prepared`."""
    world_a, docker = image_world("p5b-a", ("tooling",))
    world_b = sharing_world(world_a, "p5b-b")
    project = Project(docker, world_a)
    volume = store_volume_name(world_a.project)
    try:
        assert world_b.project == world_a.project and world_b.root != world_a.root, "precondition: two roots, one project"
        assert world_a.project.startswith(PREFIX + "-"), world_a.project
        assert not project.volume_exists(volume), f"precondition: no volume {volume} yet"

        apply_only(world_a)
        first = prepare(world_a)
        assert prepared(first) is None, f"prepare on root A: {prepared(first)}\n" + explain(first)
        assert project.volume_exists(volume), f"precondition: prepare on root A created {volume}"
        code, status, store = _store_state(verify(world_a))
        assert (code, status, store.get("status")) == (0, "verified", "prepared"), (
            f"precondition: verify on root A, whose own prepare created the volume, reports exit {code}, "
            f"status {status!r}, store {store!r}")

        apply_only(world_b)
        receipt = world_b.root / "receipt.json"
        receipt_before = receipt.read_bytes()
        second = prepare(world_b)
        assert prepared(second) is None, f"prepare on root B: {prepared(second)}\n" + explain(second)
        report = result_json(second)
        assert isinstance(report, dict) and report.get("status") == "prepared" and report.get("changed") is False, (
            "prepare on root B, whose project volume root A prepared, is not step 0 (`prepared`, `changed` "
            "false)\n" + explain(second))
        assert receipt.read_bytes() == receipt_before, "prepare's step 0 on root B changed receipt.json"

        checked = verify(world_b)
        code, status, store = _store_state(checked)
        assert code == 0 and status == "verified", (
            "verify on root B changed its exit code or top-level status\n" + explain(checked))
        assert store.get("status") == "prepared", (
            f"verify on root B reports the store {store.get('status')!r}, not `prepared` (the volume of its "
            f"project exists, owned and 0700, with nothing left on the bind); reasons: {store.get('reasons')}\n"
            + explain(checked))
    finally:
        remaining = project.down()
    assert not remaining, f"left containers behind: {remaining}"
    assert not project.volume_exists(volume), f"left the volume {volume} behind"
    assert _memory_containers(project) == [], f"left prepare containers behind: {_memory_containers(project)}"
    for world in (world_a, world_b):
        shutil.rmtree(world.base, ignore_errors=True)  # kept for inspection when any step failed
