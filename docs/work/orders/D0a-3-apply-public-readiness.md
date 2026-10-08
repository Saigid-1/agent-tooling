# D0a-3 — Apply the public-readiness decisions: audit defaults, licences, copyright, source offers

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md); the release decisions are in the Principal's release document (AT-0005/AT-0006). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: one arm (docs, files and tree edits, plus one build-system line). The Coordinator meets it with mutation-RED, and Verification reviews.

Status: frozen 2026-10-04 (DEMO-0 draft r5, Coordinator). Verification ruled it freezable with its additions (2026-10-04). Base: main at the merge of D0a-1's pull request (D0a-1 merged).

**The Principal's decisions** (2026-10-04, direct in the Coordinator's session):
- **Audit class defaults:** "Approve these defaults (Recommended)".
  - Session ids and binding or episode hashes: scrub.
  - Tenant and desk names: replace with generic examples.
  - Private repo names and references to private commits, PRs or runs: scrub.
  - Orders, census and ARM-HEADER (the method): keep, scrubbed.
  - `records/`: remove from the public tree.
  - `apps/kanban/.plan` copies of third-party docs: remove.
  - Upstream Kanban files byte-identical to upstream: keep as upstream has them.
- **The fork's own changes under `apps/kanban`:** "Apache-2.0 (Recommended)".
- **Copyright holder:** "Andrew Wedding".
- The two rows already decided in D0a-1's audit (the agents image; `claude-agent-sdk`) are applied by D0f and D0b, not here.

## Properties
- **A1. Every H7 row carries its applied decision.** `docs/work/PUBLIC-READINESS-AUDIT.md`'s decision column is filled for every row, by its class default or a recorded row override. The tree reflects each decision.
- **A2. Scrubs.**
  - Session ids, binding and episode hashes, tenant and desk instance names, private repo names, and private-history references (commits, PRs, runs) are removed or replaced in every file whose row says scrub or keep-scrubbed. Generic examples replace them where the sentence needs one.
  - **The packaged rosters stay as product vocabulary** (Verification): `assets/desk_roles.json` and `ops-roles.example.json`. Role names are not private desk names. Only real desk and tenant INSTANCES are replaced.
- **A3. Removals.**
  - `records/` and the `apps/kanban/.plan` third-party copies are removed from the tree.
  - **The regression check (Verification):** after the removals, the default and ext suites are green. Any test that reads a removed path (e.g. `records/extraction/*.json`) gets either a named regression-set exception, listed in the report, or the file is kept and its audit row says why.
- **A4. Kanban licence.**
  - `apps/kanban` states that the fork's own changes are Apache-2.0.
  - An Apache-2.0 §4(b) change notice points to the delta manifest (`apps/kanban/docs/ops-local-delta-manifest.md`), naming the upstream fork point `abd4912c27ce6b7f18b5a8106c145fd838e90cc4`.
  - The rest of the tree stays AGPL-3.0-only, and the README and NOTICE say so.
- **A5. Copyright.** NOTICE carries "Copyright 2026 Andrew Wedding and the agent-tooling contributors", and CONTRIBUTING states the same holder. The README line is D0a-2's, at release.
- **A6. Concrete source offers (Verification).**
  - NOTICE records Serena as measured: `serena-agent` is GPL-3.0-or-later and its SolidLSP component is MIT. NOTICE names `https://github.com/oraios/serena` at the pinned commit as the corresponding source for the package installed in the images.
  - The Debian packages' source offer points at Debian's source archive (sources.debian.org / snapshot.debian.org) for the pinned versions.
  - NOTICE records the `@clinebot` measurement beside the Principal's reading: no licence field, and the declared repository `cline/sdk-wip` answers 404 publicly.
- **A7. `setuptools>=77`** in both `pyproject.toml` files' `build-system.requires`. This is a product change, with its own falsifier.

## Falsifiers
- H6 (`tests/docs/test_release_hygiene.py`) red on the result, or its allowlist entries for removed or scrubbed sites left stale.
- An H7 row without an applied decision, or a row whose decision the tree does not reflect: a scrubbed value still present, a removed path still tracked.
- A "keep, scrubbed" file still containing a value of a scrubbed class.
- A packaged roster's role vocabulary changed.
- The default or ext suite red after A3, without a named exception.
- NOTICE without the Serena source URL at the pin, the Debian source-archive pointer, or the copyright line; `apps/kanban` without the Apache statement and the §4(b) notice.
- Either wheel failing to build with `setuptools>=77`, or its METADATA lacking `License-Expression: AGPL-3.0-only`.

## Write scope
- **The arm:**
  - the files the audit rows name;
  - `records/` and `apps/kanban/.plan` (removals);
  - `NOTICE`, `CONTRIBUTING.md` and `apps/kanban`'s licence and change-notice files;
  - the two `pyproject.toml` build-system lines;
  - the H6 test's allowlist (stale entries removed only);
  - the named regression-set exceptions for tests reading removed paths.
- **Not in scope:**
  - the README (D0a-2);
  - image labels and publishing (D0b);
  - the board's SDK (D0f);
  - any product code beyond the build-system line.
