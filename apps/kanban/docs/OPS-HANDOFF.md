# Kanban continuation handoff — 2026-09-23

## Ownership and intent

The Principal requested committing this fork, so that a parallel session could continue
Kanban while the originating session stays on navigation and memory tooling.
The board projects work and evidence. Manual/user-approved dispatch is desired,
but approval-bound execution of imported ADR work is not implemented yet.

## Checkout and saved code

Physical checkout: `<operator workspace>/cline-kanban-source` (an operator-local clone; the path is not part of the product)
Branch: a local trial branch
Implementation baseline: the K8 commit.
K7 adds immutable external work observations; K8 guards projected cards and
retains dependency visualization. Read `docs/ops-local-delta-manifest.md` and
`docs/ops-chat-projection.md`, plus AGENTS.md, before editing.
The source is committed locally. Upstream is fetch-only; its push URL is DISABLED.
Do not push these changes to cline upstream. K7/K8 patches are also committed in
the legacy source repository's records. A full branch Git bundle is retained on the external volume under
kanban-handoff-backups. No GitHub fork has been provisioned by this handoff.

## Live instances — preserve the original session

Original port 3484 uses kanban-trial-state and dist. It hosts a user-started Codex
session; do not restart it or reset its state without checking with the user.
Candidate port 3485 uses kanban-observer-state and dist-observer. The board is
http://127.0.0.1:3485/navigation-memory-product . It contains real observational
work plus explicitly labelled trial fixtures and ADR planning cards.
Both storage roots are under the physical operator workspace above.

The opt-in `scripts/capture_codex_board.py` in the sibling legacy checkout watches
one explicitly bound Codex session. Its binding is in
kanban-observer-state/binding.json. Do not reuse that session identity for your
session. It is not a global auto-discovery/admission mechanism. Turn completion
means Review, not success/acceptance. No global host hook was installed.

## Proven boundaries and remaining work

- task observe accepts byte-linked immutable ordered observations; replay is
  idempotent. It does not launch agents or admit memory access.
- obs_ cards are read-only; task start and worktree creation refuse them.
- Dependency arrows work, but native dependencies are not planning authority:
  stage transitions can reorient/drop edges and the tested native model accepted
  a cycle. Ordinary cards can auto-start when dependencies complete.
- Native task create can populate Backlog but lacks ADR idempotency/approval
  semantics. Do not turn the current read-only cards into dispatchable ones by
  removing guards. Introduce explicit approved work and preserve stable identity.
- ADR intake hook/skill, automatic reconciliation, durable dependency semantics,
  atomic execution claims and version-bound user approval remain to be built.
- Workspace identity is canonical Git checkout root, not logical epic/repo remote.
  Keep initiative metadata distinct from execution workspace selection.

## ADR trial and contract

Sibling legacy checkout: navigation-memory-product. Accepted integration branch:
the product integration branch; fetch GitHub and inspect concurrent work before branching.
Read docs/work/ADR-BACKLOG-INTAKE.md and the v1.2 schema. ADR Markdown remains
source; generated intake cannot assert dispatch authorization.
Private receipts: kanban-observer-state/adr-intake-trial-01. Four read-only
Backlog cards: ADR0015 checker/conversion and ADR0014 S0/S1. ADR0016 temporal
continuity is separately Proposed/deferred. No implementation session was launched.
Do not overwrite private receipts or silently update a sealed observation.

## Build and verification

Use a separate output directory to avoid the active original runtime:
`KANBAN_BUILD_OUTDIR=dist-observer node scripts/build.mjs`
`npm --prefix web-ui run build -- --outDir ../dist-observer/web-ui`
Prior K8 validation: root/UI typechecks, 23 core tests, 22 card tests; the legacy capture
script had five tests. Re-run relevant checks for new edits; prior results do not certify
new work. All new state/checkouts/caches must stay on the mounted external volume; no local fallback.
