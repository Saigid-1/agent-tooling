# Native ADR planning delivery work record

Decision: [AT-0001](../adr/AT-0001-native-planning-and-delivery.md).
Owner: coordinating Codex session; Sol implements isolated slices, coordinator
reviews and integrates.
Status: Built; implementation merged.
No production deployment claimed.

## Scoped delivery

- Native ADR intake/card lifecycle and requirement evidence: Sol native_card_lifecycle.
- Higher-level ADR registration/planning/chat/track navigation: Sol planning_ui.
- Retire legacy adapter, preserve exact legacy archive, maintain Serena runner: Sol retire_legacy_navigation.
- Durable pre-batch native lifecycle observations, integration, ADR and independent review: coordinator.

## Existing patterns identified before implementation

The pinned navigation review found native Cline task create/start/worktree/state
and review-checkpoint machinery already implemented. ADR intake was unnecessarily
coupled to the read-only external observation projection. Immutable byte retention,
supersession, locked writes and native card mutation are reusable. The old AST
adapter contains a bounded runner actively used by Serena, so delete only after
extracting that shared utility. Native dependency trash-to-start and stream
coalescing are concrete seams requiring adaptation, not reasons to add a dispatcher.

## Verification boundary

Tests use isolated physical state on the external volume. Synthetic fixtures and mocked provider
transitions do not prove a model completed a real TDD slice. No blanket session
admission, outbound model call, board mutation or source ADR amendment follows
from this work record. Record final validation and remaining gaps below at review.

## Integrated review and isolated browser journey

Implementation is on an integration branch; merge/deployment are not
implied by a successful build. The coordinating session reviewed Sol delivery
and incorporated independent findings on source/target pins, invented requirement
IDs, replay, browser metadata preservation, restored terminal sessions, bounded
capture, shutdown ordering and planning evidence freshness.

An isolated fixture was created at
`<operator workspace>/adr-planning-final-trial`
with a synthetic fixture repository.
Browser actions registered `docs/ADR-1.md` before slices existed, reconciled a
reviewed intake with an explicit execution base, and followed its track into the
native backlog card.
The board remained Backlog / Not started. Public CLI fixture GREEN, acceptance,
and amendment-proposal records appeared in the planning UI with matching hashes.
These are synthetic agent assertions exercising the record workflow, not human
acceptance or evidence of model delivery. The source Markdown stayed unchanged.

Lifecycle capture observes native producer summaries before UI batching, retains
hash-linked records, and limits parallel writes to eight. It does not collect
transcript/tool text or infer test execution from a status. Capture failures and
queue overflow remain visible gaps; it cannot recover events emitted while the
service was stopped. Existing restored producer summaries are captured on attach.

The archived navigation adapter was merged into the legacy source repository
before the extraction. The new package retains only the shared
bounded process runner needed by Serena. No legacy graph runtime was added.

## Remaining boundaries

- Architectural chat reuses the native configured Cline provider; no paid/live
  model was dispatched in this isolated fixture trial.
- Execution is native Cline; no new dispatcher or graph card service exists.
- A newer intake does not silently rewrite an existing execution scope. New scope
  needs a new explicit execution slice; automatic revision adoption is deferred.
- Amendment proposals are reviewable records. Applying them to ADR Markdown and
  merging the change remains explicit; board motion never promotes ADR status.
- Other workspaces/evidence poll every five seconds; native active execution uses
  the existing live stream. Cross-workspace dependency arrows are not implemented.
- No production board, deployment, provider account or desk admission was changed.

User guide: [ADR planning](../../apps/kanban/docs/adr-planning.md).
Evidence contract: [native intake](NATIVE-KANBAN-ADR-RECONCILER.md).

## Final validation

Final candidate passed 45 backend lifecycle/intake/inbox/planning tests, 42 UI and
board normalization tests, and 47 portable knowledge/Serena runner tests (134
focused checks total). Backend and browser TypeScript checks and production
bundle build passed. These suites are scoped regression coverage, not a complete
product audit. The isolated browser state survived a server restart. Trial receipts
remain under the trial directory above.
