# Read-only harness session resolution trial

`task resolve-session` inspects Kanban's existing workspace index and saved
`sessions.json`. Set `KANBAN_STORAGE_ROOT` to the exact physical storage root of
the board being inspected, then provide an exact workspace ID, provider ID and
either Kanban task ID or external native session ID. The command does not contact
the running board or harness and does not create a workspace, start, resume or
send to a session. A 4 MiB file bound limits each registry read.

```sh
KANBAN_STORAGE_ROOT='<operator workspace>/kanban-trial-state' \
  node --import tsx src/cli.ts task resolve-session \
  --workspace-id navigation-memory-product --provider-id codex \
  --task-id '__home_agent__:navigation-memory-product:codex'
```

An exact saved task match returns the registry file path, workspace ID, task ID,
provider, saved state and its update time, inspection time, file modification time
and SHA256. The registry path distinguishes identical task keys on different
Kanban instances. `runtimeEndpoint: null` means no live endpoint was established.
Kanban's terminal input capability is conditional on an active runtime; it is not
a structured, acknowledged harness message. Saved `running` state and a PID are
not process liveness or delivery evidence. Missing, wrong-provider, wrong-workspace
and duplicate candidate records remain unresolved or ambiguous.

Kanban's saved registry has no verified native Codex session ID mapping. An
external native ID therefore returns `unresolved` within this inspected Kanban
registry, even if a title or UUID resembles a Kanban task. It does not say the
external session is globally unreachable.

## Read-only trial, 2026-09-24

The original 3484 storage root yielded an exact `codex` saved task match for
`__home_agent__:navigation-memory-product:codex`, with PID 49992 and saved
`running` state. The snapshot file's SHA256 was recorded; the observed time was
`2026-09-24T03:40:06.108Z`. No live contact was attempted.
A supplied host session ID from another desk
returned `unresolved` against that same registry and hash at
`2026-09-24T03:40:12.402Z`. Its separate authenticated host registry would be
needed to establish a contact route. Neither 3484 nor 3485 was restarted.

Ten focused resolver and registry-reader tests and root TypeScript checking passed. This slice
adds no persistent registry or automatic discovery, and does not change
authorization, memory claims, or dispatch.

## Parent review

The coordinating agent independently reran both CLI reads and retained exact
outputs in the operator workspace's kanban-observer-state/harness-resolution-review-01/kanban.json and
a second JSON file. The saved registry SHA256 was unchanged; Kanban task resolution
succeeded and the external host ID remained unresolved in this registry. The
host-ID query's provider selector is an explicit caller input, not a verified mapping.
No live endpoint or delivery claim follows from either result.

Review tightened instance identity, missing-file reporting, same-handle metadata,
regular-file and 4 MiB guards, JSON-null refusal and portable test storage.
No live server was restarted; source CLI is available now, deployed bundles have
not been rebuilt by this slice. No messages were sent or sessions resumed.

## Native ID capture boundary, 2026-09-24

The Codex wrapper already watches its process-bound TUI log. A root
`session_meta.payload.id` event now supplies an optional provider-native binding
for the exact Kanban task/workspace in that wrapper's hook environment. Descendant
session metadata is excluded. The binding is written into that task's existing
`sessions.json` summary after the hook API confirms an active task and matching
`codex` provider. It retains the observation time, TUI log path, and SHA256 of
the exact metadata line. Existing workspace saves preserve recorded bindings;
there is no second session registry. At most 32 IDs are retained per task.

`task resolve-session --native-id <id> --provider-id codex --workspace-id <id>`
now requires an exact recorded binding. If more than one task or registry source
claims that ID, resolution is ambiguous. A task query shows any recorded native
bindings as historical evidence; it still has no verified live endpoint or
delivery assurance. A native-ID match retains the original provider even if the
task's current provider changes; task terminal input is never attributed to the
historical native session. The local hook channel supplies provenance, not remote
authentication, and a log-path/line hash is not proof of process liveness.

Host IDs use `--host-id` and a separate namespace. That host session's DeskRun
export is historical host association; its verified export SHA256 does not bind
that host ID to a Codex-native ID or Kanban task. The separate coordination-desk
capture binding identifies a provider-native ID for an observation card, not the
original 3484 home task. Neither is promoted into a contact mapping.

The read-only trial queried the original 3484 registry without restarting the
server. The known home Codex task resolved as a Kanban task with no recorded
native binding; the other desk's host ID remained unresolved. Exact JSON receipts,
with their SHA256s, are kept privately under
`<operator workspace>/kanban-observer-state/harness-native-resolution-trial-01/`.
Both inspected the unchanged 3484 registry at
`2026-09-24T11:45:07.961Z` and `2026-09-24T11:45:18.376Z`, respectively.
This source change has not been deployed to 3484/3485, and no session was sent
input, started, resumed, or probed for liveness.

Eight affected suites (51 tests), root TypeScript checking, and formatting
checks passed with temporary test files on the mounted operator workspace. This
fixture proof covers exact native binding and conflict refusal; live native
capture remains unverified until a future Kanban-owned Codex session supplies
the required metadata through a deployed capture path.

## Native Codex hook path correction

The normal Kanban Codex adapter installs `codex-hook`; it does not invoke the
optional `codex-wrapper`. The hook entrypoint now checks two explicit JSON
fields, `session_id` and `transcript_path`, if Codex supplies them. It reads only
the first 64 KiB of the exact regular transcript file, requires a root
`session_meta` header with `payload.id` equal to `session_id` and `payload.cwd`
equal to the hook process cwd, and stores a SHA256 of only that header line.
Missing fields, unreadable or symlinked files, oversized headers, mismatches,
and descendant metadata yield no binding. Ordinary hook state notifications
continue in those cases. The source tag `codex_hook_transcript_meta` distinguishes
this direct path from the optional wrapper's `codex_tui_session_meta` tag.

The controlled native trial on 2026-09-24 established that installed Codex
v0.155.0-alpha.16.3 supplies the fields required by this path. A normal Kanban
launch of task `71d54` returned `NATIVE-IDENTITY-TRIAL-OK`, moved to review, and
automatically persisted its native session ID.
An independent read verified the exact header SHA256, native ID and worktree cwd.
The resolver mapped that ID to task `71d54`; a one-character-different ID remained
unresolved. The worktree had no changes. No raw transcript was copied.

Receipts are retained locally under
`<operator workspace>/native-capture-live-01/`:
`receipt.json`, `session-summary.json`, `resolution.json`, and `wrong-id.json`.
The isolated trial server is 3486. Existing servers 3484/3485 were not upgraded.
This proves automatic capture and exact saved-registry resolution on that installed
Codex version, not provider contact, delivery acknowledgement, or other harnesses.

Parent acceptance review: 2026-09-24. Independently inspected wrapper metadata,
hook ingestion, session persistence and resolver namespaces. Eight affected suites
passed (57 tests including the desk-directory checks), root typecheck and diff
check passed. This accepts the source implementation, not a live native capture
or authenticated provider contact route. Both running servers remain unchanged.
