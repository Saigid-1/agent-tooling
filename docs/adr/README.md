# Agent Tooling architecture decisions

Numbered decisions inherited from the legacy source repository retain their identity when explicitly continued
here; this is not a claim that the entire legacy ADR set was migrated.

| Decision | Declared | Derived | Evidence |
| --- | --- | --- | --- |
| [0012 — Guided startup and orientation](0012-fresh-user-onboarding.md) | Accepted (added scope) | Accepted | Verbatim Principal ruling in the record; full guided experience not implemented |
| [AT-0001 — Native planning and delivery](AT-0001-native-planning-and-delivery.md) | Built | Built | Implementation merge and owned validation record; no production deployment claim |
| [AT-0002 — Desk profiles and memory views](AT-0002-desk-profiles-and-memory-views.md) | Deployed | Deployed | Merged; image and local runtime receipt (kept off-tree); host-emitted hook still pending |
| [AT-0003 — Distributable package: one image, one manifest](AT-0003-distributable-package.md) | Accepted (partly superseded by AT-0004) | In progress | S1, S5 and S3 built; S2 and S4 folded into AT-0004 T6 |
| [AT-0004 — Portable Kanban suite: core, extension, desks and bindings](AT-0004-portable-kanban-suite.md) | Accepted | In progress | T1, T2, T3, T4, T5, T5b, T6a, T6b, T7a, T7b and M1 built; T7 cutover done 2026-10-02; T8 order frozen |

Statuses require evidence: Proposed → Accepted (verbatim ruling) → In progress
(owned work record) → Built (implementation merge) → Deployed (environment,
release and image) → Proven (same-environment walkthrough/receipt or dated
sign-off). Superseded names the replacement; Retired requires implementation
retirement evidence. A documentation merge establishes Accepted at most. A
missing citation limits the derived status to the last supported stage.
