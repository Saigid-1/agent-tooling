# T12c: per-desk postings. A desk search costs only its desk's matches, and a reassignment is visible on the next read

Status: frozen 2026-10-06 (Coordinator's draft r1; Verification ruled it freezable with A1 and A2, both in).

**Base:** main after T12b and D0f. **Census:** the Coordinator's read-only measurement at main after T12b ("census" below; its sections are cited as A–E). **Header:** ARM-HEADER. **The stopping rule applies** (the Principal, 2026-10-05). A meet finding is a block (a leak, a redistribution, data loss or a silent failure) or it is carried to the contributors' backlog, which becomes the public repository's first issues. The meet record is one paragraph. No commit sha, PR number or run id goes into any committed file.

## Problem (census A3, measured)
- **The index carries no desk and no tenant:** `event_search(binding, episode_id, event_id UNINDEXED, text)` (es:35-45).
- **The join is forced FTS-outer** (`CROSS JOIN`, es:379), and `ORDER BY e.rank` ranks every match in the whole store before the first scope check.
- **The cost:** with the searched desk fixed at 79 episodes, 10,000 matching episodes elsewhere raise desk-scope VM steps 48× (9,103 → 439,480) and the median wall time 4.9 → 19.4 ms. Topic scope rises 18×.

## Properties
- **C1: search cost is independent of other desks' and tenants' matching text.**
  - **Fixture:** the T10 P1 world. The searched desk is held at its 79 episodes. N other episodes match the phrase: for desk scope, half in another desk of the same tenant and half in another tenant; for topic scope, all in another tenant (same-tenant matches are in topic scope). N = 10,000, then 100,000.
  - **Bounds, against N = 0:** desk-scope and topic-scope VM steps grow at most 1.5×. The median of 7 warm in-process runs grows at most 2×, reported as a ratio with the raw samples.
  - **Results do not change:** the P5 goldens are equal, and P4 and P4b are green.
  - **A2 (Verification), one more case:** the bounds also hold with the indexer stopped and K = 1,000 unapplied `desks_changed` rows waiting. Every read is in that state during a long build or a backlog, so C2's consult of unapplied rows must not re-open the cost C1 removes. Report the ratio for K = 0 and K = 1,000 beside each N.
- **C2: a reassignment is visible to the next read, before and after the indexer applies it.**
  - **Applying.** The indexer rewrites the session's postings (its desks and tenant) when it applies a `desks_changed` row, in the same index transaction that advances the watermark. The current sets come from the store at apply time.
    - `detail` is partial: a capture's claim row keeps it NULL, and it covers only the episodes re-projected in that transaction (census B1). It is not a source of truth.
    - If FEATURE reads `detail` at all, a test must read it (mutant M3, `detail` always NULL, survives every suite today).
  - **Until applied,** a read consults the session's `desks_changed` rows with `seq > idx.outbox_watermark`, inside its one store read (T10's one-roundtrip rule): no extra connection, and no change to the T11a P4 golden's `search index: search` (3 connections).
    - Rows at or below the watermark can still be present before retention (census B3). They are applied rows and are never applied twice.
  - **Topic membership from the live registry** (no store write, no outbox row) still takes effect on the next read: P4's `test_p4_catalog_binding_change_takes_effect_on_next_read` stays green. Postings narrow the candidates; the live predicate still decides.
- **C3: the upgrade.** An index built before T12c has no postings. The indexer rebuilds it by T12b's build-and-swap, and the old file keeps answering searches, with today's cost, until the rename.
  - **An amendment to T12b's B3 (A1, Verification).** T12b B3 says the indexer never infers that a full build is wanted. A schema-version mismatch is the one case in which the indexer requests its own reindex. It requests no reindex for anything else, an empty or absent index included; that is still `upgrade-sources`' or an operator's request.
  - DOCKER.md states the cost on the live-size store (T12b measured about 162 s build→rename at 0.5 CPU, about 350 MB peak).
  - **The post-deploy expectation (A1), for T12c's deploy record:**
    - After `up`, the indexer writes one `reindex` row per store whose index predates the schema.
    - `index_lag` reads 1 for the whole build, then 0. The loop makes no passes during that one drain, and its first lines read `reindex`, not `idle`.
    - Health reads healthy throughout: no passes means no stalls.
    - `upgrade-sources` still reports `reindex_requested: false`, because the request is the indexer's own, after `up`.
    - The deploy watcher's bound is the measured build time plus a margin, not 60 s. This is stated so the watcher is not "fixed" at deploy time.

## Falsifiers
- C1's growth beyond its bounds at N = 10,000 or 100,000, in either scope, at K = 0 or K = 1,000 unapplied `desks_changed` rows.
- Any of these changing: the P5 goldens; P4 or P4b; P1's small == large; T11a P4's `search index: search` connection golden.
- **Desk and topic search statement counts.** The census measured 11 for each (A2). T12c pins its own absolute counts in a new test, and any difference from 11 is stated in the meet record.
- A reassignment (claim A→B, a link) not visible to the next read with the indexer stopped, or not visible after the drain.
- After the drain, a posting places a session in a desk it left. A `desks_changed` row is applied twice.
- During C3's build, a search that fails or returns nothing.
- **Mutants that must be RED:**
  - the drain skips `desks_changed` rows;
  - the read ignores unapplied rows;
  - the read treats every present row as unapplied;
  - the postings are written outside the watermark's transaction;
  - the indexer requests a reindex on anything other than a schema-version mismatch, for example on an empty index (A1).

## Write scope
- `episodic_search.py`: the index schema and its version, `drain`'s apply, `search_records`' candidate query, and the build.
- `episodic_memory.py` and `session_sources.py`: only what C2's read of unapplied rows needs.
- DOCKER.md: the indexer section, for C3.
- New tests under `tests/`: no existing golden is regenerated.

## Not here
DOCKER.md's "Memory during a reindex" promises an unhealthy signal that an OOM restart loop cannot give, because the stall count dies with the killed process (census C.1). The paragraph was corrected ahead of T12c. A persistent, store-held stall count is backlog.

## Out of scope (carried to the backlog)
- View searches open 4–5 store connections inside the call (census E7).
- `EpisodicSearchIndex.upsert_episodes` writes postings without the lease or the watermark. Only T10 P4 calls it (census D).
- The bounded-memory build, the set-based mark, the prober-wait split, the repair pass, `_LOST_UNWRITTEN`, and the session_id on seal rows (census B2: seal rows carry none, so there is nothing to drop).

## Draft claims corrected by the census (E)
- **E5:** the triggers write `desks_changed` rows; `_reflect` fills `detail` only, and only partially.
- **E7:** one store read holds for plain desk and topic search, not for view searches. "Unapplied" means `seq > idx.outbox_watermark`.
- **E11:** P1 pins equality, not absolute counts; C1's statement-count pin is new (above).
- **E15:** a `session_id` falsifier already exists for `desks_changed` rows (M1 is killed). The gap is `detail`.
- **E18 and E19:** the live store is WAL; stores this code creates are rollback-journal. That is carried, not in T12c.

## Arms
FEATURE and TEST, blind, each with a private TMPDIR, per the standing process. TEST writes C1's fixture and measurement, C2's cases and the mutants above. FEATURE writes C1–C3. The meet runs both, the mutation-RED is limited to the falsifiers' mutants, then Verification's verdict.
