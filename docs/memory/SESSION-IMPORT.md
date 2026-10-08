# Import and attribute a native session

The new session job imports visible Codex messages from the native JSONL file,
including retained messages from before compaction. It does not reconstruct
missing history or import hidden reasoning or internal compaction summaries.
Attribution and indexing are separate outcomes. The existing bounded Claude
importer remains available; this streaming job does not support Claude yet.

## Cline / Kanban

Open **Import history** in the chat panel, or enter `/import-history`. This is a
local command: it opens the dialog and is not sent to the model. An observed
Codex binding supplies the native file and ID where available; otherwise the
operator supplies the exact file and ID. Preview verifies native metadata.

Choose an approved desk and either:

- **Full session**: import complete rows through the reviewed snapshot boundary.
  Appended messages require a new preview/import or a separate forward job.
- **Current turn onward**: begin at the latest verifiable native user-message
  boundary and continue observing appended rows while the worker is running.
  Earlier context is not silently reclassified. An ambiguous boundary is an error.

Preview is read-only. Its message counts describe the **first bounded batch**,
not the full transcript. Source bytes, excluded prefixes and incomplete trailing
rows are reported separately. Review the source, scope and desk before applying.
The owner claim is an explicit assertion, separate from capture consent; it
never admits the imported session or grants it permission to write as that desk.
Without an owner assertion, imported evidence remains available to topic search.

Progress distinguishes captured rows, indexed episodes, omitted controls,
quarantined rows and worker activity. A worker-start request is not proof of
ongoing capture. Stop pauses the job; resume continues its saved source scope.
An index failure leaves sealed source evidence intact and can be repaired.

## Host setup

Use the existing [portable memory setup](../MEMORY.md) to initialize the store,
review its catalog and admit the exact operator session. Keep state and native
transcripts outside Git. Install this tooling package in the host environment,
then configure Kanban with absolute paths:

```sh
export KANBAN_SESSION_IMPORT_EXECUTABLE=/your/tooling/bin/kp-agent-session-import
export KANBAN_SESSION_IMPORT_CONFIG=/your/private/operator-session.json
```

The browser cannot select an executable. The host configuration determines the
store and tenant; the registered-desk menu draws from that tenant's catalog.
Creating a new desk remains a registry operation, not an import side effect.
This change does not install a global Codex capture hook or alter MCP registration.

## Operator CLI

Every action emits a JSON receipt. Requests may be passed as an absolute JSON
file with `--input`, or on stdin with `--input -`. Start with `desks`, then preview:

```sh
kp-agent-session-import --config /your/private/operator-session.json desks
kp-agent-session-import --config /your/private/operator-session.json preview --input /your/private/import-request.json
```

The preview request is:

```json
{
  "schema_version": "ops.session-import.request.v1",
  "runtime": "codex",
  "source_file": "/your/native/rollout-TIMESTAMP-NATIVE-UUID.jsonl",
  "native_session_id": "NATIVE-UUID",
  "selected_desk_id": "binding:EXACT-REGISTERED-KEY",
  "mode": "full",
  "follow": false,
  "import_actor": "operator:YOUR-IDENTITY"
}
```

For forward capture use `mode: "current-turn-and-forward"` and `follow: true`.
Apply passes the returned `plan_token` with schema
`ops.session-import.apply.v1` and `consent: true`. The reviewed file prefix and
identity must remain valid; normal append growth does not invalidate a preview.

`status`, `continue`, `follow`, `stop` and `resume` accept
`{"schema_version":"ops.session-import.job-ref.v1","job_id":"RETURNED-JOB-ID"}`.
`continue` processes one bounded batch. `follow` drains a full snapshot, or stays
running for an explicitly selected forward job. Its lease prevents concurrent
workers from independently advancing the same job. A changed prefix or replaced
file refuses continuation instead of guessing a new source.

After visible evidence is captured, `assert-owner` accepts:

```json
{
  "schema_version": "ops.session-import.owner-assertion.v1",
  "job_id": "RETURNED-JOB-ID",
  "selected_desk_id": "binding:EXACT-REVIEWED-KEY",
  "asserted_by": "operator:YOUR-IDENTITY",
  "recorded_at": "2026-09-24T17:00:00Z",
  "evidence": ["YOUR-REVIEWED-SELECTION-REFERENCE"]
}
```

Use the actual assertion time. Forward owner claims carry the native source
boundary and cannot attach to earlier episodes. Ownership, contribution and role
remain additive, independently attributable claims. See
[delegation attribution](DELEGATION-ATTRIBUTION.md) for verified parent/child roles
and the proposed new-user discovery and registry configuration flow.

## Verification boundaries

The search projection is the existing source-verified literal index, not a new
semantic embedding service. Omitted controls, quarantined rows and incomplete
last rows remain explicit coverage limits. Source completeness does not imply
truth of transcript claims, successful indexing or authenticated authorship.
