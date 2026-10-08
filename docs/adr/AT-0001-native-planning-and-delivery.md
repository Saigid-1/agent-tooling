# AT-0001 — ADR planning above native Cline execution

Owner: agent-tooling. Declared: Built. Derived: Built.
This uses an agent-tooling-specific numbering namespace; decisions inherited from the
legacy source repository retain their existing identities. It does not renumber or
supersede those ADRs.

## Accepted direction

Principal ruling, verbatim in this task, 2026-09-26:

> agreed with this entirely - my real intent is to take cline and extend it to match the stated process, flow, and end state that we were originally pursuing.

> basically we need a "higher level" view at the ADR/planning level, and clicking into any ADR would bring up a list of projects/session tracks that would navigate you to the out of the box kanban view where execution state can already be viewed in real time.

> Let's also clean up the old kp_ops.code_navigation adapter and move it to the old [legacy source] repo; we should strive to keep this repo and implementation as clean and structured as possible so it stays small enough to optimize and tune as we go.

Delivery is tracked by [the owned work record](../work/ADR-PLANNING-DELIVERY.md).
Implementation: merged; scoped verification is recorded in the owned work record.
No production deployment or complete model-driven TDD journey proof is claimed.

## Decision

Extend Cline's native UI, cards, repository-backed workspaces, chat, worktrees and
session startup. Architectural planning is a separate higher-level view, not an
extra execution column. One ADR can describe several initiatives or execution
tracks; each track points to its actual workspace and native cards. Opening a
planning page does not launch a model, create a worktree or admit a desk.

The process is ADR -> bounded reviewed slice -> native card -> TDD execution ->
requirement-level acceptance -> reviewed ADR amendment. Cline owns dispatch.
Our integrations retain the intent/evidence relationship and observe outcomes.
No second dispatch engine, graph card registry or legacy runtime is required.

Before scoping a slice, use pinned code navigation to identify existing machinery,
reuse candidates, overlapping work and unresolved interface contracts. The intake
record preserves that assessment. Neither a search result nor a schema-valid
record proves architectural completeness or actual behavior.

Separate these identities and claims:

- Documentary source repository/revision/content digest.
- Execution workspace/repository/base revision and the request actually launched.
- Stable work/slice identity and native card ID.
- Observed session attempts and verified native session references when available.
- Desk attribution and admission: labels alone confer neither.
- Test evidence, acceptance decision, merge, deployment and proven behavior.

Native ADR cards are executable through explicit native Start/move. ADR dependency
arrows are informational for sequencing until their automatic execution policy is
explicitly approved. Review-to-trash is not acceptance. External chat projections
remain read-only; historical obs_ identities and evidence chains are preserved.
New capability policy must be explicit metadata, not encoded only in ID prefixes.

Retain exact intake bytes and explicit supersession. Replaying or revising intake
must not reset native progress, rewrite the running request or erase earlier proof.
Capture native state before stream batching; disconnected browsers must not be
required for tracking. The capture is an observation of producer state, not proof
that a test passed or a requested behavior executed. Gaps are reported visibly.

Acceptance records identify the requirement, concrete evidence and decision.
They can prepare an ADR amendment for review; they cannot silently amend the
source or promote status based on a column, agent completion, or supplied prose.
Built, Deployed and Proven retain their distinct evidence requirements.

## Reuse and retirement

Reuse the approved intake schema, locked native workspace mutations, durable file
primitives, Cline and terminal summary producers, existing chat panels and native
workspace navigation. Keep the portable navigation/memory/telemetry package
independent of the optional planning UI.

Archive the exact old AST subprocess adapter in the legacy source repository with a content digest and
provenance. Extract its genuinely shared bounded process utility for Serena;
reject legacy provider configuration explicitly. Preserve current SCIP/Serena
behavior and error information. Historical ops.* schema names alone do not create
a runtime dependency and need not be churned to satisfy this decision.

## Acceptance and falsifiers

1. An ADR can be registered and discussed before any slice exists. Opening it
   shows its actual source boundary; uncommitted bytes cannot masquerade as HEAD.
2. Reviewed intake creates one native card at the declared execution target.
   Replay creates no duplicate, dispatch or stage reset. A different ADR source
   repository does not become the execution repository.
3. Native Start works for the new card, while an external observed card remains
   protected. Finishing a dependency cannot silently dispatch an ADR card.
4. Rapid state transitions survive browser disconnection and observer restart
   without duplication; a capture failure is visible, not evidence of absence.
5. RED/GREEN and acceptance evidence remain linked to scoped requirements and
   exact artifacts. Rejection leaves the requirement unresolved. A review column
   or successful model exit cannot substitute for this evidence.
6. An ADR amendment is an explicit reviewable output. Its source and cited proof
   can be rechecked; its existence does not establish merge/deployment/proof.
7. Portable tooling imports and CLI/MCP parity continue working after removing
   the legacy adapter. Missing old configuration produces a clear refusal.

## Status

Accepted by the ruling above; implementation/review is underway in the linked work
record. Built requires the implementation merge URL/commit. Deployment requires
an environment and image receipt. Proven requires a same-environment walkthrough
or dated Principal sign-off; fixture tests must remain labeled as such.
