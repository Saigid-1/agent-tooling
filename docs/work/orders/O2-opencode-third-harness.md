# O2: OpenCode as a third harness

Status: frozen 2026-10-06 (the Coordinator's draft r2; Verification ruled it freezable with A1–A3, all in).

**Base:** main after O3 and O1. O1 edits the same board adapter and owns OpenCode's approval policy, and O2's tests run on O3's pinned binary. **Header:** ARM-HEADER. **The stopping rule applies:** block or carry; a one-paragraph meet record; no commit sha, PR number or run id in a committed file.

**Decided:** the Principal, 2026-10-06: "All of it: O1, O2, O3 (Recommended)". O2 was put to him as "about one large slice".

**Ground:**
- The OpenCode read at the pin (`opencode-ai@1.18.34`, tag `v1.18.34`). Every fact is cited to a tag-pinned source line; none was executed. Not committed.
- The Coordinator's census, §1–6 (not committed).
- The Coordinator's reads at the base.

**Estimate:** T9b-sized (a third harness of the T3+T9 kind).

## Problem
- **The harness lists are closed.**
  - The strategy enums are fixed in code (`harness_profiles.py:50-114`), and `PARSERS = ('claude-jsonl','codex-rollout')` (`:22`).
  - Hook event names must match `^[A-Z][A-Za-z]{0,63}$` (`:29`), so OpenCode's dotted names would not validate.
  - Every capture site branches claude-jsonl versus codex (`launch_binding.py:196-216`, `:258-261`, `:368-393`, `:457-473`; `spool_ingest.py:268`, `:351`).
- **The board ignores the launch binding for OpenCode.**
  - `opencodeAdapter.prepare` never reads `input.launchBinding` (`apps/kanban/src/terminal/agent-session-adapters.ts:1413-1478`); compare the Codex adapter, `:1057-1076`.
  - Its `opencode.json` is ONE board-wide file per runtime home, shared by every OpenCode launch (`:1431-1432`). The launch binding's MCP and hook files, by contrast, are per launch (`launch_binding.py:239-257`).
- **What OpenCode is at the pin** (docs read):
  - **Storage.** Sessions, messages and parts live in a WAL SQLite database (`$XDG_DATA_HOME/opencode/opencode.db`). There is no JSONL transcript and nothing a byte cursor can follow (§1).
  - **Session ids.** A caller cannot choose a new session's id; it learns the id from the session (§2).
  - **Export.** `opencode export <id>` writes `{info, messages:[{info, parts}]}` through OpenCode's session service, with no coupling to the storage layout (§8).
  - **Plugins.** Plugin `event` hooks are fire-and-forget and have no timeout. Root-session turn end is `session.status` with `idle`; `session.idle` is deprecated (§6).
  - **Config.** Config sources deep-merge, and `OPENCODE_CONFIG` ranks BELOW project files (§4).
  - **Auto-update** is disabled only by `OPENCODE_DISABLE_AUTOUPDATE` or the GLOBAL config (§9).
  - **Keys.** A provider key may come from `options.apiKey` with `{file:…}` substitution (§4, §10).

## Scope
- **In:**
  - a board task launched with harness `opencode` and bound to a desk;
  - its desk-memory MCP;
  - its turn-end capture into the desk's episodes, then the queue;
  - all of it on O3's image, which carries neither claude nor codex.
- **Out (CARRIED to the backlog, with the reason):**
  - **The workspace-capture native root for OpenCode,** meaning an operator's own OpenCode sessions on the host. OpenCode's store is a WAL SQLite database, and the capture role runs in a container across the Docker file-sharing boundary, where SQLite reads tear (the rule we hold for our own stores). A sweep built on export would need the OpenCode binary on the host.
  - **`kp-agent-host install-hooks --harness opencode`** (`host_adapter.py:52`, `:591-593`).
  - **Child sessions** started by OpenCode's task tool (`parentID`).
  - **The HARNESSES.md "Built" row:** D0a-2 writes it after O1 and O2 merge.

## Properties
### R1: an `opencode` profile within closed lists
- **The profile.** The packaged `harness-profiles.json` gains an `opencode` entry.
  - The strategy enums gain exactly the members OpenCode needs; FEATURE names each and says why.
  - `PARSERS` gains one parser (FEATURE names it, e.g. `opencode-export`).
  - Unknown strategies, parsers and fields are still refused.
  - The existing `claude` and `codex` entries and their tests are unchanged.
- **Session id: the existing `hook` strategy.** OpenCode cannot take a caller-chosen id (docs read §2), so the first turn-end payload carries the session id. Binding then runs as it does for Codex today (`launch_binding.py:424-433`).
- **Events keep the existing vocabulary.** The plugin maps:
  - a root `session.status` with `idle` → `Stop`;
  - `experimental.session.compacting` → `PreCompact`;
  - the board terminal's exit → `SessionEnd`.

  `CAPTURE_EVENTS` (`launch_binding.py:49`) and the event pattern stay as they are.

### R2: the launch binding reaches the OpenCode process, per launch
- **The adapter applies the binding.** The board's `opencodeAdapter` applies the launch binding's `argv_additions` and `env_additions`, the way the Codex adapter does.
- **Per-launch delivery.** `kp_desk_memory` and the capture hook travel PER LAUNCH, never in the board-wide `opencode.json`. FEATURE names the carrier (for example `OPENCODE_CONFIG_CONTENT`, or a per-launch config directory) and its precedence, citing the docs read §4.
- **O1 is not weakened.** O1's approval policy and its A1 tests stay green UNEDITED.
- **The process environment.** The launched process carries `OPENCODE_DISABLE_AUTOUPDATE=1` (only the env var or the global config can disable it, §9) and `OPENCODE_DISABLE_SHARE=1`.
- **Plugins load from file paths, never npm names.** npm-named plugins are installed at startup, §6.
- **No install and no fetch (Verification A1).** The launched process installs no package and fetches nothing except the provider request. Two OpenCode behaviours this rules out (§9):
  - a background npm install of `@opencode-ai/plugin` into every writable config directory that has no `node_modules`, at every launch, over the network;
  - an hourly fetch of `models.opencode.ai` unless `OPENCODE_DISABLE_MODELS_FETCH` is set.

  FEATURE's carrier choice and its `OPENCODE_DISABLE_MODELS_FETCH` choice follow from this property.
- **FEATURE's other choice.** FEATURE states its choice, with a reason, on `OPENCODE_DISABLE_CLAUDE_CODE` (§9).
- **The provider key.**
  - OpenRouter's key reaches OpenCode only through `options.apiKey: "{file:<operator key file>}"`, from the same operator key file the model gateway uses (DOCKER.md, the gateway section), mounted read-only.
  - It is never written into an environment dump, a receipt, a log or the per-launch config.

### R3: capture through OpenCode's session service, never its database
- **The source.** The capture source is OpenCode's own session service: `opencode export <session>`, or the server's message route (§7–8).
- **No database access.** No agent-tooling code path opens, reads or names `opencode.db` or `OPENCODE_DB`. The export child is the only reader.
- **The export child's side effects (Verification A2).** Export boots a project instance in its working directory (§8). So the child:
  - runs with external plugins off (`--pure` / `OPENCODE_PURE`, §6);
  - carries the same disable flags as R2;
  - makes no write to `opencode.db` beyond what export itself does.

  The session's binary and the export binary are the same version inside O3's image. That equality is also why the host native root is carried: on a host it cannot be guaranteed.
- **Which events are captured.** These follow the legacy rule (census §5), applied to export's `{info, parts}`:
  - only COMPLETED assistant messages (`time.completed` set) and the user messages before them;
  - `text` parts in the message's role, except synthetic or ignored text;
  - completed or errored `tool` parts, as role `tool`;
  - `reasoning` parts are excluded and counted.
- **Event ids derive from OpenCode's part ids,** which are stable.
- **The ledger.** A capture ledger (FEATURE names it, on the pattern of `rollout-capture.sqlite3`) records what has been published. Re-capturing the same snapshot publishes nothing new.
- **Bounds.** `EpisodeStore.capture` applies: 1..500 events, 128 000 B per text (split as Claude's adapter does), 2 MB per payload. Paging follows `RolloutCapture`.
- **Each published page is enqueued** (as `launch_binding.py:684`), so the S1 role summarizes OpenCode turns as it does the others.

### R4: turn end never blocks OpenCode; a missed one is recovered
- **The hook path cannot block OpenCode.** OpenCode's hooks have no timeout and its `event` hook is fire-and-forget (§6). The plugin's path to the board hook command therefore never blocks the session: it is detached or carries its own timeout, and FEATURE says which.
- **Recovery.** A failed or missed `Stop` is recovered by the next `Stop` or by `SessionEnd`, because every capture reads a whole-session snapshot through an idempotent ledger.

## Falsifiers (each with a mutant that must be RED)
- **F1, the stranger's run (Verification's falsifier, in the arms' form).**
  - **The run.** Use O3's image: `agent_sdk_absence.py` passes and there is no `claude` or `codex`. A launch is prepared with harness `opencode`, bound to a desk. `opencode` runs one turn against a STUB OpenAI-compatible provider: a local server through `@ai-sdk/openai-compatible` `baseURL` (§10). No paid call is made.
  - **The required outcome:**
    - the turn is captured;
    - one queue job is enqueued;
    - a desk search finds the turn's text within T12b's bound.
  - **The run executes with the npm registry and `models.opencode.ai` unreachable.** Only the stub provider on loopback is reachable, and the turn still completes and is captured (Verification A1).
  - **Mutants:**
    - the adapter drops the binding;
    - the parser drops `text` parts;
    - the enqueue is removed;
    - the config carrier becomes a writable directory without `node_modules` (A1).
  - **The real OpenRouter card launched from the board UI** is the Principal's rehearsal act, with his key and a paid call. It is NOT run by the arms or the meet.
- **F2, per-launch isolation.** Two concurrent OpenCode launches bound to two different desks each see only their own `kp_desk_memory` server. Mutant: write the MCP entry into the board-wide `opencode.json`.
- **F3, idempotent capture and recovery.**
  - The same snapshot captured twice gives zero new events.
  - Dropping one `Stop` means the next `Stop` captures both turns once each.
  - Mutant: ids derived from position instead of part ids.
- **F4, no database coupling.**
  - A guard test finds no `opencode.db` or `OPENCODE_DB` in agent-tooling source.
  - An open-file audit of agent-tooling's own process during a capture shows no open of `opencode.db` or its `-wal`/`-shm`; the export child is the only reader.
  - Mutant: a capture path that reads the database.
- **F5, the lists stay closed.**
  - The `opencode` profile validates.
  - An unknown strategy or parser is refused.
  - T3's swap-by-configuration tests (`tests/launch/test_t3_p1_swap_by_configuration.py`) pass unedited.
- **F6, the process environment.** A board adapter test shows the launched process carries `OPENCODE_DISABLE_AUTOUPDATE=1`, `OPENCODE_DISABLE_SHARE=1` and the binding's env, and no provider key in its environment. Mutant: drop one.
- **F7, O1 stands.** O1's A1 tests pass unedited on the merged tree, including the loosening project and global configs.

## Write scope
- **Tooling:**
  - `packages/tooling/src/kp_agent_tooling/assets/harness-profiles.json`;
  - `_impl/service/harness_profiles.py`;
  - `_impl/service/launch_binding.py`;
  - a new capture module under `_impl/service/`;
  - `_impl/service/spool_ingest.py`, only where its parser branches need the new parser.
- **The board:** `apps/kanban/src/terminal/agent-session-adapters.ts` (the `opencodeAdapter`'s binding and the plugin's turn-end mapping), plus its tests.
- **Install (Verification A3):**
  - the compose and installer rendering that mounts the operator's OpenRouter key file read-only into the `board` role;
  - the DOCKER.md line saying the board runs O3's image digest for OpenCode (the binary exists only there), beside the key-file step.
- **Tests:** new Python tests, image-marked tests run on O3's image, and vitest.
- **Not in scope:** `host_adapter.py` install-hooks, `workspace_capture.py`, HARNESSES.md.

## Two readings (Verification; no change)
- **`SessionEnd` comes from the board terminal's exit,** so a host launch gets no `SessionEnd`. That is consistent with the in-scope list: board launches only.
- **"Export or the server's message route" is FEATURE's choice.** Either way it is held to R3's side-effect property.

## The docs read's NOT SETTLED items that matter here
- The plugin-throw behaviour (item 3), the first-line timing of `run --format json` (item 6) and all runtime behaviour (item 8) are first measured by F1 in the image.
- FEATURE records what it observes for each; a contradiction with the read is reported, not designed around silently.
