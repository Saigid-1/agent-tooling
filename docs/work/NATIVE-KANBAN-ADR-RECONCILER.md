# Reviewed ADR intake to native Kanban

`kanban task reconcile-intake` consumes reviewed `ops.adr-backlog-intake.v1.2`
JSON and creates an ordinary native task that an operator can explicitly start.
It reuses native worktrees and sessions. Intake never starts a session, derives
ADR status, fetches citations, registers workspaces, or amends ADR Markdown.
The approved draft 2020-12 JSON Schema rejects unknown fields and conditional
violations. Full intake content is included in the native delivery prompt.

Use an existing target repository workspace and explicit physical
`KANBAN_STORAGE_ROOT`. The execution base must be a pinned, existing 40-character
Git commit in that repository. It is independent of the ADR's documentary source
repository/revision, including when that source is an external review package.

```sh
kanban task reconcile-intake --project-path /physical/target-repository \
  --intake /physical/reviewed-intake.json --base-ref <execution_commit>

kanban task reconcile-intake --project-path /physical/target-repository \
  --intake /physical/revised-intake.json --base-ref <execution_commit> \
  --supersedes <latest_intake_sha256>
```

Results include `card_id`, `historical_card_id`, `intake_sha256`,
`latest_intake_sha256`, `execution_base_ref`, `execution_repository`, event digest,
sequence, replay status and `dispatch: false`. Notification failure is reported
separately; replay retries notification. A different execution base on replay is
refused: reconciliation cannot silently retarget an existing native request.

## Identity and immutable documentary history

Work identity is `adr:` plus JSON encoding of `[owner.repository, work_id]`.
The stable native ID is `adr_` plus a hash of workspace and work identity. Native
cards carry explicit `adrOrigin` metadata: work identity, historical evidence ID,
bound intake digest, execution repository, and `manual_only` dispatch policy.
Dispatch policy is metadata-based. Historical `obs_` cards remain read-only and
non-executable; external observations are never globally reclassified. Explicit
reconciliation can create a native companion while retaining the historical card
and all its links. No second dispatcher or independent card registry is added.

Original UTF-8 bytes remain at
`work-observations/<historical_card_id>/intake-<sha256>.json`. SHA256 hashes exact
bytes, not reserialized JSON. Citations, derivation, predictions, reuse assessment,
acceptance scenarios, unresolved issues and documentary dispatch assertions stay
intact. Observation references seal supersession links and target execution
repository/base separately from documentary provenance in the retained intake.

Changed bytes require the latest sealed intake SHA256 in `--supersedes`.
Concurrent conflicting supersessions cannot consume the same coordinate. Replay
of old bytes cannot roll back newer history. Once the native card exists,
reconciliation preserves its original request, bound digest, arrows and progress,
even in backlog. A newer documentary snapshot does **not** rewrite a running
request. After supersession, a new explicitly scoped execution is needed before
accepting the newer scope; this slice does not implement automatic request
replacement or revision adoption.

Dependencies resolve against verified retained histories in the same workspace,
including different owner repositories. Ingest prerequisites first. Missing or
ambiguous identities, self-dependencies, cycles and non-backlog targets fail
before sealing initial projection. Source-explicit requirements become native
arrows; proposed requirements remain evidence. Native ADR cards never auto-start
when a prerequisite moves from review to trash. Manual native start remains
available and is an explicit authorization action, not proof that prerequisites
were accepted. Cross-workspace native arrows are unsupported.

## Requirement evidence and proposed amendments

`kanban task adr-evidence --project-path /physical/target-repository --task-id <id>`
reads append-only requirement history. Add `--event /physical/evidence-input.json`
to append an explicit local assertion. The typed backend/UI uses the same store.
An input contains:

```json
{
  "eventId": "41ab2ebe-e1ba-4f77-aa12-61f78e6ed921",
  "actor": { "kind": "local_operator_assertion", "id": "local-ui" },
  "requirementId": "acceptance:1",
  "intakeSha256": "<64-character current intake digest>",
  "kind": "green",
  "outcome": "passed",
  "rationale": "Observed result and remaining limitations",
  "evidence": [{ "path": "/physical/test-receipt.json", "sha256": "<receipt digest>" }]
}
```

Requirement IDs are `acceptance:1`, `acceptance:2`, etc., indexing the retained
intake's acceptance scenarios in order. Invented scenario IDs are rejected.
Actor kinds are `local_operator_assertion` and `agent_assertion`: the supplied ID
is attribution, **not authenticated human approval or host testimony**. Use a new
UUID event ID for a new assertion; retrying the same ID and input returns the
original record without duplication, even after supersession. Reusing the ID
with different input fails.

Kinds are `red`, `green`, `implementation`, `merge`, `deployment`, `acceptance`
and `amendment_proposal`. RED requires `failed`; acceptance requires `accepted`
or `rejected`; amendment proposals require `proposed` and an `amendment` text
field. Other kinds accept `passed` or `failed`. Every assertion requires rationale
and bounded physical evidence files whose bytes match the supplied hashes.
These are local assertions linked to files, not an automatic test runner or
independent verifier of the files' claims.

Accepted outcomes require the latest recorded RED/GREEN result for that scenario
and intake to be GREEN/passed, with the same captured native execution binding.
A later RED invalidates an earlier GREEN for subsequent acceptance. Behavior
evidence and acceptance cannot borrow an older execution for a newer documentary
intake. Rejection and amendment proposals can describe that unresolved mismatch.
Records retain a sequence, timestamp, asserted actor, intake scope, and execution
snapshot (bound intake digest, repository and base). Board movement and session
completion never write acceptance or infer Built, Deployed or Proven.

An amendment proposal stores reviewable text with requirement evidence. It does
not update the source ADR, publish a PR, or change derived status. Reviewing and
applying that source change remains a separate explicit action.

## Durability and limits

Native workspace locking protects intake sequence admission. Intake bytes are
published before observations; a validation failure may leave harmless unlinked
content-addressed bytes. An observation is sealed before board save, so replay
repairs interrupted initial projection. Intake retains the existing observation
store's durability boundary; it does not claim a new power-loss transaction.
Retained bytes and event chains are checked before reuse. Generic `task observe`
cannot replace intake history.

Evidence writes use the existing filesystem lock machinery, exclusive publication,
file flush and directory flush. A failed notification cannot erase a recorded
assertion. Evidence histories reject sequence gaps. Intake directory traversal
is capped at 2,048 entries per directory before files are parsed; evidence
histories are capped at 10,000 records and 64,000 bytes per record. Intake is
bounded at 1,000,000 bytes; referenced evidence files at 8,000,000 bytes. Capacity
errors are explicit; no silent history pruning occurs. Remote citations are not
fetched or executed.

## Verification

Focused tests cover public CLI reconcile/read/append and retry, exact-byte intake
retention, schema conditionals, supersession, old replay, dependency resolution,
no ADR automatic cascade, historical observation protection, explicit migration,
execution/documentary separation, immutable replay bases, progress preservation,
requirement registration, assertion attribution, idempotent evidence publication,
acceptance rejection and GREEN ordering, execution mismatch, proposed amendments,
symlinks, tampering, and traversal limits. Tests use isolated physical storage;
they do not change the live board. The bundle embeds the authoritative intake
schema and does not need repository docs at runtime. No new MCP mutation is added
by this slice.
