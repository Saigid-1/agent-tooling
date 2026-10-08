# Exact-session hooks and inbox consumption

The 2026-09-24 inspection found two product projects' SessionStart settings invoking the old `desk_memory_cli.py --role Coordinator ... bootstrap` from a dirty legacy checkout. That code can register a default before a claim. Its separate SessionEnd hook seals the graph run. This is a configuration and lifetime boundary defect; prompt reminders cannot correct it.

The replacement `extensions/ops/scripts/desk_session_hook.py` (shipped with the OPS extension) reuses `launch_claude_desk_memory.prepare_session`, the current portable session config, approved catalog and `DeskSessionLedger`. It creates no new registry or graph dependency. It deliberately does not rewrite historical graph runs or make legacy CLI claims into portable admission.

- SessionStart resolves only `CLAUDE_CODE_HOST_SESSION_ID` from the host environment. Directory, default role and hook payload claims cannot select a desk. The payload's native transcript ID is recorded as an observation in a separate namespace, not asserted as a verified alias.
- Resume uses the same admitted session. When configured with `--selection-root`, the existing host-owned exact-session selection can recover its missing config. No nearest-session, cwd or role fallback exists.
- SessionEnd observes the event without retiring or revoking the desk admission. This is not proof the session remains running. Historical sealed graph intervals remain sealed.
- Multiple independently admitted sessions can occupy the same desk concurrently. A new host ID requires its own admission; it cannot borrow another session's selection. Existing catalog withdrawal and scope checks still apply.
- Setup failures emit an explicit `unbound` receipt and allow ordinary coding to continue. They do not grant memory access. The hook does not create a claim merely to remove an error.

## Host installation

Copy this script, `extensions/ops/scripts/launch_claude_desk_memory.py`, and its generated sibling `extensions/ops/scripts/memory_tools.json` into a versioned host directory named `scripts/`; the hook imports `scripts.launch_claude_desk_memory` from that directory's parent. Invoke with the same `--session-config-root`, `--docker-command`, container and container config root as the registered portable memory MCP. No Python package install is required on the host. Replace only the legacy bootstrap invocation and mechanical graph SessionEnd invocation; retain other hooks and startup guidance. Back up settings first. No Docker image change is needed for this host adapter.

An existing graph claim or desk note does not populate the portable admission ledger. Use the existing human/host `kp-agent-desk-setup session --admit` flow for each exact session, optionally persisting `--selection-output`. Existing approved portable configs survive restarts unchanged. Until admitted, the corrected hook must report unbound. This repair does not assert that every current desk has already been migrated.

## Optional inbox consumption

Add `--inbox-endpoint http://127.0.0.1:PORT --workspace-id WORKSPACE`. On SessionStart, the adapter reads up to ten pending messages using the verified tenant and host session ID. The explicit workspace is transport scope; it does not select a desk. The consumer checks event scope and UTF-8 text digests. Reads are capped at 128 KiB, five seconds, and loopback origins without redirects. It emits pending messages as untrusted evidence, with `model_read: not-established`. It never automatically acknowledges them. Inbox downtime is reported separately and does not erase a valid binding.

The inbox remains the independent Node/filesystem service. This optional consumer needs no graph writer, embedding model, or message indexing. Startup catch-up is a bounded read; subsequent WebSocket-to-active-model delivery still needs a harness adapter. This slice does not inject text into a running terminal or pretend stdout proves model reading. Acknowledgement stays an explicit inbox operation.

## Evidence

Baseline: a commit of the legacy source repository and the local Kanban fork's head. Intake reused the existing portable launcher/ledger and filesystem inbox, extending only the host adapter. Navigation's unrestricted text search was incomplete; scoped source reads established these paths.

Targeted tests cover resume/config recovery, default-role rejection, unknown sessions, concurrent occupants, SessionEnd preserving admission, tenant/hash rejection, redirects and inbox failure separation. The isolated `bound-inbox-live-01` trial exercised real Kanban CLI/HTTP/WebSocket/storage with synthetic messages and a real local portable ledger. The fixture adapter called the ledger in-process instead of crossing Docker. Its thirteen checks passed. No production note was read, no model inference ran, and no model-read claim is made.


## Stable memory discovery

The launcher and backend publish the same 13-tool schema, including
`memory.connection_status`, even before admission. Tool discovery is not permission.
The host setup connection rechecks the existing exact-session preflight on each
call and forwards only after it succeeds. It never selects a different desk from
arguments. The admitted backend also reloads/checks admission per operation, so
withdrawal or config faults refuse reads and writes without dropping discovery.

The setup connection uses a bounded CLI bridge after repair (45-second timeout,
2-MB response limit); new admitted connections use the ordinary persistent MCP
server. A native test exercises denial → admission → successful call → withdrawal
on one connection. Host schema equality is checked against canonical backend
`TOOLS`; regenerate `extensions/ops/scripts/memory_tools.json` from that list whenever it changes.
Deploy the host script and schema together with the matching image.

There is no tool-set change on admission and therefore no admission-triggered
`list_changed` notification. Schema updates are versioned deployment changes and
require host discovery once; this does not repair an already cached old one-tool
catalog by itself. Existing legacy clients need a fresh host discovery on upgrade.
The diagnostic stays callable afterward. Nothing here grants access or promises
that every embedding host honors MCP dynamic notifications.
