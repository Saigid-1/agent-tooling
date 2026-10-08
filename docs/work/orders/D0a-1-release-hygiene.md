# D0a-1 — Release hygiene: licence, notice, DCO, no private paths, and a public-readiness audit

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md); the release decisions are recorded in the Principal's release document (AT-0005/AT-0006). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: one arm (docs, files and one tree test). The Coordinator meets it with mutation-RED, and Verification reviews.

Status: frozen 2026-10-04 (DEMO-0 draft r4, Coordinator). Verification ruled D0a-1 freezable beside the T12b arms, with conditions 1–3 below. Base: main at the time of the freeze.

**The Principal's decisions** (2026-10-04, in the Coordinator's session, verbatim; a bracketed phrase replaces a private-history reference):
- "v0.4.0 it is, fresh public history makes more sense."
- "[the path-scrub pr] was expressly for this public cutover, so we should merge into the version that we push to the new account."
- The ghcr package is public on another of his accounts.
- The licence was decided on 2026-10-03: AGPL-3.0-only for the whole tree, a root LICENSE and a NOTICE, the Kanban fork keeping its Apache headers, a DCO and no CLA.

**Problem (measured at that base).**
- There is no root `LICENSE`, `NOTICE` or `CONTRIBUTING.md`.
- The `pyproject.toml` files carry no licence field.
- 13 tracked files contain operator-local paths. The path scrub, an open pull request (20 files, +47/−44), is 154 commits behind. `git merge-tree` with main gives no conflicts, and the merged tree has 0 such files.

## Properties
- **H1. The path scrub merged.** The arm brings the path-scrub pull request onto its branch: merge its branch, or apply its diff, resolving onto main. The pull request then closes as superseded by this slice's merge. The seven code and test sites that refuse `/Volumes/` by design stay, as the path scrub kept them.
- **H2. Root `LICENSE`.** It is the verbatim GNU AGPL-3.0 text from `https://www.gnu.org/licenses/agpl-3.0.txt`, 34,523 bytes, sha256 `0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0`.
  - The Coordinator fetched it with the Principal's permission (2026-10-04) and supplies it to the arm.
  - It is NOT GitHub's licenses-API copy, which re-wraps two lines of the appendix.
  - "-only" is stated in the README's licence line (D0a-2) and in the package metadata (H5).
- **H3. `NOTICE`.** It names each third-party component present in the tree or the images, with its licence:
  - Cline Kanban: Apache-2.0, at `apps/kanban/LICENSE`, kept with its headers;
  - SCIP: Apache-2.0;
  - Serena: MIT;
  - OpenTelemetry: Apache-2.0;
  - Grafana Tempo: AGPL-3.0, as a sidecar image the manifest references and our images do not contain.
  - **The agents image's bundled CLIs (Verification, at the freeze).** D0b publishes this image and the demo runs it, so NOTICE names both CLIs with their declared licence fields, read from the npm registry and from the package inside the pinned image:
    - `@openai/codex`: Apache-2.0, pinned 0.159.2;
    - `@anthropic-ai/claude-code`: `"SEE LICENSE IN README.md"`. That is Anthropic's own terms, not an OSI licence; pinned 2.1.280.
  - **Condition 3:** the Kanban fork point is named by its **upstream** commit in `https://github.com/cline/kanban`, verified to exist upstream (a public API read), never by a private-history sha. The candidate recorded in `apps/kanban/docs/ops-local-delta-manifest.md` is `abd4912c27ce6b7f18b5a8106c145fd838e90cc4`. The Kanban revision then recorded in `docs/EXTRACTION.json` is the local branch head, not an upstream commit; the arm verifies both and names what it finds.
  - The arm lists any component it finds in the tree or the images that is not in the list above, with its licence, under AMBIGUITY.
- **H4. `CONTRIBUTING.md`.** It covers how to contribute and the DCO sign-off rule (`git commit -s`; every commit carries `Signed-off-by:`), and embeds the Developer Certificate of Origin 1.1.
  - The DCO text is verbatim from `https://developercertificate.org/`, 1,366 bytes, sha256 `f7ac75b443f4ca16b503241344b41aeff9503b0c30bedc2b119551d83cb0fa90`. The Coordinator supplies it.
  - There is no CLA.
- **H5. The package licence.** Both `pyproject.toml` files carry `license = "AGPL-3.0-only"`, a PEP 639 SPDX expression; the arm checks the build backend accepts it. `apps/kanban` keeps its Apache-2.0 `package.json` licence.
- **H6. The tree falsifier (condition 1).** It is a test over the tracked files (`git ls-files`) that matches **classes, never literal names**:
  - a drive mount under `/Volumes/<name>`;
  - a home directory under `/Users/<name>` or `/home/<name>`;
  - a personal email (`<local-part>@<provider>`, with the project's own public addresses on a reviewed allowlist);
  - a session-id shape (`local_<8 hex>-<4 hex>-…`).

  The test itself contains none of the names it forbids. The design-intended sites, the seven that refuse `/Volumes/`, are allowlisted by file and line, with a reason each.
- **H7. The public-readiness audit (condition 2).** `docs/work/PUBLIC-READINESS-AUDIT.md` is a table, one row per finding: file, line, class and why it matters, plus an empty **Principal decision** column (keep, scrub or remove).
  - It covers:
    - session ids and binding or episode hashes in docs;
    - tenant and desk names;
    - `docs/work`: orders, ADRs, census, ARM-HEADER;
    - `records/`;
    - `apps/kanban/.plan`;
    - any committed JSON under `config/` or `deploy/examples/` with a real name in it.
  - The method pieces AT-0006 wants public (orders, census, ARM-HEADER) are marked "scrub, keep", never "remove".
  - The audit file contains no private value itself: it cites file:line and class only.
  - Nothing is removed by this slice except what the path scrub removes. The decisions are the Principal's, and a later slice applies them.
  - **A required row (Verification, at the freeze): the agents image redistributes `@anthropic-ai/claude-code` under Anthropic's terms.** The Principal's decision is one of:
    - (a) publish as is;
    - (b) publish the agents image without claude-code, and install it at first run as a documented step;
    - (c) do not publish the agents image.

    The row carries the npm licence field, the pinned version, and a pointer to the package's README terms, cited, not copied.
- **H8. Carried into D0b (condition 3, recorded here so it is not lost).** The released images' `org.opencontainers.image.revision` is the PUBLIC repo's tag commit, and `org.opencontainers.image.source` is the public URL. The OCI label is the AGPL source offer, so it must resolve for a stranger.
  - **D0b does not push the agents image** until H7's claude-code row has the Principal's decision. With (b), D0c's first run documents the claude-code install step and times it inside the 10 minutes. With (c), D0c's launched task needs a host-run harness.

## Falsifiers
- `LICENSE`'s sha256 is not `0d96a4ff…abcb0`.
- `NOTICE` omits a component present in the tree or the images, including either agents-image CLI, or names the Kanban fork point by a commit that does not exist upstream.
- The audit lacks the claude-code redistribution row.
- `CONTRIBUTING.md`'s embedded DCO text differs from the pinned bytes.
- A `pyproject.toml` lacks `license = "AGPL-3.0-only"`.
- H6's test passes on a tree with one injected path of each class (the Coordinator mutates); fails on the scrubbed tree; or contains a forbidden name itself.
- The audit contains a private value, omits a class named in H7, or a row lacks a decision column.
- Any product code or test changes outside the path scrub's diff and H6's new test.

## Write scope
- **The arm:**
  - the path scrub's files;
  - the new files `LICENSE`, `NOTICE`, `CONTRIBUTING.md` and `docs/work/PUBLIC-READINESS-AUDIT.md`;
  - the two `pyproject.toml` licence fields;
  - a new test under `tests/docs/` for H2–H6.
- **Not in scope:** the README (D0a-2, after T12c), the image labels (D0b), and any removal the audit proposes.
