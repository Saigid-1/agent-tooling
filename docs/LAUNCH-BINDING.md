# Launch binding and harness profiles

A Kanban task that has a desk starts its agent session already bound to that desk: the session's desk memory is available over MCP from the first turn, and its visible transcript is captured into the desk's memory when the desk has capture on. Which harness is launched, and how, is configuration (a harness profile), not code. ADR: [AT-0004](adr/AT-0004-portable-kanban-suite.md). Order: [T3](work/orders/T3-launch-binding-harness-profiles.md).

The binding is an operator or launcher act. The desk, harness and target are fixed when the launch is prepared; neither a hook payload nor an MCP tool can choose or change them, or grant admission.

## Harness profiles

Schema `agent-tooling.harness-profiles.v1`:

```json
{"schema_version": "agent-tooling.harness-profiles.v1", "profiles": [ ...profile... ]}
```

| Field | Values |
|---|---|
| `harness` | The profile id; also the binding's `harness`. |
| `enabled` | `true` or `false`. `prepare` refuses a disabled or unknown harness. |
| `executable` | A command name or an absolute path. Kanban launches its catalog binary; another launcher may use this. |
| `session_id` | `{"strategy": "mint", "flag": "--session-id"}`: `prepare` mints the id and passes it with `flag`; the binding is made at once. Optional `field` names the payload key that carries the id in hooks (default `session_id`).<br>`{"strategy": "hook", "field": "session_id"}`: the first hook event supplies the id in `field`, and binds it. |
| `mcp` | `{"strategy": "config_file_flag", "flag": "--mcp-config"}`: a Claude-format MCP file passed with `flag`.<br>`{"strategy": "config_override", "key": "mcp_servers"}`: a `-c <key>.kp_desk_memory={...}` override. Other MCP servers the user configured are kept in both cases. |
| `hooks` | `{"strategy": "settings_file_flag", "flag": "--settings", "events": [...]}`: a Claude-format settings file.<br>`{"strategy": "config_override", "events": [...]}`: `-c features.hooks=true`, a `hooks.state` trust entry per hook, and one `-c hooks.<Event>=[...]` per event. |
| `capture` | `{"mode": "transcript", "parser": "claude-jsonl" \| "codex-rollout", "root": "<transcript root>"}` or `{"mode": "none"}`. `root` is absolute or starts with `~/` (the launching user's home). |
| `receipt_env` | The environment variable that carries the launch receipt path. |

The defaults ship as package data (`kp_agent_tooling/assets/harness-profiles.json`):

| Profile | Session id | MCP | Hooks (events) | Capture |
|---|---|---|---|---|
| `claude` | mint, `--session-id` | `--mcp-config` file | `--settings` file (`Stop`, `PreCompact`, `SessionEnd`) | `claude-jsonl` under `~/.claude/projects` |
| `codex` | hook, payload `session_id` | `-c mcp_servers.kp_desk_memory=...` | `-c` overrides (`UserPromptSubmit`, `Stop`) | `codex-rollout` under `~/.codex/sessions` |

Both use `KP_AGENT_LAUNCH_RECEIPT` as `receipt_env`.

An operator profile file has the same schema. Its profiles replace defaults with the same `harness` id or add new ones, so adding a harness that reuses an existing strategy and parser (for example `claude-alt` pointing at another executable) is configuration only. The file is private (mode 0600, owned by the running user). The registry descriptor names it with an optional absolute `harness_profiles_path`; it is the only optional key the descriptor accepts, and a descriptor without it behaves as before:

```json
{"schema_version": "agent-tooling.desk-registry.v1", "tenant_id": "my-team",
 "roster_path": "/config/roles.json", "harness_profiles_path": "/config/harness-profiles.json"}
```

A named file that is missing or invalid is refused; there is no fallback to the defaults. Disabling a profile (`"enabled": false`) makes `prepare` refuse that harness.

## `kp-agent-launch`

```sh
kp-agent-launch --config <registry operator config> prepare   # stdin: launch request
kp-agent-launch --receipt <launch receipt> hook               # stdin: hook payload
```

`--config` is an `ops.desk-memory.local.v1` configuration whose catalog is an `agent-tooling.desk-registry.v1` descriptor, with initialized registry state (`kp-agent-desk-registry --config ... initialize`). Its `provider_instance` is the harness instance every launched session is admitted under.

### prepare

Input (exactly these fields):

```json
{"harness": "claude", "provider": "...", "model": "...", "desk_id": "desk:<uuid>",
 "workspace": "/abs/worktree", "task_id": "...", "source": "board", "parent_session_id": null}
```

`source` is `board` or `host`. It refuses an unknown or disabled harness (exit 3, `"category": "harness_unavailable"`), an unknown desk, an extra field, a workspace that is not an existing directory, and uninitialized registry state (exit 1). Errors are printed as JSON on stdout.

It writes a private per-launch directory `<state_root>/launches/<launch-id>/` (0700):

| File | Content |
|---|---|
| `launch.json` | Receipt `agent-tooling.launch-receipt.v1`: desk, binding key, harness profile, target, workspace, task, ledgers. |
| `memory.json` | The registry configuration with `provider_session_id` set to the launched session. Written at once for `mint`; for `hook`, written by the first hook event. |
| `mcp.json` | `{"mcpServers": {"kp_desk_memory": <kp-agent-memory serve over memory.json>}}`. |
| `hooks.json` | Claude-format hook settings running `kp-agent-launch --receipt <launch.json> hook` for each profile event. |
| `session.json` | The bound session (and its pinned transcript, when known). |

For `mint` it records the binding immediately through the registry's `bind` (`agent-tooling.session-binding.v1`). Output for a `mint` launch:

```json
{"receipt_path": "...", "native_session_id": "<uuid>",
 "argv_additions": [...], "env_additions": {"KP_AGENT_LAUNCH_RECEIPT": "..."},
 "files": {"receipt": "...", "memory_config": "...", "mcp_config": "...", "hook_settings": "..."}}
```

`files` lists only files that exist, as private (0600) files, when `prepare` returns. A `hook` launch has no session id yet, so its memory configuration cannot be written: `files` omits it, and a separate `deferred_files` names it. The first hook event writes it at bind, with the same private mode:

```json
{"receipt_path": "...", "native_session_id": null,
 "argv_additions": [...], "env_additions": {"KP_AGENT_LAUNCH_RECEIPT": "..."},
 "files": {"receipt": "...", "mcp_config": "...", "hook_settings": "..."},
 "deferred_files": {"memory_config": "..."}}
```

The receipt records the same `files` and, for a `hook` launch, the same `deferred_files`. Until the first hook binds the session, the MCP server named in `mcp_config` starts but refuses memory calls (`memory.connection_status` reports `not_ready`).

`argv_additions` and `env_additions` are complete on their own: a launcher that appends them to the profile's executable gets a bound session.

### hook

The harness runs it with the hook payload on stdin. It prints `{}` on stdout (no instruction to the harness) and its result as JSON on stderr; a refusal exits 1 with the reason on stderr.

- The event must be one of the profile's `events`; `cwd` must be the launch workspace.
- `mint`: the payload's session id must be the minted one.
- `hook`: the first event binds its session id, after checking its transcript, and writes `memory.json`. Every later event must present that same session and transcript; a payload for another session (for example a Codex child thread) is refused.
- Transcripts: `claude-jsonl` must be exactly `<root>/<workspace with non-alphanumerics as '-'>/<session>.jsonl`. `codex-rollout` must lie under `<root>`, be named for the session (`rollout-…-<session>.jsonl`), and, once it exists, start with a root `session_meta` row for that session and workspace.
- Admission is re-verified through the session's memory configuration on every event; the admitted desk must be the receipt's desk.
- `Stop`, `PreCompact` and `SessionEnd` capture when the desk has `capture: true`; other events only bind or verify. A desk with `capture: false` captures nothing, while binding and memory still work.
- `claude-jsonl` capture is the existing `ClaudeEpisodeCapture`, identical to the sidebar's. `codex-rollout` capture seals the visible events that `native_history_import` reads (`event_msg` user and agent messages and `response_item` messages, as that importer defines them), pins the rollout by path, device and inode, verifies the consumed prefix by SHA-256, records a page as pending before sealing it, and skips rows over 1 MiB as omissions. Both are idempotent: a replayed event adds nothing.

## Kanban

- A task card has an optional `deskId` (`desk:<uuid>`). The task create dialog and the inline create/edit card show a **Desk** selector listing the registry's desks (the same `desks.list` the Desks menu uses). Choosing **No desk** clears it.
- Starting a task sends the card's `deskId`. For a Claude or Codex task with a desk, the adapter runs the JSON argv in `KANBAN_LAUNCH_BINDING_COMMAND` with `prepare` appended, sending harness = the agent id, provider and model `harness-default` (Kanban passes no provider or model flag, so the CLI default applies and is not verified), the task worktree, the task id and source `board`.
- Claude: the capture hooks and Kanban's own hooks are merged into the launch's one settings file, passed once with `--settings`; `--session-id` and `--mcp-config` are added; `--strict-mcp-config` is not used, so the user's other MCP servers stay available.
- Codex: the launch's hook commands are merged into Kanban's `hooks.<Event>` overrides (Kanban's group first), every group gets its own `hooks.state` trust entry, and the MCP override is added before any `resume` subcommand.
- `env_additions` (the receipt) join the agent's environment.
- A task without a desk, or whose harness has no enabled profile (exit 3), launches exactly as before. A desk task on a host without `KANBAN_LAUNCH_BINDING_COMMAND`, or with any other refusal, does not start; the reason is shown. When the command runs `kp-agent-launch` itself and its `--config` file does not exist yet, the board says that launch binding is not configured and that the registry configuration is not written, without running the command.
- A desk-bound Claude task restored from trash is refused: its launch mints a new session id, which Claude cannot combine with `--continue`. Codex resumes bind the resumed session on its first hook.
- The sidebar assistant keeps its isolated memory (`KANBAN_ASSISTANT_MEMORY_*`); it never takes a desk.

Example for a local, non-container run:

```sh
export KANBAN_LAUNCH_BINDING_COMMAND='["/abs/venv/bin/kp-agent-launch","--config","/abs/config/registry-session.json"]'
```

In the Docker runtime the compose `board` role sets it, and the command runs inside the board container on the runtime's one registry operator config, the file host launches use ([HOST-ADAPTER.md](HOST-ADAPTER.md#docker-runtime-installer)):

```sh
KANBAN_LAUNCH_BINDING_COMMAND='["/usr/local/bin/kp-agent-launch", "--config", "/config/launch/registry.json"]'
```

Board launches there need the `agents` image (it carries the `claude` and `codex` CLIs) and `kp-agent-install --board-agents`, which mounts the repositories read-write into the board for task worktrees ([DOCKER.md](DOCKER.md#board-tasks-with-in-container-agents-agents-image)). The board's CLIs run with `HOME=/state`, so their transcripts are under `/state/.claude/projects` and `/state/.codex/sessions`. With `--board-agents` the board role sees the installer's board profiles (`$root/config/launch/board-harness-profiles.json`, `~/` expanded against `/state`) at `/config/launch/harness-profiles.json`, the `harness_profiles_path` the descriptor names, so `prepare` in the board records those roots and the hooks find the transcripts. Other roles keep the host-path profiles for host launches.

## Known limitations

- No launch-directory retention yet: launch directories accumulate under `<state_root>/launches/`.
- Kanban records provider and model as `harness-default` until task cards carry a model; the binding target's provider and model are therefore not verified.
- A desk-bound Claude task cannot be restored from trash (see Kanban above).
- A desk task does not start on a host where `KANBAN_LAUNCH_BINDING_COMMAND` is unset (fail-closed). The image itself sets no default: the compose `board` role sets it, as above.
- The receipt is a private file in the state root. In this single-owner deployment the file owner is the authority; receipts are not a boundary against processes running as that owner.
- Kanban launches only its catalog binaries for `claude` and `codex`; profile `executable` and new profile ids are for other launchers (the host adapter, T4).
