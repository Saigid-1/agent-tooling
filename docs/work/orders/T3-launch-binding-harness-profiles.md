# T3 — Launch binding and harness profiles

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

## Problem (observed at the T2 merge)

- **Only the sidebar binds.** Only the Kanban sidebar assistant binds a session to memory at launch (`assistant_host_cli.prepare`). Its approach:
  - mint a session id;
  - admit it;
  - write per-launch hooks and an MCP config;
  - launch Claude with `--session-id`, `--settings` and `--mcp-config`;
  - hooks capture only against the launch receipt.
- **Task agents get no desk.** They launch through `apps/kanban/src/terminal/agent-session-adapters.ts`. Kanban injects its own notification hooks (Claude `--settings`; Codex `-c` overrides from `codex-hook-config.ts`), but no desk memory or capture.
- **Harness knowledge is hard-coded** in each adapter.
- **What T2 provides:** desks with `capture` and `memory_write` flags, a configured roster, and `bind` (registry CLI, `agent-tooling.session-binding.v1`), which admits exactly one native session to one desk for the registry config's `provider_instance`.

## Interface surface

- **Harness profiles.** Schema `agent-tooling.harness-profiles.v1`: default profiles ship as package data. An operator file, named by an optional `harness_profiles_path` in the registry descriptor, overrides or adds profiles by id. Each profile has:
  - `harness`: the id, also used as the binding's `harness`.
  - `enabled` (bool) and `executable` (a name or absolute path).
  - `session_id`: `{ "strategy": "mint", "flag": "--session-id" }`, or `{ "strategy": "hook", "field": <payload key> }`, where the first hook event supplies the id.
  - `mcp`: `{ "strategy": "config_file_flag", "flag": "--mcp-config" }` or `{ "strategy": "config_override", "key": "mcp_servers" }`.
  - `hooks`: `{ "strategy": "settings_file_flag", "flag": "--settings", "events": [...] }` or `{ "strategy": "config_override", "events": [...] }`.
  - `capture`: `{ "mode": "transcript", "parser": "claude-jsonl" | "codex-rollout", "root": <transcript root> }` or `{ "mode": "none" }`.
  - `receipt_env`: the environment variable name that carries the launch receipt path.

  Defaults: `claude` and `codex`, both enabled. Adding a harness never needs a code change when its strategies and parser already exist.
- **Launch-binding command.** New console script `kp-agent-launch` (module `kp_agent_tooling.launch_cli`, core package):
  - `kp-agent-launch --config <registry operator config> prepare`:
    - Stdin JSON: `{harness, provider, model, desk_id, workspace, task_id, source: "board"|"host", parent_session_id|null}`.
    - Refuses unknown or disabled harnesses, unknown desks, and any extra field.
    - Writes a private per-launch directory: receipt, memory config, MCP config and hook settings.
    - For `mint` it mints the id and calls T2's `bind` immediately. For `hook` it binds on the first hook event.
    - Stdout JSON: `{receipt_path, native_session_id|null, argv_additions, env_additions, files}`.
  - `kp-agent-launch --receipt <path> hook`: stdin is the hook payload.
    - Binds a `hook`-strategy session on its first event.
    - Refuses a payload whose session id, `cwd` or transcript does not match the receipt or the bound session.
    - Captures visible transcript events into the desk's memory using the profile's parser, when the desk has `capture: true`.
    - Is idempotent on replay.
- **Kanban.**
  - Tasks gain an optional `desk_id`, set from task create/edit in the web UI; the desk selector lists registry desks.
  - When a task with a `desk_id` launches with an enabled profile, the adapter calls the command in `KANBAN_LAUNCH_BINDING_COMMAND` (a JSON argv; the image default is the in-image `kp-agent-launch`), then merges `argv_additions` and `env_additions` into the spawn.
  - For Claude, capture hooks and Kanban's own hooks are merged into one settings file, and the user's other MCP servers are preserved (no `--strict-mcp-config`).
  - For Codex, the `-c` overrides merge with Kanban's existing Codex hook overrides.
  - Tasks without a `desk_id`, or with a harness that has no enabled profile, launch exactly as before.
- **Sidebar.** `assistant_host_cli` keeps its isolated-memory contract (`ops.assistant-memory.local.v1`), its receipts and its existing tests. It is re-expressed on the shared launch-binding core where that needs no behaviour change.

## Properties and falsifiers

- **P1: swap by configuration.** Consider an operator profile file that adds a new harness id reusing existing strategies and parser (for example `claude-alt`, pointing at a different executable path). A desk task launched with it is prepared, bound (with `harness` = the new id) and captured with no code change. Disabling a profile makes `prepare` refuse it.
  Falsifier: a code change is needed, or a disabled profile is still prepared.
- **P2: Claude tasks bind at launch.** For a task with a desk, the spawned argv carries:
  - the minted session id;
  - an MCP config whose memory server config is exactly that session's;
  - a settings file containing both Kanban's hooks and the capture hooks.

  The binding is recorded with `source: "board"`, and MCP `initialize` plus `memory.search` over the generated config succeed.
  Falsifier: missing injection, an unbound session, Kanban hooks lost, or a memory config for another session.
- **P3: Codex tasks bind on the first hook.** The spawned argv carries the MCP and hook overrides, and the environment carries the receipt. The first hook event binds that Codex session id to the desk (`source: "board"`), and later payloads with a different session id are refused.
  Falsifier: no binding, a second session accepted, or Kanban's Codex hooks lost.
- **P4: capture follows the desk.** Claude Stop/PreCompact and Codex Stop hooks capture that session's visible events into the desk's memory. Replay adds nothing. A desk with `capture: false` captures nothing, but the binding and memory still work. A payload for another session, workspace or transcript is refused.
  Falsifier: missing or duplicate capture, capture on a `capture: false` desk, or a foreign payload accepted.
- **P5: compatibility.**
  - Tasks without a desk spawn byte-identical argv and environment to the base, apart from Kanban's own existing hooks.
  - Sidebar behaviour and its tests are unchanged.
  - Existing adapter tests pass unmodified, except the two stale K1 agent-registry expectations, which T6 owns.

  Falsifier: an argv or environment change for unassigned tasks, or an edited existing test.
- **P6: no model path to a desk.** Neither hook payloads nor MCP tools can choose or change a desk, harness or admission; the receipt fixes them.
  Falsifier: a payload or tool call that changes the bound desk.

## Write scope

- **FEATURE:**
  - `packages/tooling/src/kp_agent_tooling/launch_cli.py`, `_impl/service/launch_binding.py`, `_impl/service/harness_profiles.py`, `assets/harness-profiles.json`, the `[project.scripts]` entry for `kp-agent-launch`, and minimal edits to `assistant_host_cli.py`;
  - Kanban: `src/terminal/agent-session-adapters.ts`, `src/terminal/codex-hook-config.ts`, the task model and api-contract, tRPC task routes, and the web-ui task create/edit components with their tests;
  - `docs/` (a new `docs/LAUNCH-BINDING.md`).
  - Do not edit T2's desk registry modules beyond calling their public functions.
  - Do not edit `agent_tooling.py`, `cli.py`, `desk_cli.py` or `refresh_cli.py`.
- **TEST:**
  - Python tests under `tests/launch/`, using fake transcripts and a fake `claude`/`codex` executable (a script that records its argv and environment and invokes the configured hook commands).
  - Kanban tests under `apps/kanban/test/runtime/launch/` and web-ui `__tests__/`.
  - Real CLIs are never required.

## Acceptance at the meet

The Coordinator runs both suites plus the Kanban and web-ui tests. The Coordinator then performs one real end-to-end launch of a Claude task on a scratch registry and a scratch workspace, and checks the binding, the capture after Stop, and `memory.search`.
