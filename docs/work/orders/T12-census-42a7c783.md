# T12 census: the search-index write path (read-only; the new base is main with T11a and T11b merged)

Re-run of the earlier T12 census, which was taken at the old base, main after T10h (that census's tree and that main are identical). Run in a detached worktree at the base above, by a read-only census agent. T12a is not on main and is ignored.

**Abbreviations** (as before):
- `svc/` = `packages/tooling/src/kp_agent_tooling/_impl/service/`
- `pkg/` = `packages/tooling/src/kp_agent_tooling/`
- `ops/` = `extensions/ops/src/kp_agent_tooling_ops/`
- new: `leaf` = `pkg/_impl/leaf.py`; `opstests/` = `extensions/ops/tests/`; `ss` = `svc/session_sources.py`; `es` = `svc/episodic_search.py`.
- Line ranges are at the new base. "old" is at the old base.

---

**T12a's delta at T12b's base (Coordinator, at the freeze).** `git diff --stat <new base> <T12b base>` over product code touches four files, and no index writer:
- `runtime_install.py`: `volume_presence`, T12a A3. It is inserted after :1898, and every census citation lies before it.
- `service/spool_ingest.py`: an 8-line docstring added in `watch` after :592. Citations at or after :593 shift by +8, so :672-716 becomes :680-724.
- `assets/deploy/compose.yaml`: `restart: unless-stopped` on the `x-role` anchor (:49), so the census's "no service declares `restart:`" is superseded at this base. Service lines shift by +9: capture :149→:158 (its command :167→:176), board :183→:192 (:193/:194→:202/:203), tooling :102→:111 and refresh :117→:126.
- `workspace_capture_cli.py`: the watch loop (T12a A1). The cited :45 (`_await_operator_files`) is now :54. W5 stays in `workspace_capture.py`, which is unchanged.

No index-writer pattern (`EpisodicSearchIndex(`, `upsert_episodes`, `rebuild(`, `coverage_token`, `event_search`, `indexed_episodes`) is added or removed by T12a.

## Delta from the old census

**Status rule.** MOVED = the same code at new lines, including a pure leaf re-spelling (`leaf.sha256_hex` for `hashlib.sha256(...).hexdigest()`, `leaf.private_lock` for the inline flock helper) whose behaviour is identical for an in-volume path. The one behaviour every store path gained in T11b (a marked `leaf.StorePath` is refused at open, in the Docker runtime, when it does not resolve under `/state/memory`) is counted once, as D01, not on every site. CHANGED = observable behaviour or shape differs; the difference is quoted. NEW = found at this base by the census's own discovery method (some NEW items were already present at the old base and were missed by the old census; each says so).

**Counts per status (147 ledger items):** UNCHANGED 40 · MOVED 90 · CHANGED 8 · GONE 1 · NEW 8.

**Method (how lines were mapped).** `linemap.py` in my TMPDIR runs `difflib.SequenceMatcher` over `git show <old base>:<path>` and `git show <new base>:<path>` and maps every old line to its identical new line; a range is MOVED only when every line maps with one shift. Every PARTIAL range was read by hand. `git diff -M --name-status <old base> HEAD` shows no renames or deletions; 38 files added, the rest modified.

### Ledger

| ID | Item (old census) | Old | New | Status | Difference |
|---|---|---|---|---|---|
| D01 | Index file `<state_root>/episode-search.sqlite3` and its derivation | desk_memory_runtime.py:257; `store.path.with_name('episode-search.sqlite3')` | store path `leaf.store_file(root, leaf.EPISODES_DB)` desk_memory_runtime.py:237 (the only `EpisodeStore(` in product); name `leaf.SEARCH_INDEX_DB` leaf:957; derived `leaf.store_path(store.path, leaf.SEARCH_INDEX_DB, sibling=True)` at 10 sites, `leaf.store_path(root, leaf.SEARCH_INDEX_DB)` at pkg/assistant_host_cli.py:115 | CHANGED | The index path is now a marked `leaf.StorePath` (`store_path` keeps the anchor's mark, leaf:969-973; `EpisodicSearchIndex.__init__` keeps it via `leaf.as_path`, es:49). `check_store` (leaf:1003-1012) refuses it at open (`sqlite_uri` leaf:346-350, `open_fd` leaf:398-428) with `store_outside_volume` when `AGENT_MEMORY_VOLUME` is set and it resolves outside `/state/memory`. |
| D02 | No product code sets a journal mode (DELETE) | — | `git grep -i journal_mode` hits only tests/install/t9b_harness.py; probe: `pragma journal_mode` = `delete` | UNCHANGED | |
| D03 | `_connect(writable=True)` creates the file and runs `_SCHEMA` | es:55-67 | es:52-82 (create branch 55-70) | CHANGED | Old: `db = sqlite3.connect(self.path)` (plain path) … `self.path.chmod(0o600)`. New: `db = leaf.sqlite_create(self.path)` (O_WRONLY\|O_CREAT\|O_EXCL\|O_NOFOLLOW at 0600, then `file:<realpath>?mode=rw`), `except FileExistsError: db = None  # created concurrently: opened below like any existing index`; no chmod. By reading (not run): a racing second creator no longer runs `_SCHEMA` and no longer reaches the `unlink` at es:68; it opens the file rw and, if the first creator has not yet run the schema, gets `EpisodeUnavailable` from the `metadata` check (es:74-82). |
| D04 | `rebuild()` | es:88-120 | es:91-123 | MOVED | |
| D05 | `upsert_episodes()` | es:140-194 | es:143-197 | MOVED | |
| D06 | `coverage_token()` | es:122-128 | es:125-131 | MOVED | |
| D07 | `_ensure_token` | es:37-44 | es:37-44 | UNCHANGED | |
| D08 | `metadata` written only at creation | es:21-22 | es:21-22 | UNCHANGED | |
| D09 | `initialize()` has no product caller | es:81-86 | es:84-89 | MOVED | still no caller (grep) |
| D10 | W1 Claude capture upsert | claude_episode_capture.py:405; `_publish` 401-419; handle_hook claude_memory_hook.py:81; ops/claude_hook_cli.py:61-64; launch_binding.py:487, 492-496; launch_cli.py:102-107; spool_ingest.py:319, 510-512; assistant_host_cli.py:101-107 | claude_episode_capture.py:403; `_publish` 399-417; claude_memory_hook.py:79; ops/claude_hook_cli.py:60-65 (hook call 93-94); launch_binding.py:461, 466-470; launch_cli.py:101-108 (hook call 102); spool_ingest.py:234, 425-427; assistant_host_cli.py:113-121 | MOVED | |
| D11 | W2 Codex rollout upsert | launch_binding.py:703, via 497-499 | launch_binding.py:681, via 471-473 | MOVED | |
| D12 | W3 child rollout upsert | spool_ingest.py:287 in `ChildRolloutCapture._page`; index 409-413 | no own call: `ChildRolloutCapture` (spool_ingest.py:193-207) now inherits `RolloutCapture._page` (launch_binding.py:601-686) and overrides only `_session_meta_matches` (206) and `_NOT_THIS_SESSION` (200); upsert is launch_binding.py:680-681; index constructed spool_ingest.py:326 in `_capture_child` 322-330 | CHANGED | W3 is still a distinct writer path (ingest-spool child events, `_child_event` 262-319, call at 316; `_launch_event` 233) but shares W2's line. The old 96-line duplicate `_page` was deleted (T11a P7). |
| D13 | W4 gateway upsert | model_gateway.py:523; `record_in_memory` 497-528; caller 725; `is_file` 521; swallow 525-526 | model_gateway.py:493; 467-498; 695; 491; 495-496 | MOVED | |
| D14 | W5 workspace capture upsert | workspace_capture.py:430; `_repair_index` 427-431; called 474, 593 | workspace_capture.py:431; 428-432; called 475, 594; compose.yaml:167 unchanged | MOVED | |
| D15 | W6 session-import upsert | session_import_job.py:571; `_repair_index` 564-583; called 596, 706 | session_import_job.py:561; 554-573; called 586, 696; Dockerfile:128 unchanged | MOVED | |
| D16 | W7 host-card full rebuild | host_card_bridge.py:88-89 | host_card_bridge.py:87-88; pkg/host_bridge_cli.py:17 unchanged | MOVED | |
| D17 | W8 import-native-history rebuild | native_history_import.py:423-424; desk_cli.py:43-52 | native_history_import.py:416-417; desk_cli.py:39-53 | MOVED | |
| D18 | W9 `index-history` rebuild | desk_cli.py:60-63 | desk_cli.py:61-64 | MOVED | |
| D19 | W10 `coverage_token()` in `_establish_coverage` | ss:1334-1335; 1329-1363; `_backfill` 1229, 1255; `upgrade` 395 | ss:1316-1317; 1311-1345; 1211, 1237; 391 | MOVED | |
| D20 | **W11** ops reconcile script rebuild | (present at the old base, same line; not in the old census) | extensions/ops/scripts/reconcile_desk_history.py:65 `EpisodicSearchIndex(state/'episode-search.sqlite3',episode_store=store).rebuild()` | NEW | Operator script on an isolated copy: backs up every `*.sqlite3` of the source state with plain `sqlite3.connect` (43-51), imports a legacy export (62-64), then rebuilds in place. Unmarked plain path. Outside `packages/*/src` and outside the T11 guard's surface (`tests/t11a_census.py:29-31`: T, O, D only). Documented at docs/MEMORY-RECONCILIATION.md:40. |
| D21 | Count: 9 upsert/rebuild call sites, 10 with W10 | | 8 in `pkg/` + `ops/` (5 upsert + 3 rebuild), 9 with W10; plus 1 rebuild in extensions/ops/scripts (W11) = 9 / 10 | CHANGED | W3 lost its own line (D12); W11 found (D20). |
| D22 | Count: 8 inline (W1–W8), 2 operator-only (W9, W10) | | same for W1–W10; W11 is operator-only (isolated copy) | UNCHANGED | |
| D23 | Count: 12 `EpisodicSearchIndex(...)` constructions, reader at episodic_memory_tools.py:381 | | 12 in `pkg/` + `ops/`, reader at episodic_memory_tools.py:384; +1 in the ops script (W11) | MOVED | |
| D24 | W1 fed by three constructions | ops/claude_hook_cli.py:62, launch_binding.py:487, assistant_host_cli.py:101 | 63, 461, 115 | MOVED | |
| D25 | File creation: W1–W3, W5–W9 can create; W4, W10 check `is_file` | model_gateway.py:521; ss:1332 | model_gateway.py:491; ss:1314 | MOVED | W11 can create too. How creation happens changed: D03. |
| D26 | Sealer that never indexes: legacy_desk_import | ops/_impl/service/legacy_desk_import.py:170 | :171 | MOVED | |
| D27 | Sealer that never indexes: session_catalog_import | :32 | :28-32 (claim 28, link 30, import_episode 32) | UNCHANGED | file byte-identical |
| D28 | Sealer that never indexes: SessionSources claim/link | ss:512-573, 614-634 | ss:504-565, 606-626 | MOVED | |
| D29 | Sealer that never indexes: delegation claims | ops/_impl/service/delegation_attribution.py:158-167 | :158-167 | UNCHANGED | file byte-identical |
| D30 | Pending state 1: `workspace-capture.sqlite3 files.index_pending` | DDL :38; set 589-592; seals 568; drained 593-594, next `_capture` 473-475; `unchanged_complete` 376; rotation 357-367; error 605-609; pass continues 400-404 | DDL :39; set 590-593; 569; 594-595, 474-476; 377; 358-368; 606-610; 401-405 | MOVED | The journal's own open changed: D104. |
| D31 | Pending state 2: `session-import-jobs.sqlite3 jobs.index_pending` | DDL :43; set 694-700 in `BEGIN IMMEDIATE` 588; drained 703-706, next `advance` 595-597; `index_error` 572-578 | DDL :44; 684-690 in 578; 693-696, 585-587; 562-568 | MOVED | |
| D32 | Pending state 3: Claude ledger `cursor.pending` | set 374-378; seal 402, enqueue 403, upsert 405, clear 410-418; replay 332-335; status 230-234 | 372-376; 400, 401, 403, 408-416; 330-333; 228-232 | MOVED | |
| D33 | Pending state 4: rollout ledger `cursor.pending` | set 696-699; seal 700; enqueue 701; upsert 702-703; advance 710-718 | 674-677; 678; 679; 680-681; 688-696 (call 685) | MOVED | |
| D34 | Pending state 4, child equivalent | spool_ingest.py:280-291 | — | GONE | The child now runs the parent's lines (D33); same file (`receipt['capture_ledger']`, rollout-capture.sqlite3). |
| D35 | Pending state 5: spool retries | `ATTEMPTS=8` :51; 546-552; 630-640 | :51; 461-467; 545-555 | MOVED | spool-ingest.sqlite3 is now created by `leaf.create_new_empty` and opened `leaf.sqlite_connect(self.path, mode='rwc', resolve=True)` (spool_ingest.py:72-80). |
| D36 | No pending state: W4, W7 (replay repairs), W8, ops imports, claim/link | host_card_bridge.py:86-87 | host_card_bridge.py:85-86 | MOVED | |
| D37 | `covered` is not a queue; no outbox/watermark/AUTOINCREMENT | grep empty | grep empty (command C5) | UNCHANGED | |
| D38 | Index schema | es:20-34 | es:20-34 | UNCHANGED | |
| D39 | Readers/writers require `version == (1,)` | es:71-72, 256-257 | es:74-75, 259-260 | MOVED | |
| D40 | `rebuild()` `BEGIN IMMEDIATE`, in-place DELETEs | es:100, 102-103 | es:103, 105-106 | MOVED | |
| D41 | `rebuild()` iterates `raw_rows` on an open store cursor; one readonly connection per source episode | ss:671-685 | ss:663-677 (`raw_rows` 663-666, `read` 668-677) | MOVED | |
| D42 | `optimize`; `mark_coverage(reset=True)` after commit | es:114, 119 | es:117, 122 | MOVED | |
| D43 | `rebuild()` holds the index write lock and a store SHARED lock (rollback mode) | es:98-117 | es:101-118 (store opened rw at 101 via `self.episodes._connect()`) | MOVED | |
| D44 | `upsert` ≤2000 IDs; sources verified outside the transaction | es:154-164 | es:157-167 | MOVED | |
| D45 | `upsert` skip unchanged; full-FTS-scan DELETE on change; `INSERT OR REPLACE` | es:170-185 | es:173-188 (skip 179-180, DELETE 182) | MOVED | |
| D46 | `upsert` `mark_coverage` after commit | es:190-192 | es:193-195 | MOVED | |
| D47 | Search: `resolve_scope`; readonly `sources.connect()` | es:218, 220; ss:759-780 | es:221, 223; ss:751-772 | MOVED | |
| D48 | Search validation; `counts` | es:224-231 | es:227-234 | MOVED | |
| D49 | Phrase; `candidate_limit` | es:239-240 | es:242-243 | MOVED | |
| D50 | `index_unavailable` | es:243-250 | es:246-253 | MOVED | |
| D51 | `ATTACH ... ?mode=ro` | es:255 `self.path.resolve().as_uri() + '?mode=ro'` | es:258 `leaf.sqlite_uri(self.path, 'ro', resolve=True)` | CHANGED | Same URI text; now goes through `check_store` first (D01 refusal applies to the reader too). |
| D52 | Token read; the one cross-file statement; desk `member` | es:258-270; ss:320-327 | es:261-273; ss:316-323 | MOVED | `ORDER BY e.rank` before scope/LIMIT unchanged (es:272). |
| D53 | Token mismatch → covered 0; candidate trim | es:273-277 | es:276-280 | MOVED | |
| D54 | Phrase regex; digest mismatch marks stale | es:278-289 | es:281-292 | MOVED | |
| D55 | Batch reopen; `plan.admits`, sealed digest, sealed-text regex | es:292-311 | es:295-314 | MOVED | `_sha(found.raw)` → `leaf.sha256_hex(found.raw)` (es:305), same digest. |
| D56 | Stale subtracted; status | es:333-337 | es:336-340 | MOVED | |
| D57 | FTS rows store `binding` (`''` for source_episodes); search never reads `e.binding` | es:109-111, 156-162, 265 | es:112-114, 159-165, 268 | MOVED | |
| D58 | Desk membership in `episode_desks`; unlinked legacy → storage binding | ss:57-58, 78-79, 209-228, 1035-1037, 1055 | ss:57-58, 78-79, 205-224, 1017-1019, 1037 | MOVED | |
| D59 | Topic scope through the live registry | ss:326-336 | ss:322-332 | MOVED | |
| D60 | Today's delete is a full FTS scan, pinned by tests/test_episodic_search_incremental.py | | file byte-identical | UNCHANGED | |
| D61 | Contract T10 P4 | tests/test_t10_p4_projection_honesty.py:86-118 | same | UNCHANGED | |
| D62 | Contract P4b | tests/test_t10_p4b_integrity.py:7-10; es:296-304 | same test; es:299-307 | MOVED | |
| D63 | Detection point: `_reflect` old and new desks | ss:1026-1028, 1043-1045 | ss:1008-1010, 1025-1027 | MOVED | |
| D64 | Store side: `scope_state('coverage_token')`, `covered`, `_contributions` | ss:231-238 | ss:227-234 | MOVED | |
| D65 | `mark_coverage` and `_cover` | ss:1090-1108 (1099-1100, 1102-1105), 1110-1132 | ss:1072-1090 (1081-1082, 1084-1087), 1092-1114 | MOVED | |
| D66 | `_reflect` carries `covered` | ss:1009, 1021-1023, 1047-1051 | ss:991, 1003-1005, 1029-1033 | MOVED | |
| D67 | `_establish_coverage`: token, reset, batches under the store write lock | ss:1329-1363 (1335, 1338-1344, 1347-1357) | ss:1311-1345 (1317, 1320-1326, 1329-1339) | MOVED | |
| D68 | Read side: aggregates vs summed `covered` | ss:790-823 | ss:782-815 | MOVED | |
| D69 | `index_status` values; `absence_verdict` | es:243-250, 335-344 | es:246-253, 338-347 | MOVED | |
| D70 | Goldens hold only `results` (28) and `zero_results` (21) | | fixture byte-identical; re-counted: 28 / 21 | UNCHANGED | |
| D71 | Invariants T12 must keep (no over-report; new file no coverage; seal/claim/link never open the index; `covered == total` after reassignment; response fields) | es:190-191, 338-344; test_t10_p4:152-191 | es:193-194, 341-347; test unchanged | MOVED | |
| D72 | `EpisodeStore._capture` seal | episodic_memory.py:163-183 (169, 178, 181); ss:426-443 | episodic_memory.py:161-181 (167, 176, 179); ss:418-435 | MOVED | |
| D73 | `import_operator_episode` | episodic_memory.py:215-242 | episodic_memory.py:213-240 (insert 236, `_reflect` 240) | MOVED | |
| D74 | `SessionSources.import_episode` | ss:651-668 (663, 666, 668) | ss:643-660 (655, 658, 660) | MOVED | |
| D75 | claim, link, backfill seals | ss:555-572, 617-634; 1299, 1313, 1316, 1321 | ss:547-564, 609-626; 1281, 1295, 1298, 1303 | MOVED | |
| D76 | One row per transaction in capture loops | workspace_capture.py:568; session_import_job.py:679 | 569; 669 | MOVED | |
| D77 | `_reflect`; early return when the projection is not installed | ss:1001-1076, 1007-1008 | ss:983-1058, 989-990 | MOVED | |
| D78 | `_TRIGGERS` (`scope_written_*` AFTER INSERT) | ss:94-97 | ss:94-97 | UNCHANGED | over `_MARKED` ss:47 |
| D79 | `_install` installs the triggers | ss:1187-1200 | ss:1169-1182 | MOVED | |
| D80 | `episode_scope(kind, seq)`; schema comment | ss:986-999; 66-68 | ss:968-980; 66-68 | MOVED | `_sha` → `leaf.sha256_hex`, same digest. |
| D81 | No AUTOINCREMENT; nothing deletes sealed rows; no cross-table sequence | | greps C5, C9 empty | UNCHANGED | |
| D82 | `scope_state 'cursor:<table>'` is transient | ss:1274-1276, 1326, 1264 | ss:1256-1258, 1308, 1246 | MOVED | |
| D83 | Index `BEGIN IMMEDIATE` | es:100, 167, 125 | es:103, 170, 128 | MOVED | |
| D84 | Index opened by `sqlite3.connect`, default 5 s timeout | es:59, 70 | es:60 `leaf.sqlite_create(self.path)`, es:73 `leaf.sqlite_connect(self.path, mode=mode, resolve=True)` | CHANGED | Create now opens a resolved `file:…?mode=rw` URI instead of the plain path. Timeout still sqlite3's default (no `timeout=`; probe `busy_timeout` 5000), `isolation_level` not passed (`''`), `check_same_thread` True. |
| D85 | No WAL | | probe `journal_mode=delete` | UNCHANGED | |
| D86 | Coverage written in a separate store transaction (not atomic with the index) | es:192; ss:1090-1108 | es:195; ss:1072-1090 | MOVED | |
| D87 | `_establish_coverage` holds the store write lock while it reads the index | ss:1347-1357 | ss:1329-1339 | MOVED | |
| D88 | Workspace capture process `flock LOCK_NB` on `workspace-capture.lock`; contention → `CaptureError` | workspace_capture.py:329-341 | workspace_capture.py:329-342 (contention 338) | CHANGED | Old `os.open(lock_path, os.O_CREAT \| os.O_RDWR, 0o600)` + `os.fdopen`; new `leaf.open_fd_stream(lock_path, 'rb', access='rw', create=True)`, which adds O_NOFOLLOW and `check_store`. |
| D89 | Workspace capture per-file lease 120 s | workspace_capture.py:446-468, 587-591, 612-615 | 447-469, 588-592, 613-616 | MOVED | |
| D90 | Import-job lease 120 s; `advance` holds jobs `BEGIN IMMEDIATE` around its upsert | session_import_job.py:727-735, 749-751, 767-769; 588, 596, 705-706 | 717-725, 739-741, 757-759; 578, 586, 695-696 | MOVED | |
| D91 | Claude capture flock, 2 s deadline | claude_episode_capture.py:255-272 | 253-270 | MOVED | lock opened via `leaf.open_binary` |
| D92 | `RolloutCapture` flock, 2 s deadline | launch_binding.py:556-582 | 530-556 | MOVED | lock opened via `leaf.open_binary` |
| D93 | Spool blocking `LOCK_EX` | spool_ingest.py:89-96 | spool_ingest.py:89-90 → `leaf.private_lock` (leaf:787-794) | MOVED | same flags (O_RDWR\|O_CREAT\|O_NOFOLLOW 0600) and LOCK_EX |
| D94 | Launch `_locked` | launch_binding.py:91-94 | launch_binding.py:77-78 → `leaf.private_lock` | MOVED | |
| D95 | Refresh `.refresh.lock` | refresh_cli.py:786-789 | refresh_cli.py:777-780 | MOVED | |
| D96 | Episodic queue leases | episodic_queue.py:141-198 | episodic_queue.py:138-195 | MOVED | |
| D97 | No one-indexer lease; memory volume where POSIX locks hold | compose.yaml:29-34 | compose.yaml:26-34 (file byte-identical) | UNCHANGED | |
| D98 | Swap hazard: readers refuse a symlinked index; installer walks `*.sqlite3` with sidecars | es:53, 241; runtime_install.py:1166-1173, 1213-1214 | es:53, 244; runtime_install.py:1122-1129, 1169-1170 | MOVED | |
| D99 | `workspace_capture_cli watch`, `worker()` | pkg/workspace_capture_cli.py:45-58, 69-75, 83-87 | same | UNCHANGED | file byte-identical |
| D100 | The only handler wraps all of `main` | :90-92 | :90-92 | UNCHANGED | |
| D101 | Exceptions that end the loop with a traceback | desk_binding.py:58; episodic_memory.py:35 | desk_binding.py:59; episodic_memory.py:34 | MOVED | |
| D102 | Per-file failures do not end it | workspace_capture.py:400-404, 409-410, 433-440, 605-609 | 401-405, 410-411, 434-441, 606-610 | MOVED | |
| D103 | Transient causes that end the loop | workspace_capture.py:117-126, 157-159, 256, 334-337, 353, 370, 188-189, 346-392; ss:415-420 | 114-123, 154-156, 253, 335-338, 354, 371, 185-186, 347-393; ss:411-416 | MOVED | |
| D104 | `initialize`/`_db` sqlite errors (journal open) | workspace_capture.py:267-274 | workspace_capture.py:257-286 | CHANGED | Old `return sqlite3.connect(self.path, timeout=10)` and `self.path.chmod(0o600)`. New: `leaf.sqlite_create(self.path, timeout=10)` when absent, else `leaf.sqlite_connect(self.path, mode='rwc', resolve=True, timeout=10)` (280-286); chmod removed; policy digest via `leaf.sorted_sha256` (266). |
| D105 | Capture role entrypoint/command; `container.py` preflight then `execvp` | compose.yaml:166-167; container.py:248-277 | compose.yaml:166-167; container.py:234-263 | MOVED | |
| D106 | `spool_ingest.watch` exits with the child's status | spool_ingest.py:672-716 (699-701) | 587-631 (614-616; return 628-631) | MOVED | |
| D107 | The watcher's own errors never end it | 692-696, 656-663, 630-640 | 607-611, 571-578, 545-555 | MOVED | |
| D108 | Refresh per-cycle `except Exception`; setup can exit | refresh_cli.py:790-802, 784-789 | 781-793, 775-780 | MOVED | |
| D109 | No `restart:`; `x-role` anchor; `init: true` | compose.yaml:37-43, 99-257, 40 | same (file byte-identical; grep `restart` empty) | UNCHANGED | |
| D110 | Installer copies compose byte for byte; overlays add only volumes | runtime_install.py:540-541, 544-561 | 536-537, 540-557 | MOVED | |
| D111 | `deploy/compose.yaml` is a symlink to the packaged asset | | unchanged | UNCHANGED | |
| D112 | `SERVICES`/`COMPONENTS` and the manifest check | runtime_install.py:62-63, 486-488 | runtime_install.py:63-64, 482-484 | MOVED | values unchanged |
| D113 | Role pins in tests (s3_harness.py:38; test_p4_rendered_isolation.py:60-82; t7a stays-up) | | files byte-identical | UNCHANGED | |
| D114 | Direct calls: tests/test_episodic_search.py | :24, 45, 55, 74, 86, 105 (76) | same | UNCHANGED | |
| D115 | tests/test_episodic_search_incremental.py and its DELETE counter | | same | UNCHANGED | |
| D116 | tests/test_session_sources.py | :23, 88, 113, 133 (128) | same | UNCHANGED | |
| D117 | tests/test_desk_profiles.py:18 | | same | UNCHANGED | |
| D118 | tests/test_assistant_memory_slice.py:87, 89 | | same | UNCHANGED | |
| D119 | opstests/test_imported_desk_runtime.py:46; opstests/test_legacy_desk_import.py:90 | | same | UNCHANGED | |
| D120 | T10 helpers (t10_corpus, t10_measure, t10_world) | | byte-identical | UNCHANGED | |
| D121 | T10 and T10h tests | | byte-identical | UNCHANGED | |
| D122 | Patches and fakes | | byte-identical | UNCHANGED | |
| D123 | Counters that touch the index | | byte-identical | UNCHANGED | |
| D124 | Tests that rely on inline indexing (the old B4 list) | | files byte-identical | UNCHANGED | Re-derivation (§8) finds one entry is not a relier: opstests/test_launch_claude_desk_memory.py:270 is a setup-missing diagnostic that asserts `isError` (:286), not a search after a capture. |
| D125 | Relier: opstests/test_claude_memory_hook.py | (present at the old base) | :136-137 and :161 search right after `handle_hook` with a real index (W1) | NEW | |
| D126 | Relier: tests/test_workspace_capture_adversarial.py | (present) | :104-110 search right after `capture_cli ... once` (W5) and assert results | NEW | |
| D127 | Reliers (Docker image tests that search after the live stack captures) | (present) | tests/install/test_t7b_p2_desk_tasks_image.py:149-185; tests/install/test_t7b_p4_assistant_memory_image.py:157-163 (components `("tooling","board")`, :58); tests/install/test_t7a_p2_documented_steps_image.py:391-408; tests/host/test_t7a_p7_unconfigured_capture_image.py:80-85 | NEW | t7a_p2 and t7a_p7 poll until a deadline. t7b_p2 (components tooling, capture, board; :64) and t7b_p4 search ONCE right after the board-role Stop hook (t7b_p2:175-180, t7b_p4:155-163), with no wait. None can call an in-process `drain()`. |
| D128 | T11a/T11b tests that pin index opens | — | tests/test_t11a_carried.py:38-44; tests/fixtures/t11a/generate_connection_profiles.py:195-197 and connection_profiles.json; tests/test_t11a_p4_connection_profiles.py; tests/test_t11b_regression_set.py `RULINGS` | NEW | The P4 golden records the index opens: `import jobs: advance` #7 (`file://<real>/state/episode-search.sqlite3?mode=rw`, timeout 5.0, `''`), `launch: first codex hook` #41 and `search index: rebuild` #1 (both creates, ruled under T11b Q2). Steps hold 12 (`advance`), 4 (`rebuild`) and 3 (`search`) connections. |
| D129 | T11a P7 rollout golden | — | tests/fixtures/t11a/rollout_capture.json, tests/test_t11a_p7_rollout_capture.py | NEW | Pins every `RolloutCapture`/`ChildRolloutCapture` receipt byte-for-byte, including `"indexed": false` (20 occurrences). |
| D130 | T11 leaf guard | — | tests/test_t11_leaf_single_home.py | NEW | Outside leaf and its allowlist: zero `sqlite3.connect` (P4, :379-381), zero `.sqlite3` string literals (P6, :391-393; pattern `tests/t11a_census.py:550`), zero order-P3 primitives `os.open+O_CREAT`, `O_NOFOLLOW`, `open-x`, `tempfile.*`, `os.replace`, `os.link`, `os.fdopen` (:167-168, :370-376). |
| D131 | `index-history`: in-place rebuild | desk_cli.py:13, 60-63 | desk_cli.py:14, 61-64 | MOVED | |
| D132 | `upgrade-sources` | desk_cli.py:53-59; ss:381-396, 1329-1363 | desk_cli.py:54-60; ss:377-392, 1311-1345 | MOVED | |
| D133 | `import-native-history --apply` | desk_cli.py:38-52 | desk_cli.py:39-53 | MOVED | |
| D134 | `session-import continue` drains `index_pending` | session_import_cli.py:83-85 | session_import_cli.py:84-86 | MOVED | |
| D135 | `workspace-capture once` drains `files.index_pending` | workspace_capture.py:593-594 | 594-595 | MOVED | |
| D136 | A host-card replay rebuilds | host_card_bridge.py:88-89 | 87-88 | MOVED | |
| D137 | The ledgers replay `pending` | claude_episode_capture.py:332-335; launch_binding.py:696-699 | 330-333; 674-677 | MOVED | |
| D138 | No standalone repair script | | still none for live state; W11 reindexes an isolated copy | UNCHANGED | |
| D139 | Instrument: capture never opens the index | test_t10_p4:100-101, 166-167 | same | UNCHANGED | |
| D140 | Instrument: single writer (connect audit, `db == index_path` trace, `_traced`) | | same | UNCHANGED | |
| D141 | Instrument: per-step connect recorder | — | tests/fixtures/t11a/generate_connection_profiles.py (records every `sqlite3.connect` per step: database, uri, timeout, isolation_level, first statement); tests/test_t11b_q2_sqlite_profile.py `resolving_recorder` (:43-56) | NEW | |
| D142 | Instrument: no in-place delete (`_deletes`) | | same | UNCHANGED | |
| D143 | P3(c) VM steps exist; matching-text growth fixture missing | t10_measure.py:45-58, 442-495, 401-416 | same | UNCHANGED | |
| D144 | P3(c) median wall time: none | | none | UNCHANGED | |
| D145 | Reassignment/dirty-set instruments | | same | UNCHANGED | |
| D146 | Restart resumes at the watermark: none | | none | UNCHANGED | |
| D147 | Watch loop survives an error: only `t7a_harness.assert_stays_up` | | same (T12a not on main) | UNCHANGED | |

### Draft T12b claims against this base

**Problem paragraph.**
- "**Ten index writers.**" NO LONGER TRUE. The census's own grep (C1) finds a writer that the old census missed: W11, extensions/ops/scripts/reconcile_desk_history.py:65, `.rebuild()`. It is unchanged since before the old base, so the old count was already short. Separately, W3 no longer has its own call site (D12). The writer paths are W1–W11. The textual sites are 8 upsert/rebuild calls in `pkg/` + `ops/`, 1 in the ops script, and W10.
- "Eight run inline across four roles". STILL TRUE. The per-file caller counts are identical at both bases (C7). Inline writers run in the capture role (compose.yaml:166-167), the board role (`KANBAN_LAUNCH_BINDING_COMMAND` compose.yaml:193, `KANBAN_ASSISTANT_MEMORY_COMMAND` :194, `KANBAN_SESSION_IMPORT_EXECUTABLE` Dockerfile:128), host CLIs and the model gateway.
- "W1–W6 upsert". STILL TRUE as writers. There are now 5 upsert lines, because W2 and W3 share launch_binding.py:681.
- "W7–W9 rebuild in place". STILL TRUE (host_card_bridge.py:87-88, native_history_import.py:416-417, desk_cli.py:64 → es:105-106). W11 rebuilds in place too.
- "W10 the coverage token". STILL TRUE (ss:1316-1317).
- "**Five pending-index states** sit in five files". STILL TRUE (D30–D35): workspace-capture.sqlite3, session-import-jobs.sqlite3, the Claude capture ledger, rollout-capture.sqlite3 (parent and child) and spool-ingest.sqlite3.
- "**No outbox, watermark or lease** exists". STILL TRUE. C5 and C6 are empty.
- "**`rebuild()` deletes in place** while holding a store SHARED lock, and the store is in rollback-journal mode". STILL TRUE: es:101-107, ss:663-666, no `journal_mode` in product code, probe `delete`.

**B1.**
- "Every insert into `episodes`, `source_episodes`, `session_claims` and `session_episodes`, by any path". The insert set is STILL TRUE. The same 8 statements exist: episodic_memory.py:167, 176, 236 and ss:434, 563, 623, 655, 658 (C8).
- "the sealers that never index today (`legacy_desk_import`, `session_catalog_import`, delegation claims)". STILL TRUE (D26, D27, D29). The ops script follows a legacy import with a rebuild, on a copy (D20).
- "AFTER INSERT triggers, like T10's `scope_written_*`". STILL TRUE (ss:94-97). They are installed only by `_install` (ss:1169-1182), when the projection is installed, not by `ensure_catalog`.
- "When the projection is not installed, `_reflect` returns early". STILL TRUE (ss:989-990).
- "`_reflect` fills in the old and new desk sets". The premise is STILL TRUE: `_reflect` holds both (ss:1008-1010, 1025-1027).
- "The indexer resolves the session's episodes from `episode_scope`". STILL TRUE: column `source_session_id`, index `episode_scope_session` (ss:70-75).
- Falsifier instrument `t10_instruments.connected(index_path)`. STILL TRUE (D139).
- Not a census claim, but contradicted by this base: "part of the catalog schema that `ensure_catalog()` guarantees before any write (T10h). So no writer is ever without it". NOT TRUE of the current code (also not at the old base). `ensure_catalog` (ss:394-416) has two callers: `upgrade` (ss:385) and `WorkspaceCapture.initialize` (workspace_capture.py:263). `EpisodeStore._capture`, `import_operator_episode`, `import_episode`, `claim`/`link` and the import jobs seal without calling it. `_capture_identity` returns early when the catalog is absent (ss:419). A new store gets the catalog only through `EpisodeStore.initialize` → `upgrade` (episodic_memory.py:104).

**B2.**
- "flock LOCK_NB on `<state_root>/index.lock`, on the volume". The premise is STILL TRUE: the memory volume is where POSIX locks hold (compose.yaml:26-34). A non-blocking precedent exists at workspace_capture.py:334-338.
- "Host CLIs that seal outside Compose (W1's hook, the W4 gateway, the W7 host card, W8's import)". STILL TRUE for those four. Note that W1(b), W1(c), W2 and W6 seal inside the board role (see step 5).
- "No module other than `drain` and the reindex writes `event_search`, `indexed_episodes`, the watermark or the coverage token". The precondition is STILL TRUE: every such write today is inside es (C3, C4).
- "The five pending states retire". STILL TRUE that there are five.
- Falsifier trace `db == index_path`. STILL TRUE (D140). A new per-step connect recorder exists (D141).

**B3.**
- "builds a new file … never deleting in place". This is the opposite of today's es:105-106, which is still current.
- "the T10 backfill pattern". STILL TRUE (ss:1251-1258, 1281-1308).
- "`_ensure_token` mints a random token". STILL TRUE (es:37-44).
- "Sealed rows are immutable". STILL TRUE: C9 is empty.
- "Readers keep `ATTACH ?mode=ro`". STILL TRUE (es:258, now via `leaf.sqlite_uri`).
- "**Callers.** `index-history`, `import-native-history`, `host_card_bridge` and `upgrade-sources`". NO LONGER TRUE as the list of rebuild callers: it omits W11 (reconcile_desk_history.py:65).
- Not a census claim, but contradicted by this base (T5): "the operator's step is T10h's integrity path: `kp-agent-desk … upgrade-sources` (`quick_check`)". `upgrade-sources` runs `PRAGMA quick_check` only after a key-first `episode_desks` rebuild (ss:386-388) or on an incomplete or behind projection (ss:1215-1216). On a complete projection, it returns after the coverage step with no `quick_check` (ss:1209-1214; docstring ss:1192-1196). The same code was at the old base.

**B4.**
- "Tests that search right after capture (the census list)". NO LONGER TRUE as a list. Re-derivation adds 6 reliers (D125–D127; all present at the old base) and drops opstests/test_launch_claude_desk_memory.py:270 (D124). Four of the additions are Docker image tests that cannot call an in-process `drain()`. Two of them (t7b_p2, t7b_p4) search once, with no wait, right after a board-role hook, and t7b_p4 runs `("tooling","board")` with no capture role (:58).
- "T10's goldens are unchanged". STILL TRUE: byte-identical, 28 `results`, 21 `zero_results`. New goldens, T11a P4 (D128) and P7 (D129), pin index-touching steps and receipts.
- "`test_episodic_search_incremental`'s DELETE counts". STILL TRUE (file byte-identical).

### New facts bearing on T12b's write scope (step 5)

1. **How the index is opened now** (es:52-82; confirmed by an in-process probe in my TMPDIR, against a scratch file):
   - **Create:** `leaf.sqlite_create(path)` → `create_new_empty` (`os.open` O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW, mode 0600, after `check_store`). Then `sqlite3.connect('file:<realpath>?mode=rw', uri=True)`. No `timeout` is passed (sqlite3 default 5.0 s; probe `busy_timeout` 5000). No `isolation_level` is passed (`''`, legacy; the code issues `BEGIN IMMEDIATE` itself at es:103/128/170). `check_same_thread` is True. The file ends at 0600. `journal_mode` is `delete`.
   - **Existing file:** `leaf.sqlite_connect(path, mode='rw'|'ro', resolve=True)` (es:73), with the same profile.
   - **Reader:** `ATTACH` via `leaf.sqlite_uri(path, 'ro', resolve=True)` (es:258).
   - **Store side during index work:** `self.episodes._connect()` opens `file:<realpath>?mode=rw` (episodic_memory.py:110). `rebuild()` and `upsert` use it for reading (es:101, 157).
2. **Is the index path a marked store path?** Yes, at all 12 product constructions. `EpisodeStore` is constructed only at desk_memory_runtime.py:237, from `leaf.store_file` (marked and checked). `leaf.store_path(..., sibling=True)` keeps the mark and adds no check. The assistant hook's `root` is `leaf.mark_store(...).resolve()` (assistant_host_cli.py:94). `check_store` runs at open, not at derivation. The one exception is W11's plain `state/'episode-search.sqlite3'`. No `leaf.STORE`-style constant exists for a lock or build file. Any new name containing `.sqlite3` must be a leaf constant (leaf:955-966), or the P6 guard fails.
3. **`parent.mkdir` before `sqlite_create`.** Confirmed at es:58: `self.path.parent.mkdir(parents=True, exist_ok=True)` precedes `leaf.sqlite_create` at es:60, which is where `check_store` first runs. In the probe, with `AGENT_MEMORY_VOLUME` set and a marked path outside `/state/memory`, `_connect(writable=True)` raised `StoreOutsideVolume`, and the parent directory had already been created. The same order is at desk_binding.py:87-88 and episodic_memory.py:94-95. `leaf.replace_file(make_parents=True)` checks first (leaf:621-622).
4. **`runtime_install` and the packaged compose as they stand.**
   - `COMPONENTS = ('tooling', 'refresh', 'capture', 'board', 'telemetry')` (runtime_install.py:63).
   - `SERVICES = frozenset({'tooling', 'refresh', 'capture', 'board', 'tempo', 'collector'})` (:64). Both are unchanged.
   - The manifest must have exactly `SERVICES` (`manifest_contract_changed`, :482-484). Every role that mounts `/state` must mount the `memory` volume at `/state/memory` (`_memory_unmounted`, :485-490, 495).
   - `COMPOSE_PROFILES` is the selected components minus `tooling` (:533). Only `tooling` is required (:244), so `tooling+board` without capture is a valid install.
   - Packaged compose.yaml is byte-identical: services `tooling` :102 (no profile), `refresh` :117 [refresh], `capture` :149 [capture], `board` :183 [board], `tempo` :228 and `collector` :245 [telemetry]. There is no `restart:`.
   - Tests that pin roles: s3_harness.py:35-39, t9b_harness.py:78, test_p4_rendered_isolation.py:57/72, test_t7a_p1_readiness_and_operator_files.py:37, test_t7a_p1_roles_start_image.py:42, test_t7a_p2_documented_steps_image.py:231/370/409, test_t7b_p1:52, test_t7b_p2:64, test_t7b_p4:58.
5. **The board role seals inside Compose.**
   - It runs W1(b)/W2 via `kp-agent-launch` (compose.yaml:193), W1(c) via `assistant_host_cli` (:194) and W6 via `kp-agent-session-import` (Dockerfile:128).
   - An `indexer` "selected with capture" leaves a `tooling+board` install with no drainer.
   - test_t7b_p4_assistant_memory_image.py runs exactly that install and searches after the assistant's capture (:157-163).
6. **T11 guard on code shape** (D130).
   - `drain`, the lease and the swap cannot use `sqlite3.connect`, `os.open(...O_CREAT)`, `O_NOFOLLOW`, `os.replace`, `os.fdopen` or `tempfile` outside leaf.
   - leaf has a blocking `private_lock` (leaf:787-794). It has no non-blocking lock and no rename-a-built-file primitive. `replace_file` (leaf:609-652) writes bytes to a temp, and `os.rename` is only report-level.
   - So `leaf.py` (plus its constants and the guard's allowlist or P8 references) is in T12b's FEATURE scope, though the draft does not name it.
7. **Goldens that T12b's change will move** (not named in the draft's TEST scope):
   - The T11a P4 connection golden and T11b `RULINGS` (D128). Removing inline indexing from `import jobs: advance` (#7) and `launch: first codex hook` (#41), and replacing `search index: rebuild` (#1), changes connection counts and order. That needs a named regression-set exception.
   - The P7 rollout golden (D129) pins receipts with `"indexed": false`. Any change to receipt shape breaks it.
8. **Out-of-scope writer.** W11 (extensions/ops/scripts/reconcile_desk_history.py:65) is outside the draft's "ten W sites" and outside the T11 guard. If it is not converted, it remains a second in-place rebuild path.
9. **W3** needs no separate edit. Its upsert is W2's line (launch_binding.py:680-681), so "the ten W sites" is nine textual edits for W1–W10.
10. **`index_lag` surface.** `memory.connection_status` is at svc/episodic_memory_tools.py:377-381 and returns `status` `ready`/`not_ready` from `SessionSources.projection_ready()`.

---

## Discovery commands (run in the worktree; `$W` = worktree, `PAT` as shown)

- C1 `git grep -n "EpisodicSearchIndex(" -- packages extensions deploy scripts apps ':!*/tests/*' ':!extensions/ops/tests'`, and the same at the old base.
- C2 `git grep -n "upsert_episodes"` and `git grep -n "rebuild("` over the same pathspecs, at both bases (`\.rebuild(` for the old base).
- C3 `git grep -n "event_search"` and `git grep -n "indexed_episodes"` over `packages extensions deploy scripts apps`.
- C4 `git grep -n "coverage_token"` and `git grep -n "SEARCH_INDEX_DB\|episode-search"` over product, then `git grep -c "episode-search\|SEARCH_INDEX_DB"` over the whole tree.
- C5 `git grep -n -i -E "outbox|watermark|AUTOINCREMENT" -- 'packages/*.py' 'extensions/*.py' 'deploy/*.py' ':!extensions/ops/tests'` (empty); C6 `git grep -n -E "index\.lock|index_lag|def drain|drain\("` (empty); `git grep -n -i journal_mode -- .` (tests only).
- C7 callers: `git grep -n -E 'attach_card\(|import_native_history\(|record_in_memory\(|ClaudeEpisodeCapture\(|RolloutCapture\(|ChildRolloutCapture\(|\.advance\(|\.once\(\)|handle_hook\(|_establish_coverage\(|\.upgrade\(|_capture_child\(|lb\.hook\(|[^_]hook\(receipt|_repair_index\('` over `packages/tooling/src extensions/ops/src extensions/ops/scripts deploy` at both bases; the per-file counts diff is empty (30 = 30).
- C8 `git grep -n -i -E "INTO (episodes|source_episodes|session_claims|session_episodes)[ (]"` at both bases (8 = 8).
- C9 `git grep -n -i -E "(DELETE FROM|UPDATE) (episodes|source_episodes|session_claims|session_episodes)\b"` (empty).
- C10 tests: `PAT='EpisodicSearchIndex\(|upsert_episodes|\.rebuild\(|coverage_token|event_search|indexed_episodes|index-history|episode-search|SEARCH_INDEX_DB'`; `git grep -n -E "$PAT" HEAD -- tests extensions/ops/tests` and at the old base (80 vs 74 lines; new files: test_t11a_carried.py, t11a generator and golden).
- C11 search callers in tests: `git grep -l -E "memory\.search|search_records\(|\.search\(SESSION|index\.search\(|\.search\(session|'search'|\"search\""` at both bases (48 vs 46 files). Each file's search, index and capture lines were dumped and read.
- C12 locks: `git grep -n -E "fcntl\.flock\(|private_lock\(|owned_directory_lock\(|_locked\(|lease_until|LOCK_NB"` at both bases.
- C13 line mapping: `python3 linemap.py <path> <old ranges…>` (difflib over `git show` at both bases), for every cited file.
- C14 probe: `python3 -I` with the worktree's `packages/tooling/src` on `sys.path`, recording `sqlite3.connect` calls from `EpisodicSearchIndex._connect` on a scratch path in TMPDIR (results in step 5, item 1, and item 3).

---

## 1. Index writers

**Primitives** (es):
- `_connect(writable=True)` creates the file through `leaf.sqlite_create` and runs `_SCHEMA` (52-82; create 55-70; D03).
- `rebuild()` (91-123), `upsert_episodes()` (143-197) and `coverage_token()` (125-131) each run in `BEGIN IMMEDIATE` (103, 170, 128). `_ensure_token` is at 37-44.
- `metadata` is written only at creation (21-22).
- `initialize()` (84-89) has no product caller.

**Call sites:**

| # | Site | Function | Process or role |
|---|---|---|---|
| W1 | svc/claude_episode_capture.py:403 upsert | `ClaudeEpisodeCapture._publish` (399-417), via `capture()`, via `handle_hook` (claude_memory_hook.py:79) | host `kp-agent-claude-capture hook` (ops/claude_hook_cli.py:60-65, call 93-94); `launch_binding._capture` (launch_binding.py:461, 466-470) via `kp-agent-launch hook` (launch_cli.py:101-108) in host and board (compose.yaml:193), and capture's ingest-spool (spool_ingest.py:234, 425-427); board assistant hook (assistant_host_cli.py:113-121; compose.yaml:194) |
| W2 | svc/launch_binding.py:681 upsert | `RolloutCapture._page` (601-686, Codex), via 471-473 | same as W1(b) |
| W3 | svc/launch_binding.py:681 (inherited) | `ChildRolloutCapture` (spool_ingest.py:193-207) pages with `RolloutCapture._page`; index at spool_ingest.py:326 in `_capture_child` (322-330), via `_child_event` (262-319, call 316) and `_launch_event` (233) | capture role, ingest-spool |
| W4 | svc/model_gateway.py:493 upsert | `record_in_memory` (467-498), called from 695; only if the index file exists (491); errors swallowed (495-496) | `kp-agent-models serve`/`call` |
| W5 | svc/workspace_capture.py:431 upsert | `_repair_index` (428-432), called at 475 and 594, from `_capture`/`_run`/`once` | capture role, workspace capture watch (compose.yaml:167) |
| W6 | svc/session_import_job.py:561 upsert | `_repair_index` (554-573), called at 586 and 696, from `advance`/`follow` | `kp-agent-session-import` (board role; Dockerfile:128) |
| W7 | svc/host_card_bridge.py:87-88 rebuild | `attach_card(apply=True)`: a FULL rebuild on every card event | `kp-agent-host-card` (host_bridge_cli.py:17) |
| W8 | svc/native_history_import.py:416-417 rebuild | `import_native_history(apply)` | `kp-agent-desk import-native-history --apply` (desk_cli.py:39-53) |
| W9 | pkg/desk_cli.py:61-64 rebuild | the `index-history` verb | operator CLI |
| W10 | svc/session_sources.py:1316-1317 `coverage_token()` | `_establish_coverage` (1311-1345) via `_backfill` (1211, 1237), via `upgrade` (391) | `kp-agent-desk upgrade-sources` (desk_cli.py:54-60) |
| W11 (NEW) | extensions/ops/scripts/reconcile_desk_history.py:65 rebuild | `reconcile()` after legacy `import_export` (62-64), on an isolated copy made by plain `sqlite3` backups (43-51) | operator script (docs/MEMORY-RECONCILIATION.md:40) |

**Counts:**
- 8 upsert/rebuild call sites in `pkg/` + `ops/` (5 upsert: claude_episode_capture.py:403, launch_binding.py:681, model_gateway.py:493, session_import_job.py:561, workspace_capture.py:431; 3 rebuild: host_card_bridge.py:88, native_history_import.py:417, desk_cli.py:64). That makes 9 counting W10, and 10 with W11.
- 8 inline (W1–W8), 3 operator-only (W9, W10, W11).
- 12 `EpisodicSearchIndex(...)` constructions in `pkg/` + `ops/`: 11 feed writers, and episodic_memory_tools.py:384 is read-only. W11 adds 1.
- W1 is fed by three constructions: ops/claude_hook_cli.py:63, launch_binding.py:461, assistant_host_cli.py:115. launch_binding.py:461 also feeds W2, and spool_ingest.py:326 feeds W3.

**File creation.** W1–W3, W5–W9 and W11 can create the file. W4 and W10 check `is_file` first (model_gateway.py:491; ss:1314). Creation is now O_EXCL at 0600, and a concurrent creator opens the existing file (D03).

**Sealers that never index:**
- legacy_desk_import (ops/_impl/service/legacy_desk_import.py:171);
- session_catalog_import (:28-32);
- SessionSources claim/link (ss:504-565, 606-626);
- delegation claims (ops/_impl/service/delegation_attribution.py:158-167).

## 2. Pending-index states (five, in five files; none in the store or the index)

1. **`workspace-capture.sqlite3 files.index_pending`** (DDL at workspace_capture.py:39).
   - Set at 590-593, in the journal transaction that advances the cursor, after the per-row seals at 569.
   - Drained at 594-595, or at the next `_capture` (474-476).
   - A file with pending entries is never `unchanged_complete` (377). It is re-selected only by fair rotation (358-368).
   - A failure becomes the file's `error` (606-610), and the pass goes on (401-405).
   - The journal is created with `leaf.sqlite_create(timeout=10)` or opened `mode='rwc', resolve=True, timeout=10` (280-286).
2. **`session-import-jobs.sqlite3 jobs.index_pending`** (DDL :44).
   - Set at 684-690, in the jobs `BEGIN IMMEDIATE` (578).
   - Drained at 693-696, while the jobs DB is write-locked, and at the next `advance` (585-587).
   - A failure moves the job to `index_error` and raises `ImportJobError('index_failed')` (562-568).
   - The jobs store is opened with `leaf.sqlite_connect(mode, resolve=True, timeout=10)`, or created with `leaf.sqlite_create`.
3. **Claude capture ledger `cursor.pending`.**
   - Set at claude_episode_capture.py:372-376, before the seal.
   - The publish sequence is: seal (400), enqueue (401), upsert (402-403), then clear in `BEGIN IMMEDIATE` (408-416).
   - A failed upsert is replayed by the next capture (330-333). Status fields are at 228-232.
4. **Rollout ledger `cursor.pending`.**
   - Set at launch_binding.py:674-677.
   - The sequence is: seal (678), enqueue (679), upsert (680-681), advance (685 → `_advance` 688-696).
   - The child rollout now runs these same lines (D34).
5. **Spool retries.** `ATTEMPTS=8`, after which the event is marked failed and the cursor moves on (spool_ingest.py:51, 461-467, 545-555).

**No pending state at all:**
- W4 (best effort);
- W7 (raises; a replay repairs it, host_card_bridge.py:85-86);
- W8, W11;
- the ops imports;
- claim/link.

**`episode_scope.covered` and `scope_counts.covered`** are relative to the coverage token and unordered; they are not a queue. A grep for `outbox|watermark|AUTOINCREMENT` in product code finds nothing (C5).

## 3. `EpisodicSearchIndex`

**Schema** (20-34; unchanged):
- `metadata(version)` with value 1.
- `indexed_episodes(episode_id PK, binding, payload_sha256)`.
- `event_search` is FTS5 with columns `binding UNINDEXED, episode_id UNINDEXED, event_id UNINDEXED, text` and `unicode61`.
- `coverage_token(token)` is created lazily (34, 37-38).
- Readers and writers require `version == (1,)` (74-75, 259-260).

**`rebuild()`** (91-123):
- The store is opened rw (101). One `BEGIN IMMEDIATE`, then `DELETE FROM event_search` and `DELETE FROM indexed_episodes`, deleting in place (103, 105-106).
- It iterates `raw_rows` on the open store connection, re-reading each record and opening one readonly connection per source episode (ss:663-677).
- Then `optimize` (117) and commit (118). `mark_coverage(..., reset=True)` runs after the commit (122).
- The whole time, it holds the index write lock and a store SHARED lock. In rollback mode, that SHARED lock blocks store commits.

**`upsert_episodes(ids)`** (143-197):
- At most 2000 IDs. Sources are verified outside the transaction (157-167).
- An unchanged `(binding, digest)` is skipped (177-180). A changed one runs `DELETE ... WHERE episode_id=?`, which scans the whole FTS table (173-176, 182), then `INSERT OR REPLACE` (183-188).
- `mark_coverage` runs after the commit (195), so coverage can under-report but never over-report (193-194).

**Search** (`search_records`, 205-347):
1. `resolve_scope` (221); readonly `sources.connect()` (223; ss:751-772).
2. Validation (227-233); `counts` (234).
3. Phrase (242); `candidate_limit = min(200, limit*10)` (243).
4. No index file: `index_unavailable` (246-253).
5. Otherwise `ATTACH` via `leaf.sqlite_uri(self.path, 'ro', resolve=True)` (258), then the token (261-264), then one statement (267-273).
   - `member` comes from ss:316-323.
   - `ORDER BY e.rank` ranks every match before scope and LIMIT apply (the T10 P3(c) cost; unchanged).
6. A token mismatch sets covered to 0 (276-277). Candidates are trimmed (278-280).
7. The phrase regex runs on the indexed text, and a digest mismatch marks the episode stale (281-292).
8. The chosen episodes are reopened in a batch (295-296). Each is checked with `plan.admits`, the sealed digest (`leaf.sha256_hex`, 305) and the sealed-text regex (299-314).
9. Stale episodes are subtracted (336-337), and the status is set (338-340).

**What an event stores.** `binding` is `''` for every `source_episodes` row (112-114, 159-165). Search never reads `e.binding` (268).

**Desk membership** lives only in `episode_desks`, derived from active `session.owner` claims (ss:57-58, 78-79, 205-224). An unlinked legacy episode is assigned its storage binding as the desk, with tenant `''` (ss:1017-1019, 1037).

**What per-desk postings would change.** The same observations as before:
- Topic scope goes through the live registry (ss:322-332).
- The addressable-delete need is unchanged. tests/test_episodic_search_incremental.py still pins DELETE behaviour.
- Contracts to keep: T10 P4 (test_t10_p4_projection_honesty.py:86-118) and P4b (test_t10_p4b_integrity.py:7-10; es:299-307).
- Detection point: `_reflect` old desks (ss:1008-1010) and new desks (1025-1027).

## 4. Coverage

**The store side:**
- `scope_state('coverage_token')`, `episode_scope.covered`, and `scope_counts.covered` via `_contributions` (ss:227-234).
- `mark_coverage` (ss:1072-1090) runs in `BEGIN IMMEDIATE` (1080). It does nothing if the projection is not installed (1081-1082). On a reset or token change it zeroes covered and records the token (1084-1087). `_cover` sets covered from digest equality, in chunks of 2000 (1092-1114).
- `_reflect` carries `covered` (991, 1003-1005, 1029-1033).
- `_establish_coverage` (1311-1345) reads or creates the index token (1317) and resets on a mismatch (1320-1326). It batches over `episode_scope` under the store write lock while reading index digests (1329-1339).

**The read side:**
- Unfiltered scopes read aggregates; filtered scopes sum `covered` (ss:782-815).
- `index_status` is one of `index_unavailable`, `incomplete_index`, `zero_results` or `results` (es:246-253, 338-340).
- `absence_verdict` is `not-established`.
- The goldens hold only `results` (28) and `zero_results` (21), re-counted at this base.

**Invariants T12 must keep.** These are unchanged:
- `covered` never over-reports (es:193-194).
- A new file starts with no coverage (es:37-44).
- Seal, claim and link never open the index (test_t10_p4_projection_honesty.py:152-191).
- After a reassignment, `covered == total`.
- The response fields stay as they are (es:341-347), and the P5 goldens stay unchanged.

## 5. Sealing

**Every seal is one store `BEGIN IMMEDIATE`, with `_reflect`:**
- `EpisodeStore._capture` (episodic_memory.py:161-181): insert (167), `_capture_identity` (ss:418-435, its claim insert 434), link (176), `_reflect` (179).
- `import_operator_episode` (episodic_memory.py:213-240).
- `SessionSources.import_episode` (ss:643-660): insert (655), link (658), `_reflect` (660).
- claim (ss:547-564), link (609-626), and the backfill (1281, 1295, 1298, 1303).

**One row per transaction** in capture loops (workspace_capture.py:569; session_import_job.py:669).

**`_reflect`** (ss:983-1058) returns early when the projection is not installed (989-990).

**Triggers.**
- Same-transaction precedent: `_TRIGGERS` (ss:94-97) over `_MARKED` (47), installed by `_install` (1169-1182).
- `ensure_catalog` (ss:394-416) installs no triggers. Its two callers are ss:385 and workspace_capture.py:263.

**Sequences:**
- `episode_scope(kind, seq)` (ss:968-980; schema comment 66-68). It is indexed by `source_session_id` (`episode_scope_session`, 75).
- There is no AUTOINCREMENT, nothing deletes or updates sealed rows (C5, C9), and there is no cross-table sequence.
- `scope_state 'cursor:<table>'` is transient (ss:1256-1258, 1308, 1246).

## 6. Locks and leases

**The index:**
- `BEGIN IMMEDIATE` in `rebuild`, `upsert` and `coverage_token` (es:103, 170, 128).
- Opened through `leaf.sqlite_create` (60) and `leaf.sqlite_connect(mode, resolve=True)` (73), with the default 5 s timeout, `isolation_level` `''` and no WAL (D84, D85).
- Coverage is written in a separate store transaction (es:195), so the two are not atomic.
- `_establish_coverage` holds the store write lock while it reads the index (ss:1329-1339).

**Existing leases and locks** (no new ones; the set is identical at both bases, C12):
- Workspace capture: `flock LOCK_NB` on `workspace-capture.lock`, opened via `leaf.open_fd_stream(..., create=True)` (workspace_capture.py:329-342; D88). Per-file lease of 120 s (447-469, 588-592, 613-616).
- Import jobs: per-job lease of 120 s (session_import_job.py:717-725, 739-741, 757-759). `advance` holds the jobs `BEGIN IMMEDIATE` around its upsert (578, 586, 695-696).
- Claude capture: flock with a 2 s deadline (claude_episode_capture.py:253-270).
- `RolloutCapture`: flock with a 2 s deadline (launch_binding.py:530-556).
- Spool: `leaf.private_lock`, blocking `LOCK_EX` (spool_ingest.py:89-90; leaf:787-794).
- Launch `_locked`: `leaf.private_lock` (launch_binding.py:77-78).
- Refresh: `.refresh.lock` (refresh_cli.py:777-780).
- Episodic queue leases (episodic_queue.py:138-195).

**No one-indexer lease exists.** The store and the index live on the `memory` volume, where POSIX locks hold (compose.yaml:26-34).

**Swap hazard.** It is unchanged:
- Readers refuse a symlinked index (es:53, 244).
- The installer's backup and prepare steps walk every `*.sqlite3` file with its sidecars (runtime_install.py:1122-1129, 1169-1170).
- `os.replace`, O_CREAT and `os.fdopen` are forbidden outside leaf (D130).

## 7. Watch loops and restart policies

**`pkg/workspace_capture_cli.py`** (byte-identical):
- `watch` (45-58, 83-87); `worker()` (69-75).
- The only handler is at 90-92. Exceptions ending it with a traceback: `DeskLaunchUnavailable` (desk_binding.py:59), `EpisodeUnavailable` (episodic_memory.py:34).
- Per-file failures do not end it: `_run` catches them (workspace_capture.py:401-405), reports a partial pass (410-411) and records it (434-441, 606-610).

**Transient causes that end the loop:**
- `git rev-parse` failure or timeout (114-123, 154-156) in `__init__` (253);
- lock contention (335-338);
- a candidate that vanished (354, 371);
- the directory bound (185-186);
- sqlite errors in `initialize`/`_db` (257-286; D104) and `_run` (347-393); `ensure_catalog` at ss:411-416.

**Supervision of the capture role:**
- compose.yaml:166-167. `container.py` runs the preflight, then `execvp` (234-263).
- `spool_ingest.watch` (587-631) exits with the child's status (614-616, 628-631). Its own errors never end it (607-611, 571-578, 545-555).

**Refresh:** per-cycle `except Exception` (refresh_cli.py:781-793). The setup before the loop can exit (775-780).

**Restart policies:**
- No `restart:` anywhere (compose.yaml byte-identical: `x-role` 37-43, services 99-257, `init: true` 40).
- The installer copies compose byte for byte (runtime_install.py:536-537), and its overlays add only volumes (540-557).
- Pins: `SERVICES`/`COMPONENTS` (runtime_install.py:63-64, 482-484); s3_harness.py:35-39; test_p4_rendered_isolation.py:57, 60-82; t7a stays-up (test_t7a_p1_roles_start_image.py:8-21, 42; t7a_harness.py:264-285).

## 8. Tests that pin index behaviour

**Direct calls, T10 helpers, T10/T10h tests, patches and fakes, and counters.** These are as in the old census, at the same lines (all files byte-identical since the old base; D114–D123).

**New since the old base:**
- tests/test_t11a_carried.py:38-44: rebuild, then search.
- T11a P4 connection golden and T11b `RULINGS` (D128).
- T11a P7 rollout golden (D129).
- T11 leaf guard (D130).
- tests/test_t11b_q2_sqlite_profile.py: the resolved-URI recorder.

**Tests that rely on inline indexing (re-derived with C11).** Each file's searches were read against the capture that precedes them. Explicit `rebuild`/`upsert`/`index-history` callers are excluded.
- tests/launch/test_t3_p1:45-46, test_t3_p2:81, test_t3_p3:103, test_t3_p4:67-68, 154 (hook capture, W1/W2).
- tests/host/test_t4_p1_host_launch.py:81, through host_harness.py:880-882.
- tests/desks/test_t2_p1:140, 171; test_t2_p2:60, 71, 93; test_t2_p3:63, 80, 118, 127; t2_harness.py:330. These go through `capture_card` → `kp-agent-host-card --apply`, i.e. W7 (t2_harness.py:238-246).
- tests/test_assistant_host.py:25 (W1, assistant hook).
- tests/test_native_history_import.py:51, 240 (W8).
- NEW: opstests/test_claude_memory_hook.py:136-137, 161 (W1).
- NEW: tests/test_workspace_capture_adversarial.py:104-110 (W5 `once`).
- NEW (Docker image):
  - tests/install/test_t7b_p2_desk_tasks_image.py:149-185, one-shot search after the hook;
  - tests/install/test_t7b_p4_assistant_memory_image.py:157-163, one-shot search, components `("tooling","board")`;
  - tests/install/test_t7a_p2_documented_steps_image.py:391-408, polls;
  - tests/host/test_t7a_p7_unconfigured_capture_image.py:80-85, polls.
- Dropped: opstests/test_launch_claude_desk_memory.py:270. It is a setup-missing diagnostic (asserts `isError` at :286). The twin in test_launch_claude_desk_memory_ops_original.py:85 is the same.
- Not reliers: tests/test_t10h_p2_capture_never_backfills.py:315 asserts only "no error" after `upgrade-sources`, and tests/test_session_sources.py searches after claims, not captures.

## 9. Reindex and repair tooling

- `index-history`: in-place rebuild (desk_cli.py:14, 61-64).
- `upgrade-sources`: catalog, `episode_desks` rebuild, backfill, coverage (desk_cli.py:54-60; ss:377-392, 1311-1345). `quick_check` runs only on rebuild or an incomplete projection (ss:386-388, 1215-1216).
- `import-native-history --apply`: rebuild (desk_cli.py:39-53).
- `session-import continue`: drains `index_pending` (session_import_cli.py:84-86).
- `workspace-capture once`: drains `files.index_pending` (workspace_capture.py:594-595).
- A host-card replay rebuilds (host_card_bridge.py:87-88).
- The ledgers replay `pending` (claude_episode_capture.py:330-333; launch_binding.py:674-677).
- `reconcile_desk_history.py`: rebuilds an isolated copy (W11).
- There is no standalone repair script for live state.

## Instruments that exist, and the gaps

- **Capture never opens the index:** `t10_instruments.recording().connected(index_path)` (test_t10_p4_projection_honesty.py:100-101, 166-167).
- **Single writer:** the connect audit, the statement trace with `db == index_path`, and `_traced` in test_episodic_search_incremental. NEW: the T11a P4 per-step connect recorder (generate_connection_profiles.py) and the T11b Q2 `resolving_recorder` (test_t11b_q2_sqlite_profile.py:43-56).
- **No in-place delete:** a trace of `DELETE FROM event_search` (`_deletes`).
- **P3(c) VM steps:**
  - Exists: `Measured.steps`, `t10_measure.measure` and `History.grow` (t10_measure.py:45-58, 442-495).
  - Missing: a matching-text growth fixture. `_fill` writes non-matching text (401-416).
- **P3(c) median wall time:** NONE. Only a single `monotonic` bound exists (test_t10_p4_projection_honesty.py:184-191).
- **Reassignment and the dirty set:** the P4 shape, P4b's `t10_tamper`, the P5 goldens, and `t10_oracle.select_for`.
- **Restart resumes at the watermark:** NONE.
- **The watch loop survives an error:** only `t7a_harness.assert_stays_up`. In-process: NONE.
