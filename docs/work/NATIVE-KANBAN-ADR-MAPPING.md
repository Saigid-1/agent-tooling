# Native Kanban cards and ADR requirements

Assessment of the extracted Cline fork, 2026-09-26. Recommendation: use its native
board/task storage and retire the legacy card runtime after migration accounting.
No graph card mint or legacy dispatcher is necessary to create a native card.
This review does not claim historical legacy cards have been migrated or deleted.

## Actual entry points

`apps/kanban/src/commands/task.ts:createTask` resolves a repository-backed workspace,
uses the runtime client to mutate that workspace's board, and calls
`addTaskToColumn(..., "backlog", ...)`. The native function creates the ID, validates
prompt/baseRef, and records timestamps. Creating the backlog item does not start a
session. Native Start/session machinery is a separate concern with stronger effects.

Our chat projection also uses these same native mutations. `task observe` ingests
`ops.work-observation.v1`, seals its ordered hash-linked receipts, and projects one
`obs_` card per workspace/work identity. Its status and evidence come from explicit
observations, not a legacy dispatch event. Current projection guards reject dispatch
and ordinary editing/movement of these cards; dependency edits remain available.
The `ops.*` schema name is historical naming, not a runtime dependency on the legacy system.

Workspace registration binds an ID to a repository path. An initiative/epic is a
separate conceptual grouping; it should not create another execution workspace or
pretend the board supports a repo-independent project abstraction today. A board
may display cross-repo work, but the execution target and ownership must stay explicit.

## Mapping and gaps

| Requirement | Native card / existing adapter | Retain or add |
|---|---|---|
| Stable identity and title | Native `id`, `title`; projection hashes workspace + work_id | Keep repository-qualified work_id plus initiative_id/slice_id in intake; persist their mapping to card ID |
| Outcome and scope | Native `prompt`, `baseRef` | Render entry point, outcome and source pin from the structured intake; baseRef alone is not an evidence receipt |
| Human-facing WIP | Column contains card; backlog/in_progress/review/trash | Treat as a display projection, never the ADR lifecycle |
| Dependencies | Board-level `fromTaskId`, `toTaskId`, ID, timestamp | Keep requirement, basis, reason and stable work identity outside the arrow; verify targets/cycles and direction |
| Desk/session attribution | Native `agentId` selects a harness, not a desk; observations carry session IDs | Use portable verified attribution references; never infer identity from agentId or title |
| Reuse/pattern matching | No native structured fields | Retain required reuse_assessment with pinned current/in-flight evidence before execution admission |
| Tests, predictions and falsifiers | Prompt can display prose | Preserve acceptance_scenarios, predictions and prediction_gap as structured evidence |
| ADR status and provenance | No corresponding native fields | Preserve declared_status, derived_status, verification, reason, citations and derivation separately |
| Dispatch approval | `startInPlanMode` and auto-review flags are behavior settings | A separate approval must bind exact work/input digest, action and actor; intake remains not_authorized |
| Durable update/replay | Existing observation sequence/hash chain and locked mutations | Reuse these; the versioned intake-to-card reconciler now retains typed references and full intake bytes without a second card registry |

Native schema is `runtimeBoardCardSchema` in `apps/kanban/src/core/api-contract.ts`:
ID, title, prompt, plan/review flags, images, agent/model settings, baseRef and
timestamps. Dependencies live on the board, not within each card. Zod object parsing
does not retain arbitrary added ADR properties: merely adding JSON fields to a card
is not a safe extension strategy. Use a validated sidecar record and a narrow typed
reference if/when the UI needs one; render a bounded summary in the prompt meanwhile.

A particularly important mismatch: native `done` normalizes to `trash`. Neither
means Built, Deployed, Proven, or acceptance. Do not map those ADR states to a Done
column. Evidence of a merge, environment/image deployment, and actual runtime proof
remain different claims, with the last supported status derived from citations.

## Retained contract and ADR alignment

The approved v1.2 schema is copied unchanged to `adr-backlog-intake-v1.2.schema.json`
from a product checkout of the legacy source repository, path
`docs/work/adr-backlog-intake-v1.2.schema.json` (clean at inspection). Its schema name
stays unchanged for compatibility. It retains typed non-file citations, verification
reasons, reproducible derivation, predictions/falsifiers and `dispatch.state` fixed
to `not_authorized`. Importing this contract is not implementing its producer or
checker. ADR Markdown remains authoritative; generated intake is a projection.

Agent-tooling ADR-0012 covers guided setup/desk selection and suggested first skills.
An ADR-to-slice skill should use this contract and native board seams. The extracted
repository does not contain the complete old ADR set or an automatic ADR parser
or citation checker. The reviewed-intake reconciler is now delivered; see
[NATIVE-KANBAN-ADR-RECONCILER.md](NATIVE-KANBAN-ADR-RECONCILER.md). Preserve legacy ADR-0013's board-surface intent and ADR-0015's
citation-based lifecycle requirements when migrating their documents; do not imply
those capabilities are Built merely because the board can display their text.

## Delivered intake reconciliation and remaining work

1. Reuse the approved intake validator and navigation reuse assessment to produce
   reviewed slices; keep unresolved evidence explicit.
2. Delivered: `task reconcile-intake` reconciles validated reviewed records through
   observation ingestion, retaining exact input bytes/digests, explicit source
   supersession, stable native cards and verified dependency mapping. This first
   slice is backlog-only and non-dispatchable; it is not an ADR parser/checker.
3. Display richer source/ADR/dependency details without changing dispatch authority.
4. Only if native Start is adopted, add a common version-bound approval/claim gate
   before it and any chat launch path. Test stale approvals, duplicates and concurrent
   claims. Native dependencies/auto-start must not bypass that gate.
5. Inventory/export remaining legacy cards, map every active item and its evidence,
   reconcile counts, then disable legacy writers. Do not maintain dual card stores.

Acceptance: an approved ADR slice appears once, replays without duplication, shows
its owner/pin and unresolved evidence, links dependencies, cannot dispatch without
separate approval, and cannot claim Proven from a board move or completed agent turn.
