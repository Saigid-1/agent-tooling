# T10h — Projection hotfix: the runtime's SQLite accepts the projection; capture never re-checks the whole store

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

Status: frozen 2026-10-03 (r2, Coordinator); Verification cleared it for dispatch with P1 precisions, the P2 amendment and two deploy lines, all applied here.
- **Base:** main with T9b and T10 merged. This is a hotfix found at the live deploy.
- **State at the time of writing:** the capture role is down. Memory reads work, but coverage reports `incomplete_index`.

## Problem (measured on the live runtime, 2026-10-03, read in-VM only)

- **D1: the runtime's SQLite reports an integrity failure in T10's projection where there is none.**
  - The runtime image is `python:3.12-slim-bookworm`, with SQLite 3.40.1. In it, `PRAGMA quick_check` on the live `episodes.sqlite3` returns 100 rows of `NULL value in episode_desks.tenant`.
  - `SELECT count(*) FROM episode_desks WHERE tenant IS NULL` returns 0 (42,775 rows mid-backfill; Verification counted 94,623 after capture's pass completed, 0 NULL).
  - **Cause:** `episode_desks` is `WITHOUT ROWID`, with the NOT NULL non-key column `tenant` declared between its primary-key columns: `(desk, tenant, kind, seq, episode_id, PRIMARY KEY (desk, kind, seq, episode_id))`.
  - **Minimal reproduction:** an in-memory table with that DDL and five non-NULL rows. On 3.40.1 (the image) `quick_check` reports NULLs; on 3.53.4 (the host) it reports `ok`. The same DDL with `tenant` after the key columns, or as a rowid table, gives `ok` on 3.40.1. Script: `nn_probe.py`, quoted in the PR.
  - **Why the suites missed it:** they run on the host's or CI's SQLite, never the image's. Every other projection table declares its key columns first, and passes.
- **D2: capture runs the whole-store integrity check on every watch pass.**
  - `workspace_capture_cli.main` constructs `WorkspaceCapture` per pass. `once()` calls `initialize()` (`workspace_capture.py:323`), which calls `SessionSources.upgrade()` → `backfill()`. That runs `PRAGMA quick_check` over the whole store (`session_sources.py:1119`) before it checks whether any work remains.
  - The live store is 1.6 GB, and the pass interval is 30 s. That is the full scan of slow-moving data that T10 moved out of construction (T10 meet note), and the Principal's rule forbids it.
  - Under D1, the first pass after the deploy succeeded: it created the projection tables and backfilled them, and `quick_check` ran while `episode_desks` was still empty. The second pass raised `EpisodeUnavailable`, a `RuntimeError` the watch loop does not catch, and the role exited 1. Its restart policy is `no`.
- **D3: `kp-agent-desk … upgrade-sources` refuses on the live store** ("episode store integrity check failed; backfill refused"), from D1. T10's coverage step therefore never ran, and `memory.search` reports `index_status: incomplete_index`, `covered_episodes: 0`.

## Properties and falsifiers

- **P1: the projection passes `quick_check` on the runtime's SQLite.**
  - Every projection table declares its primary-key columns before any other column.
  - `upgrade()` rebuilds an existing `episode_desks` that has the old column order, in one transaction. The rebuild:
    - creates the new table;
    - copies the rows across;
    - drops the old table;
    - renames the new one;
    - recreates any index or trigger that names the table.
  - **Detected, not assumed.** `PRAGMA table_info('episode_desks')` with `tenant` at cid 1 means rebuild; at cid 4, nothing.
  - **The rebuild.** It runs under `BEGIN IMMEDIATE` and recreates the `episode_desks_episode` index. No trigger names `episode_desks`: the four `scope_written_*` triggers are on the marked tables.
  - **Row count and content are unchanged.**
  - **The live store is the old-order case,** so P3(d) is the deploy path.
  - A store created new gets the new order directly.
  - Falsifiers:
    - In the product image (SQLite 3.40.1), `PRAGMA quick_check` on a populated store after `upgrade-sources` returns anything but `ok`.
    - A store whose `episode_desks` has the old order fails `quick_check` after one `upgrade-sources`.
    - The rebuild changes the multiset of `(desk, tenant, kind, seq, episode_id)` rows.
    - The change of row order alters T10's goldens. The reads use `d.desk = ? AND d.kind = s.kind AND d.seq = s.seq` and `ORDER BY (kind, seq)` on the primary key, so nothing should change. The instrument is T10's unchanged suite.
- **P2: capture never backfills and never runs the whole-store check (Verification amendment).** T10 P6 made the upgrade "one documented, idempotent operator step". Today a capture watch pass did it implicitly: it installed the projection and backfilled about 220k rows inside a 30 s pass, holding the store's write lock against every MCP server in batches.
  - **What capture runs.** A `WorkspaceCapture.once()` pass runs no `PRAGMA quick_check` and projects no existing row. `initialize()` ensures only the schema its own writes need. FEATURE names what capture's writers do when the projection is not installed: they skip projecting, and their rows count as `written` only once marks exist.
  - **Where the backfill runs.** Only from `kp-agent-desk … upgrade-sources`, and from `EpisodeStore.initialize()` on a store it has just created, which is empty.
  - **`upgrade()`/`backfill()` early return.** They still read `scope_state.complete` and `scope_marks` first. When the projection is complete and nothing is behind, they return without `quick_check` and without any scan. The operator's CLI keeps `quick_check` before any projection it does.
  - **Falsifiers:**
    - A capture pass on a store whose projection is incomplete creates a projection row for an existing episode, or traces a `PRAGMA quick_check`. It must capture, and leave the projection incomplete; reads then refuse with `projection_incomplete` until the operator runs `upgrade-sources`, as T10 P6 says.
    - Two capture passes on a complete projection trace a `quick_check`, or a scan as T10 P2 defines it.

    Instrument: T10's in-process statement trace (`tests/t10_instruments.py`).
- **P3: the image runs what the suites run.** An image-marked test, under the `t10h-` prefix, inside the product image, on a populated store that has a T10 projection, runs:
  - (a) `kp-agent-desk … upgrade-sources` → `status` complete, with coverage established;
  - (b) `PRAGMA quick_check` → `ok`;
  - (c) two consecutive `kp-agent-workspace-capture --config … --policy … once` runs → both exit 0 with `status` `ok` (the second is what died live);
  - (d) the same with a store pre-built with the old `episode_desks` order, which must be rebuilt by (a).

  Falsifier: any of (a)–(d) failing in the image while it passes on the host.
- **P4: nothing else changes.**
  - T10's golden outputs and statement counts are unchanged, except that the statement count of a capture pass on a complete projection falls by the removed `quick_check`.
  - Every existing test passes unchanged. The regression-set exception is a T10 test that asserts the old `episode_desks` DDL text; TEST lists it if any.

## Write scope

- **FEATURE:** `_impl/service/session_sources.py` (DDL, the rebuild, the early return, a schema-only entry for writers), `_impl/service/workspace_capture.py` (`initialize()` never backfills), and `docs/MEMORY.md` if it names the DDL or the step.
- **TEST:**
  - new files under `tests/`: in-process tests for P1's rebuild and P2's trace, and the image-marked P3;
  - the P4 exception, if any.

## Deploy (after merge)

1. Build and deploy as T9b's `deploy` script does: stop, back up in the VM, plan, apply. `prepare` is a no-op on a prepared root.
2. `up`, then `upgrade-sources` in the tooling container. Expect the rebuild, then coverage.
3. Confirm `quick_check` → `ok` in the VM.
4. Run `up -d capture`, and confirm two watch passes, each exiting cleanly with the pass interval respected.
5. Confirm `memory.search` from an admitted desk reports a complete index.
6. The deploy script prints `memory_store.operator_files_naming_old_store_paths` in full, and refuses to run `up` while that list is non-empty. That guard closes the 2026-10-03 truncated-print outage; it is a script change, not an order property.

## Not in scope (carried)

- **T12:** the watch loop must not end on a transient or store error. Retry with backoff, and report it. That covers the 2026-10-03 git-identity exit and this `RuntimeError`. T12 also rules capture's restart policy `no`, under which one uncaught error is an outage until someone notices.
- **T12 or a later slice:** a newer SQLite in the image (bookworm → trixie, or a bundled SQLite). P3 keeps the image honest meanwhile.
