# T12b — One indexer and an outbox: seals append to an outbox; one leased indexer drains it from a watermark; build-and-swap reindex

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

Status: frozen 2026-10-04 (T12 draft r5, Coordinator). Verification ruled r5 freezable after T12a merges (2026-10-04). Base: main with T12a merged. [Census](T12-census-42a7c783.md) (main after T11b; T12a touches no index writer), re-checked for T12a's delta at the base. The freeze PR also carries the Coordinator's golden base-reset: the T11a goldens are regenerated at the base and T11b's `RULINGS` is emptied (B4).

Principal direction (2026-10-02, accepted): "net new transcript content is a direct write that can be indexed separately in the background — the two actions shouldn't be bound together in a single writer"; "indexing should only be reading from the last value indexed forward".

**r5 (Coordinator, 2026-10-04).** This revision adopts Verification's r4 review: three blockers decided (marked "r5" below) and amendments 4–11 folded in. Under r5 the hook path does no indexing inside Compose.

**r4 (Coordinator, 2026-10-04).** This revision follows the census re-run at main after T11b (T12a touches no index writer). It corrects:
- the writer count: W11;
- B4's test list;
- two false premises: B1's `ensure_catalog` and T5's `quick_check`;
- the drainer gap for a tooling+board install.

It also adds the census's write-scope facts: `leaf.py`, the goldens, and the mkdir order. Census references are `Dnn`. The base is main after T12a, because both T12a and T12b edit the packaged `compose.yaml` and its role list.

**Problem (census).**
- **Eleven index-writer paths:** W1–W11.
  - Eight run inline across four roles: capture, board, host CLIs and the model gateway. W1–W6 upsert, and W7–W9 rebuild in place.
  - W10 writes the coverage token.
  - **W11** (D20) is `extensions/ops/scripts/reconcile_desk_history.py:65`. It rebuilds an isolated copy in place, by a plain, unmarked path, outside the T11 guard. It was missed by the first census.
  - W3 shares W2's line (`launch_binding.py:680-681`, D12), so W1–W10 are nine textual edits.
- **Five pending-index states** sit in five files (D30–D35).
- **No outbox, watermark or lease** exists.
- **`rebuild()` deletes in place** while holding a store SHARED lock, with the store in rollback-journal mode (es:101-107).
- **The board role seals inside Compose.** It runs W1(b)/W2 through `kp-agent-launch`, W1(c) through the assistant hook, and W6 through session import. A tooling+board install, with no capture, is valid (runtime_install.py:244).

**Properties.**
- **B1: the outbox, as schema (Q4, S4).**
  - **The table.** `index_outbox(seq INTEGER PRIMARY KEY AUTOINCREMENT, reason TEXT NOT NULL, episode_id TEXT, session_id TEXT, detail TEXT)` lives in the STORE. `reason` is one of `seal`, `desks_changed` or `reindex`. `detail` is nullable JSON, reserved for T12c's old and new desk sets.
  - **Where it is installed (r5, replacing "`ensure_catalog()` guarantees").** `ensure_catalog` has two callers (ss:385, workspace_capture.py:263), and most sealers never call it.
    - **One file, one connection path.** All four watched tables (`episodes`, `source_episodes`, `session_claims`, `session_episodes`) are in the one store file. Every sealing INSERT (the eight statements, C8) reaches it through `EpisodeStore._connect` (episodic_memory.py:106-110); `SessionSources` uses `self.store._connect()`.
    - **The hook.** The writable `_connect` therefore ensures the outbox. It does a read-only check (FEATURE names it, e.g. `PRAGMA user_version` or a `sqlite_master` lookup) and, only when the outbox is missing, runs `CREATE TABLE IF NOT EXISTS` and `CREATE TRIGGER IF NOT EXISTS` for every watched table that exists.
    - **Tables created later.** A watched table created afterwards gets its trigger in the same schema step that creates it: `EpisodeStore`'s schema, and the catalog schema at ss:28-34.
    - **Coverage.** A store created at any earlier version gets the outbox on its first writable open by the T12b code, before any seal in that connection.
    - **Cost.** The check runs on the hook path, so FEATURE measures it per hook flow, as T11b did, with a bound: no write when the outbox exists.
  - **Every insert appends an outbox row in the same transaction.** That covers every insert into `episodes`, `source_episodes`, `session_claims` and `session_episodes` (the eight statements, C8), by any path, including `legacy_desk_import`, `session_catalog_import`, delegation claims and older-writer SQL. The triggers write exactly one row per insert (T2):
    - `episodes` and `source_episodes` → `seal`, with `episode_id`;
    - `session_episodes` → `desks_changed`, with `episode_id` and `session_id`, `detail` NULL;
    - `session_claims` → `desks_changed`, with `session_id`, `episode_id` NULL, `detail` NULL.
  - **Desk sets, for T12c.** In the same transaction, `_reflect` sets `detail` on the row its trigger just wrote: the latest row for that `session_id` and reason, by `seq`. With no projection installed, `_reflect` returns early and `detail` stays NULL.
  - **Falsifiers:**
    - a path inserting a watched row with no outbox row in the same commit;
    - an outbox row whose insert rolled back;
    - a claim or link producing more than one row;
    - for each seal path, a fresh store opened through that path alone lacking the outbox or its triggers (r4);
    - **existing stores (r5):** a store created at BASE (a T10 corpus store, and one shaped like the live T10h-upgraded store), sealed through each of the eight statements by the T12b code, not having exactly one outbox row per insert;
    - capture, claim or link opening the index (`t10_instruments.connected(index_path)`).
- **B2: one indexer (Q2, S1, S5).**
  - **The library function.** Indexing is `drain(store, index, lease, batch)`.
  - **The lease.** A non-blocking exclusive lock on `<state_root>/index.lock` on the volume, plus an advisory row recording the holder and the time. A held lock means skip, never wait. The leaf gains the non-blocking lock primitive; precedent: workspace_capture.py:334-338.
  - **Watermark and retention.** The watermark is the highest outbox `seq` applied. It lives in the INDEX and advances in the same transaction as the postings.
    - **Retention (S1, r5).** After each committed batch, the drainer that applied the batch deletes the rows ≤ the watermark, at most `batch` rows per store transaction. Inside Compose that is only the `indexer` role, so the store's `BEGIN IMMEDIATE` for retention is never on the hook path.
  - **The lease's advisory record (r5).** It is the lock file's own content: holder pid, host and acquisition time. It is not in the index (the swap replaces the index file) and not in the store.
  - **Who drains (r5, the Principal's direction: indexing is a separate background action, not bound to the writer).**
    - **Inside Compose, sealers never drain.** The `indexer` role is the only drainer. Inside Compose means `AGENT_MEMORY_VOLUME` is set, the same marker as T11b Q1; it also covers `docker exec` and one-off runs. No hook, capture pass, board process or gateway in the runtime opens the index.
    - **The `indexer` role.** It runs `drain` continuously, at a fixed interval of **2 s**, set in the packaged manifest's command and read from there by tests. It follows T12a's loop rules: backoff, `--max-passes`, restart `unless-stopped`.
    - **Its command carries the `--watch` token (T12a R1),** so a store-preflight refusal makes it wait, not exit.
    - **A lease skip is a clean pass.** It is not an error, and it does not advance the backoff.
    - **Host installs without Compose** (`AGENT_MEMORY_VOLUME` unset) keep drain-after-seal, as r3 had it. Each sealing host CLI (W1(a) hook, W4 gateway, W7 host card, W8 import) calls `drain` once after its seal, without blocking; a held lease means skip.
    - **Selection.** `indexer` is selected whenever capture or board is: both seal. A tooling+board install has a drainer.
    - **The third case: unsupported for indexing (r5, Verification).** A host process can set `AGENT_MEMORY_VOLUME` without a Compose `indexer`; T11b allows that, because setting it only refuses more.
      - Such a process seals but never drains, and nothing else drains for it. That is unsupported for indexing.
      - `verify`'s gap line ("capture or board selected and no `indexer`") is the one place it is reported, so a search that never updates has a named cause.
    - **`runtime_install`.** `SERVICES`/`COMPONENTS` gain `indexer`. The manifest-equals-`SERVICES` rule (:482-484) and the memory-volume rule (:485-490) apply to it.
    - **Upgrade of an existing runtime root (r5).**
      - Adding `indexer` makes every existing manifest `manifest_contract_changed`. The operator step is `plan`, `apply`, then `up -d`.
      - Until then, `verify` names the gap: a root with capture or board selected and no `indexer` service is reported by name, never silent.
      - The first drain on an existing store applies nothing. The outbox is created empty, and everything sealed before the upgrade was indexed inline.
      - The upgrade writes no `reindex` row unless the store is an **older-writer store**. That is defined by the existing coverage computation in `upgrade-sources` (ss:1316-1317): the index does not cover every sealed (episode, digest). The live T10h-upgraded store is complete, so it gets none.
  - **The other writers are removed.** No module, including `extensions/ops/scripts`, other than `drain` and the reindex writes `event_search`, `indexed_episodes`, the watermark or the coverage token. The five pending states retire. Their fields carry seal or replay state only, and receipt shapes are unchanged: T11a's P7 golden, `"indexed": false` ×20, stays byte-identical.
  - **`index_lag` (T4).** It is the outbox row count, reported in three places:
    - `memory.connection_status` (episodic_memory_tools.py:377-381), as a number beside `status`;
    - the `indexer` role's per-drain JSON line;
    - the `indexer` role's health (r5): unhealthy when the watermark has not advanced across **5** consecutive non-skipped drains while outbox rows exist.
      - A drain that skipped on a held lease does not count.
      - Steady inflow with a moving watermark is healthy, so a post-deploy catch-up never reads as unhealthy.
  - **Falsifiers:**
    - a statement trace with `db == index_path` from any module other than the indexer;
    - two concurrent drains, both writing;
    - a restart that re-applies below the watermark or skips above it;
    - more outbox rows after N drained seals than the batch bound;
    - a tooling+board install whose assistant capture is not searchable within 2× the indexer's interval (r4);
    - **(r5)** inside the runtime, any process other than the `indexer` role opening the index, or deleting outbox rows (statement trace, per role);
    - **(r5)** an indexer whose health reads unhealthy during a catch-up in which the watermark advances.
- **B3: build-and-swap reindex (S2, S3).**
  - **Building.** A full reindex builds a new file beside the old one, under the lease, and never deletes in place. It reads episodes in batches, each its own read transaction, and never holds a store read across the build.
    - The build file's name is a leaf constant: the P6 guard requires `.sqlite3` names in the leaf.
    - The swap uses a new leaf rename-into-place primitive, with fsync of the file and its parent (T11b Q3).
    - **(r5) Its own P3-style falsifier.** These must be observed, using the T11b N4 fd-identity instrument for the fsyncs:
      - fsync of the built file, then the `rename`, then fsync of the parent directory;
      - both names are regular files in the same directory;
      - no symlink at either name (`lstat` before the rename).

      O_NOFOLLOW does not apply to `rename`, so that is what is checked instead. FEATURE names its check for "no hot journal of the old file".
  - **Watermark.** The build records the outbox watermark at its start. Rows sealed during the build are drained after the swap.
  - **Coverage token, option (i).** The new file carries the old token only if the build verified, before the swap, that it holds every (episode, digest) the marks claim. Otherwise the reindex refuses and keeps the old file.
  - **When the check fails (T5, r4).** The refusal names the episodes. The operator's step is an explicit integrity check, `PRAGMA quick_check` run inside the runtime (DOCKER.md gains the command), then the recovery docs.
    - It is not `upgrade-sources`: on a complete projection, `upgrade-sources` returns before any `quick_check` (ss:1208-1214).
  - **The swap.** It runs under the lease, with no hot journal of the old file; FEATURE names the check. Readers keep `ATTACH ?mode=ro` via `leaf.sqlite_uri`.
  - **Callers request a reindex (r4 adds W11).** `index-history`, `import-native-history`, `host_card_bridge` and `upgrade-sources` (the one-time request for older-writer stores) each write a `reindex` outbox row.
    - **W11** (`reconcile_desk_history.py`) builds its isolated copy's index through the same build function, never an inline `rebuild()`, and opens it through the leaf (a marked path).
      - The copy is on a host path with `AGENT_MEMORY_VOLUME` unset, so the mark refuses nothing there.
      - The conversion's value is one rebuild implementation, not a refusal.
    - None of them rebuilds inline, and the indexer never infers that a full build is wanted.
  - **Falsifiers:**
    - `DELETE FROM event_search` on any reindex;
    - covered 0 reported during or after a swap on a store whose marks were complete;
    - a seal during a build that is not indexed after the swap;
    - a reader error during the swap;
    - W11 calling `rebuild()` (new in r4).
- **B4: synchronous tests (r4: the corrected list, D124–D127).**
  - **In-process reliers** call one test helper, `drain()`. This is a regression-set exception limited to adding that call. The list, re-derived:
    - the census list minus `opstests/test_launch_claude_desk_memory.py:270`, which is a setup-missing diagnostic, not a relier;
    - plus `opstests/test_claude_memory_hook.py:136-137, 161`;
    - plus `tests/test_workspace_capture_adversarial.py:104-110`.
  - **Image reliers cannot call `drain()`.** They are:
    - `test_t7b_p2_desk_tasks_image.py:149-185` and `test_t7b_p4_assistant_memory_image.py:157-163`, which search once, right after a board-role hook;
    - `test_t7a_p2_documented_steps_image.py:391-408` and `tests/host/test_t7a_p7_unconfigured_capture_image.py:80-85`, which already poll.

    **(r5)** The named exception lets t7b_p2 and t7b_p4 wait for the search to answer, bounded by 2× the indexer interval, instead of searching once.
    - The interval is read from one place: the rendered manifest's `indexer` command.
    - Inside Compose only the indexer indexes, so the wait is the indexer's interval, not a lease race.
  - **Goldens.**
    - T10's goldens are unchanged.
    - `test_episodic_search_incremental`'s DELETE counts are re-read against the indexer.
    - **The golden base (r5, Verification's option a).** T12b's base commit carries a Coordinator base-reset in the freeze PR:
      - the T11a goldens (P1, P3, P4, P7, U3; core and ops) are regenerated at the base by their committed generators;
      - Verification checks the regeneration byte-reproducible on its side;
      - T11b's `RULINGS` is emptied in the same commit. Its record stays in git and in T11b's Amendment 1.

      Each slice then has one base. T11a's P3/P4/U3 tests read through `assert_matches_base_except_ruled` against that base.
    - **T12b's own regression set.** TEST extends `tests/test_t11b_regression_set.py` (or a T12b sibling) with a **B2 kind** in `label()`/`ruling_mismatch`: connection removed from a seal flow, a step's connection count changed, and an index open replaced by `drain`/the reindex. T12b rules its own differences there:
      - `import jobs: advance` #7;
      - `launch: first codex hook` #41;
      - `search index: rebuild` #1;
      - those steps' counts.

      Every other entry stays byte-identical to the T12b base.
    - **P7** stays byte-identical.
- **Order of checks (from Verification's T11b notes).** `episodic_search`'s `parent.mkdir` (es:58) moves after the store check, so a marked path outside the volume creates nothing. `desk_binding:87` and `episodic_memory:94` are out of scope and stay carried.

**Write scope.**
- **FEATURE:**
  - `episodic_search.py`: the indexer, `drain`, the build-and-swap reindex, and the mkdir order;
  - `session_sources.py` and `episodic_memory.py`: outbox schema and triggers where the watched tables are created, plus coverage;
  - the nine W1–W10 sites (writers removed, `drain` calls added);
  - W11, `extensions/ops/scripts/reconcile_desk_history.py`;
  - `workspace_capture.py` and `session_import_job.py`: pending states retired;
  - `desk_cli.py`;
  - **`_impl/leaf.py`**: the non-blocking lock, the rename-into-place primitive, new file-name constants, and the guard's allowlist or P8 references;
  - the packaged `compose.yaml` plus `runtime_install` `SERVICES`/`COMPONENTS`: the `indexer` role and its selection rule;
  - `episodic_memory_tools.py`: `index_lag`;
  - docs: DOCKER.md (the role, the selection, the integrity command) and MEMORY-RECONCILIATION.md (W11).
- **Coordinator (freeze PR):** the golden base-reset at T12b's base (regenerated goldens, `RULINGS` emptied), verified byte-reproducible by Verification.
- **TEST:**
  - new files;
  - the B4 exceptions: the in-process `drain()` call and the bounded wait in t7b_p2/p4;
  - the P4 exception through T12b's own regression set: `tests/test_t11b_regression_set.py`, named in scope, with the B2 kind;
  - the rename-into-place primitive's falsifier;
  - the existing-store outbox falsifier, with base-created stores;
  - the role-pinned install tests named for the new service (s3_harness:35-39, t9b_harness:78, test_p4_rendered_isolation, t7a P1/P1-image/P2, t7b P1/P2/P4).

## Amendment 1 (meet rulings, Coordinator with Verification, 2026-10-04 to 2026-10-05)

Entries are in the order they were ruled; where a later entry supersedes an earlier one, the earlier one says so.

- **The B1 ensure check versus "every other P4 entry stays byte-identical" (TEST AMBIGUITY 1).** RULED: a **B1 kind** in `tests/test_t11b_regression_set.py`.
  - (i) It excuses exactly one change per entry: `first_statement` becomes the ensure check. Every other field is identical.
  - (ii) The check is ONE read statement, pinned by text in the kind. Verification's preference is `PRAGMA user_version`, with the outbox install bumping `user_version`; FEATURE may adopt it or argue otherwise with a measurement. FEATURE names the statement.
  - (iii) DDL or any write as a first statement is red, except on a pre-T12b store's first writable open (covered by the base-created-store test). That happens exactly once per store: the second open shows the same first statement as a fresh store.
  - FEATURE's hook-path cost measurement includes this statement per writable open, with the count of writable opens per hook flow stated.
- **`index_lag` versus "T10's goldens are unchanged" (TEST AMBIGUITY 2).** RULED: a named exception for T10 P5's `status` entry only.
  - The one difference is an added integer `index_lag`, and its value is pinned in the fixture at **0**, because the test helper has drained at that step.
  - Every other T10 golden stays byte-identical.
- **The three inline-failure tests (TEST AMBIGUITY 9).** RULED: a named regression-set exception for exactly these three, with replacements written by TEST at the meet. Each keeps its original subject and is not weaker.
  - `test_workspace_capture_adversarial::test_large_visible_row_is_reconstructable_and_index_failure_repairs`: the large visible row stays reconstructable; an index outage during a drain loses no seal; and the next drain indexes it.
  - `test_claude_memory_hook::test_mapped_large_attachment_does_not_block_short_stop`: a short stop with a large mapped attachment still does not block, and the seal is asserted never to open the index (the P4 honesty instrument). Without that assertion, "does not block during an outage" would be true by construction, and the test would become vacuous.
  - `test_native_history_import` replay: the replay seals exactly once, and `indexed_episodes == 1` after one drain, not inline.
  - The exception names each old assertion and its replacement.
- **Smaller readings, accepted:**
  - "only the indexer writes" counts writes; read-only ATTACH by readers and operator verbs is allowed;
  - the role pins reach t7b_p3 through `s3_harness`;
  - seal rows' `session_id` is not asserted at T12b.
- **Carried to T12c.** T12c's per-desk postings read `desks_changed` rows by `session_id`. A mutant that drops `session_id` on seal rows survives T12b, and T12c must kill it.
- **R2 as implemented at the meet (Verification accepted).** `index_lag` is pinned exactly per step: **0** at 51 steps, and **2** at `capture-alpha-unindexed`. That one step deliberately leaves its seal for the indexer, so its two outbox rows, the seal and the link, are waiting. Do not "fix" it to 1 or to 0. Draining there would change 9 other reads (measured), so the exact pin keeps the goldens byte-identical, and a drainer that never drains still fails.
- **The seam reconciliation at the meet.** None of these was a FEATURE defect.
  - The only-indexer positive control counts drain writes during or after the flow, because host drain-after-seal runs at every former inline site, ruled correct.
  - `SwapRun`, the retention test and the swap-fsync test request the reindex through `seams.request_reindex`, which writes the row only. In host mode `index-history` drains inline.
  - The restart crash hook ignores SQLite-internal trace statements (`--`, `'schema'.'table'`), because FTS5 runs them during COMMIT.
  - The host card is dropped from the DELETE test, per W7.
- **W7 (Verification's finding, ruled).** A card event requests no reindex: `attach_card` seals and, on a host install, drains once. This amends B3's caller list; `index-history`, `import-native-history --apply` and `upgrade-sources` (older-writer only) keep the request.
  - **Consequence:** a card's success does not depend on the index. With an unreadable index, `attach_card` returns, and its receipt reports `index_unavailable` with the drain result. The card's seal and claim are committed and visible in the store before the receipt returns, the next drain indexes the card, and the readable-case receipt is byte-identical.
- **R3 extended to six tests (Verification):** the original three, plus `test_claude_episode_capture::test_index_failure_replays…`, `test_session_import_job::…index_repair_is_durable` and `test_host_card_bridge::…retry_repairs_projection`.
  - `tests/t12b_ruled_exceptions.py` records each old assertion and its replacement.
  - Under stubs, each replacement is RED on the old inline path and RED on a dropped outbox row.
- **T10h P2 `marks_behind`, both modes:**
  - the Compose case, under the marker, with the assertion as written;
  - a host case asserting that a host seal drains what it finds, so `covered` moves.
- **The in-process Compose control (Verification).** `tests/test_t12b_b2_compose_control.py` checks W1, W4, W5, W6 and W7 under the marker: no index connection, `drain_after_seal` returns None, and the outbox waits for the test's `drain()`.
  - The one patch is `leaf.STORE_ROOT` pointed at the temp root. `docker_runtime()` reads the real variable.
- **`test_t10_p6_upgrade`: R4 (Verification, re-ruled at the meet).**
  - **The first ruling was wrong.** It told FEATURE to exclude `index_outbox` from T10's projection discovery in the product. That discovery is TEST code (`tests/t10_tamper.py` `_tables()`), and in the product fresh and upgraded stores cannot be equal without breaking B1. This was a claim about code not read, and it cost a round.
  - **R4.** `index_outbox` is a STORE table in `t10_tamper` (`STORE_TABLES`), not projection.
    - P6 compares its DDL, the four watched tables' triggers and `user_version` = 1 exactly between the fresh and the upgraded store (`store_table_schema`), and excludes only its rows.
    - The rows case, an upgraded store whose triggers do not fire, is carried by `test_b1_a_store_created_at_base_gets_one_row_per_insert`.
    - It is recorded as R4 in `tests/t12b_ruled_exceptions.py`, and its checker checks it.
    - Comparing after a drain was rejected: it binds P6 to retention.
- **The B1 kind as implemented at the meet (Coordinator), in `tests/test_t11b_regression_set.py`.**
  - **What it excuses.** A P4 entry reads `B1 ensure check` only when its connection is a writable store open (the store file, `mode=rw`) whose head first statement is exactly `PRAGMA user_version`, with every other field identical. Inside a B2 step, the connection must also be kept at its own position.
  - **What it refuses:** DDL as the first statement, the check on another file or on a read-only open, or the check beside any other change.
  - **Interaction with B2.** B2 compares the connections outside the index runs modulo B1.
  - **One named run wrapper** (`B2_RUN_WRAPPERS`, under R3). The import job's journal transaction that wrapped its retired inline index repair may be dropped, but only with the index run it immediately precedes and only when that run really changes. It is never dropped alone, and no other non-index connection may be dropped.
  - **RULINGS: 48 P4 entries at first** (50 after the bounded-reset fix, 53 after the universal bound; see below). 23 are B1, and 25 are B2 over three steps: `import jobs: advance`, `launch: first codex hook` and `search index: rebuild`. The instrument tests show B1 and the wrapper excuse nothing else.
- **T11b Q2's options test (meet).** In a step that reads as B2, the positional timeout and option comparison gives way to the B2 reading. That reading already holds Q2's property: kept connections are identical except for B1's first statement, and drain or reindex connections carry a base connection's open profile. A changed timeout on a kept connection is still red (mutant M14).
- **The bounded post-swap coverage marks (Verification).** It is in scope for B3's intent ("a reindex must not block seals") but outside its letter, and FEATURE wrote it unasked. It was accepted with three conditions:
  1. **TEST's property, `tests/test_t12b_b3_bounded_coverage_marks.py`.** It is RED on the product before them (3 of 5 cases) and GREEN with them. It checks that:
     - the calls are bounded by `batch` and only the first resets on a new token (superseded below: MARK_BATCH, read per store transaction);
     - a carried token never resets and marks only the uncovered episodes;
     - the two empty-call edges behave as specified;
     - the indexer is the only `mark_coverage` caller in the flows.
  2. **The re-measurement** on the backup copy with them, recorded in the packet.
  3. **The soundness premise.** The uncovered set is read outside the marking transactions. That is sound because the indexer is the only coverage writer in the flows; the test states this and measures it.
- **Blindness (Verification ruling).** FEATURE's item-5 measurement prompt asked for a file without giving its path. FEATURE located it with `docker inspect` (metadata only) and a name-only `find` that walked directory names in other trees, possibly including other arms' scratch directories. It printed no other names.
  - The lapse is the Coordinator's.
  - **The rule:** a prompt that asks for a file gives its path. It goes into the retro set beside T12a's FEATURE copy into the shared scratchpad.
- **Verification's read of the round-1 meet head: G1–G4, H1/H2.** Each point below was a stated property that no test could turn red; each now has one.
  - **G1.** The indexer's health rule (B2, lines 77–79) and T12a's loop rules for the indexer loop (line 59), tested in-process on `watch(..., clock=<fake>)`.
    - After 5 non-skipped stalled passes the line reads unhealthy, `indexer-health.json` is written, and `health(root)` exits 1 naming the store.
    - A held-lease pass leaves the count exactly as it was (this kills MV9), and an applying pass resets it.
    - The loop backs off after a failure but not after a skip, `max_passes` ends it, and it survives a failing store.
  - **G2.** The reindex refusal (B3, lines 99–100) names the episode, keeps the old file's inode and token, removes the build file, and search still answers. An untampered store is the positive control.
  - **G3.** `OutboxAhead` (`index_ahead_of_outbox`), FEATURE's addition beyond the order: a restored store with a newer index reports it and applies nothing, and a reindex resolves it.
  - **G4.** `index_lag` is ONE function in `episodic_memory`, which both `connection_state` and `episodic_search` call (FEATURE fix (c)). Before the fix there were two implementations, and `_lag` returning 0 survived every test. G1 asserts that the per-drain line's `index_lag` equals the outbox's row count before and after an applying pass, which kills MV12.
  - **H1, FEATURE fix (a).** A failed write of `indexer-health.json` is reported in that pass's line and the loop goes on, because only a signal or `--max-passes` ends it. A missing or stale health file reads as unhealthy (fail-safe).
  - **H2, FEATURE fix (b).** The first `upgrade-sources` on an older-writer store that has sealed episodes and no index requests the one reindex (`once=True`), and an empty store requests nothing. DOCKER.md says an absent index covers nothing.
  - **MV11 accepted (Verification).** `_commit`'s watermark guard against a writer outside the lease is defence in depth behind the lease, and the lease has its own test.
- **The bounded-marks re-measurement (condition 2).** It runs under the role's limits (`--cpus 0.5 --memory 512m`), on a copy in a named VOLUME, which is the role's I/O path. A bind-mounted copy is I/O-bound on Docker Desktop's virtiofs and counts as a worst-case row only. Every row states volume or bind. The table is recorded in the packet.
- **Seam fix at the meet (Coordinator):** `tests/install/test_t12b_b2_indexer_health_image.py` `_health_argv`.
  - **The defect:** TEST, writing blind, ran a `CMD-SHELL` healthcheck's string as argv. Docker runs `CMD-SHELL` through `/bin/sh -c`, and FEATURE's rendered indexer healthcheck is `CMD-SHELL` with `&&`. So every `docker exec` returned 127, while the indexer's own log read healthy throughout.
  - **The fix:** `CMD-SHELL` → `["sh", "-c", <string>]`. `CMD` keeps argv.
  - **A second defect in the same test:** health was judged by a word search ("unhealthy", "stalled", "refused"). A healthy record carries its rule text and a `stalled` key, so the reading is now the record's `status` field.
  - **Result:** alone at the t12b-meet images it passes: 35 samples, 23 with outbox rows, indexed 0→1631, health exits [0].
  - Both are TEST seam defects, not FEATURE ones (fixed at the meet).
- **Image suites at the round-1 meet head:** 179 passed and 4 failed.
  - Three were load timeouts (`docker diff` 120 s, the Engine API socket in P5, `kanban --help`), all at load averages 13–15. Each passed when rerun alone at load 7–8.
  - The fourth was the health test above.
  - `check_ops_variant`: 8 properties hold.
- **The timed reindex, Verification's run of record at the round-1 meet head.** Product image b1183e00, one-off container with `--network none --init --read-only --memory 512m --cpus 0.5` and `AGENT_MEMORY_VOLUME` set. The copy sat on the named volume `t12b-verif-copy`, with sha256 inside the container equal to the source. The store held 2,278 episodes plus 226,332 source episodes, giving 228,610 episodes and 755,459 events built.
  - **VOLUME (representative):**
    - build to rename 851.8 s, whole drain 865.5 s;
    - token carried; the post-swap marks were 1 `mark_coverage` call with 0 episodes, 0.075 s, and no reset (the 367 s lock is gone);
    - a concurrent sealer probe (`BEGIN IMMEDIATE` every 0.25 s, 2,893 probes) waited at most 0.859 s, with 0 timeouts, so "a reindex must not block seals" holds under the role's limits;
    - **peak RSS 347.5 MB of 512 MB (68%).**
  - **HOST BIND (worst case, abandoned):** after 44 min, 799 MB of 1.34 GB was built (virtiofs-bound). A SIGKILLed build leaves its build file, which `_discard` removes on the next build.
  - **Memory headroom (Verification).** The build's memory grows with the store: a digests dict per episode plus SQLite caches. At about 1.5× today's store, the indexer role (`mem_limit 512m`) would be OOM-killed mid-build, Docker would restart it, and the reindex row would still be waiting, so the loop repeats. G1's rule then reads `unhealthy` after 5 stalled drains, which is the right signal.
    - **The operator remedy is written in DOCKER.md's indexer section:** raise the running indexer's limit with `docker update` (see the DOCKER.md entry below), then let it build.
    - **Carried to T12c:** a bounded-memory build. It is not a T12b block.
- **New-token post-swap marks (Verification ruling, 2026-10-05).**
  - **The measurement.** FEATURE's L3 run with the bounded marks (a forced new token, re-marking every episode in batch-500 transactions under 0.5 CPU and 512 MiB) had a longest store write transaction of 6.07 s. One prober commit waited 7.76 s against the sealers' 5 s busy timeout. That is a blocked seal, measured, so it is NOT acceptable under B3 ("a reindex must not block seals").
  - **The ruling:** a separate module constant `MARK_BATCH = 100`, beside `DRAIN_BATCH`, bounds the episodes per `mark_coverage` store transaction. It is never `batch`, which counts outbox rows per index transaction, a different quantity; coupling them produced the defect. TEST's bounded-marks property asserts ≤ MARK_BATCH per call and ≥ ceil(N/MARK_BATCH) calls (superseded below: read per store transaction once `mark_coverage` chunks).
  - **Acceptance for the re-measurement:**
    - **conditions:** the role's limits, a named volume, the role's mode (`AGENT_MEMORY_VOLUME` set), a new token forced, host load ≤ 8 and reported;
    - **pass:** longest store write transaction ≤ 2.5 s (half the sealers' timeout), and 0 prober waits ≥ 5 s;
    - **reported beside them:** build-to-rename and RSS.
  - **Carried to T12c:** a set-based mark. 500 marks at 1.2–6 s is 2–12 ms per row in `_cover`.
- **`health()` on a missing record reads exit 1 `absent`: ACCEPTED (Verification).**
  - **Why:** it is fail-safe. `start_period: 30s` with `start_interval: 2s` covers the first pass, and a role still waiting on its store preflight already reads unhealthy through `tooling-container health`.
  - **TEST pins all four states:**
    - absent → 1;
    - after the first write → 0 healthy;
    - a record whose writer pid is not running → 1 `stale`;
    - garbage bytes → 1 `unreadable`.
  - **Stated reading, not a fix:** a loop that is alive but hung (making no passes) keeps a healthy record and reads healthy, because the stall rule cannot see a loop that makes no passes. That is T12a's watchdog class.
- **Fix (b), its assumption (Verification's read).** It counts `episode_scope` rows, which exist only once the projection is installed. `_establish_coverage` is reached from `upgrade` after that install, and H2's docstring states this.
- **Fix (c) (Verification's read).** `outbox_lag(db)` re-raises any OperationalError other than "no such table": stricter than before.
- **The 852 s vs 152 s gap: two variables.** Verification's row ran at host load 13–15, during the Coordinator's image suites, with the role's mode set. FEATURE's ran at load 5–7, with the mode unset. Verification re-measures at the packet head in the role's mode at load ≤ 8. If the gap is still over 2×, mode is A/B-tested alone. Until then, the row of record is the role's mode.
- **TEST round 2 (a fresh agent; the first TEST agent hit its context limit with G1–G3/H1/H2 uncommitted).** Every red/green row is from the new agent's own runs (Verification's condition). Its head was merged into the meet branch.
  - **H2 was redesigned.** As first written it could never pass against a correct fix: on a host, desk_cli drains the requested reindex at once, so it was split into a Compose case and a host case.
  - **`stale` is not run on macOS** (no /proc). Linux CI runs it, and Verification runs it inside the container.
- **A TEST defect found at the meet.** TEST's `_p4_mutated` was written while RULINGS was empty. It set a temporary ruling and then deleted the key, which deleted the meet's real ruling for every key its mutations touched.
  - **Why it hid:** T11a P3/P4/U3 and T11b Q2 read RULINGS in the same process, so they failed whenever the regression set ran first. Normal file order hid this; only a combined run showed it.
  - **The fix:** the helper saves and restores, and an autouse fixture fails any test in the file that leaves RULINGS changed. Reintroducing the defect makes both B2 instrument tests ERROR.
- **R4b (Verification ACCEPTED, read at the meet's P6 fix).** After fix b, P6 takes the older copy index-less from the fresh store as sealed, then drains the fresh reference (what a running indexer leaves). It then requires that:
  - every read answers `results`;
  - `_reads(older) == expected_reads`;
  - for both stores, `scope_state.coverage_token` equals the store's own index's token, a property the old P6 never had.

  Projections are compared with only that one random value set aside (`_token_free`, keyed on `('str','coverage_token')` in `scope_state` alone). R4 stands beside it, and R4b is recorded as `r4b` in R4's entry.
- **MARK_BATCH (FEATURE, Verification read).** `MARK_BATCH = 100` is its own quantity, used only in the post-swap marks (superseded below: every mark, "one rule, implemented once"); the drain's `batch` is untouched.
- **The reset in bounded transactions (Verification ruled, 2026-10-05; FEATURE's bounded-reset fix, with TEST's cases).** The MARK_BATCH re-measurement failed on the RESET: the first `mark_coverage(reset=True)` zeroed every covered row in one transaction, 16.74 s. The fix, in this order:
  1. While the store still names the old token, `covered` is zeroed in transactions of at most MARK_BATCH rows. Readers already report covered 0 on a token mismatch: FEATURE quotes those lines as the premise.
  2. One small transaction switches the token and resets `scope_counts`, changing no `episode_scope` row.
  3. Then the bounded marks.

  TEST's property counts `episode_scope` rows changed per store transaction (`total_changes` deltas):
  - it is ≤ MARK_BATCH for every transaction, and the token switch changes 0;
  - RED at the MARK_BATCH commit (350 rows in one transaction), GREEN at the bounded-reset fix;
  - the one-statement mutant and a switch-touches-a-row mutant are RED.

  The pin is `test_b3_mark_batch_is_the_product_constant_at_the_ruled_value`: (100, 'product').

  RULINGS gains the reset's two store opens (`search index: rebuild` #7, #8; B2): 50 entries in all.
- **DOCKER.md, memory during a reindex (the meet).** About 350 MB peak on the 228k-episode store against `mem_limit 512m`; an OOM-killed build restarts and rebuilds until the role reads unhealthy. The operator remedy raises the running indexer's limit with `docker update --memory 1g --memory-swap 1g "$(docker compose ... ps -q indexer)"`, which needs no edit of the rendered root and holds until the container is recreated. A supported setting and a bounded-memory build are carried to T12c.
- **The reset fix: ACCEPTED on its numbers** (Verification). FEATURE's bounded-reset fix, in the role's mode:
  - **N3 / N4 (new token):** longest transaction 0.324 / 0.365 s, 0 waits ≥ 5 s.
  - **C3 (carried):** 0.0014 s.
  - **Order:** zero → switch → mark, with the switch touching no `episode_scope` row.
  - **The keyset form.** `episode_scope` is WITHOUT ROWID, so the bounded reset selects its rows by the primary key.
  - **The premise, cited:** episodic_search.py:383-384 and :353-356; session_sources.py:754-775; `connection_state` and `counts(coverage=False)` do not report covered.
- **Every coverage reset is bounded (Verification: FIX BOTH NOW).** `mark_coverage`'s mismatch branch (reached from `drain`: an index lost or deleted is recreated by the next drain with a new token) and `_establish_coverage` (upgrade-sources) both go through `_reset_coverage`. TEST extends the rows-per-transaction property to a drain against a recreated index and to upgrade-sources on a mismatched token, each with the one-statement mutant red.
- **A reading (Verification), not a block, LATER MEASURED NOT TO HOLD (see the round-3 entry below): the prober's 1–2 s waits.** The store runs in rollback-journal mode, so a sealer's COMMIT needs the EXCLUSIVE lock and waits for every open SHARED read to end. The build reads 500 payloads per read transaction under 0.5 CPU, and that read is the ~2 s. So during a build, the bound on a sealer's commit wait is the build's read batch, not any write transaction. It sits inside the 5 s timeout with today's batch and CPU. A smaller read batch, or WAL (a store-wide decision, T12c or later), would shrink it. FEATURE measures it once: the longest read transaction during the build, beside the prober's longest wait.
- **One rule, implemented once (Verification RULED, 2026-10-05).** `mark_coverage` chunks its own marking into MARK_BATCH-row store transactions whatever the caller: drain marks after a batch, upsert, post-swap marks, and the operator's re-mark in `_establish_coverage`. `_cover`'s 2000-row chunk disappears into it.
  - **Why:** the post-deploy catch-up on the live store. Every drain batch of 500 newly applied seals would otherwise hold the store about 2 s under the role's CPU, every 2 s, for the length of the backlog.
  - **Semantics are unchanged.** Each chunk commits `covered = 1` under the token already switched, so a reader between chunks under-reports and never over-reports, and the index commit precedes the marks.
  - **`--batch-rows`** now governs only the projection backfill (desk_cli help).
  - **TEST's invariant is universal:** no store transaction changes more than MARK_BATCH `episode_scope` rows. It is tested on:
    - the post-swap path;
    - a drain against a recreated index;
    - upgrade-sources on a token mismatch, at the CLI default;
    - the drain's catch-up (>1,000 seals, DRAIN_BATCH 500).

    A whole-batch-in-one-transaction mutant is red on all of them.
  - **Measurement, one row more:** the catch-up case, a 1,500-seal backlog drained under the role's limits.
- **The universal bound, as built (FEATURE round 3, with TEST's cases).**
  - **`mark_coverage(digests, token, *, reset=False, held=None)`** (session_sources.py:1104) is the one chunking site: one store transaction per MARK_BATCH chunk, each checking the token. On a token mismatch it calls `_reset_coverage(token)` and retries (:1143), which is the drain's path after a lost index. `_cover`'s 2000-row chunk is gone.
  - **`_reset_coverage`** (:1146) is the only reset. Its docstring lists its three callers and the ordering argument.
  - **`_establish_coverage`** (:1420) resets through it on a mismatch (:1462) and re-marks in MARK_BATCH pages through `mark_coverage` (:1488). Its docstring states why the bounded reset is sound without the index lease.
  - **The post-swap marks** are one `mark_coverage` call, so the bound lives in one function.
  - **`--batch-rows`** sizes only the projection backfill (desk_cli help).
  - **No bulk form remains.** The remaining `covered = 0` sites are: `_reset_coverage`'s id-list UPDATE (:1191-1192, ≤ MARK_BATCH ids), the switch's `scope_counts` aggregate (:1195), `_cover`'s flips inside one chunk (:1220-1221), and new rows starting at 0 (:1004).
  - **TEST's invariant holds on every path:** post-swap (new and carried token), a recreated-index drain, upgrade-sources at the CLI default, and a catch-up of 1050 seals. All three new cases are RED at the bounded-reset fix. A no-chunking mutant is RED on every path but upgrade-sources, which pages by MARK_BATCH itself; with both bounds removed it is RED there too.
  - **RULINGS 53:** the codex hook step's host drain gained three store connections (#47–#49) for the bounded mismatch reset.
- **The measurement, round 3, all PASS** (the round-3 product image 4725fcafbfa0, the role's mode, fresh sha-verified copies):

  | Run | Case | Longest write txn (to end of COMMIT) | Prober max wait | Waits ≥ 5 s | Notes |
  |---|---|---|---|---|---|
  | R1 | new-token reindex | 0.293 s (mark) | 2.78 s | 0 | build→rename 118.5 s; RSS 353 MiB |
  | K3 | catch-up: 1,500 real episodes re-queued | 0.120 s | 0.351 s | 0 | 15 mark txns; drain 1.44 s |
  | K1 | catch-up: 1,500 new seals + 1,500 desks_changed | 0.204 s | 0.339 s | 0 | 18 mark txns; drain 3.48 s |

  Load at start was ≤ 8, with R1's in-run mean at 8.04.
- **The 1–2 s prober waits: Verification's rollback-journal reading NOT confirmed (measured).**
  - **The store is in WAL mode** (`PRAGMA journal_mode` = `wal`).
  - **During the build:** the longest read transaction was 1.423 s, and 28 prober commits went through it, the slowest in 0.008 s. The prober's longest wait during the whole build was 0.216 s.
  - **After the swap:** every wait of 1 s or more (1.05–2.78 s; 5 of them) came 25–59 s after the rename, in the mark phase. No build read was open then, and no drainer transaction exceeded 0.293 s.
  - **Their cause is unmeasured.** A candidate is WAL auto-checkpoints in the prober's own COMMIT. The next step is to split each wait into `BEGIN IMMEDIATE` wait and COMMIT duration, carried to T12c. They stay inside the 5 s busy timeout.
- **B1 (ii), pinned (TEST).** Before this, the Coordinator's mutant M2 survived every suite: it removed `ensure_outbox`'s early return, so every writable open took `BEGIN IMMEDIATE` on an installed store.
  - **The test:** `test_b1_once_installed_a_writable_open_executes_only_the_one_read[fresh|t10_corpus|t10h_upgraded]`. It traces each connection that `store._connect()` opens (`set_trace_callback`, installed as `sqlite3.connect` returns). On an installed store, a writable open executes exactly `PRAGMA user_version`, opens one connection, and returns one that is not `in_transaction`. A base-created store's first open, the positive control, shows the install (`BEGIN IMMEDIATE`, the outbox `CREATE TABLE`), and its second open shows only the read.
  - **Result:** RED under M2 (`['PRAGMA user_version', 'BEGIN IMMEDIATE', 'PRAGMA user_version', 'COMMIT']`), GREEN at the round-3 meet head. The default suite with TEST's pin: 1594 passed, exit 0.
- **The Coordinator's mutation-RED at the final product** (worktree at-meet-t12b-mut, venv at-meet-t12b-mut, whose editable install points at the mutation worktree; each mutant asserted to apply once and its diff stat printed; the tree clean after each restore):
  - **Killed at the round-3 meet head:**
    - M1 the claim trigger writes nothing;
    - M3 the lease is ignored;
    - M4 the watermark is not persisted;
    - M5 an in-place DELETE before the swap;
    - M6 the new file's watermark taken at the build's end;
    - M7 no parent fsync;
    - M8 a host seal never drains (the H2 host test, and the full suite);
    - M9 the plan does not select indexer;
    - M10 a card requests a reindex;
    - M11 retention deletes one row above the watermark;
    - M12b no MARK_BATCH chunking;
    - M13 a carried token marks every episode;
    - M14 the jobs timeout 10→5;
    - M15 three of four triggers;
    - M16 `user_version` 0;
    - M17 the bounded reset zeroes everything in one transaction;
    - M19 `index_lag` always 0;
    - M20 the stall rule never fires.
  - **M2** (the ensure check takes the write lock on every open) survived at the round-3 meet head, which is why TEST's B1 (ii) test was written. **Rerun with that test: KILLED** by `test_b1_once_installed_a_writable_open_executes_only_the_one_read` [fresh, t10_corpus, t10h_upgraded]; baseline 126 passed, 1 skipped.
  - **M18** (the reset never switches the store token): **HUNG**. With that test in place and a 600 s limit per run, there was no result within 600 s. It is detected as a hang, not as a red assertion; see the observation below.
  - **The first run was void for two mutants,** an instrument lapse of the Coordinator's: the meet venv's editable install pointed at the meet worktree, so hermetic installer subprocesses never saw M8 and M9. The venv was rebuilt and those two rerun; all other results stand.
- **An observation for the record: `mark_coverage`'s retry loop has no bound.**
  - **What happened:** under M18, where the reset never switches the store's token, the token mismatch never clears and `mark_coverage` resets and retries indefinitely. The suite hung, and the Coordinator stopped it after 70 minutes (that one pytest process, by pid).
  - **In the product,** the loop converges whenever the switch lands, and a mismatch needs a concurrent swap to recur. But a broken token invariant shows as a hang rather than an error.
  - **A retry cap would turn that hang into a reported error** (for example a few resets, then raise naming the store). Put to Verification, which RULED it into T12b (see the cap entry below; M18 is now a kill).
- **The reset-retry cap (Verification RULED in T12b, after M18 hung the suite).**
  - **The change (FEATURE):** `COVERAGE_RESETS = 3` (session_sources.py:104), and `class CoverageTokenUnstable(EpisodeUnavailable)` with category `coverage_token_unstable` (:107-109). On the mismatch branch (:1154-1159), the fourth reset raises, naming the store, the store token and the index token. A `reset=True` reset does not count, and the normal path is unchanged.
  - **Why (Verification):** a broken token invariant was a silent hang of the one indexer. A hung loop makes no passes, so the stall rule never saw it.
  - **TEST:** with M18 applied in-process (a `_reset_coverage` spy whose switch never lands) and every call under a 10 s `signal.alarm` deadline, `mark_coverage` raises `coverage_token_unstable` within 3 resets. A drain's line reads `error` with that category. RED at the universal bound (every test hit the deadline, about 28,000 resets); GREEN with the cap.
- **Lost coverage marks are held in the store (Verification RULED (c); FEATURE in two commits, TEST in three).**
  - **The defect FEATURE found:** `_drain` commits the index batch, which moves the watermark, before the marks. A mark failure (`coverage_token_unstable`, a busy timeout, anything) lost those marks for good. The next drain deleted the applied rows and went idle, the stall count reset, health read healthy, and coverage stayed unmarked. This predates the cap.
  - **The record.** Any exception after the index commit writes one `scope_state` row, `coverage_marks_lost` = {category, message, seq, at}, in its own small transaction. It is written in `_drain` (es:583), and in `_build_and_swap` around the post-swap marks (es:732, seq = the new file's watermark; the site was confirmed by Verification). If the write itself fails, the error carries `coverage_record: {status: unwritten}` and the next pass's start writes it.
  - **The signal.** `drain_line` reads the record on every pass. While it is present, the line carries `coverage: lost` with `{category, seq, at}` and reads `health: unhealthy`, whether or not rows are waiting. `_health_record` lists such stores under `coverage_lost`, and `health(root)` exits 1. `watch` reads every store's record before its first health write, so a restarted role starts unhealthy.
  - **Only a FULL re-mark clears it** (Verification, superseding its first "any successful chunk"):
    - **What clears it:** a reindex's post-swap marks completing (new or carried token), or `_establish_coverage`'s re-mark completing, and later T12c's repair pass. Each calls `SessionSources.clear_marks_lost()` (ss:1172) in one small transaction after its last chunk.
    - **What does not:** a drain's incremental chunk never clears it.
    - **Why:** FEATURE's probe showed one new seal's drain erasing the record while seven episodes stayed uncovered. That was the silence again.
  - **`advanced` reports the truth.** On an error, `drain_line` compares the index watermark after the pass with the one before, and reports null if either read failed, never a default `false`. The stall rule keeps its one meaning, "the watermark did not move". TEST's scenario-B expectation of 5 stalls came from the old false field and was dropped (Verification).
  - **DOCKER.md, "Lost coverage marks":** the record holds until the whole store is re-marked by `kp-agent-desk --config <session.json> index-history` (a reindex) or `upgrade-sources`. A new seal does not clear it.
  - **TEST's `tests/test_t12b_b3_token_unstable.py`,** 7 tests, all GREEN at TEST's final head:
    - scenario A: one failing pass, then idle passes reading `coverage: lost` and `unhealthy` with `advanced: false`;
    - scenario B: each pass `error`, `advanced: true`, stalled 0, `coverage: lost` with that pass's seq, `unhealthy`;
    - restart: a second `watch` reads unhealthy from its first pass, a new seal's drain LEAVES the record, and a requested reindex clears it, after which health exits 0;
    - the unwritten record, written at the next pass;
    - the cap;
    - `advanced` true when the watermark moved.

    The restart case is RED at FEATURE's first lost-marks commit, where a new seal's drain cleared the record. Mutants, all RED: never writes the row; clears in the per-chunk transaction; the unwritten retry never rewrites; `advanced` defaults to false.
  - **Carried to T12c:** the drain-side repair pass, which re-marks the uncovered rows whose digest the index holds, in MARK_BATCH chunks. The record is what it reads first.
- **RULINGS 54 (23 B1, 31 B2):** `search index: rebuild` #9 is the reindex's clear of the record, on its own store connection after the full re-mark (FEATURE's full-re-mark fix).
- **At the final product:**
  - default exit 0, 1601 passed / 11 skipped / 183 deselected / 17 xfailed;
  - ext exit 0, 505 passed;
  - images built on the first attempt: runtime e423a31e5385, product 2d92d4031660, agents 5809a04a43a1, ops bddb104c2fb9, unrevisioned 8c00f730bfbd;
  - image suites exit 0, 183 passed, no failures;
  - `check_ops_variant`: 8 properties hold.
- **The Coordinator's mutation-RED at the final product, 22 mutants, all KILLED.** Baseline 133 passed, 1 skipped. Each run had a 600 s limit, each mutant was asserted to apply once, and the tree was clean after each.
  - **Killed:** M1–M11, M12b and M13–M17 as at the round-3 meet head, plus:
    - M2 (by the B1 one-read test);
    - M19 and M20;
    - **M18** (the reset never switches the token): 44 red. It is now a kill, not the earlier hang, because the cap turns the stuck loop into `coverage_token_unstable` errors;
    - **M21** (the cap removed): 7 red, through TEST's deadline tests;
    - **M22** (a full re-mark never clears `coverage_marks_lost`): 1 red, the restart case.
