# K1: the OPS knowledge tools follow the published navigation profile

Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

Status: frozen 2026-10-07 (the Coordinator's draft r2; Verification's read r1 → r2, ruled freezable).
- r2 took in Verification's first read, items 2–7:
  - P5 names the rebuild trigger and the read bound;
  - F1 names the tool and the observable;
  - `capabilities` moves to the generation fields;
  - the `reference_generation` carry gets its sentence and done condition;
  - P3 names the refusal shape;
  - F8 (no widening) is added.
- At the freeze, F5 gained P3's third case.

Base: main after O2.

**Where K1 lands, the Principal's word** (2026-10-07, AskUserQuestion in the Coordinator's session): "Parallel with D0b (Recommended)".
- K1's arms run alongside D0b's, both cut from main after O2. The path to the rehearsal stays O2 → D0b → rehearsal.
- K1's tests run in-process, with no Docker step. Its only Docker step is the live deploy after merge, announced through Verification and taken one at a time under `docker_step.sh`.
- The two slices' write scopes are disjoint. D0b touches the workflow, `deploy/Dockerfile`, `docs/DOCKER.md`, the audit, two install fixtures and ARM-HEADER; K1 touches `extensions/ops`, the one `agent_tooling.py` branch and `docs/PORTABLE-KNOWLEDGE.md`. Whichever merges second rebases on the first.
- (Verification's item 1 had flagged that a serial K1 would lengthen the path by one slice.)

**The Principal's decision** (2026-10-07, AskUserQuestion in the Coordinator's session): "Refresh now + code fix (Recommended)".
- The refresh was done by hand at about 00:53Z; it is the live-catalog record below.
- This order is the code fix.

## Problem (measured on the live runtime, 2026-10-07)
**How the knowledge tools pick their catalog.**
- `AgentTooling.call` (`packages/tooling/.../service/agent_tooling.py`, the `local_names` branch) follows the active navigation profile only when no installed provider supplied knowledge: `navigation_profile and not self._provider_knowledge`. In that case it uses `LocalKnowledgeProvider(profile['knowledge_config'])`.
- The OPS extension does supply knowledge. `OpsTools.knowledge_provider` returns `PortableKnowledgeProvider`, which is built once from `knowledge-runtime.json`, and that file's `catalog_path` names a STATIC operator file (`/config/knowledge.json`).
- So, under the ops configuration, every `knowledge.*` call reads the operator's file, whatever refresh has published.

**The consequence.**
- The file's `ref` and `path` pinned the 09-25 generations until the hand refresh, and they will pin today's generation until the next hand edit.
- The catalog ages with every merge, while `navigation.*` (which does follow the profile) moves on. Two surfaces of the same server then answer at different revisions.

**The hand-refresh lesson (attempt 1 regressed).**
- Changing `ref` alone pointed at commits that the static `path` (`/state/repositories/<repo>`, a clone that is not refreshed) does not contain. Result: `knowledge_unavailable`, rolled back in 20 s.
- `ref` and `path` must move together, to the same generation. The active profile already guarantees that pairing (`navigation_workspace.active`: identity by `knowledge_config_sha256`, then the catalog's `ref`/`path` equal to the profile's `revision`/`path` per repo, then the platform sources).

**Why the published catalog cannot simply replace the operator's file** (`state/navigation/generations/<gen>/knowledge.json` against `config/knowledge.json`):

| Field | Published catalog | Operator catalog |
|---|---|---|
| `tenant_ids` | refresh's own tenant | the operator's tenant (differs) |
| `artifacts`, `entry_symbols`, `default_branch_ref` per repo | absent | present (OPS, ats) |
| top-level `navigation` (provider `published_scip`/`serena`) | absent | present; `PortableKnowledgeProvider` refuses without it |
| repositories | every refreshed repo (4) | the repos the operator serves (3) |
| `platforms`, `scip_indexes` | the generation's | copied by hand from the generation |

## Properties
- **P1. Generation fields follow the active profile.**
  - Under a configured `navigation_profile`, the OPS knowledge tools take each served repo's `ref`, `path` and `capabilities`, and the `platforms` and `scip_indexes` entries for the served repos, from the active profile's `knowledge_config`.
  - **`capabilities` is a generation field** (Verification): `knowledge.py` uses it as a map from capability id to a path in the repo at its revision, so its value is a function of (repo, ref), and refresh publishes it. An operator copy ages exactly as `ref` did.
  - That catalog is admitted only through `navigation_workspace.active`, so its identity and ref/path pairing checks hold. No second reader re-implements them.
  - A refresh that publishes a new generation changes what `knowledge.*` answers on the next call, with no operator edit and no restart.
- **P2. Operator fields stay the operator's.**
  - `tenant_ids`, `artifacts`, `entry_symbols`, `default_branch_ref`, `corpus_scope`, the top-level `navigation` section, and WHICH repos are served all come from the operator's catalog, unchanged.
  - **`corpus_scope` is an operator field** (Verification): it binds the catalog row to the operator's embedded corpus and lifecycle ledger (`knowledge.py`, `diagnostics.py`, `recovery.py`), both built by the operator's publish step, not by refresh.
  - A published field never widens the served set or the tenant set.
  - `navigation_workspace.active` requires the profile's repo set to equal the tooling config's `repos`. So the served subset (3 of 4 today) is the operator catalog's business.
- **P3. Refuse, never mix and never fall back.**
  - **The refusal's shape:** the existing knowledge result status vocabulary (the `knowledge_unavailable` family: a status plus a reason word). Never a raised ValueError that surfaces as a transport error, and never a partial answer.
  - FEATURE names the reason words, one per case. `active()`'s own refusals (for example "navigation catalog identity mismatch" or "catalog repository membership mismatch") pass through as the reason and are not re-implemented, which is P1's "no second reader".
  - The tools answer nothing from either catalog when any of these holds:
    - the active profile is refused by `active()`;
    - a served repo is absent from the published catalog;
    - the published catalog has no `platforms` or `scip_indexes` entry that a served repo needs.
  - A silent answer from the operator's stale `ref`/`path` is the defect this order removes. It is a BLOCK class (silent failure), not a fallback.
- **P4. Without a profile, nothing changes.**
  - With no `navigation_profile` configured, the OPS provider reads the operator catalog exactly as today, byte for byte in behaviour. This is the configured-pins mode.
- **P5. The provider is rebuilt, not reloaded by error.**
  - `PortableKnowledgeProvider.call` currently compares only its runtime file, raising "knowledge runtime configuration changed; reload required". The `KnowledgeService` is built once, on the static catalog.
  - A profile advance is NOT a configuration change: the next call answers at the new generation.
  - **The rebuild trigger:**
    - on each call, the provider reads `active()`;
    - if the returned `knowledge_config_sha256` (or `profile_sha256`) differs from the one the current service was built from, the provider rebuilds the service from the merged catalog;
    - otherwise it reuses the service.
  - **The bound:** per call, at most the profile, the catalog and the status file are read, each at most 131072 bytes by `active()`'s own budget. No index or corpus is reopened unless the generation changed. This keeps the one-roundtrip-per-call rule.
  - A change to the operator's runtime file or catalog keeps today's refusal.

## Falsifiers (each with a mutant that must be RED)
- **F1. A new generation is followed.** Publish generation B (a new `ref` and `path` for one served repo) over generation A without touching the operator files. Then call `knowledge.symbol`, the live check that answered at 00:53Z.
  - **The observable:**
    - the result's answered revision equals B's `ref` for that repo;
    - the result carries `navigation_profile.profile_sha256` equal to B's profile. This is the field the local path already adds in `agent_tooling.py`'s `local_names` branch.
  - RED at base: it answers from A or from the operator's pin, and carries no `navigation_profile`.
- **F2. The stale-path regression.** The operator catalog's `path` names a clone without B's commit. F1's observable still holds, with no `knowledge_unavailable`.
  - This is the 2026-10-07 attempt-1 failure, as a test.
- **F3. Operator fields survive.** After following B:
  - a tenant-scoped query for the operator's tenant answers;
  - the published catalog's tenant (a different one) is refused;
  - `entry_symbols` and `artifacts` are the operator's.
- **F4. Served set.** A repo present only in the published catalog is not answerable through `knowledge.*`.
- **F5. Refusals.** Each of these gives the named refusal, and the result carries no answer from A, B or the operator pin:
  - a published catalog whose sha differs from the profile's;
  - a served repo missing from it;
  - a published catalog without a `platforms` or `scip_indexes` entry that a served repo needs (Verification, at the freeze);
  - a profile `active()` refuses.
- **F6. Configured pins.** No `navigation_profile` gives today's behaviour. This is a guard, GREEN at base.
- **F7. A profile advance between two calls** in one process gives no "reload required".
  - Its pair: two calls on the SAME generation rebuild nothing. The service object is reused, and no corpus or index is reopened.
- **F8. No widening** (Verification). A published catalog listing a fourth repo, or extra or different tenant ids, changes neither the served set nor the tenant set.
  - A query for a published-only tenant is refused, and the operator's tenant answers.
  - A published `corpus_scope` that differs from the operator's does not change which corpus is searched.
- **F9. A new capability is followed.** Generation B adds a capability document. `knowledge.capabilities` lists it with no operator edit.

## Write scope
- **FEATURE:**
  - `extensions/ops/src/kp_agent_tooling_ops/_impl/service/portable_knowledge.py`;
  - `.../ops_tools.py` (the `knowledge_provider` hook);
  - and, only if the hook needs it, the `local_names` branch in `packages/tooling/.../service/agent_tooling.py`.

  FEATURE states where the merge of P1/P2 lives, and that `navigation_workspace.active` is called, not copied.
- **TEST:** new tests under `extensions/ops/tests/`, built on a two-generation world (A, B) with a published profile, in the style of `test_published_navigation.py` and `test_portable_knowledge.py`.
- **Docs:** `docs/PORTABLE-KNOWLEDGE.md` only, where `knowledge-runtime.json` and its `catalog_path` are described. It must say that under a navigation profile the static catalog is the operator overlay (P2 fields), not the pin. It must also carry the `reference_generation` sentence from "Not fixed by K1".
  - `docs/DOCKER.md` is NOT in scope. It is D0b's file, and its `retention_references` section stays true, because K1 keeps the reference.
- **Not in scope:**
  - refresh's publisher;
  - the profile schema;
  - Core's rebuild interval (Core ask 3, pending the Principal);
  - mounting worktree roots (backlog 42);
  - the live deploy, which is announced through Verification after merge.

## Live (after merge, announced)
- Deploy the tooling image, then call the core symbol check that answered at 00:53Z. It must answer at the CURRENT generation's core `ref`, not the hand-pinned one.
- The hand-refreshed `config/knowledge.json` then stays as the operator overlay. Its `ref` and `path` no longer decide anything (P1), and that is recorded in the live record.
- `retention_references: ["/config/knowledge.json"]` becomes redundant protection, because the active profile is protected by `published_references`. It is kept, not removed, in this order.

## Not fixed by K1 (CARRIED, Verification's ruling)
- **`reference_generation`** in `knowledge-runtime.json` pins the operator's publish output (the code-reference store written by `knowledge_publish_cli.py`), not refresh's generation. It is the same defect class (answers that age with a static pin) in a different pipeline.
- **After K1, reference evidence still ages with the operator's publish pin.** A reader of K1 must not conclude that knowledge no longer ages.
- **The carry's done condition:** the reference store follows the publish step's own current output, or the publish step runs with refresh. It goes to the backlog as its own entry.

## Amendment 1 (Verification, 10-07): `capabilities` is an operator field

**Measured at the K1 meet.** Refresh's publisher writes `capabilities: {}` for every repository (`packages/tooling/src/kp_agent_tooling/refresh_cli.py`, its publish step). Every live generation carries an empty map. The operator catalog carries the maps that its `entry_symbols` require; `knowledge.py` refuses entry symbols without configured capabilities. Taking `capabilities` from the generation would therefore empty `knowledge.capabilities` and refuse every `knowledge.*` call.

The freeze's ground ("refresh publishes it") was written without reading that line. This amendment corrects it and narrows the order.

- **P1, P2.** The generation fields are `ref`, `path`, `platforms` and `scip_indexes`. `capabilities` moves to P2's operator fields.
- **What still ages, and how it fails.** The operator's map is resolved at the GENERATION's `ref`: each capability manifest is read from the repository at `repo['ref']` (`GitSource(repo['path'], repo['ref'])`, then `catalog.entry(repo['capabilities'][capability_id])`).
  - A map entry that goes stale fails loudly, never as a silent answer from an old tree.
  - Measured at the meet: `knowledge.context` answers `status: error` with gap `manifest_unavailable` at the generation's revision; `knowledge.check_references` answers `status: error` with `knowledge_unavailable`.
- **F9, inverted, in both shapes:**
  - (a) the published map is empty, as refresh writes it today;
  - (b) the published map is non-empty and differs from the operator's.

  In both shapes, `knowledge.capabilities` lists the operator's capabilities and the merge takes nothing from the published map. The mutant "capabilities from published" must be RED on both shapes.
- **P3's fourth case, `merged_catalog_invalid`** (the merged catalog fails validation, for example a published `scip_indexes` entry without a valid digest), is in P3's family: a refusal, never a mix.
- **CARRIED (contributors):** refresh publishes `capabilities: {}`. Done when refresh publishes each repository's capability map at its ref AND F9 returns to its generation form, in one slice. Not on the release path.

## Open (for the freeze)
- None. Where K1 lands was decided by the Principal (see the base).
