# T4 — Host adapter: native Claude/Codex CLIs, spool capture, delegation

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

## Problem

Users who run Claude or Codex natively on the host, including for computer use, need the same desk binding, memory and capture that the board gives its task agents (T3), without Claude Desktop-specific plumbing.

- The legacy desktop launcher, capture bridge and session hook now live in `extensions/ops` (T1). They are keyed to `CLAUDE_CODE_HOST_SESSION_ID` and to hook copies pinned under the runtime directory.
- T3 provides `kp-agent-launch prepare/hook` and harness profiles. Receipts are produced where the runtime state lives: in the container for a Docker runtime, or on the host for a local install.
- Codex hooks have a 5-second timeout, so a hook cannot wait on `docker exec`.
- The legacy Claude hook captures synchronously through `docker exec` and fails closed on PreCompact. On 2026-10-01 a corrupt search index in the runtime made every capture fail, and that hook refused a live session's compaction until the Coordinator repaired the index. A spool hook keeps a backend fault from blocking the harness.

## Interface surface

- **Console script `kp-agent-host`** (core package). It reads an adapter config, `agent-tooling.host-adapter.v1`: `{runtime: {mode: "local"|"docker", container?, config_path}, spool_root, transcript_roots[]}`. The installer renders it into `host/`.
- **`kp-agent-host launch <harness> --desk <desk_id> [--provider P] [--model M] [-- <cli args>]`.**
  - Calls T3's `prepare` with `source: "host"`: in-process in `local` mode, or through `docker exec -i <container> kp-agent-launch …` in `docker` mode.
  - Rewrites the MCP config for the host. In `docker` mode the memory server command is `docker exec -i <container> kp-agent-memory --config <container path> serve`, never `-t`.
  - Rewrites hooks to call `kp-agent-host hook --launch <id>`.
  - Then `exec`s the real CLI with the user's arguments unchanged, plus the profile's injections.
- **`kp-agent-host hook --launch <id>`.** Reads the hook payload on stdin and appends it, with the launch id and a receipt reference, to `<spool_root>/<launch_id>/events.jsonl`, using append plus fsync.
  - No network, no `docker`, no memory store access.
  - Bounded payload.
  - Must finish in under 1 s.
- **Spool ingestion.** A watcher inside the runtime (`kp-agent-launch ingest-spool --root <container spool path>`) runs in the `capture` role (`network_mode: none`) or locally. For each event it applies T3's hook semantics against the receipt, which covers bind-on-first-event, capture and refusal.
  - It keeps a durable cursor per spool file and is idempotent across restarts.
  - Transcript roots are mounted read-only at identical paths; the S3 installer's `--transcript-root` already supports this.
- **`kp-agent-host bind <harness> --native-session-id <id> --desk <desk_id>`.** An explicit operator bind (`source: "operator"`) of a session that was not launched through the adapter.
- **`kp-agent-host install-hooks --project <repo> --harness claude|codex --desk <desk_id>` and `uninstall-hooks`.**
  - Merges adapter hooks into the project's native hook configuration. For Claude that is `.claude/settings.local.json` in the project; for Codex it is the documented project or user configuration the harness profile names.
  - Installing records an operator policy: sessions started in that project bind on their first hook to that desk only (`source: "operator"`).
  - Both actions are idempotent and must not clobber existing user hooks. `uninstall-hooks` restores the original file byte for byte.
- **Delegation.** A hook event carrying a new native session id under an already-bound launch binds that session as a child, with `parent_session_id` set to the launch's session and the same desk. This happens only if the native transcript proves the parentage; the arm cites the evidence it uses, for example Codex rollout metadata. Otherwise the event is refused. Claude subagents appear as sidechains in the same transcript and are captured under the parent.
- **Installer.** `kp-agent-install` renders `host/kp-agent-host.json` and a spool directory. The compose `capture` role mounts the spool read-write and runs the ingestion watcher. The other roles don't mount it.

## Properties and falsifiers

- **P1: host launch.** A `claude` launch through the adapter:
  - executes the real executable with the user's arguments and environment unchanged, plus T3's injections;
  - records a binding with `source: "host"`;
  - makes the memory MCP reachable in both runtime modes;
  - captures a Stop event through spool ingestion, after which `memory.search` finds that turn.

  Codex behaves the same, with binding on the first hook.
  Falsifier: user arguments altered or dropped, an unreachable MCP in either mode, or no capture.
- **P2: fast, isolated hooks.** `kp-agent-host hook` finishes in under 1 s with Docker unavailable. It writes only inside its spool, and cannot choose a desk. Ingestion refuses a foreign payload (another session, workspace or transcript) and never double-captures after a restart.
  Falsifier: a hook that blocks or needs Docker, a write outside the spool, a payload that picks a desk, or a duplicate capture.
- **P3: operator acts only.** Sessions not launched through the adapter bind only by `bind` or by an installed project policy, and only to that policy's desk. `uninstall-hooks` restores the file byte for byte and stops new bindings.
  Falsifier: an arbitrary session auto-admitted, a binding to another desk, or a non-identical restore.
- **P4: delegation needs evidence.** A child session binds under its parent only with transcript evidence. Without it, the event is refused.
  Falsifier: an unverified child accepted, or a verified child refused.
- **P5: portability.** `local` and `docker` modes produce the same binding and capture results for the same inputs. The image bakes in no host path.
  Falsifier: a mode-dependent result, or a host path in the image.

## Write scope

- **FEATURE:** `packages/tooling/src/kp_agent_tooling/host_cli.py`, `_impl/service/host_adapter.py`, `_impl/service/spool_ingest.py`, the `kp-agent-host` entry in `[project.scripts]`, minimal edits to T3's `launch_cli.py` (`ingest-spool`), `_impl/runtime_install.py` (host config and spool), the compose manifest's capture role (spool mount and command), and a new `docs/HOST-ADAPTER.md`.
- **TEST:** new tests under `tests/host/`, using fake `claude`/`codex` executables, fake transcripts, and both runtime modes. For Docker mode, mark tests `image` (reading `AGENT_TOOLING_TEST_IMAGE`) or use a local stand-in for `docker exec` that runs the same commands.
