# Isolated sidebar assistant: Claude capture slice

This opt-in adapter binds a fresh Claude Code Kanban sidebar session to one
operator-selected Workspace Assistant store. It reuses portable admission, MCP,
Claude transcript capture, indexing, consolidation queue and hook receipts.
It does not run a summarizer. Decisions about summarization quality require
longitudinal evidence, not one trial.

## Configuration

Create the isolated assistant catalog/config/policy described in
ASSISTANT-FIRST-SLICE.md. An operator-owned private host contract has:

```json
{
  "schema_version": "agent.assistant-host.v1",
  "config_template": "/private/config/assistant-template.json",
  "transcript_root": "/home/operator/.claude/projects"
}
```

The template's workspace_root must exactly match the selected workspace. It must
use the assistant schema and a separate physical state_root. The host contract
is explicit authorization to admit fresh sidebar sessions to that desk; a hook
payload cannot choose a desk or grant itself admission.

Configure the board process with:

- KANBAN_ASSISTANT_MEMORY_WORKSPACE: exact selected workspace path.
- KANBAN_ASSISTANT_MEMORY_COMMAND: JSON argv array containing an absolute Python
  executable, `-m`, `kp_agent_tooling.assistant_host_cli`, `--binding`, and the
  absolute host-contract path. The interpreter must have the package installed.

Each launch generates an exact native UUID, private session config, admission,
MCP configuration, hook settings and launch receipt. Claude receives only the
private assistant-memory MCP and scoped settings (`--strict-mcp-config` and an
empty `--setting-sources`). Existing board lifecycle hooks are retained. This
mode intentionally does not inherit other MCP servers or user/project hooks.
It retains Claude's normal subscription login and permission controls.

SessionStart provides the assistant's memory instructions. Stop, PreCompact and
SessionEnd verify UUID, cwd and the expected transcript path, then bind and
incrementally capture/index using the existing adapter. Queue and hook state
must remain inside the isolated root. Source omissions and pending bytes remain
visible in receipts. Transcripts remain at the harness's configured native source
location; derived memory, queues and receipts stay in the selected store.

## Boundaries

Only fresh Claude Code sidebar sessions are supported here. A configured
assistant workspace refuses other harnesses and trash-resume rather than silently
launching an unbound assistant. Other workspaces and ordinary cards are unchanged.
No native Cline/OpenRouter capture is claimed. No background summarizer is started.

A separate global transcript watcher is an independent writer. For the first
trial, use a workspace outside its approved repository scope. Do not enable this
adapter in a globally captured workspace until the global policy has an explicit
exclusion for these sessions. Scoped Claude settings alone cannot disable a
separate global watcher.

## Measured browser trial

A separate board on port 3492 launched Claude through the sidebar. The model
called memory.connection_status, completed a marker turn, then called memory.search
and found that captured turn. Two real Stop-hook receipts reported captured and
zero pending bytes. Two episodes existed in the assistant store; read-only global
queries found zero records for that session and zero matches for the marker.
Consolidation jobs were queued, with no summarization model calls.

Private evidence is retained under the operator's private assistant-ui-live-trial
folder (verification.json, per-launch receipts, capture/index/queue state).
The result establishes binding, automatic capture/indexing, model-facing recall
and trial-store isolation. It does not establish summarization quality, all
harnesses, or general global-watcher exclusion.
