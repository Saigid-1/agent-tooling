# Scoped native workspace capture

`kp-agent-workspace-capture` is an operator-only local worker. It enrolls visible
Claude and Codex JSONL rows from selected projects into tenant-scoped source
sessions. An unassigned source gets no desk owner, admission, write authority, or
outbound summary. Existing manual sources in a tenant's global view remain
visible; this policy controls new automatic enrollment.

The operator supplies an existing private desk-memory configuration and a private
capture policy (`0600`). `preview` reads discovery and the current journal without
creating one; `once` processes at most `max_candidates` files and one bounded
batch per file. `watch` repeats `once` after `--interval-seconds`, reloading both
policy and admission on every pass. `once` never loops until backlog is empty.

The policy schema is `ops.workspace-capture.v1` with these exact fields:

```json
{
  "schema_version": "ops.workspace-capture.v1",
  "approval_record": "/absolute/path/to/approved-scope.json",
  "approval_sha256": "sha256-of-approval-record-bytes",
  "tenant_id": "selected-tenant",
  "approved_repo_keys": ["repo-key-from-approval-record"],
  "repos": {"repo-key-from-approval-record": ["/absolute/verified/checkout"]},
  "native_roots": {"claude": "/absolute/.claude/projects", "codex": "/absolute/.codex/sessions"},
  "excluded_sessions": {"claude": [], "codex": []},
  "max_candidates": 64,
  "max_batch_bytes": 4000000,
  "max_batch_rows": 2000
}
```

The approval record must contain `tenant_id` and `repositories`. The ordered
`approved_repo_keys` must match that frozen list. Each key has an explicit list
of checkout roots, including independent clones of the same approved repository.
An empty list means the selected repository currently has no resolvable root;
it appears as a coverage gap. The worker never expands scope from a changed desk
catalog. Policy changes invalidate the discovery inventory, while the approval
record continues to pin the approved keys. Run the CLI with:

```sh
kp-agent-workspace-capture --config '/absolute/private/desk-memory.json' --policy '/absolute/private/capture-policy.json' preview
kp-agent-workspace-capture --config '/absolute/private/desk-memory.json' --policy '/absolute/private/capture-policy.json' once
```

Mount native roots and each configured checkout, its `.git` common directory,
and any worktree `cwd` read-only at paths that match the native metadata. Project
membership is a native `cwd` resolved through Git's common directory to an
explicit policy root. A missing/deleted cwd, missing Git metadata, or a candidate
outside those identities is a reported gap. Basenames, Claude project folder
slugs, and transcript prose do not grant membership. The native session's
starting workspace is the automatic repository membership recorded here;
an operator may still annotate a session with several relevant repositories.
The policy's exact native session exclusions are for sources owned by existing
hooks; `managed_by_exact_hook`
does not assert that a hook recently ran. Operators should list the session identities that existing
hooks already capture here before activation.

The worker enumerates Claude project files and native nested subagent files,
and Codex `sessions/YYYY/MM/DD` files. A separate Codex `archived_sessions`
directory is outside this version's discovery boundary. The report states the
candidate count, per-pass inspected count, remaining candidates, selected repos
without roots, file cursors, remaining bytes, omissions, and explicit gaps. This
is observed coverage of configured roots, not a claim that all host transcripts
or projects were found. Expected native control rows count as omitted; malformed
or event-bound rows count as quarantined. A quarantined row or source error makes
the pass `partial` and requires operator review. Index-pending episode IDs remain durable and are repaired before a file
advances again. Raw native files remain the evidence source.

## Continuity across container restarts

A tracked source continues from its byte cursor when its tenant, runtime, native
session identity and repository key match its journal record and its first
`cursor` bytes still hash to the recorded `prefix_sha256`. That digest is
re-verified under the lease before every batch, and it is the integrity check.
The file's `(device, inode)` is only a hint: Docker Desktop file sharing gives
the same host file a new inode number whenever the container is recreated. A
renumbered source therefore continues, and the record adopts the current
`(device, inode)` when the batch's cursor receipt is written. No row is imported
twice, because the cursor continues and a replayed row's sealed episode is
verified rather than re-imported. A source whose prefix differs, or which is now
shorter than its cursor, is still refused (`native source prefix changed or
truncated`), and so is a source whose tenant, runtime, native identity or
repository key differs (`source identity or project changed`). The checks within
one batch (`source changed during batch`, `source changed before cursor
receipt`) are unchanged.

## Directory moves

A source's project membership is sealed from its starting `cwd` (that of the
first Claude row, within its first 32, carrying its session identity and a
`cwd`, or of the Codex `session_meta` row that opens the file), and a directory
move never changes it. A later Claude row whose `cwd` differs from that starting
`cwd` is resolved by the same Git common-directory rule as discovery, once per
distinct `cwd` per batch:

- a `cwd` in any approved repository of the policy (a subdirectory, a linked
  worktree, or another approved checkout) is captured as part of the same source
  session;
- any other `cwd` (unapproved, missing, relative, not a string, or unresolvable)
  is omitted with the counted omission `cwd_outside_policy`, and capture
  continues past it. Nothing from that row is imported.

The omission counts toward `omitted_rows`. The `once` file report carries
`omissions: {"cwd_outside_policy": n}` for the batch, and the journal record's
`counts.omissions` keeps the running total; both appear only when the count is
not zero. A row whose `sessionId` names another session, or a sidechain row
whose `agentId` names another agent, is still refused (`native workspace
identity changed`).

A Codex `session_meta` row with a different `cwd` continues when that `cwd` is
in an approved repository. A `session_meta` row whose `cwd` is in no approved
repository, or whose session `id` differs, is still refused (`native workspace
identity changed`): Codex event rows carry no `cwd` of their own, so the rows
after it cannot be shown to belong to an approved repository.

## Refusal reasons

Every refusal of a tracked source is written as its current journal `error`,
including the identity check made before the lease. A batch that completes
clears it. `preview` reports, across all tracked sources (not only this pass's
candidates), `tracked_sources`, a `refusals` list of `{source_file, reason}` and
`refusal_counts` per reason. Discovery gaps (for example `workspace_unavailable`
or `managed_by_exact_hook`) are not capture refusals: they stay in the report's
`gaps` and are not written to the journal. Reasons are fixed strings; no
transcript text, path or exception detail appears in one:

- `source identity or project changed`
- `native source prefix changed or truncated`
- `native workspace identity changed`
- `native row exceeds bounded read limit`
- `next native row exceeds batch bound`
- `source changed during batch`
- `source changed before cursor receipt`
- `capture lease ownership changed`
- `sealed native source row changed`
- `source project membership changed`
- `source reference already sealed with different evidence`
- `source session unavailable to this tenant`
- `source session integrity mismatch`
- `source episode unavailable`
- `source episode integrity mismatch`
- `Native source is shorter than its saved cursor.`

Any other failure is reported by kind only: `capture store error` (SQLite),
`file access error` (filesystem), `native row rejected` (the Claude row parser),
or `unclassified capture failure`. Free text recorded by an earlier version is
shown by `preview` as `unclassified legacy error` until the source's next batch
replaces or clears it.
