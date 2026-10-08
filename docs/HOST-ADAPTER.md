# Host adapter: native Claude and Codex CLIs

`kp-agent-host` runs the Claude or Codex CLI on the host, including for computer use, with the same desk binding, desk memory and capture that the board gives its task agents ([LAUNCH-BINDING.md](LAUNCH-BINDING.md)). It needs no Claude Desktop plumbing. ADR: [AT-0004](adr/AT-0004-portable-kanban-suite.md). Order: [T4](work/orders/T4-host-adapter.md).

The parts:

- **`launch`** prepares a T3 launch with `source: "host"` where the desk state lives, rewrites the memory MCP server and the hooks for the host, then `exec`s the CLI.
- **`hook`** is the only command the harness runs. It appends the hook payload to a spool file and exits. It has no network, Docker or memory store access, and finishes in well under a second.
- **`kp-agent-launch ingest-spool`** runs where the desk state lives. It applies T3's hook semantics (bind on first event, capture, refusal) to each spooled event.

A fault in the memory backend (for example a damaged search index) therefore never blocks the harness: the event waits in the spool and is applied by a later pass.

Nothing a hook payload or a model carries can choose a desk or grant admission. Bindings are made by the launch receipt, by an operator `bind`, or by an operator project policy.

## Adapter config

Schema `agent-tooling.host-adapter.v1`. `kp-agent-host` reads it from `--config` (before or after the action) or from `KP_AGENT_HOST_CONFIG`:

```json
{"schema_version": "agent-tooling.host-adapter.v1",
 "runtime": {"mode": "docker", "container": "agent-tooling-capture",
             "config_path": "/config/launch/registry.json"},
 "spool_root": "/abs/runtime-root/spool",
 "transcript_roots": ["/abs/home/.claude/projects", "/abs/home/.codex/sessions"]}
```

| Field | Meaning |
|---|---|
| `runtime.mode` | `local`: the desk state is on this host, and the adapter calls T3 in-process. `docker`: the desk state is in a container, and the adapter uses `docker exec -i <container> …` (never `-t`). |
| `runtime.container` | Docker mode only: the container that holds the state, mounts the workspaces and transcript roots at their host paths, and runs ingestion. The installer names it `<project>-capture`. |
| `runtime.config_path` | The registry operator config (`ops.desk-memory.local.v1` with an `agent-tooling.desk-registry.v1` catalog), as the runtime sees it. This is the same file Kanban's `KANBAN_LAUNCH_BINDING_COMMAND` names. |
| `spool_root` | Host directory that hooks append to. It must belong to you and must not be writable by group or others. `launch` creates it (0700) if its parent exists. |
| `transcript_roots` | Native transcript roots (host paths). A harness profile's capture root, as the runtime resolves it, must lie under one of them. In docker mode they are the roots mounted read-only at identical paths (`kp-agent-install --transcript-root`), and the runtime reads the profiles the installer rendered with `~/` already expanded (see [Docker runtime](#docker-runtime-installer)). |

`schema_version` is optional. Any other field is refused. The file must belong to you and must not be writable by group or others.

## launch

```sh
kp-agent-host --config ADAPTER launch claude --desk desk:<uuid> [--provider P] [--model M] -- <claude arguments>
kp-agent-host --config ADAPTER launch codex  --desk desk:<uuid> -- <codex arguments>
```

Run it in the workspace. The current directory becomes the launch workspace; in docker mode it must exist in the container at the same path. `--provider` and `--model` record the binding target and default to `harness-default`. They are not passed to the CLI: give the CLI its own `--model` after `--` if you want one.

Steps:

1. **Resolve the profile.** The adapter asks the runtime for the harness profile and checks the desk. In docker mode this is `docker exec -i <container> kp-agent-host runtime`. Then it checks the transcript root and your arguments, and finds the profile's `executable` on `PATH`. Any refusal happens here, before anything is bound.
2. **Prepare.** In local mode it calls T3's `prepare` in-process; in docker mode it runs `docker exec -i <container> kp-agent-launch --config <config_path> prepare`. The request carries `source: "host"`, `task_id: "host"` and the workspace. A Claude (`mint`) launch is bound now. A Codex (`hook`) launch is bound by its first hook event, at ingestion.
3. **Write the host launch directory** `<spool_root>/<launch_id>/` (0700):

   | File | Content |
   |---|---|
   | `launch.json` | `agent-tooling.host-launch.v1`: launch id, harness, desk, the runtime receipt path, mode. |
   | `mcp.json` | Claude only: the memory MCP server. |
   | `hooks.json` | Claude only: hook settings. |
   | `events.jsonl` | The spool file the hook appends to (created empty). |

4. **Exec the CLI.** The argv is the profile executable, then the injections, then your arguments exactly as given. The environment is yours plus T3's `env_additions` (the receipt path).

The injections:

| | Claude (`claude` profile) | Codex (`codex` profile) |
|---|---|---|
| Session | `--session-id <minted>` | none; the first hook supplies it |
| Memory MCP | `--mcp-config <spool>/<id>/mcp.json` | `-c mcp_servers.kp_desk_memory={…}` |
| Hooks | `--settings <spool>/<id>/hooks.json` | `-c features.hooks=true`, `-c hooks.state=…` (trust), `-c hooks.<Event>=…` |

The memory server is T3's server over the launch's `memory.json`. In local mode that is `python -m kp_agent_tooling.memory_cli --config <memory.json> serve`. In docker mode it is `docker exec -i <container> kp-agent-memory --config <container path of memory.json> serve`. Other MCP servers you configured stay available.

Every hook runs `kp-agent-host hook --launch <id> --config <adapter>` with a 5-second timeout. That is Codex's hook budget as Kanban configures it (`CODEX_HOOK_TIMEOUT_SECONDS` in apps/kanban/src/terminal/codex-hook-config.ts). The Codex trust entries are computed with that timeout.

`launch` refuses arguments that would replace an injection, rather than editing them:

- for Claude, `--session-id` and `--settings`;
- for Codex, `-c` overrides of `hooks`, `hooks.*`, `features`, `features.hooks`, `mcp_servers` or `mcp_servers.kp_desk_memory`.

Exit codes: 3 when the harness has no enabled profile, 1 for any other refusal. Either way a JSON reason is printed on stderr, and nothing is executed.

## hook

The harness runs `hook` with the payload on stdin:

- It reads the adapter config and the launch or policy record under `<spool_root>/<id>/`.
- It appends one line to `events.jsonl` (`O_APPEND`, an exclusive lock with a 0.5 s budget, then `fsync`): `{schema_version: "agent-tooling.host-spool-event.v1", launch_id, kind, receipt, received_at, payload_bytes, payload_reduced, payload}`.
- `receipt` is the runtime receipt path for a launch, or `policy:<id>` for a project policy. It always comes from the record, never from the payload.
- A payload up to 64 KiB is kept verbatim. A larger one, up to 4 MiB, is reduced to its identity fields (`hook_event_name`, `session_id`, `transcript_path`, `cwd` and a few small scalars), and `payload_reduced` is set. A larger payload is refused.

Output and exit code:

- It prints `{}` on stdout (no instruction to the harness) and a JSON receipt on stderr.
- A refusal (unknown id, invalid payload, busy spool) exits 1 and writes nothing. It never exits 2, so a hook cannot block compaction or a stop.
- Under a revoked policy it writes nothing and exits 0 with `not_applicable`.

It writes only `<spool_root>/<id>/events.jsonl`, in a launch directory that `launch` or `install-hooks` already created.

## Spool ingestion

```sh
kp-agent-launch ingest-spool --root <spool> [--once]                           # one pass
kp-agent-launch ingest-spool --root <spool> --watch [--interval-seconds 2]      # until signalled
kp-agent-launch ingest-spool --root <spool> --watch -- <command …>              # also supervise a command
```

It runs where the desk state lives: on the host in local mode, in the capture container in docker mode. Each pass reads every `<spool>/<uuid>/` directory from its cursor.

**Launch events.** The event is checked against its T3 receipt (it must be `source: "host"` and name this launch) and handled with T3's `hook`:

- the event must be one the profile configures;
- `cwd` must be the workspace;
- the session and transcript must match the receipt;
- the first Codex event binds;
- `Stop`, `PreCompact` and `SessionEnd` capture when the desk has capture on.

**Delegation.** An event whose session id differs from the launch's bound session is a delegation candidate. It is bound only when the native transcript proves the parentage.

For **Codex**, the evidence is the child's own rollout:

- the rollout must lie under the launch's transcript root and be named for the child;
- its first row must be a `session_meta` with `payload.id` equal to the child and `payload.cwd` equal to the workspace;
- `payload.source.subagent.thread_spawn` must be present. This is how Kanban recognises a descendant thread: `isCodexDescendantSession` in apps/kanban/src/commands/hook-events/codex-hook-events.ts. Its `parent_thread_id` must be the launch's session, the field the OPS delegation adapter verifies (see [DELEGATION-ATTRIBUTION.md](memory/DELEGATION-ATTRIBUTION.md));
- top-level `parent_thread_id` and an inherited `session_id`, which `native_history_import` reads as the native parent, must be absent or name the same parent.

A verified child is bound with `parent_session_id` set to the launch's session, the same desk, harness, target, workspace and source. Its memory config is written beside the receipt, under `children/`. Its `Stop` events capture its rollout under its own session.

Anything else is refused and binds nothing:

- the rollout does not exist yet;
- it has no `thread_spawn`;
- it names another parent;
- it is for another workspace;
- it is a grandchild, whose parent is a child.

**Claude subagents** run inside the parent session. Their hook payloads carry the parent's `session_id` and `transcript_path`, and inline subagent rows are `isSidechain` rows of the parent transcript, so they are handled under the parent binding. A Claude event with any other session id is refused.

**Policy events.** These come from `install-hooks`. The event must be one of the policy's events, and its `cwd` must be the policy project. For a session not yet bound:

- the policy must be active;
- the transcript must already exist at the profile's path for that project and session (for Codex, opening with a root `session_meta` for it);
- the session is then bound with `source: "operator"` to the policy's desk, `workspace` = the project, and provider and model `harness-default`;
- a receipt for that session is written under the state root's `launches/`, with a deterministic id derived from the policy and session.

That event and later events then follow T3's `hook`. A session already admitted to another desk is refused. A revoked policy binds no new session, though sessions it already bound keep their capture for events already spooled.

**Cursors.** Each spool file has a cursor: offset, SHA-256 of the consumed prefix, and a retry count. Cursors and per-event results live in `<state_root>/spool-ingest.sqlite3` beside the registry state, so the spool can be mounted read-only. A rewritten or truncated prefix stops that file.

An event is applied before its cursor advances. T3 binding and capture are idempotent, so a restart replays at most the last event and never captures twice.

- A **refusal** (a foreign session, workspace or transcript, an unproven child, a desk conflict) is recorded, and the cursor moves on.
- A **runtime fault** (a busy or damaged store, an unfinished lifecycle flush) leaves the cursor in place and is retried on later passes. After 8 attempts the event is recorded as `failed` and skipped. A later `Stop` captures the backlog once the fault is repaired.

Each pass prints a JSON summary. The exit code is 1 when a spool could not be read.

With `--watch -- <command>`, the command runs as a child, `SIGTERM` and `SIGINT` are forwarded to it, and the watcher exits with the child's status.

## bind

```sh
kp-agent-host --config ADAPTER bind claude --native-session-id <id> --desk desk:<uuid> [--workspace DIR] [--provider P] [--model M]
```

This is an operator bind of a session not launched through the adapter: an `agent-tooling.session-binding.v1` record with `source: "operator"`, `parent_session_id: null`, and the workspace (default: the current directory).

The harness must have an enabled profile. A session already admitted to another desk or target is refused. In docker mode it runs through `docker exec -i <container> kp-agent-host runtime`.

## install-hooks and uninstall-hooks

```sh
kp-agent-host --config ADAPTER install-hooks   --project <repo> --harness claude|codex --desk desk:<uuid>
kp-agent-host --config ADAPTER uninstall-hooks --project <repo> --harness claude|codex
```

`install-hooks` records an operator policy `<spool_root>/<policy_id>/policy.json` (`agent-tooling.host-policy.v1`, `source: "operator"`) and merges one hook group per profile event into the project's native configuration:

- **Claude: `<repo>/.claude/settings.local.json`.** Existing hooks and settings are kept; the policy's group is appended to each event's list.
- **Codex: `<repo>/.codex/config.toml`**, Codex's project configuration. The adapter appends a delimited block holding:
  - a `[[hooks.<Event>]]` group per event;
  - `features.hooks = true`, added under an existing `[features]` table or as a new one;
  - trust entries `[hooks.state."<config path>:<event>:<group index>:0"]`, using the same hash scheme as the session-flag entries.

  The merged file is re-parsed, and the adapter checks that every original key is unchanged. A configuration that cannot be merged that way is refused untouched: for example, an inline `hooks` table, or `features.hooks = false`.

The original bytes and mode are recorded in the policy.

Installing again with the same desk reports `unchanged`. Installing with another desk is refused. One policy per project and harness.

`uninstall-hooks` first revokes the policy, so no new session binds after this point. Then it restores the original file:

- if the file is still exactly what `install-hooks` wrote, the original is restored byte for byte, with its mode. A file that did not exist is removed, along with a `.claude` or `.codex` directory that `install-hooks` created and that is now empty;
- if the file changed since install, it is left as it is, and the command exits 1 with `revoked_not_restored`.

Uninstalling again reports `not_installed`.

## Docker runtime (installer)

`kp-agent-install apply` renders:

- `host/kp-agent-host.json`, with `mode: "docker"`, `container: "<project>-capture"`, `config_path: "/config/launch/registry.json"`, `spool_root: "<root>/spool"` and the planned transcript roots;
- the `spool/` directory (0700);
- `$root/config/launch/harness-profiles.json` (0600), the profiles host launches use. The container's home is `/state`, so a profile root that starts with `~/` cannot be resolved there; the installer runs as you on the host and resolves it instead:
  - it starts from the packaged profiles (`kp_agent_tooling/assets/harness-profiles.json`);
  - every capture root that starts with `~/` is expanded against your home directory;
  - the home directory is a plan input, recorded as `inputs.home`, so the same inputs give the same plan. It is `kp-agent-install plan|apply --home <absolute existing directory>`, or the invoking user's `$HOME` without the flag. `verify` re-hashes the rendered file and never compares `inputs.home` with the `$HOME` of whoever runs it;
  - a profile whose expanded root is neither equal to nor under a planned `--transcript-root` is rendered with `enabled: false`, and the plan's `preview.warnings` names it with the remedy: plan with that `--transcript-root`.

  The file is installer-owned and hashed in `receipt.json`, so a hand edit shows as drift in `verify`. To use different profiles, point `harness_profiles_path` at your own file instead.

To use docker mode (the whole sequence is [DOCKER.md](DOCKER.md#first-run-from-an-empty-root), steps 1 to 4):

1. Plan with the `capture` component and a `--transcript-root` for each native root, for example your `~/.claude/projects` and `~/.codex/sessions` written as absolute paths. Add `--home <dir>` when the CLIs run under a home other than the planning user's `$HOME`.
2. Write the registry operator config to `$root/config/launch/registry.json`. Its `state_root` must be under `/state`, and must exist before `initialize` ([DESK-MENU.md](DESK-MENU.md#in-the-docker-runtime)).
3. In that registry's descriptor (`agent-tooling.desk-registry.v1`), set `"harness_profiles_path": "/config/launch/harness-profiles.json"`. With it, the packaged defaults work in docker mode. Without it, the container would expand `~/` against `/state`, and `launch` and `install-hooks` refuse because the root is outside `transcript_roots`.
4. Initialize the registry and save a desk; `launch --desk` names that desk.

**One registry for host and board launches.** The board's desk task launches use the same registry operator config: the compose `board` role sets `KANBAN_LAUNCH_BINDING_COMMAND` to `["/usr/local/bin/kp-agent-launch", "--config", "/config/launch/registry.json"]` and its Desks menu to `kp-agent-desk-registry --config /config/launch/registry.json`, both run in the board container ([LAUNCH-BINDING.md](LAUNCH-BINDING.md), [DOCKER.md](DOCKER.md#optional-roles)). Desks, bindings, receipts and captures live in the one `state_root` under `$root/state/`, so a desk made in the Desks menu is the desk `launch --desk` names, and the bindings of both sources (`host`, `board`) are in one `list`. What differs is where each CLI writes its transcripts: host CLIs under the planned transcript roots, the board's own CLIs (`agents` image, `kp-agent-install --board-agents`) under `/state`. The descriptor names one `harness_profiles_path`, `/config/launch/harness-profiles.json`; with `--board-agents` the board role sees `$root/config/launch/board-harness-profiles.json` at that path (`~/` expanded against `/state`), while the capture role, which runs host launches, sees the host-path profiles described above.

The compose `capture` role:

- has the fixed name `<project>-capture`;
- mounts `spool/` at `/spool`, read-only. Its cursors are in `/state`, its one writable bind;
- uses the entrypoint `tooling-container kp-agent-launch ingest-spool --root /spool --watch --`, which supervises the workspace capture command.

Host launches need only the registry: workspace capture's own files (`$root/config/capture/*`) are optional for them. While those are absent, the workspace capture command logs `not_configured` and waits instead of exiting, so the watcher, the spool ingestion and the container stay up, and the `docker exec -i <project>-capture …` that every docker-mode command (`launch`, `bind`, `install-hooks`) runs keeps working. A workspace capture file that is present but invalid still ends the command, and the watcher exits with it ([DOCKER.md](DOCKER.md#optional-roles), capture).

No other role mounts the spool. The image carries no host path: every host path is in the rendered runtime root.

## Known limitations

- Launch directories under `<spool_root>/` and `<state_root>/launches/` are not pruned.
- Codex project-config hook trust (`hooks.state` keyed by the project config path) follows the session-flag scheme and has not been verified against a real Codex CLI.
- A Codex child whose rollout is not yet written when its first event is ingested is refused; its next event binds it once the rollout exists.
- A grandchild thread (spawned by a child) is refused.
- Inline Claude sidechain rows are recorded by the existing capture as omissions, not as episode text.
- Provider and model default to `harness-default`, as for board launches.
- The rendered launch profiles take the home directory from `kp-agent-install --home`, or from `$HOME` at plan time. Plan for the user who runs the CLIs.
