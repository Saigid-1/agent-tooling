# T10 — One roundtrip per call: memory reads use indexed projections

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.
Principal ruling, 2026-10-02, verbatim: "no piece of data or module should have more than one roundtrip per call, and roundtrips against slow moving data should be avoided (historical transcript data should be indexed and embeddings/metadata should be projected so it's not being continuously scanned)." Fixing this "would allow result sets to be expanded while still increasing performance."

## Problem

Measured at main at the order's base. Sources: the Verification desk, an independent review and the Coordinator.

- **The whole-store walk.** `SessionSources.select` (`_impl/service/session_sources.py:308`) walks every `session_episodes` row, the metadata and claims of every session, every `episodes` row, and every tenant `source_episodes` row with a `json_extract` of each payload.
- **That walk repeats within one call:**
  - `memory.search`: once in `search`, then once per result through `read_scoped`, up to 20 (`episodic_memory_tools.py:325`);
  - `memory.read_event`: twice (`:382`, then `episodic_memory.py:223`);
  - `memory.episode_directory` (`:368`) and `memory.list` (`:252`): once each;
  - a handoff: once per cited episode, up to 32 (`episodic_provenance.py:37`);
  - citation records: once or twice (`episodic_memory.py:246/252`).
- **Admission and registry repeat too.** `sessions.resolve` (`desk_binding.py:121-136`) runs `assert_ready`, which opens the ledger and runs `PRAGMA quick_check` (`:71-76`), and then a registry read (descriptor JSON plus `registry_desks`; `desk_memory_runtime.py:138-158`, `desk_registry.py:213`). It runs on every `read_scoped` and `_binding`, so a 20-result search runs about 60 integrity checks and 60 registry reads.
- **Payload reads.** Search rereads and hashes every in-scope payload for coverage (`episodic_search.py:177-183`), loads all of `indexed_episodes` (`:187`), materialises every in-scope ID twice, reads up to 200 candidate payloads (`:214-218`) and then each result again. `read_event` reads its payload twice.

Every memory call therefore costs time linear in the tenant's history, several times over. The T9 catch-up roughly triples that history.

## Instruments

- **In-process only.** TEST may wrap `sqlite3.connect`, for example with monkeypatch, so every connection gets a trace callback (the statement list) and a progress handler (VM steps). `EXPLAIN QUERY PLAN` runs on the traced statements. Instruments run in-process: the stdio server route is for functional checks only, because a monkeypatch cannot see a subprocess.
- **Measure from a fresh object.** Counts start from a freshly constructed tools object and include its construction. No store content may be cached across calls.
- **No reads outside SQLite.** A `sys.addaudithook` `open` audit asserts that a read path opens no file under the state root other than:
  - the SQLite databases;
  - the registry descriptor, imported catalog and roster JSON files, each at most once per call.
- **Positive controls.** Every count comparison asserts a non-empty trace containing the expected read, and that the large case really is large: `limit=20` returns 20 results, and the handoff cites 32 episodes.
- **"Scan" defined.** Any plan line `SCAN <table>`, including `USING COVERING INDEX`, is a scan. Selectivity is judged by P3's ratios, not by the plan text.
- **Golden outputs.** They are committed files, together with the command that generated them from base at this order's merge SHA, with a frozen clock so claim timestamps and IDs are stable. Separately, `SessionSources.select` stays in tests as a reference oracle: on every seeded state, the new scope must equal `select`'s.

## Properties and falsifiers

- **P1: a call's statement count does not depend on its result count.**
  - **Coverage:** every tool in `TOOLS` (parametrized), with arguments sized small and large: `limit=1` and `limit=20` (or the tool's maximum), and a handoff citing 1 and 32 episodes.
  - **The property:** the statement count is the same for small and large.
  - **Once per call:** admission and the ledger integrity check, registry resolution, scope resolution, and the batched reopen of returned records each happen at most once.

  Falsifier: more statements for the large case than the small; `PRAGMA quick_check`, the descriptor read or a registry or ledger select more than once in one call.
- **P2: no scan of slow-moving tables, and payloads only as needed.**
  - **No scans:** no traced statement on a read path scans `episodes`, `source_episodes`, `session_episodes`, `session_claims`, `source_sessions`, `source_projects`, `desk_session_contexts` or `indexed_episodes`. FTS `MATCH` is allowed.
  - **Columns:** statements select named columns.
  - **Payloads:** `payload` is selected only by ID, in one batched read per call, and only for the records the call returns or cites. Cited records include a handoff's attribution and a proposal's quote checks.
  - **Candidates:** they are verified (the phrase regex) against the indexed event text. Replacements for rejected candidates come from the candidate list without reading payloads.
  - **Returned records:** reopened from the sealed row in one batched read, where quote and digest are verified. A record whose sealed row disagrees with the index is dropped. Only a corrupt index can cause that.

  Falsifier: a scan as defined above; `payload` selected for an ID not returned, or twice.
- **P3: cost does not grow with history.** The base store has at least 1,000 episodes across several desks. "Unrelated" means episodes outside the call's resolved scope. The statement count stays identical and VM steps grow at most 1.25× when either of these grows tenfold:
  - (a) unrelated episodes and sessions, with no matching text;
  - (b) the caller's own desk's episodes and sessions, with no matching text.

  Scope:
  - (a) and (b) hold for every tool in `TOOLS` called unfiltered, in desk and topic scope (for topic scope, (a) reads "another tenant").
  - Calls with attribution, claim, session or profile-view filters satisfy (a), P1 and P2. Their exact counts (`total_episodes`, `covered_episodes`, the `memory.list` total, `excluded_unresolved_episodes`, `historical_claim_intervals_unverified`) may cost time proportional to the filtered scope; the counts contract is unchanged.
  - For unfiltered scopes, those counts are maintained aggregates.

  - **Coverage aggregates span both files.** A reassignment moves coverage between desks without opening the index. FEATURE names how, for example the index writer recording per-episode coverage in the source store.
  - **Moved to T12:** cost independence from matching text in other desks (P3(c)) needs per-desk postings in the text index and an index rewrite on reassignment, so it moves to T12, whose indexer owns every index write. Until T12, a desk-scope search for a common term costs time proportional to the FTS matches in other desks; scope filtering happens after `MATCH`, against the source projection.

  Falsifier: steps above 1.25× under (a) or (b); a changed statement count.
- **P4: projections are written when facts change, and stay honest.**
  - **What is projected:** storage table and storage binding; an order sequence assigned at seal (backfill assigns it in rowid order); source session; payload digest; source coordinates, with a type guard (only an `int` `start` applies a ranged claim, as in `_at_episode`); resolved desks; attribution status; facts sufficient to evaluate narrowed claims by indexed lookup.
  - **Where and when:** in the source store, in the same transaction as seal, claim or link. A claim, supersession, retraction or owner change re-projects that session's episodes with set-based statements bounded by the session.
  - **The index:** T10 does not require an index-side copy of scope. If FEATURE adds one, correctness must not depend on it (P4b), and staleness after a reassignment is accepted until T12 drains it. Seal and claim never open or block on the index.
  - **Registry facts are not projected.** Tenant membership of a storage binding comes from the once-per-call registry read.
  - **Publicly observable, with no operator step:**
    - a read immediately after a public seal or claim reflects it, including an owner reassignment from desk A to B (B finds the reassigned text, A does not) before any index refresh;
    - a binding added to or removed from the catalog changes the next read;
    - seal and claim succeed while the index file is absent.

  Falsifier: any of those observations fails.
- **P4b: integrity, the projection grants nothing.** For returned records, scope is re-derived from that session's sealed, content-addressed claims (IDs verified) in one batched read, and a record whose re-derived scope excludes the caller is dropped. A tampered claim of a returned record's session refuses the read with the existing category.
  Falsifier: edit a projection row (or the index scope copy) to move an episode into desk B; B's search returns it. Or: tamper a claim; the read does not refuse.
  - **Behaviour change, stated:** a session whose sealed metadata is unavailable, or one of whose claims fails its ID check, now refuses only reads whose result or scope includes it. Today either case refuses every read in the tenant.
- **P5: results are identical.** Every memory tool returns outputs identical to the golden outputs and to the oracle, except the P4b behaviour change and the new P6 category. The corpus includes:
  - several desks, with resolved, unresolved and conflicting attribution, where conflicting episodes are visible to both desks in desk scope;
  - legacy `episodes` rows, including one whose binding is absent from the registry, and one linked to a session whose owner claims are all inactive (it becomes unresolved and leaves its storage desk's scope);
  - source episodes with and without `source_coordinates`, with non-dict coordinates, and with string, float and bool `start` values; multi-file sessions; `start == start_offset`;
  - claims with source ranges, supersession chains of two or more, retraction of a superseding claim, owner reassignment, and `valid_from`/`valid_until`;
  - a linked session belonging to another tenant;
  - more than `candidate_limit` matches in another desk;
  - FTS-vs-regex disagreements: `foo_bar` for "foo bar", and `café bar` for "cafe bar", with valid matches ranked below them;
  - desk and topic scope, attribution, claim and session filters, and profile views;
  - `memory.list` order; error categories.

  Reads are interleaved after every write step.
  Falsifier: any difference from the goldens or the oracle; a modified existing test.
- **P6: existing stores upgrade explicitly, in bounded steps.**
  - **The step:** one documented, idempotent operator step (extend `kp-agent-desk … upgrade-sources` in `kp_agent_tooling/desk_cli.py`). It works in bounded batches, each its own transaction. It verifies against the sealed source, reports counts and ends with a completion marker.
  - **Marks:** per table (episodes and source episodes, claims, links), so a row written by an older writer without a projection is detected in O(1).
  - **Fresh stores:** a fresh or new-version store never reports `projection_incomplete`.
  - **Incomplete stores:** reads refuse with `projection_incomplete` and operator guidance, never falling back to a scan. `memory.connection_status` reports the same state.
  - **Safety:** the backfill is safe beside live new-version writers, which project their own rows in-transaction.
  - **Idempotence:** a second backfill changes nothing, comparing `iterdump()`.

  Falsifier: a silent fallback to scanning; a backfill differing from a fresh projection; a second backfill that changes the dump; a single unbounded transaction; an older-writer row not detected.

## Write scope

- **FEATURE:**
  - under `packages/tooling/src/kp_agent_tooling/_impl/service/`:
    - `session_sources.py`;
    - `episodic_search.py`;
    - `episodic_memory.py`;
    - `episodic_memory_tools.py`;
    - `episodic_provenance.py`;
    - `episodic_handoff.py` (paging reads);
    - `desk_profiles.py` (view scope through the projection);
    - `desk_binding.py` and `desk_memory_runtime.py` (resolve once per call; per-call caching only);
  - `packages/tooling/src/kp_agent_tooling/desk_cli.py` (`upgrade-sources`);
  - `docs/MEMORY.md`.

  Name the cross-file join you chose and its measured cost.
- **TEST:** new files under `tests/`.
  - Drive the public tools (`EpisodicMemoryTools`, or the `kp-agent-memory … serve` stdio server), public writes (capture, claims and links through the supported entry points) and the `upgrade-sources` CLI, with synthetic stores.
  - Use the instruments above. No Docker, no network.

Raising the search `limit` or `candidate_limit` is safe only once T12's P3(c) holds. That is a follow-up decision, not part of this order.
