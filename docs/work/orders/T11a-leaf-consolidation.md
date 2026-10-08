# T11a — One leaf module: each invariant has one home (no observable change)

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

Principal ruling, 2026-10-02, verbatim: "the single leaf module is the right approach; no piece of data or module should have more than one roundtrip per call".

Status: frozen 2026-10-03 (r3, Coordinator). r3 adds Verification's M1 and M2, and Verification ruled it freezable at main after T9b with the re-census.
- r1 was one T11. Verification's pre-freeze review (L1–L9, Q1–Q3) split it by observability:
  - **T11a (this order):** everything whose falsifier is "nothing observable changes";
  - **T11b:** every ruled behaviour change.
- **Base:** main with T9b, T10 and T10h merged and deployed.
  - **The census.** It was re-run at the freeze base, main with T9b and T10 merged (L9); see "Re-census" below. From the freeze base to the base above, T10h changed product code only in `session_sources.py` and `workspace_capture.py`, which adds one plain, non-canonical `json.dumps`. Every category reading below holds at the base above.

## Problem

Measured by AST walk at the census commit, an earlier main. The census, [T11a-census-1e74728.md](T11a-census-1e74728.md), lists every site with file:line. The re-census at the freeze base (below) corrects these numbers and supersedes them where they differ.

- **Canonical JSON.** 49 canonical sites (0 embedded): 26 helper definitions and 23 inline. 6 argument combinations give 4 distinct outputs, which differ in `ensure_ascii` and `allow_nan`. Those outputs are hashed into persisted content addresses.
- **Hashing.** 24 generic helper definitions, 10 of them identical `_sha` copies. 12 domain identity hashers. The `deskdoc:` chunk-ID scheme is implemented twice (`retrieval.py:32`, `document_coordinates.py:28`). The `hashlib.sha256` sites total 172 in module code plus 4 embedded.
- **Private files.** 30 helper definitions with different guarantees (the re-census correction), and 144 primitive sites.
- **SQLite open.** 39 `sqlite3.connect` sites (36 module, 3 embedded): 10 per-store helpers and 26 inline. 9 connection profiles, 3 URI spellings.
- **The store-path rule exists twice:** `deploy/image/container.py:105-227` and `_impl/runtime_install.py:123-133, 724-783`.
- **Store filenames.** 27 literals. `episode-search.sqlite3` appears 12 times with no constant; one name is built as `name + '.sqlite3'`.
- **`_page`.** `launch_binding.py:624-708` and `spool_ingest.py:206-292` share 70 of 81 lines. They differ in the exception alias, the session predicate and two message strings. No existing test names `RolloutCapture` or `ChildRolloutCapture` (`git grep` at the census commit).
- **Dead code.** 24 definitions, 514 lines, with no reference anywhere.
- **Imports.** Six function-local imports exist only to reach a helper in a heavy module.

## The leaf

`packages/tooling/src/kp_agent_tooling/_impl/leaf.py`, stdlib only. It imports no product module. Every module may import it at top level. FEATURE names the public API; the census proposal is a starting point.

In T11a every leaf function reproduces each call site's current behaviour exactly. Where sites differ today (URI resolution, timeout, fsync, create-then-chmod), the difference is an explicit leaf argument, set at each site to what that site does now. T11b changes those arguments under its own rulings.

## Properties and falsifiers

- **P1: content addresses do not move.** Every canonical serialisation and every identity digest is produced by the leaf. Each of the 4 compact output variants is an explicit leaf argument, reproduced byte for byte.
  - **A fifth variant: sort_keys-only, not compact (Verification M1).** It is the leaf's `sorted_json`, also reproduced byte for byte. It feeds persisted digests at:
    - `commit_rationale.py:689`, `:693`, `:695`;
    - `verification_adjacency.py:80`;
    - `verification_correctness.py:29`;
    - `verification_packet.py:63`, and its `encoded` at `:17`;
    - `agent_tooling.py:66`;
    - `refresh_cli.py:460`.
  - **`scip_entry.py:41`** hashes an unsorted `dumps` of the SCIP entry as emitted. It is ruled a non-canonical wire hash and allowlisted under P2 with that reason. Its digest is in the corpus.
  - **The golden corpus.** It is committed, together with the command that generated it at base. For every identity scheme in use, it maps a fixed input set (non-ASCII text, NaN where a variant allows it, nested, empty) to its output. The schemes:
    - episode/claim `_id`;
    - `binding_key`;
    - `code_references.models.canonical_digest`;
    - `behavior_model.digest`;
    - `observations.digest`;
    - `review_ledger.canonical_digest`;
    - `finding_digest`;
    - `revision._sha256_identity` and `embedding_identity`;
    - `scip_navigation.digest`;
    - `desk_write_scope._digest`;
    - `episodic_queue._digest`;
    - `memory_budget._digest`;
    - `session_bindings._digest`;
    - `workspace_setup.digest`;
    - `refresh_cli.digest_json`;
    - the hash chain in `claude_episode_capture._digest`;
    - `deskdoc:` chunk IDs;
    - `change_lineage_key` and `change_occurrence_id`;
    - `codex_trust_entry`, in all four combinations (L1);
    - every `sorted_json` digest above, and `scip_entry.py:41` (M1).
  - **`codex_trust_entry` is two trust entries by design (L1).** `host_adapter.py:475` uses `HOOK_TIMEOUT_SECONDS` 5. `launch_binding.py:149` uses 30, takes `matcher` and fixes `source`. Each must match Kanban's `codex-hook-config.ts` for its own hook group. The one implementation takes timeout, matcher and source as parameters. The corpus holds timeout 5 and 30, matcher present and absent, and both source forms.

  Falsifier:
  - any golden output differs at head;
  - an existing store's sealed episode no longer verifies (T10 P4b, `upgrade-sources`);
  - a Kanban hook trust entry changes (`apps/kanban` tests unchanged).
- **P2: one home per scheme (Q1).** Outside the leaf there are zero:
  - (i) generic sha256 helper definitions;
  - (ii) canonical-serialisation→digest constructions;
  - (iii) second implementations of any identity scheme;
  - canonical `json.dumps`/`json.dump` calls.

  Inline `hashlib.sha256(<bytes>).hexdigest()` on raw bytes may stay where it is, and so may streaming `.update` sites. The stdlib-only embedded scripts (`runtime_install.py` `_INSPECT`, `_OWN`, `_CLEAR`, `_STORE_LIB`, `_MIGRATE`; `dependency_identity.py:124-130`) are a frozen allowlist; they run under `python3 -I -c`. Domain identity hashers keep their names and schemes and call the leaf.

  Falsifier: a guard count above zero for (i)–(iii) outside the allowlist.
- **P3: one home for private files, guarantees unchanged.** Every private-file write, read, directory and lock helper is implemented once, in the leaf. Each site keeps its current guarantees through explicit arguments: create-then-chmod or create-with-mode, NOFOLLOW or not, fsync or not. Outside the leaf there are zero private-file helper definitions, and zero of these primitives:
  - `os.open` with `O_CREAT`;
  - `O_NOFOLLOW`;
  - `'x'` open modes;
  - `tempfile.mkstemp`, `tempfile.NamedTemporaryFile`, `tempfile.mkdtemp`;
  - `os.replace`, `os.link`, `os.fdopen`.

  Exceptions are a frozen allowlist with a reason each, for example directory renames that are not private files, and the 0o400 delivery at `tool_delivery.py:57`.

  Falsifier: a guard count above zero; an existing test that changes; an `os` call sequence that differs from base for any helper. Instrument: `sys.addaudithook` on `open`, `os.chmod`, `os.rename` and `os.link`, comparing base and head per helper on the same inputs.
- **P4: one SQLite open, profiles unchanged.** Every product connection is opened by the leaf, and each site keeps its current profile through explicit arguments:
  - URI spelling, `mode`, `resolve` or not;
  - `timeout`: the default equals sqlite3's 5.0, and the sites with 10 and 30 keep theirs;
  - `isolation_level=None` (`model_gateway.py:338`);
  - `check_same_thread=False` (`manifest.py:41`);
  - `:memory:`.

  `row_factory` and `text_factory` stay set by the caller. The leaf adds no implicit pragma. It calls `sqlite3.connect` by attribute at call time and passes neither `factory` nor `cached_statements` (`tests/t10_instruments.py:298-341`). The embedded scripts are allowlisted.

  Falsifier: `sqlite3.connect` outside the leaf and the allowlist; a T10 test or statement count that changes; a connection whose raw `database` argument, `uri`, `timeout`, `isolation_level` or `check_same_thread` differs from base.

  Instrument (Verification M2): the T10 recorder realpaths the database argument and records no kwargs, so it cannot see these. TEST records the raw `database` argument and every connect kwarg, run at base and at head on the same inputs. It does this either through a raw-capture flag added to `tests/t10_instruments.py`, leaving T10's own behaviour and tests unchanged, or through its own wrapper.
- **P5: one store-path rule (consolidation only).** `STORE_ROOT`, `STORE_KEYS`, `TEMPLATE_KEY`, `LEGACY_STORES` and `required_store_path`/`outside_store` are implemented once, in the leaf. `_impl/runtime_install.py` and `deploy/image/container.py` use it.
  - **Policy change, ruled here:** `container.py` imports the installed package's leaf. It runs in `product-base`, where the package is installed (`deploy/Dockerfile:101-114`).
  - **Behaviour unchanged.** That includes `runtime_install`'s control-character rejection, which `container.py` lacks today: the leaf keeps it, and `container.py` gains a refusal only for a path no operator file could hold. T11a names that as the one reading.
  - Enforcement at open is T11b.

  Falsifier: two implementations of the rule; any T9b test that changes.
- **P6: one name per store file.** Each store filename is one leaf constant, and store paths are derived by one leaf function. Zero `*.sqlite3` literals or `+ '.sqlite3'` constructions remain outside the leaf, except docstrings and the embedded allowlist.
  Falsifier: a guard count above zero.
- **P7: one `_page`.** `RolloutCapture._page` and `ChildRolloutCapture._page` become one implementation, with the session predicate, the exception and the message as parameters.
  - **Instrument (L6).** No test exercises either class today, so TEST adds a golden capture test generated at base.
    - It covers rollout and child-rollout capture over fixtures for: a complete page, a partial trailing line, a prefix change, and each of the 7 conflict raises.
    - It compares outputs, cursors and the exception type and message byte for byte.
  - **The near-duplicate guard** (the census method: functions of at least 15 statements, at least 90% statement similarity) reports exactly one pair at base: the two `_page`s. A guard that reports zero, or more than one pair, at base is wrong.

  Falsifier: the guard reports a pair at head; any golden capture difference.
- **P8: no dead code, with three rulings (L5).**
  - **Removed:** 23 of the 24 unreferenced definitions. `scope_ids` and `capture_identity` (`session_sources.py`) are T10 residue, removed here and named as such.
  - **Kept:** `ProductionLocalEmbedding` (`embedders.py:198-277`) is the production embedder T13's embeddings overlay will need. It is listed for the Principal with that note.
  - **Five word matches to explain.** The census cross-check (a raw word count including `apps/kanban`) flags 19 of the 24. The re-census names the other 5, with why their word matches are not references.
  - **Test-only:** the 10 definitions referenced only by tests stay, listed for a later ruling.
  - **The guard** reports, at base, exactly the census set. At head it reports nothing outside the allowlist: `do_GET`, `do_POST`, `log_message`, `get_request`, `ProductionLocalEmbedding`, and Protocol stubs if any remain.

  Falsifier: an unreferenced definition outside the allowlist at head.
- **P9: everything else is unchanged.**
  - **Old names stay importable (L8).** Moved helpers keep one-statement delegations under their old names where tests or other repositories import them. Examples: `episodic_memory._id` and `_bytes`, `desk_memory_runtime.private_json`, `launch_binding.write_private`. A one-statement delegating `def` is not a definition under the guard.
  - **The tests.** Every existing test passes byte-unchanged, except the regression-set exception: a test that monkeypatches a moved helper may change only its patch target. TEST lists each.
  - **Outputs.** T10's golden outputs and statement counts are unchanged. T9b's tests are unchanged. `apps/kanban` tests are unchanged.

  Falsifier: any other test change, or a golden, statement-count or trust-entry difference.

## Carried items

- **TEST, T10 V3.** "A record whose sealed row disagrees with the index is dropped" (`episodic_search.py:300-305`).
  - Setup: after indexing, re-serialise one sealed episode's payload as equivalent JSON (same content address, different raw sha256).
  - Expected: search drops it, and `covered_episodes` falls by 1.
- **TEST, dependency farm.** It installs `setuptools` into the scratch venv for `tests/boundary/test_t1_p3`/`_p4` (Python 3.14 venvs lack it).
- **FEATURE, docs.** T10's closing line becomes: raising `limit` or `candidate_limit` is safe only once T12's P3(c) holds.
- **Unchanged after FEATURE's DOCKER.md edits (T9b K1):** "`verify` observes the store with one read-only one-off container" stays true.

## Instruments

- **The guard test** (`tests/test_t11_leaf_single_home.py`).
  - Census AST patterns (a)–(h), aliases resolved. Embedded scripts parsed and allowlisted.
  - **Base reading (L9).** Before freeze it is run at base and must report the re-census numbers per category, module and embedded separately. At the census commit, for orientation:
    - (a) 49 canonical, 0 embedded;
    - (b) 24 helpers, 12 domain hashers; 172 module + 4 embedded sha256 calls;
    - (c) 29 helpers, 144 primitives;
    - (d) 36 module + 3 embedded connects;
    - (h) 27 store filenames.
  - At head it reports zero outside the allowlists.
- **Golden corpora** (P1 identities, P7 capture) are generated at base by committed commands.
- **Base-vs-head comparisons** (P3 audit-hook sequences, P4 connection profiles) run the same inputs through base and head in-process.
- No Docker except the existing image suites, which must stay green. No test asserts today that `container.py` is stdlib-only, so P5's policy change (it imports the leaf) is checked only by the image suites staying green.

## Write scope

- **FEATURE:**
  - `_impl/leaf.py`;
  - every product module the census names for (a)–(h);
  - `deploy/image/container.py`;
  - docs that name a moved helper;
  - the T10 closing line.

  It names its leaf API, and each allowlist entry with its reason.
- **TEST:**
  - new files under `tests/`: the guard and its base reading, the P1 corpus and generator, P3/P4 base-vs-head, the P7 golden capture, and the V3 test;
  - the dependency-farm fix;
  - the P9 regression-set exception.

## Re-census at the freeze base (main with T9b and T10 merged; it holds at the base above)

- **What changed since the census commit.** Only `runtime_install.memory_readiness` (+16/−11). No category site was added or removed; one `json.dumps` moved from :1958 to :1960.
- **Base readings for the guard (module + embedded):**
  - (a) 49 canonical + 0, split 26 helpers and 23 inline (purpose reading);
  - (a) fifth variant: 9 + 1;
  - (b) `hashlib.sha256` calls 172 + 4; generic helpers 24 + 3 (embedded `digests`, `copy_bytes`, `file_digest` are streaming); domain hashers 12;
  - (c) helpers 30 + 6, corrected from 29; primitives 132 + 12;
  - (d) `sqlite3.connect` 36 + 3; connection helpers 10 + 2;
  - (e) 14 `state_root` sites; 2 copies of the rule;
  - (f) exactly one pair: `_page`, ratio 0.968;
  - (g) 24 definitions, 514 lines;
  - (h) 27 + 2.
- **(c), the 30 helpers.** The count adds `desk_registry.py:126 RoleRoster._locked` and `spool_ingest.py:90 Cursors.locked`, the latter a copy of `launch_binding._locked`.
- **Fifth-variant sites, corrected.**
  - Added: `workspace_capture.py:264` (`_sha(json.dumps(policy, sort_keys=True).encode())`) and embedded `runtime_install.py:1439e/1477e`.
  - Removed: `verification_packet.py:17 encoded`, which never reaches sha256; it only sizes payloads.
  - `scip_entry.py:41` is `sha256(json.dumps(sorted_list).encode())`.
- **(g), the 6 definitions the raw word count did not flag.**
  - `_desk_note_scope_matches`, `DeskDocRetrievalResult`, `_repo_scope`, `_render`, `_markdown_text`: each is used only inside dead code.
  - `ProductionLocalEmbedding`: its only mentions are its own error strings.
  - `scope_ids` and `capture_identity` are T10 residue, per blame.
- **P9 tests.**
  - No patch target changes.
  - 27 by-name imports in 24 files. The one-statement delegations under the old names keep them unchanged.
  - Global patches the leaf must respect, by looking names up by attribute at call time:
    - `tests/t10_instruments.py:331-348` (`sqlite3.connect`);
    - `tests/refresh/t5b_rig.py:109-123` (`open`, `os.open`, `stat`, …).

## Carried from the T10h meet (Verification, 2026-10-03)
- **TEST, in-process coverage guard.** Mutant M-V1, which skips the coverage step on the complete path (`if False and coverage_index is not None`), passes every in-process test; only T10h's image test P3 catches it. Add one assertion: `SessionSources.upgrade(coverage_index=…)` on a complete store reports `coverage.index == 'present'` and `memory.search` covers every episode.

## Amendment 1 (2026-10-03; meet rulings, Coordinator with Verification; additive)

- **P8, census miss (allowlist rule 2).**
  - `extensions/ops/src/kp_agent_tooling_ops/_impl/behavior_model.py:69-109`, `assess` (41 lines), is dead at base:
    - its importers take only `digest` and `validate`;
    - its only test was excluded at extraction.
  - The census missed it because the name matches the live `ObservationStore.assess`: the census method's own documented false-negative class.
  - It is removed with the P8 definitions. The guard's base reading for (g) is 24 + 1.
  - **A second miss, the same class.** `behavior_model.project(graph, model)` (`behavior_model.py:48`) is dead at base. Its importers take only `digest` and `validate`. The only `.project(` call, `apps/kanban/scripts/test_desk_run_export.py:22/44`, belongs to a different module's six-argument `project`, and the legacy source repository has no import of `behavior_model`. It is removed too. The name clash hides it from both the census method and the guard's (g) count. Two other look-alike calls are not references: `tests/test_operation_navigation.py:45/51` call a `project(path, model)` whose import is commented out at `:5-7` ("S5 excluded: kp_ops.operation_navigation is OPS-only, not extracted"); and `extensions/ops/tests/test_reference_bounds_continuation.py:40`'s `project(enabled)` is a one-argument local. The guard's (g) method reads 24 + 1 at base: word matches in `records/extraction/source-files.json` and `tests/EXTRACTED-TEST-LEDGER.md` keep `project` live to it. `project` is removed on the direct evidence above, and the method is not widened to see it.
- **Base readings in the guard's own vocabulary** (Verification). These sit beside the Re-census's purpose readings, so "matches at base" is a literal, re-runnable check:
  - (a) 49 canonical + 0 embedded. The guard's split is 26 helpers / 23 inline, with `manifest.py:65` and `knowledge_publish_cli.py:31` classified the other way round from the census.
  - (b) embedded generic helpers: 0 by pattern, plus 3 streaming digest functions (`digests`, `copy_bytes`, `file_digest`).
  - (c) embedded helpers: 5 named.
  - (d) connection helpers: 10 + 1.
  - (e) 11 `['state_root']` subscript reads, plus 3 parameter sites (14 in the census's purpose reading); 2 rule copies.
  - (f) exactly one pair, `_page`, at ratio 0.967 under the guard's normalisation.
  - (g) 24 census definitions, plus `behavior_model.assess` (`project` is removed on direct evidence; the guard's word-match method cannot see it).
  - (h) 27 + 2.
- **Allowlist reason classes added** (each matched by file + enclosing def + kind):
  - **`wire_hash`:** a digest of the exact bytes sent over a wire, kept as a receipt or record field, and never re-derived from a value. Members: `scip_entry.py:41`; `episodic_summarizer.py:343→346` (`request_sha256`). Not this class: `refresh_cli.py:460`'s different `request_sha256`, which is re-derived and compared (`:526`). It is a `sorted_json` identity site, in the corpus.
  - **`file_content_hash`:** a digest of the exact bytes written to a file, recorded beside it, and verifiable only against that file. Its JSON form is a file format, not an identity scheme. Members:
    - `capture_cli.py:50→53`;
    - `transcript_cli.py:36→39` and `:43→46`;
    - `runtime_install._json` rendered files hashed at `:945`;
    - `host_adapter.py:669/710 → _sha(merged)` at `:784`.
    - `scip_navigation.py:58` (`write_partitioned_index`): the part file's sha256, recorded in the descriptor and verified only against the file.
  - **Same shape, still counted as identities.** These must go through the leaf and are in the P1 corpus:
    - `navigation_search_pages.py:53` `_save` and `navigation_snapshot.py:92/108` `capture`: content addresses that other records cite;
    - `knowledge_publish_cli.py:31` `_write`: a census domain hasher.
- **P3 primitives.** The census's wider primitive set (chmod, 0700 mkdirs, renames, fsync, `st_mode & 0o077`) is a `--report` reading in T11a, informational at base and head. It does not bind. T11b, which rules private-write behaviour, may adopt it as a property with a falsifier. P3's own primitive list binds.
  - **The two populations at base, from the guard's `--report` at base** (module code only; the 12 embedded primitives are on the allowlist as `embedded_script`).
    - **The census's 132 by kind:**
      - Path.chmod 13, Path.mkdir(mode) 17, Path.rename 1;
      - open-x 8;
      - os.chmod 15, os.fchmod 3, os.fsync 15;
      - os.link 7, os.mkdir(mode) 1, os.open+O_CREAT 14, os.rename 4, os.replace 6;
      - st_mode&0o077 13;
      - tempfile.NamedTemporaryFile 3, tempfile.mkdtemp 2, tempfile.mkstemp 10.
    - **P3's own list, asserted: 77** = 50 sites of the census kinds that P3 names (open-x 8, os.open+O_CREAT 14, os.link 7, os.replace 6, tempfile 15) + `O_NOFOLLOW` markers 12 (they overlap the `os.open` sites; the census counted them separately) + `os.fdopen` 15. The census has no row for `os.fdopen`; P3 names it, so the guard counts it.
    - **The wider set, report-only: 77** = the remaining census kinds (Path.chmod 13, Path.mkdir(mode) 17, Path.rename 1, os.chmod 15, os.fchmod 3, os.fsync 15, os.mkdir(mode) 1, st_mode&0o077 13 = 78), less the allowlisted 0o400 chmod at `tool_delivery.py:57`.
    - **The split adds up:** 50 + 78 + the 4 `os.rename` directory renames (allowlisted as `directory_rename`) = 132. The two 77s are different populations that happen to coincide.
- **Goldens.** Not bound to one CPython. P1 runs on every CPython ≥ 3.11. P3 and P4 run ungated, unless a measured version difference is normalised in the recorder, or gated per entry with the diff named.
- **P2(ii) base reading.**
  - The guard counts every serialisation→digest construction outside the leaf: any JSON form, leaf serialisers, and constructions through module-level serialiser or digest helpers, including one-statement delegations.
  - Base reading: 57 + 1. Of these, 6 are class members (`wire_hash` and `file_content_hash`, including `scip_navigation.py:58`), so 51 identity constructions are asserted. The helper-mediated ones include `episodic_memory._id`, `observations.digest`, `runtime_install.py:950` `plan_sha256`, and the `semantic_index` and `navigation_snapshot` identities. `_save`, `capture` and `_write` are in the P1 corpus, generated at base.
  - Without the helper-mediated count, a head that delegates through local helpers would read zero.
- **CI coverage.** `extensions/ops/tests/test_t11a_ops_goldens.py` runs the ops parts of P1, P3 and P4 in the extension job. The core job runs `tests/` without the extension, where those parts skip.
- **P5, invalid values.** The two copies of the store-path rule handled a value containing a control character differently: `container.py`'s preflight refused it, and `runtime_install` ignored it. P5 now reads: behaviour identical for both consumers. The evidence: 0 decision differences and 0 text differences over 208 real-path cases. The leaf has one rule with two readers (`reject_control`, below). T11b may unify the readings as a ruled change.
- **Scope beyond the order (Verification F1).** A wider-set P3 primitive that FEATURE moved into the leaf stays moved only if a base-vs-head instrument exercised that site with 0 differences: the P3 audit, the raw-capture suite recording, an `os.fsync` identity wrapper, or a mode-table check for `st_mode` predicates. Any other moved site reverts to its base code, for T11b.
- **P2(ii) compositions.** A composition of two leaf parts, such as `sha256_hex(canonical_json(x))`, is not a second implementation of a scheme: the scheme's home is already the leaf. It still becomes one leaf call, so that the head's zero is read in the same construction vocabulary as the base reading. The 8 such sites at the meet are a guard-vocabulary requirement, not leaks. Each digest's consuming line decides its class: a token compared on resume is an identity, kept in the corpus; a body digest sent on the wire is `wire_hash`.
- **`DOMAIN_HASHERS`.** The exemption's membership is fixed to the census's 12 domain identity hashers, by file and def. Each is exempt only while it is a one-statement call into a leaf digest function. Any other digest-returning definition is either a generic helper, which is delegated or removed, or an identity scheme the census missed, which gets an amendment line and P1 corpus entries under rule 2. It never enters the exemption.
- **Two more identity schemes, census misses (rule 2).** FEATURE classified both, with consuming lines; they are added to P1's scheme list, with corpus entries generated at base through public entry points.
  - **Host transcript record ids**, now `leaf.transcript_record_id`, which replaces `host_transcript_capture._record_id`. They are persisted in the extraction bundle's `use_record_id`, `result_record_id` and `malformed_rows`, and written to `manifest.json` (`transcript_cli.py:43`).
  - **The session-import plan token**, now `leaf.canonical_token` and `canonical_token_bytes`, which replace `session_import_job._plan_token`. It is compared when `apply(plan_token)` resumes (`session_import_job.py:495`).
- **Guard precision at the meet.** These are not widenings; each base reading above is unchanged by them.
  - A leaf digest call whose argument contains another leaf digest is two identities, not a construction outside the leaf.
  - Serialiser helpers are classified by what they do, not by their name; JSON readers are not serialisers.
  - A generic sha256 helper returns the digest itself. A def returning a container with a digest field is not one; for example, the projection-row builders' raw-bytes `payload_sha256` (Q1).
- **P5's mechanism.** `leaf.state_path` and `leaf.required_store_path` take a required `reject_control` argument: runtime_install passes `True` and ignores such a value, while container.py passes `False` and refuses as at base. A distinct "invalid" result would have made container.py also refuse `/state/memory/x\x00`, which base accepts. A 208-case check over both consumers' real code paths shows 0 differences in decisions and in text.
- **The F1 fsync instrument.** The base-vs-head fsync comparison recorded the file path behind each fd (`F_GETPATH`), because inode numbers differ from run to run. That is adequate for a base-vs-head comparison on one machine. It is not T11b's N4 instrument, which must see the parent directory's fsync, so it does not carry over to T11b.
- **P2(i), one rule for base and head.** A def is a generic sha256 helper only if its digest's input is its own parameters, or a canonical serialisation of them. That is the base's rule, and the head is read by it, whether the digest comes from `hashlib` or from a leaf digest function. Under it, `revision.embedding_identity` and `navigation_search_pages._manifest_cache_path` are domain hashers at base and at head, because they hash domain-built inputs behind domain logic. `revision._sha256_identity`, one of the base 24, stays generic and becomes one leaf call. The base reading is unchanged: 24.
- **The guard method's known false negatives (for the next census).** The (g) word-match method cannot see a definition whose name appears in a file that also names its module. `behavior_model.project` is kept live this way by `records/extraction/source-files.json` (13 matches) and `tests/EXTRACTED-TEST-LEDGER.md` (3 matches). The method is not tuned to exclude them.
- **The P3 golden drops the `desk_memory_runtime._private_json` entry.** FEATURE removed the name, and nothing imported it, so P9 does not require it. At base its recorded sequences, outcomes and trees were identical to `private_json`'s, and that behaviour stays covered by the `private_json` entry. No coverage is lost.
- **The P2(i) consistency rule is admitted only as a precision fix.** Under it, the base reads the same 24 names, listed by `--report`, and `revision.embedding_identity` and `navigation_search_pages._manifest_cache_path` read as domain hashers at base as well as at head. If the base list changed by one name, this would be a re-reading of the base and would be recorded here with both lists.
