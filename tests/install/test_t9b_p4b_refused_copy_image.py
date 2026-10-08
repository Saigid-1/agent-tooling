"""T9b M1/M2: a copy that differs from its source is refused, and the refused run leaves nothing
behind (image-marked).

Order: docs/work/orders/T9b-memory-store-volume.md, Amendment 7, M1/M2, with:
- Amendment 6, H1: when any step from 4 on refuses, `prepare` removes the volume it created in this
  run;
- Amendment 5, P3: a database's equality is logical, so each ordinary table's content digest (rows in
  pk or rowid order) must equal the source's;
- Amendment 5, P4 step 5: `copy_or_check_failed`, with both sides unchanged.
The image-marked test drives `prepare` through its `docker=` parameter. A wrapper alters one non-key
column of one row in the migration copy after the copy is made, with the row count unchanged. The
test asserts:
- the refusal `copy_or_check_failed`, naming the table;
- no `<project>_memory` volume afterwards (this run created it);
- the host tree, receipt.json and the absence of any `.migrated-` directory, all unchanged.
It kills "a refusal after creation keeps the volume" (H1, mutant M1) and "the per-table digest is
skipped" (mutant M2).

Setup:
- an empty root, `plan` and `apply` (never prepared), then the pre-T9b stores on the host bind
  (t9b_harness.build_store: state/memory and state/registry), with no volume of the project yet;
- `prepare` runs in this process, `kp_agent_tooling._impl.runtime_install.prepare(root, docker=...)`.
  This is the one T9b test that imports the implementation, because the order names this seam;
- the wrapper is the real docker CLI (`_Docker`). The one exception is the one-off container labelled
  `agent-tooling.install.step=memory-migrate`, whose `-c` script gets one inserted statement after
  `origin.backup(copy)`: in the copy of state/memory/desk-history/episode-search.sqlite3, the
  `episode_id` column (not a key) of the lowest-id row of table `terms` changes, and the row count is
  unchanged;
- `sqlite3.Connection.backup` cannot be monkeypatched (an immutable C type), so the script text is
  patched at the call site. If the anchor text is not found exactly once, the test fails loudly.

Readings (repeated in the arm report under AMBIGUITY):
- "the refusal" is the `PrepareRefused` that `prepare` raises (the CLI prints its `result()`);
- the refusal "names the table" when its reason is `copy_or_check_failed` and its result names
  `terms` in quotes (a name no other fact of this refusal contains);
- "the host tree" is t9b_harness.host_tree of the whole runtime root;
- a stray container is any `<project>-memory-*` container, running or not, after `prepare` returns.
"""
from __future__ import annotations

import json
import re
import shutil

import pytest

from s3_harness import explain
from t9b_harness import (PREFIX, Project, apply_only, build_store, host_tree, image_world, migrated_directories,
                         store_volume_name, tree_diff)

pytestmark = pytest.mark.image

MIGRATE_LABEL = "agent-tooling.install.step=memory-migrate"
ANCHOR = "        origin.backup(copy)\n"
TAMPERED_DATABASE = "desk-history/episode-search.sqlite3"
TAMPERED_TABLE = "terms"
# Inserted after ANCHOR, inside copy_database: `label` is "<store directory>/<path>", `copy` the open
# connection to the backup copy. One non-key column of one row changes; the row count is unchanged.
TAMPER = (
    '        if label.endswith("/' + TAMPERED_DATABASE + '"):\n'
    '            _t9b_changed = copy.execute("update ' + TAMPERED_TABLE + ' set episode_id = episode_id + 1000003 '
    'where id = (select min(id) from ' + TAMPERED_TABLE + ')").rowcount\n'
    '            copy.commit()\n'
    '            if _t9b_changed != 1:\n'
    '                raise RuntimeError("t9b-tamper: changed %d rows of ' + TAMPERED_TABLE + ', not 1" % _t9b_changed)\n'
)


class AnchorMissing(RuntimeError):
    """The migrate script no longer holds the anchor this test patches."""


def _tampering_docker():
    from kp_agent_tooling._impl import runtime_install

    class TamperingDocker(runtime_install._Docker):
        """The real docker CLI, except that the migrate container's `-c` script alters its copy."""

        def __init__(self):
            super().__init__()
            self.tampered, self.problems, self.created = 0, [], []

        def __call__(self, *args, timeout=120):
            if args[:2] == ("volume", "create"):
                self.created.append(args[-1])
            if args and args[0] == "run" and MIGRATE_LABEL in args:
                args = list(args)
                if "-c" not in args:
                    self.problems.append("the migrate container has no `-c` script")
                    raise AnchorMissing(self.problems[-1])
                index = len(args) - 1 - args[::-1].index("-c")
                script = args[index + 1]
                found = script.count(ANCHOR)
                if found != 1:
                    self.problems.append(f"the anchor {ANCHOR.strip()!r} occurs {found} times in the migrate "
                                         "script, not once")
                    raise AnchorMissing(self.problems[-1])
                args[index + 1] = script.replace(ANCHOR, ANCHOR + TAMPER)
                self.tampered += 1
            return super().__call__(*args, timeout=timeout)

    return runtime_install, TamperingDocker()


def _memory_containers(project: Project) -> list[str]:
    """Containers left by prepare's one-off steps (`<project>-memory-<step>-<hex>`), running or not."""
    proc = project.docker_run("ps", "-a", "--filter", f"name={project.project}-memory-", "--format", "{{.Names}}")
    assert proc.returncode == 0, explain(proc)
    return sorted(name for name in proc.stdout.split() if name.startswith(f"{project.project}-memory-"))


def test_a_copy_whose_table_changed_with_its_count_is_refused_and_leaves_nothing_behind():
    """GREEN-IF `prepare` (driven in-process, its migrate copy altered in one non-key column of one row of
    table `terms` after the backup) refuses `copy_or_check_failed` naming `terms`; afterwards no
    `<project>_memory` volume exists (this run created it), the runtime root's host tree and receipt.json are
    unchanged, no `.migrated-` directory exists, and no `<project>-memory-*` container is left."""
    world, docker = image_world("p4b-copy", ("tooling",))
    project = Project(docker, world)
    volume = store_volume_name(world.project)
    try:
        assert world.project.startswith(PREFIX + "-"), world.project
        apply_only(world)
        build_store(world.root, crashed=False, legacy=("memory", "registry"))
        assert not project.volume_exists(volume), f"precondition: no volume {volume} yet"
        assert migrated_directories(world.root) == [], "precondition: no .migrated- directory"
        tree_before = host_tree(world.root)
        receipt_before = (world.root / "receipt.json").read_bytes()

        runtime_install, wrapper = _tampering_docker()
        try:
            result = runtime_install.prepare(str(world.root), docker=wrapper)
        except runtime_install.PrepareRefused as error:
            refusal = error
        else:
            refusal = None
        assert wrapper.problems == [], "the tamper could not be inserted: " + "; ".join(wrapper.problems)
        assert wrapper.tampered == 1, f"the migrate container ran {wrapper.tampered} times with the tamper, not once"
        assert wrapper.created == [volume], f"precondition: this run created {volume} (created: {wrapper.created})"
        assert refusal is not None, (
            f"prepare accepted a copy whose table {TAMPERED_TABLE} changed with its row count unchanged: "
            + json.dumps(result, sort_keys=True)[:3000])
        reported = json.dumps(refusal.result(), sort_keys=True)
        print(f"refusal: {refusal.reason}: {refusal.detail[:1500]}")
        assert refusal.reason == "copy_or_check_failed", (
            f"prepare refused {refusal.reason!r}, not `copy_or_check_failed`: {reported[:3000]}")
        assert re.search(r'\\*"' + TAMPERED_TABLE + r'\\*"', reported), (
            f"the refusal does not name the table {TAMPERED_TABLE}: {reported[:3000]}")

        assert not project.volume_exists(volume), (
            f"the refused prepare left the volume {volume} it created behind: {reported[:1500]}")
        tree_after = host_tree(world.root)
        assert tree_after == tree_before, "the refused prepare changed the host tree:\n" + "\n".join(
            tree_diff(tree_before, tree_after))
        assert (world.root / "receipt.json").read_bytes() == receipt_before, "the refused prepare changed receipt.json"
        assert migrated_directories(world.root) == [], (
            f"the refused prepare renamed: {migrated_directories(world.root)}")
        assert _memory_containers(project) == [], f"left prepare containers behind: {_memory_containers(project)}"
    finally:
        remaining = project.down()
    assert not remaining, f"left containers behind: {remaining}"
    assert not project.volume_exists(volume), f"left the volume {volume} behind"
    shutil.rmtree(world.base, ignore_errors=True)  # kept for inspection when any step failed
