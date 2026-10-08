# ADR-0012 — Guided startup and orientation

Owner: agent-tooling. Declared status: Accepted for the added scope below.
Derived status: Accepted; no implementation or deployment of the complete guided
experience is claimed. Date: 2026-09-25.

## Continuity and evidence

This is the agent-tooling continuation of ADR-0012 of the legacy source repository,
not a new claim that its historical requirements have all shipped. The original
decision, in that unpublished repository, remains historical authority for fresh
repository navigation and bounded history import. Its status was In progress.
It was not present in the extracted agent-tooling ADR directory before this update.
This continuation leaves the legacy checkout and its status log unchanged.

Principal ruling, verbatim from the current task, 2026-09-25:

> items to add to ADR: new user startup and orientation guided experience - some of this will simply be building on top of what cline offers and extending it, including importing a repo, configuring indexing, selecting/configuring personas (desks in our vernacular), and suggested first prompts (we can configure as skills, similar to the ecc onboarding skill, but using the richer tooling we expose).

## Decision

Provide one guided startup and orientation journey for humans and agents using
the portable product. Extend Cline's existing project selection, configuration,
chat and import surfaces. Reuse the portable setup, catalog, admission, index and
memory services; do not introduce a parallel registry, import engine or desk
binding path. The guided UI is optional: the same reviewed setup plan and receipt
must be usable through CLI/tool calls by an agent.

The journey includes:

1. **Import/select a repository.** Explain local versus container/remote paths,
   validate access, identify the source revision and default branch convention,
   preview the plan and register the repository. Distinguish a repository from a
   project or epic; do not silently collapse their scopes.
2. **Configure indexing.** Show the repository manifest and actual coverage.
   Offer literal/source navigation, Serena, language-appropriate SCIP, maintained
   document/RAG selection and runtime observation pointers according to available
   capabilities. Explain model downloads, resource needs, exclusions and refresh
   triggers before applying the selected plan. Show building, ready, stale,
   divergent and unavailable states with actionable reasons. A completed build
   does not establish semantic completeness or architectural correctness.
3. **Select/configure personas (desks).** Reuse Cline's persona affordances and the
   portable role/catalog machinery. Explain a role's responsibilities, doctrine,
   scope and shared memory. Keep model choice independent of desk identity and
   support concurrent sessions at the same desk. A suggestion or persona name is
   not admission: the user's selected desk is bound to the exact host session
   through the existing mechanism, with a visible receipt and resume state.
4. **Orient with useful first prompts.** Offer small, optional skills for an
   architecture walkthrough, a requirement-to-implementation reuse check, a
   bounded change-impact briefing and desk-memory continuity. These should use
   our manifest, revision-pinned navigation, evidence slices and memory scopes.
   ECC onboarding is an interaction analogy, not a dependency or an instruction
   to copy its implementation. Each skill states outcome, evidence boundary,
   applicable tools, fallback and a bounded context budget. Tool use is guided,
   not mandatory for its own sake.
5. **Verify and hand off.** Run a small chosen-repository journey and show source
   citations, coverage gaps, desk binding and recovery instructions. An optional
   history import has separate preview/consent, source selection and attribution.
   Completion means the selected capabilities were exercised; it does not imply
   unselected tools, production behavior or a remote hosting model were proven.

## Existing machinery to reuse

- [Fresh workspace setup](../FRESH-WORKSPACE.md): installed setup/navigation,
  catalog and bounded history pathways.
- [Container import](../CONTAINER-IMPORT.md): installed CLI, explicit config,
  memory and transcript mounts; synthetic single-operator image acceptance.
- [Session import](../memory/SESSION-IMPORT.md): preview, capture, continuation,
  ownership assertions and coverage receipts.
- [Deployment assessment](../COMPOSABLE-DEPLOYMENT.md): optional components,
  readiness and bare-install obligations.
- Cline project, chat, persona/configuration and import UI: inspect the current
  implementation and pattern-match reuse at slice intake before adding controls.

These are reusable foundations, not proof of the complete orientation journey.
No new model call, automatic repository-wide ingestion, blanket admission or
outbound data consent follows from this ADR.

## Acceptance and falsifiers

- Given a fresh install and unfamiliar repository, when a user completes the
  guided plan, source navigation returns committed evidence at the displayed
  revision without operator-specific paths. A hidden manual conversion or
  hardcoded known repository falsifies the outcome.
- Given an unavailable index provider, the user can see the reason and use an
  available fallback. Showing all tools as ready or treating missing results as
  absent code falsifies readiness.
- Given a selected desk, CLI and native MCP resolve the same exact-session
  binding; resume preserves or explicitly re-establishes it. A persona label
  without a binding receipt falsifies completion. Concurrent desk occupancy is
  allowed and is not a failure.
- Given a suggested first prompt on an unfamiliar codebase, it produces a bounded
  evidence-backed result with unresolved questions and follow-up navigation.
  Merely reproducing the calibration example, or citing unobserved behavior,
  falsifies the orientation claim.
- Given a interrupted setup, restart resumes or presents a safe re-plan without
  duplicate imports, silently changed scope or inferred authorization.

## Status and next delivery

Accepted by the quoted ruling. The next work record must name the existing Cline
seams, reusable public operations, ownership and smallest end-to-end slice before
implementation. Record Built only against an implementation merge, Deployed only
against an environment/image receipt, and Proven only against a walkthrough from
that environment. This documentation merge advances none of those states.
