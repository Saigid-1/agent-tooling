# Delegated session attribution

The operator adapter records historical relationships between two **existing**
native Codex sessions. It reads only the first `session_meta` JSONL row of each
selected file. The child file's ID must match its filename and its
`source.subagent.thread_spawn.parent_thread_id` must match the selected parent
file's native ID. The selected `agent_path` must match too. `CODEX_THREAD_ID`
can identify the running child, while `CODEX_SESSION_ID` may be inherited from
the parent; neither environment variable is used as proof by this adapter.

The native metadata proves parentage and the child identity. It does **not**
prove an assigned work role: Codex's `agent_role` may be null. The operator
supplies an exact role name and a parent role-assignment evidence reference.
The adapter appends `session.assigned_role` and `session.delegated_from` on the
child, and `session.contributor` on the parent. It does not create
`session.owner`, admit the child to a desk, or confer model or tool privileges.
Several children under one parent remain separate source sessions even when
they share the same model and host seat.
This is local host metadata selected by the operator, not a cryptographic
attestation against someone who can rewrite that host's files.

The adapter ships with the OPS extension. With `packages/tooling` and
`extensions/ops` installed, preview first:

```sh
python -m kp_agent_tooling_ops.delegation_cli \
  --config /private/operator/desk-session.json \
  --parent-file /private/native/parent-rollout.jsonl \
  --child-file /private/native/child-rollout.jsonl \
  --agent-path /root/analyst \
  --role-id analyst \
  --asserted-by parent:operator-session \
  --assignment-ref parent-assignment:turn-17 \
  --recorded-at 2026-09-24T17:00:00Z
```

Repeat the same command with `--apply` to record claims. The configured
operator session must be the selected native parent. For a different historical
parent, use `--historical-parent` explicitly. The receipt distinguishes those
verification bases. An exact retry with the same arguments is idempotent;
if interrupted between individual claim writes, retry the same command to
complete the set. Do not change the assignment timestamp during that retry.

To scope claims from a known task boundary, supply explicit
`--child-source-range-json` and `--parent-source-range-json`, each shaped as
`{"native_id":"...","source_file":"/absolute/file.jsonl","start_offset":123}`.
The byte offset must begin a JSONL row in that selected file. The child range
applies to its role and parent claims; the parent range applies to its
contributor claim. Offsets are operator-supplied, never inferred from task
text. Without a range, the SessionSources claim retains its whole-session
scope; `valid_from` still records when the role was asserted and should not be
read as proof that earlier work had the same role.

The role identity is exact and configurable. The reviewed catalog contains
approved desk roles; other role names can be recorded as unregistered
historical claims, which the receipt labels `unregistered_role_claim`. A
claim does not register a new desk. The checked-in example catalog has only
fixture Implementation and Verification desks; imported catalogs may contain
names such as `Coordinator`, `Verification`, or `Deployment Engineering`.
The current Kanban Cline integration exposes task Plan/Act mode and model
selection, but no checked-in named persona inventory or automatic
role-to-desk mapping. Provider labels and task mode are separate from these
operator role claims. A bounded read of this host's `~/.cline/kanban`
configuration found selected harness IDs (`claude`, `codex`, `opencode`) and
task settings, with no persona or role fields in the inspected JSON files.
The private Cline session database was unavailable to this inspection, so
this observation does not establish the absence of personas elsewhere on
the host.

For a future role editor, list role templates and desk bindings from the
operator-approved catalog, retaining each exact role identity and approval
reference. Let the operator create or revise roles through the catalog's
reviewed configuration operation; a `session.assigned_role` claim alone must
not create a desk. The UI should show three independent facts for each native
session: its asserted work role, its `session.owner` desk (if any), and its
contributor relationship to a parent. Additional analyst, investigator,
reviewer, auditor, or implementer labels can be recorded as unregistered
claims until the operator approves corresponding templates and desks.

When an existing child session is reused for a new task, a date-stamped
whole-session role claim documents the new assignment but does not prove that
older imported episodes used that role. Prefer a verified source byte range
for claims intended to govern per-episode attribution. Without one, display
the assignment as a session-level assertion with uncertain earlier scope.

## Next historical import step

The current job imports one explicitly selected native Codex file. A broader
history picker should first discover bounded native files by runtime, date,
and session ID, then let the operator select exact files. Its read-only preview
should report visible-row counts, omitted/control rows, byte coverage, and
which session identities and source ranges were verified. Keep the existing
unresolved topic-only path available when the operator makes no owner claim;
an optional owner choice should append a scoped historical claim rather than
grant admission. Queue bounded background jobs for large selections and show
durable cursor, progress, repair, and stop states. Ask for explicit consent at
the source-range and owner-claim boundaries. The new streaming job currently
supports Codex JSONL; present Claude or other formats as unsupported until a
format-specific parser and proof are implemented.
