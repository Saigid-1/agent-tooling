# Scoped Claude Desktop capture bridge

`desk_session_hook.py` observes desk binding only. `claude_capture_host.py` separately captures the explicitly mapped transcript at Stop, PreCompact and SessionEnd. It uses Python's standard library, starts no model requests, performs no admission, and sends no messages. Source text and recaps are never printed to hook stdout. A successful hook prints only `suppressOutput`; capture or index failures report a sanitized error on stderr. PreCompact failure exits 2; other failures exit 1.

The current installation scope is one separately approved host session, not every project session. Project hooks can run in other sessions, which return `not_applicable` without invoking Docker. Missing host environment is refused. A matched host with an unexpected native ID, cwd or transcript path is refused. No cwd/default desk fallback exists.

## Operator configuration

The host scripts ship with the OPS extension in `extensions/ops/scripts/`: `claude_capture_host.py`, `desk_session_hook.py` and `launch_claude_desk_memory.py`. Keep them together in a versioned directory named `scripts/`: each adds that directory's parent to `sys.path` and imports `scripts.launch_claude_desk_memory`. They use only Python's standard library, so the host needs no package install. Store the bridge JSON outside Git in an owned physical file with mode 0600, with exactly these fields:

```json
{
  "schema_version": "ops.claude-capture-host.v1",
  "host_session_id": "local_OPERATOR_VERIFIED_HOST_ID",
  "native_session_id": "OPERATOR_VERIFIED_NATIVE_UUID",
  "registry_path": "/ABSOLUTE_HOME/Library/Application Support/Claude/claude-code-sessions/ACCOUNT/WORKSPACE/local_OPERATOR_VERIFIED_HOST_ID.json",
  "cwd": "/exact/registered/project",
  "host_transcript_path": "/ABSOLUTE_HOME/.claude/projects/EXACT_PROJECT/OPERATOR_VERIFIED_NATIVE_UUID.jsonl",
  "container_transcript_path": "/transcripts/ats-current.jsonl",
  "session_config_root": "/physical/private/session-configs",
  "docker_command": "/physical/path/to/docker",
  "container": "memory-1",
  "container_config_root": "/config/sessions",
  "capture_ledger": "/state/capture.sqlite3",
  "queue": "/state/queue.sqlite3",
  "telemetry": "/state/capture-telemetry.sqlite3"
}
```

`ABSOLUTE_HOME` stands for the operator's home directory as an absolute path. Verify `sessionId`, `cliSessionId` and `cwd` directly in the exact Desktop registry file. Those identity fields, not the mutable whole-file hash, are checked each invocation. The transcript basename must match the native UUID. The runtime must supply `CLAUDE_CODE_HOST_SESSION_ID`; hook payload values cannot choose a configuration or admit a session. The existing private session config must belong to that host ID, and the capture backend rechecks the existing desk admission.

Mount only that exact transcript file read-only in the memory container at the configured container path. Do not mount all of the user's home or all Claude projects. A directly mounted file replaced by inode rotation may require remounting; verify container-visible source bytes before resuming. Keep host and container paths distinct and explicit. The bridge checks the payload against the host path and passes the container path to the capture backend.

Before enabling hooks, initialize the existing queue/capture/telemetry stores if necessary. Use the same fixed backend arguments as the bridge with action `bind` to persist the verified native/source mapping. If an earlier verified bulk import covers a prefix, seed only using an operator receipt and verified source hash/offset; never set the cursor to the current end merely to avoid work. The backend `seed` action requires `--seed-offset`, `--seed-prefix-sha256` and `--seed-provenance`, and verifies the prefix. Otherwise capture starts at zero. Indexing uses the admitted backend's local episode search index.

Generate settings using the same absolute Python executable intended for runtime:

```sh
'/absolute/python3' '/versioned/scripts/claude_capture_host.py' \
  --config '/private/operator/capture.json' --settings
```

This prints correctly shell-quoted commands for Stop, PreCompact and SessionEnd, with a 60-second hook timeout around a 45-second backend timeout. Merge these entries into the approved project's `.claude/settings.local.json` while preserving other hooks; back up that file first. The helper never installs settings itself. Restart or reload Claude's project settings as required by the host, then confirm a real Stop hook receipt and backend capture/index status. SessionStart continues to use the existing binding hook; the first subsequent Stop provides capture catch-up. Multiple bounded batches may be needed after a long backlog; status must show remaining bytes explicitly.

To roll back, remove only these exact capture hook commands and the scoped source mount, retaining other hooks and persisted memory/capture receipts. Additional sessions require a separately verified mapping and existing admission. This setup does not grant blanket capture authority.

## Imported history as citation evidence

Native bulk imports remain immutable `source_episodes`; live capture uses `episodes`. Both can support `memory.propose` and consolidation only when the current attribution resolves unambiguously to the writing session's admitted desk. Topic-search visibility alone is insufficient. Conflicting, unresolved, retracted and other-desk ownership cannot authorize a proposal. The import operator is not the source author, and a native transcript UUID need not equal its Desktop host session ID. Do not rewrite either ID or copy imported episodes into the live table to repair citation failures.

Consolidation, queue source validation and capsule source recovery use the shared attribution-aware source resolver. Exact event ranges and quotes remain required. This establishes citation availability, not the truth of the proposed interpretation.
