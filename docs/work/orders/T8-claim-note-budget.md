# T8 — Claim notes can cite ordinary episodes; refusals say why

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

## Problem

The first desks seated after the T7 cutover (2026-10-02) could not write claim notes with `memory.propose`.

- **One desk.** Four attempts failed with `invalid_arguments`. That desk's seat traced the cause to `_impl/service/summary_evidence.py:31-32`. `qualifiers()` scans every sentence of every event in every cited episode and raises `ValueError('source qualifier budget exceeded; …')` once it has collected 64 negated sentences, or meets one longer than 2,000 bytes. An ordinary working episode (85 events) exceeds that, so it cannot be cited at all.
- **Another desk.** Its first two attempts failed the same way. It succeeded only after shrinking the note to one item.
- **Hidden reason.** `tool_failure` in `_impl/service/episodic_memory_tools.py` maps every `ValueError` to the generic `invalid_arguments` ("Check the tool input schema and bounded arguments"). The specific reason never reaches the caller, even though the budget message contains no source or desk identifier.

Qualifiers are a review signal: a summary should not hide a nearby "not". They are not an admission control, and today they make citation of real desk history impossible.

## Properties and falsifiers

- **P1: qualifiers come from what the note cites.** `memory.propose` collects source qualifiers from the cited events only: the `(episode_id, event_id)` pairs that the items' citations name, not every event of every cited episode. The capsule records which events were scanned.
  Falsifier: a qualifier drawn from an uncited event, or a negated sentence inside a cited event that is missing below the budget.
- **P2: over-budget is recorded, never refused.** When the cited events hold more qualifier sentences than the budget, or one is longer than the sentence bound:
  - the capsule keeps the qualifiers within the budget;
  - it records an explicit omission (how many sentences were omitted, per cited event);
  - it marks the capsule `review_required` with the reason `source_qualifiers_omitted`.

  `memory.propose` succeeds. The existing bounds on items (32), episodes (32) and text sizes remain refusals.
  Falsifier: a propose refused only for qualifier count or length; an omission that is not recorded; or a capsule with omitted qualifiers that is not `review_required`.
- **P3: refusals name the bound.** A bounded-input refusal from the memory tools returns a specific category (for example `bound_exceeded`), and guidance naming which bound and its limit (items, episodes, item text size, citation range, budget), in place of the generic `invalid_arguments`. Schema-shape errors keep `invalid_arguments`. No response includes a source text, a source path, a desk identifier or a binding key that the caller did not supply.
  Falsifier: a bounded refusal reported as plain `invalid_arguments`, or any of those identifiers leaking into an error.
- **P4: existing capsules and checks are unchanged.**
  - Capsules stored before this change read back and verify exactly as before.
  - The consolidation pre-flight (`consolidate_with` and `consolidate_chunked_with`) keeps refusing input it cannot represent before any paid call, now with the specific category.
  - Every existing test stays unmodified and green.

  Falsifier: an existing capsule that no longer verifies, a paid call made on unrepresentable input, or a modified existing test.

## Write scope

- **FEATURE:**
  - `packages/tooling/src/kp_agent_tooling/_impl/service/summary_evidence.py`;
  - `_impl/service/episodic_memory.py` (the propose path: pass the cited events and record omissions);
  - `_impl/service/episodic_memory_tools.py` (`tool_failure` categories);
  - `docs/MEMORY.md` (the claim-note bounds and error categories).
- **TEST:** new files under `tests/` next to the existing summary-evidence and episodic-memory tests (`tests/test_summary_evidence.py`, and the memory tool tests), driving the public tool surface (`memory.propose` through the memory MCP server or `EpisodicMemoryTools`) with synthetic episodes. No network, no Docker.
