# S1a: a scheduled summarizer role

Status: frozen 2026-10-06 (the Coordinator's draft r2; Verification ruled it freezable with A1, A2 and the P1 wording, all in).

**Base:** main after O3 (the A1 default-plan pin is O3's, reused here unchanged). **Header:** ARM-HEADER. **The stopping rule applies:** block or carry; a one-paragraph meet record; no commit sha, PR number or run id in a committed file.
**Decided:** the Principal, 2026-10-06 (AskUserQuestion, verbatim labels):
- "Scheduled summarizer role";
- "Opt-in at install (Recommended)";
- "Standing approval + gateway budget (Recommended)".
**Ground:** the Coordinator's census of O2, O3 and S1 (not committed), §9–14, plus the Coordinator's reads at the O3 base. Every line below cites the base.
**Estimate:** S1a is about one T11a-sized slice. S1b, the workspace-capture feed, follows under its own order once census item 6 is grounded; together they are T11-sized. With O1 (small) and O2 (T9b-sized), the release path before D0b grows by about four slices.

## Problem
- **The pipeline is ported but nothing runs it.** `kp-agent-memory-queue work-once` (`queue_cli.py:63-96`) is the only caller of `ConsolidationQueue.run_once` (`episodic_queue.py:201-247`). No role, schedule or DOCKER.md step runs it.
- **Every run needs per-episode approval.** Each run requires `--approved-digests`, an operator list of 1..512 visible-event SHA-256s (`queue_cli.py:64-72`), checked at `episodic_queue.py:233-236`.
- **The summarizer bypasses the model gateway.** Its transport is a hard-coded `HTTPSConnection('openrouter.ai')` (`episodic_summarizer.py:188`), with no USD or calls-per-hour ledger. `BudgetLedger` exists only in `model_gateway.py:285-380`.
- **The responding model is never compared with the requested one.** `model_reported` is only recorded (`episodic_summarizer.py:379-381`; the gateway's copy at `model_gateway.py:679`).
- **The model metadata receipt expires after 24 h** (`memory_budget.py:50-56`; re-checked at `episodic_summarizer.py:321-323`). Its builder is a repo script (`scripts/memory_model_profile.py`), not a packaged entry point.
- **The gateway's USD cap counts an unpriced call as zero spend.** A call without a reported cost and without `estimated_usd_per_call` adds nothing (`model_gateway.py:318-331`), so a dollar-per-day cap without an estimate may never trip.
- **Workspace capture enqueues nothing** (`workspace_capture.py:565-566`; census §11). That is S1b.

## S1a: properties

### P1: a standing approval per desk (the Principal's first decision)
- **The approval record.** The authority is ONE operator file (FEATURE names it, e.g. `summarizer-approval.json`; schema `agent-tooling.summarizer-approval.v1`). It names:
  - the approved desks;
  - the approved model id and the profile reserves (the five `memory_model_profile.py` reserves);
  - the gateway capability the role calls (e.g. `memory.summarize`).
- **The admission is the operator's act, never the role's.** For each approved desk the role holds its own admission row, created with the existing `admit` (`desk_memory_runtime.py:257-271`):
  - under the role's own `provider_instance` (FEATURE names it, e.g. `summarizer`), and a per-desk session id;
  - `provider_id` is the route's provider and `model_id` is the approved model.

  The role never calls `admit` itself. DOCKER.md gives the operator's two steps: write the approval, then admit each desk. Admission rows are immutable, so **changing the approved model requires a new admission per desk** (a new per-desk session id). Until then, that desk reads `not_configured`.
- **What the approval permits.** In the role, `approve_sources` is true only when ALL of these hold:
  - the job's binding is an approved desk;
  - the role holds an admission for that desk;
  - that admission's `model_id` equals the approved model.

  No per-digest list is needed for an approved desk.
- **Revocation.** A desk removed from the approval is not claimed on the next tick:
  - none of its episodes are read;
  - no request is sent for it;
  - its queued jobs stay `queued`.

  The admission rows are immutable (`desk_binding.py:117-150`), so revocation lives in the approval file.
- **The manual path is unchanged.** `work-once --approved-digests` keeps its current requirements, and its existing tests stay green unedited.
- **The MEMORY.md property change, stated (this is the Principal's decision, written into the docs).**
  - **Today** (`docs/MEMORY.md:178-180`, `:292-295`):
    - "The queue's `work-once` retains the existing explicit model profile, source-digest approval and key-environment requirements";
    - "Outbound summarization is optional and not triggered by install, binding, capture, or a read."
  - **After S1a** (FEATURE writes it in substance, in BOTH `docs/MEMORY.md` and the packaged `assets/MEMORY.md`, which differ today, census §13):
    - With the optional `summarizer` role selected and a desk in its standing approval, every sealed episode of that desk that reaches the queue is sent for summarization. It goes under the approved model and the gateway budget, without a per-episode digest approval.
    - Without the role, or for a desk not in the approval, nothing changes: install, binding, capture and reads never trigger an outbound call.
  - The ZDR, no-data-collection, no-substitution and review-not-retry sentences stay, now enforced on the role's path too (P2).

### P2: through the gateway under a dollar-per-day budget (the Principal's second decision)
- **The gateway's pre-network order.** Every completion request the role makes passes it (`model_gateway.py:7-11`, `:573-650`):
  1. the route;
  2. the budget configured;
  3. the key file checked (`read_api_key`; a permissive file is refused);
  4. `BudgetLedger.reserve` in one IMMEDIATE transaction;
  5. exactly one request;
  6. `finish` with the cost.

  The provider and base URL come only from the gateway route. The role never uses the hard-coded `openrouter.ai` connection.
- **Pinning.** The route's model, the approved model and the refreshed profile's model must be equal. Otherwise the role reads `not_configured` before any request.
- **Model comparison (new enforcement).** A response whose reported model is not the requested model gives:
  - the attempt `failed_or_uncertain`;
  - the job `needs_review`;
  - no capsule.

  The comparison is exact equality, unless FEATURE names one normalization with a fixture that shows why. Its first real exercise is the live role's first day: no paid call is made in this slice. A mismatch fails closed (review), never silently.
- **The budget must be priced.** The capability's budget needs `max_usd_per_day`, `max_calls_per_hour` AND `estimated_usd_per_call`. Without the estimate the role reads `not_configured`, so the dollar cap holds even when the provider reports no cost.
- **A refusal before the network is not an uncertain attempt.** When the gateway refuses before sending (budget exceeded, confirmation required, key file refused):
  - the job does not go to `needs_review`;
  - the attempt is recorded as not sent;
  - the job is claimable again on a later tick.

  Only a request that may have reached the provider goes to review (`episodic_queue.py:225-229`, `:240-242` today; the gateway's own `requests_sent`/`billing` fields at `:651-660`).
- **Privacy routing stays in code.** Every role request carries `zdr: true`, `data_collection: 'deny'` and `require_parameters: true` (today `episodic_summarizer.py:331-333`).
  - A route `params` entry that would weaken them is refused before the network. `provider` is not in `RESERVED_PARAMS` (`model_gateway_providers.py:47`).
  - The role has no `--no-require-zdr`.
  - OpenRouter's fallback among ZDR-eligible upstreams keeps its current default (`docs/MEMORY.md:205-210`). "Only the route's provider" means the gateway provider (base URL).
- **The gateway writes nothing content-bearing for the role.** It writes its ledger row and content-free provenance only: no output artifact and no desk memory record (`write_outputs` `:442-465`, `record_in_memory` `:467-505`). The summary's only home is the capsule.

### P3: the role, optional, scheduled, `not_configured` until configured
- **A new component `summarizer`:**
  - in `COMPONENTS` and `SERVICES` (`runtime_install.py:63-64`);
  - a `profiles: [summarizer]` service in `compose.yaml`;
  - its operator files in `OPERATOR_FILES` (`:114-119`).

  O3's A1 pin stays green UNEDITED: the default and live-like plans never render it.
- **The schedule.** Every interval (FEATURE names the default and the env var), for each approved desk, the role works jobs one at a time until one of these happens:
  - the queue is idle;
  - the gateway refuses;
  - a per-tick cap is reached.

  There is one replica, and the existing lease fences double claims.
- **`not_configured`.** It is read when any of these is missing or invalid:
  - the approval;
  - the gateway route;
  - the budget or its estimate;
  - the key file;
  - a desk's admission.

  The role then reports `not_configured`, naming the missing piece and never the key, in a status FEATURE names, and opens NO connection.
- **Freshness (Verification A2).** The model metadata receipt expires after 24 h, so the role refreshes it from the route provider's model listing. That read is an outbound call, so it is held to P2's boundary:
  - it goes to the gateway route's base URL with that provider's key file only, never to a hard-coded host;
  - it happens only after every `not_configured` check has passed;
  - it happens at most once per receipt lifetime (24 h);
  - it is a metadata read, never a completion.

  The refreshed entry must still name the approved model and support the selected parameters (`queue_cli.py:79-87`). Otherwise the role reports `not_configured: profile_changed` and never uses another model. Apart from completions, this listing read is the role's only outbound request.
- **Egress.** `summarizer` has no `network_mode: none`. The `compose.yaml` diff adds the summarizer service and changes no other service; `capture` and `indexer` keep `network_mode: none`.
  - Census correction to the scope line: today `tooling`, `refresh`, `board`, and telemetry's `tempo` and `collector` all have default egress (census §14). So `summarizer` joins them; it is not "the second".
- **Mounts.**
  - the state root, read-write, for the writes in P4;
  - the operator files and the key file, read-only;
  - nothing else.

### P4: every store write the role makes (Verification's freeze point 1)
| store | writer, when | transaction | statements | index? |
|---|---|---|---|---|
| `queue.sqlite3` | role, claim | 1 IMMEDIATE | expiry UPDATE + lease UPDATE (`episodic_queue.py:134-148`) | no |
| `queue.sqlite3` | role, per packet (≤ `max_proposal_calls`) | 1 IMMEDIATE (intent) + 1 IMMEDIATE (receipt) | INSERT attempts + UPDATE jobs; UPDATE attempts, receipt ≤ 4096 B (`:159-189`) | no |
| `queue.sqlite3` | role, finish | 1 IMMEDIATE | UPDATE jobs + UPDATE attempts (`:191-199`) | no |
| `episodes.sqlite3` | role, per succeeded job | 1 transaction, ONE row | `INSERT OR IGNORE INTO capsules` (`episodic_memory.py:505-506`); payload ≤ handoff 12 000 B + evidence directory (≤ 32 episodes × ≤ 500 events) | **no**: `capsules` has no outbox trigger (`OUTBOX_WATCHED`, `episodic_memory.py:84-100`). Summaries reach readers through `memory.status/list/handoff/resume`, never desk search |
| gateway ledger (`<artifact_root>/.model-gateway/`) | role, per request | 1 IMMEDIATE (reserve) + 1 (finish) | INSERT calls; UPDATE calls (`model_gateway.py:350-381`) | no |
| `sessions.sqlite3` | **operator**, once per approved desk | existing `admit` | INSERT `desk_session_launches` + `desk_contexts` (`desk_memory_runtime.py:257-271`) | no |
| role state | role | file writes | the refreshed profile receipt and its status file (FEATURE names the paths) | no |

- **Reads.** The admission path's `PRAGMA quick_check` on `sessions.sqlite3` (census §10: 10 per successful run) is a known cost. It is CARRIED, not changed here.
- **The role is a third writer on `episodes.sqlite3`,** beside capture and the indexer's retention. Its property is T12b's:
  - one bounded transaction of one row;
  - the store's busy timeout, the same as capture's;
  - the first-day telemetry instrument, extended to the summarizer container, counts its lock lines beside capture's.
- **The capsule and the job finish stay two transactions in two files** (census §10). A crash between them leaves a `running` job that becomes `needs_review` and is never resent. This stands as it is.

## S1a falsifiers (each with a mutant that must be RED)
- **(a) No network when not configured.** With any ONE of these absent, the role reads `not_configured` and opens zero connections, INCLUDING the model listing read:
  - the approval;
  - the route;
  - the budget;
  - the budget's estimate;
  - the key file;
  - the desk's admission.

  Test it under `network_mode: none`, or with a transport stub that records connection attempts, as D0f did. Mutants: drop one check; fetch the listing from a host other than the route's base URL; fetch it before the checks pass.
- **(b) Only the route's provider, at the pinned model, under the budget.**
  - A configured tick sends one request per packet, to the route's base URL only, with `model` equal to the approved model and the privacy routing present.
  - Mutants:
    - a fallback list or a route `params` weakening ZDR;
    - removing the model comparison (a fixture response with another model must give `needs_review`);
    - treating a budget refusal as uncertain (the job must stay claimable, with zero requests).
  - Gateway budget refusal before the network is covered by the EXISTING tests, named and not rewritten:
    - `tests/models/test_m1_p3_budgets.py` `::test_p3_calls_per_hour_cap_refuses_before_the_network` (:52), `::test_p3_usd_per_day_cap_refuses_before_the_network` (:75), `::test_p3_confirm_over_usd_requires_confirmation_before_any_call` (:95), `::test_p3_calls_per_hour_counter_survives_restart` (:62), `::test_p3_usd_per_day_counter_survives_restart` (:84);
    - `tests/models/test_m1_p2_secrets.py::test_p2_permissive_key_file_is_refused` (:92);
    - `tests/models/test_m1_p1_routing.py` `::test_p1_model_change_in_config_changes_the_outbound_model` (:27) and `::test_p1_provider_change_in_config_changes_the_outbound_target` (:51).

    Plus ONE new test on the role's path: the USD cap reached by the estimate gives zero requests. Also a first test of `budget_unconfigured` (none exists, census §12).
- **(c) Never selected unless asked.**
  - O3's A1 pin passes unedited.
  - A plan with `--components tooling,summarizer` renders the service and adds `summarizer` to `COMPOSE_PROFILES`.
  - A rendered-compose test shows the summarizer is the only added service.
  - `capture` AND `indexer` keep `network_mode: none`. The indexer half is new; census §14 found only the capture test.
- **Standing approval.**
  - A desk not in the approval: its jobs are never claimed, there are zero reads of its episodes and zero requests.
  - An admission whose `model_id` is not the approved model gives `not_configured` for that desk.
  - Removing a desk stops it on the next tick.
- **Writers.** A statement trace over one successful tick (the T12c instrument, `tests/t10_instruments.py`) equals P4's table, and the capsule transaction holds exactly one row.

## S1a write scope
- **Source:**
  - a new role module under `_impl/service/` (FEATURE names it), plus its console entry in `packages/tooling/pyproject.toml`;
  - `episodic_summarizer.py`, the transport through the gateway;
  - `model_gateway.py`, only for the content-free mode, if needed;
  - `episodic_queue.py`, for the not-sent refusal state;
  - `queue_cli.py`, only if shared code moves; the manual path stays unchanged.
- **Install:**
  - `runtime_install.py` (COMPONENTS, SERVICES, OPERATOR_FILES);
  - `install_cli.py`, if needed;
  - `deploy/compose.yaml`, the one service.
- **Docs:**
  - `docs/MEMORY.md` and `packages/tooling/src/kp_agent_tooling/assets/MEMORY.md`, the stated change;
  - DOCKER.md, the summarizer section: the approval, the admission, the gateway route and priced budget, the key file, the interval.
- **Tests:** new test files.

## S1b (not in this order)
The workspace-capture feed is frozen separately, after census item 6 is grounded on a fixture built by real workspace capture. Its order carries a cost property: no backfill of episodes sealed before a desk's approval unless the operator asks.

## Live
- **Selecting the role in live is the Principal's act:** the approval, the admissions, the key file, and the priced budget, announced through Verification.
- **Its first day:** the telemetry counts the summarizer's lock lines, its `needs_review` jobs (the model comparison's first real exercise) and its spend against the cap.
