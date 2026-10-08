# Public-readiness audit (D0a-1 H7)

Order: [D0a-1](orders/D0a-1-release-hygiene.md), property H7. This audit lists what the public v0.4.0 release would publish that is private, internal or third-party. The Principal decides each row.

- **What a row holds.** Each row cites a file, a line and a class. It never repeats the value it found, so no session id, hash, tenant, repository name, path or address appears here.
- **The decision column.** The **Principal decision** column holds each row's applied decision. D0a-3 filled it on 2026-10-04 with the Principal's class defaults (below), or with a row override and its reason. The claude-code and claude-agent-sdk rows were decided directly; D0b and D0f apply them.
- **"Scrub, keep."** AT-0006 wants some method pieces public: the orders, the census and ARM-HEADER. Their rows are marked **scrub, keep**: the private values are scrubbed and the record stays. No row marks them "remove".
- **What changed.** D0a-1 removed nothing beyond the path scrub's diff. D0a-3 applied the decisions. Line numbers still refer to the tree D0a-1 audited.
- **The tree audited.** The D0a-1 arm's branch after the path-scrub merge. Line numbers refer to that tree. A range such as `1–44` means the whole file. `12 (+4)` means the first line and the number of further lines of the same class.

## Decisions applied (D0a-3, 2026-10-04)

The Principal approved these class defaults, and D0a-3 applied them to every row:
- session ids and binding or episode hashes: scrub;
- tenant and desk instance names: replace with generic examples (the packaged role rosters stay as product vocabulary);
- private repository names and references to private commits, pull requests or runs: scrub;
- orders, census and ARM-HEADER: keep, scrubbed;
- `records/`: remove;
- the `apps/kanban/.plan` copies of third-party documentation: remove;
- upstream Kanban files byte-identical to upstream: keep.

A row override records its reason in the row. The decision words: **Scrubbed** and **Kept, scrubbed** (the values were removed or replaced and the file stays), **Removed**, **Keep** (the class default or an override keeps the value), **Replace** (applied by another slice), and **DECIDED** (decided directly by the Principal).

## Classes

| Class | Meaning |
| --- | --- |
| SESSION | A session id: the `local_<8 hex>-…` shape, or a native harness session UUID. |
| HASH | A hash of private state: a desk binding, an export, a registry snapshot, a private receipt or catalog. |
| TENANT | A real tenant name. |
| DESK | The operator's internal desk and seat instances, or a Principal ruling quoted in prose. Role names from the packaged rosters (Coordinator, Verification and the rest) are product vocabulary, not instances. |
| REPO | A private repository name. |
| PRIVATE-REF | A commit, branch, image id, pull request or CI run of the private history. The fresh public history will not contain it, so the reference dangles. |
| PROCESS | An internal process record: an order, a census, the arm header, an ADR work record, an inventory. |
| RECORD | An operator trial or acceptance record. |
| UPSTREAM | A file from upstream Cline Kanban, already public there. |
| 3P-DOC | A copy of third-party documentation. |
| 3P-TERMS | A third-party component whose terms need a decision before publication. |
| HOME | A home-directory path. |
| EMAIL | An email address. |

## 1. Required: the agents image redistributes claude-code

Verification required this row at the freeze.

| File | Line | Class | Why it matters | Principal decision |
| --- | --- | --- | --- | --- |
| deploy/Dockerfile | 153 | 3P-TERMS | The `agents` target installs `@anthropic-ai/claude-code`, **pinned 2.1.280**; the pin also appears at `deploy/image/agents/package.json:6` and `deploy/image/agents/package-lock.json:13`. **npm licence field: `SEE LICENSE IN README.md`** (npm registry and lockfile, read 2026-10-04). Its platform binary packages declare `SEE LICENSE IN LICENSE.md`. These are Anthropic's own terms, not an OSI licence. The terms are the `README.md` and `LICENSE.md` in the npm package `@anthropic-ai/claude-code@2.1.280`, <https://www.npmjs.com/package/@anthropic-ai/claude-code/v/2.1.280>. They are cited here, not copied. Publishing the agents image redistributes the CLI under those terms. **Options:** (a) publish the agents image as is; (b) publish it without claude-code and install claude-code at first run as a documented step, which D0c times inside its 10 minutes; (c) do not publish the agents image, in which case D0c's launched task needs a host-run harness. D0b does not push the agents image until this row is decided. || DECIDED 2026-10-04: no image containing Claude Code or Codex is published (the `agents` target is local-build only); "we cannot provide nor authoritatively ensure license compliance". |

## 2. Other third-party terms

| File | Line | Class | Why it matters | Principal decision |
| --- | --- | --- | --- | --- |
| deploy/Dockerfile | 73 | 3P-TERMS | The pinned Serena commit declares GPL-3.0-or-later for the `serena-agent` package as a whole; its SolidLSP component is MIT. The order expected MIT. The product, agents, ops and acceptance images carry Serena in `/opt/serena`, so publishing them distributes GPL-3.0-or-later software and its source obligations. Serena's LICENSE names the last MIT-licensed commit (tag `mit-final`), which is an alternative pin. NOTICE records the licence as measured. | Keep the Serena pin. NOTICE records serena-agent as GPL-3.0-or-later with its SolidLSP component MIT, and names https://github.com/oraios/serena at the pinned commit as the corresponding source (D0a-3 A6). |
| apps/kanban/package.json | 94 | 3P-TERMS | The board depends on `@clinebot/core` and `@clinebot/shared` 0.0.38, and through them on `@clinebot/llms`. None of the three declares a licence, in the npm registry or the lockfile (`apps/kanban/package-lock.json:1298`, `:1347`, `:1404`). A package with no declared licence states no grant to redistribute. The board build bundles the dependencies it imports into `dist`, which the product-based images carry. | Accept, remedy on backlog (external pull via D0f's action). Principal decision, 2026-10-04, relayed by Verification: "Let's accept the risk on clinebot, but maintain an external pull mechanism on the backlog in the event it gets flagged. Simple enough to remedy quickly but doesn't add another slice that may be unnecessary". Trigger: a licence flag from Cline or a user. The packages stay bundled; NOTICE keeps the measurement (no licence field; the declared repository `cline/sdk-wip` answers 404 publicly) beside his reading (D0a-3 A6). No code change. |
| apps/kanban/package-lock.json | 225 | 3P-TERMS | `@anthropic-ai/claude-agent-sdk` 0.2.128 declares `SEE LICENSE IN README.md` (Anthropic's terms). It is reached through `@clinebot/llms` and `ai-sdk-provider-claude-code`. MEASURED at the D0a-1 meet (Coordinator and Verification, 2026-10-04, in the built product image and the live ops image): the board bundle `/app/dist/cli.js` contains it. `cli.js.map` lists `@anthropic-ai/claude-agent-sdk/sdk.mjs` among its sources, and the bundle also carries `@clinebot/*` modules. Principal's decision: it is not redistributed in any published image, and D0f externalizes it behind a user-initiated install action with a no-warranty, no-licence notice. || DECIDED 2026-10-04: not in any published image; users obtain it themselves via a clickable install action with a no-warranty, no-licence notice (D0f). NOTICE states that it is not redistributed in any published image (D0a-3 A6). |
| apps/kanban/web-ui/package-lock.json | 5745 | 3P-TERMS | `posthog-js` 1.357.1, a direct web UI dependency, declares `SEE LICENSE IN LICENSE`. The fork disables PostHog egress. Whether its code remains in the built web UI bundle was not measured. | Keep, unchanged: no class default and no Principal decision covers this row. NOTICE already lists it. Escalated in the D0a-3 report. |
| deploy/Dockerfile | 43 | 3P-TERMS | The images install Debian `git` (GPL-2.0-only) on the Debian base system, so publishing binary images redistributes GPL software. GPL-2.0 section 3 requires the corresponding source or a written offer. The decision is how the published images meet that, for example by pointing to Debian's source archive for the pinned packages. | Keep. NOTICE points to Debian's source archive (sources.debian.org, snapshot.debian.org) for the installed versions (D0a-3 A6). |
| apps/kanban/package.json | 72 | 3P-TERMS | The fork keeps Apache-2.0 (H5). Relative to the upstream fork point, this repository modifies 57 upstream files and adds 83 under `apps/kanban`. Apache-2.0 section 4(b) requires modified files to carry prominent change notices; today they do not, and NOTICE points to the delta manifest instead. The tree also does not state the licence of the added and modified files: Apache-2.0, like the package, or AGPL-3.0-only, like the rest of the tree. | Apache-2.0 for the fork's own changes. `apps/kanban/NOTICE` carries the section 4(b) change notice; it names the fork point and points to the delta manifest, which lists every changed file (D0a-3 A4). |
| apps/kanban/web-ui/src/assets/open-targets/ | 11 SVG files | 3P-TERMS, UPSTREAM | Upstream ships logos of third-party editors and terminals. They are trademarks, not licensed works. | Keep: byte-identical to upstream at the fork point (class default; checked by Git blob). |
| apps/kanban/package.json | 5–8 | 3P-TERMS | The fork kept upstream's `"publishConfig": {"access": "public", "provenance": true}` and had no `"private": true`. The board's `dependencies` reach `@anthropic-ai/claude-agent-sdk` (through `@clinebot/llms` and `ai-sdk-provider-claude-code`), so an `npm publish` from apps/kanban would publish a package that pulls the SDK: the redistribution D0f prevents, with a provenance attestation. Found by Verification at the D0f meet. | Replace (D0f): `"private": true`, `publishConfig` removed; tests/docs/test_kanban_packages_unpublishable.py keeps every apps/kanban package.json private with no publishConfig. |
| deploy/image/opencode/LICENSE | 1–21 | 3P-TERMS | The optional `opencode` image target (O3) installs the OpenCode CLI, npm package `opencode-ai` **pinned 1.18.34** (`deploy/image/opencode/package.json`, its lockfile, and `ARG OPENCODE_VERSION` in deploy/Dockerfile). **npm licence field: `MIT`**, and the upstream LICENSE at tag `v1.18.34` of <https://github.com/anomalyco/opencode> (redirected from `sst/opencode`) is the MIT License; both read 2026-10-06. The platform binary packages it installs, `opencode-linux-*` 1.18.34, which carry the binary itself, have **no licence field** in their npm metadata and no licence file. This file is that upstream LICENSE, byte-identical. | DECIDED 2026-10-06 (the maintainer): "Use upstream MIT". The upstream repository's MIT LICENSE at tag v1.18.34 is the licence basis of the `opencode-linux-*` platform binary packages. The `opencode` image carries the LICENSE text at `/usr/local/share/doc/opencode/LICENSE`; NOTICE records `opencode-ai` (MIT) and the platform packages with that basis. |

## 3. Session ids and binding or episode hashes in docs

| File | Line | Class | Why it matters | Principal decision |
| --- | --- | --- | --- | --- |
| apps/kanban/docs/harness-session-resolution.md | 38 | SESSION | An operator session id in an operator trial record. H6 allowlists this line until the decision. | Scrubbed (D0a-3 A2). H6's pending entry is removed. |
| apps/kanban/docs/harness-session-resolution.md | 82 | SESSION | The same operator session id, naming a DeskRun export. H6 allowlists this line until the decision. | Scrubbed (D0a-3 A2). H6's pending entry is removed. |
| apps/kanban/docs/harness-session-resolution.md | 123 | SESSION | A native Codex session id from a live capture trial. | Scrubbed (D0a-3 A2). |
| apps/kanban/docs/harness-session-resolution.md | 36 | HASH | The SHA256 of a private registry snapshot file. | Scrubbed (D0a-3 A2). |
| apps/kanban/docs/harness-session-resolution.md | 51 (+1, at 96) | HASH | An abbreviated form of the same registry hash. | Scrubbed (D0a-3 A2). |
| apps/kanban/docs/harness-session-resolution.md | 84 | HASH | The SHA256 of a private DeskRun export. | Scrubbed (D0a-3 A2). |
| apps/kanban/docs/harness-session-resolution.md | 93 (+1, at 95) | HASH | The SHA256s of private trial receipts. | Scrubbed (D0a-3 A2). |
| apps/kanban/docs/ops-desk-temporal.md | 43 | SESSION | An operator host session id. H6 allowlists this line until the decision. | Scrubbed (D0a-3 A2). H6's pending entry is removed. |
| apps/kanban/docs/ops-desk-temporal.md | 63 | HASH | The SHA256 of the same private export. | Scrubbed (D0a-3 A2). |
| docs/memory/SESSION-IMPORT-ACCEPTANCE.md | 42 | SESSION, DESK | A native Codex session id, named as an internal desk's session. | Scrubbed (D0a-3 A2). |
| records/telemetry/core-consumer-handoff.json | 8 | SESSION | The native Codex session id that produced the handoff. | Removed with `records/` (D0a-3 A3). |
| docs/adr/0012-fresh-user-onboarding.md | 13 | HASH | The SHA256 of a source document in the private legacy graph. | Scrubbed (D0a-3 A2). |
| config/desk-context/catalog.example.json | 36 (+3, at 37, 52, 53) | HASH | Desk binding keys in the example catalog; see section 9. | Scrubbed (D0a-3, Verification's ruling at the meet, overriding the kept row): a placeholder repository key, a synthetic revision and the binding keys the product derives from them; the T8 golden is regenerated by its generator under a named exception (`tests/d0a3_ruled_exceptions.py`). |
| config/desk-context/memory.example.json | 4 (+1, at 17) | HASH | The same binding keys, as memory fixture keys; see section 9. | Scrubbed (D0a-3, Verification's ruling at the meet, overriding the kept row): a placeholder repository key, a synthetic revision and the binding keys the product derives from them; the T8 golden is regenerated by its generator under a named exception (`tests/d0a3_ruled_exceptions.py`). |
| records/desk-menu/2026-09-29-capture-scope.json | 6 | HASH | The SHA256 of the private desk catalog. | Removed with `records/` (D0a-3 A3). |

## 4. Tenant and desk names

| File | Line | Class | Why it matters | Principal decision |
| --- | --- | --- | --- | --- |
| records/desk-menu/2026-09-29-capture-scope.json | 5 (+1, at 22) | TENANT | The operator's real tenant name. The other tenant values in the tree are placeholders. | Removed with `records/` (D0a-3 A3). |
| README.md | 5 (+1) | DESK | The README names the legacy source repository and a Principal ruling. The README belongs to D0a-2's scope; it is listed here so the decision covers it. | Replace (class default), applied by D0a-2 at release: the README is outside D0a-3's scope. |
| docs/CUTOVER.md | 3 (+10) | DESK | A cutover record: Principal rulings and internal desk names throughout. | Scrubbed (D0a-3 A2). |
| docs/DESK-ROLE-ROSTER.md | 27 | DESK, REPO | Names an internal desk and its private repository as a default. | Scrubbed (D0a-3 A2). |
| docs/FRESH-WORKSPACE.md | 49 (+1) | DESK | Names internal desk instances in product guidance. | Scrubbed (D0a-3 A2). |
| packages/tooling/src/kp_agent_tooling/assets/FRESH-WORKSPACE.md | 49 (+1) | DESK | The packaged copy of the same guide; it ships in every wheel and image. | Scrubbed (D0a-3 A2). |
| docs/CLAUDE-CAPTURE-HOST.md | 5 | DESK | Names an internal desk's host session as the installation scope. | Scrubbed (D0a-3 A2). |
| docs/SESSION-HOOK-CONTINUITY.md | 3 | DESK | Names internal desks' project settings. | Scrubbed (D0a-3 A2). |
| docs/MEMORY-RECONCILIATION.md | 3 | DESK | Principal ruling in prose. | Scrubbed (D0a-3 A2). |
| docs/DESK-CHARTER-IMPORT.md | 27 | DESK | Principal charters as source evidence. | Scrubbed (D0a-3 A2). |
| docs/workspace-capture.md | 56 | DESK | Names internal desks' hooks in operator guidance. | Scrubbed (D0a-3 A2). |
| docs/memory/DELEGATION-ATTRIBUTION.md | 59 | DESK | Uses internal desk names as examples. | Keep: the names are roster role names (Coordinator, Verification, Deployment Engineering), product vocabulary (D0a-3 A2); no desk instance. |
| docs/DOCKER.md | 1185 | DESK | Names the operator's desks in the image-target table. | Scrubbed (D0a-3 A2). |
| docs/INDEPENDENT-IMAGE-ACCEPTANCE.md | 17 | DESK | Names a desk instance's coordinator handoff. | Scrubbed (D0a-3 A2). |
| docs/TRIAL-INDEPENDENT-IMAGES.md | 16 | DESK | Names an internal desk. | Scrubbed (D0a-3 A2). |
| apps/kanban/docs/OPS-HANDOFF.md | 5 | DESK, RECORD | An operator handoff note: Principal request, live instances and ports on the operator's machine. | Scrubbed (D0a-3 A2). A RECORD outside `records/` is scrubbed, not removed (D0a-3 AMBIGUITY). |
| apps/kanban/docs/ops-desk-temporal.md | 3 (+3) | DESK | Quotes a Principal message verbatim and names internal desks. | Scrubbed (D0a-3 A2). The Principal's quotes stay verbatim (Principal decision, 2026-10-04: "Verbatim in ADRs too"); a desk or tenant instance name inside a quote is bracketed and generalised. |
| apps/kanban/docs/ops-local-delta-manifest.md | 55 | DESK | Principal ruling in prose. NOTICE cites this file as the change record. | Scrubbed (D0a-3 A2). |
| apps/kanban/scripts/project-desk-run-export.py | 112 | DESK | Code text names the Principal grant. | Scrubbed (D0a-3 A2). Text only. |
| extensions/ops/README.md | 3 (+1) | DESK | The OPS extension's README names the legacy source repository and its desks. | Scrubbed (D0a-3 A2). |
| extensions/ops/src/kp_agent_tooling_ops/assets/verify-behavior.md | 85 (+1) | DESK | Packaged asset text names internal desks. | Scrubbed (D0a-3 A2). Text only. |
| extensions/ops/src/kp_agent_tooling_ops/_impl/lifecycle_matrix.py | 27 (+2) | DESK | Product code carries internal desk names in fixture rows. | Keep (override): the scenario text is part of the authored lifecycle model whose digest (`lifecycle.model_digest`) binds retained execution receipts, so a change is a behaviour change, and D0a-3 changes no product code. Escalated in the D0a-3 report. |
| extensions/ops/src/kp_agent_tooling_ops/_impl/observation_adapters.py | 6 | DESK | A docstring names internal desks. | Scrubbed (D0a-3 A2). Docstring only. |
| extensions/ops/src/kp_agent_tooling_ops/_impl/service/ops_tools.py | 116 | DESK | A tool description names an internal desk's fixtures. | Scrubbed (D0a-3 A2). Description text only; the tool's name and schema are unchanged. |
| packages/tooling/src/kp_agent_tooling/_impl/navigation_search_pages.py | 26 | DESK | A code comment names an internal desk's repository size. | Scrubbed (D0a-3 A2). Comment only. |
| packages/tooling/src/kp_agent_tooling/assets/desk_roles.json | 1–87 | DESK | The starter role roster mirrors the operator's own desk set. The roles are generic, but the set is the organisation's. | Keep: product vocabulary (D0a-3 A2). |
| packages/tooling/src/kp_agent_tooling/assets/roles/ops-roles.example.json | 1–51 | DESK | The example OPS roster, the same set. | Keep: product vocabulary (D0a-3 A2). |

## 5. Private repository names and private-history references

| File | Line | Class | Why it matters | Principal decision |
| --- | --- | --- | --- | --- |
| docs/EXTRACTION.json | 4 (+1, at 5) | REPO, PRIVATE-REF | The extraction source: a private repository and its commit. | Scrubbed (D0a-3 A2). |
| docs/EXTRACTION.json | 10 | PRIVATE-REF | The Kanban "source_revision" is the local branch head, not an upstream commit: the GitHub API answers 422 for it in `cline/kanban`. NOTICE names the upstream fork point instead. | Scrubbed (D0a-3 A2). The fork point replaces the local branch head. |
| docs/CUTOVER.md | 31 (+1, at 44) | PRIVATE-REF | A build commit and a local image id of the private deployment. | Scrubbed (D0a-3 A2). |
| docs/CUTOVER.md | 111 | REPO | A private repository name. | Scrubbed (D0a-3 A2). |
| docs/legacy-navigation-retirement.md | 5 (+2, at 6, 7) | REPO, PRIVATE-REF | A private repository, its path and its commits. | Scrubbed (D0a-3 A2). |
| docs/telemetry/CORE-CONSUMER-HANDOFF.md | 14 (+2, at 16, 18) | REPO, PRIVATE-REF | A private repository URL, branch and head. | Scrubbed (D0a-3 A2). |
| docs/SESSION-HOOK-CONTINUITY.md | 27 | PRIVATE-REF | Private-history baselines. | Scrubbed (D0a-3 A2). |
| docs/adr/0012-fresh-user-onboarding.md | 10 | REPO, PRIVATE-REF | A link into a private repository at a private commit. | Scrubbed (D0a-3 A2). |
| apps/kanban/docs/OPS-HANDOFF.md | 14 | PRIVATE-REF | A private branch and its baseline commit. | Scrubbed (D0a-3 A2). |
| apps/kanban/docs/ops-desk-temporal.md | 11 | PRIVATE-REF | A private legacy-repository commit. | Scrubbed (D0a-3 A2). |
| apps/kanban/web-ui/src/components/desk-session-dialogue.tsx | 113 | REPO | **Product UI**: an input placeholder shows private repository names to every user. | Scrubbed (D0a-3 A2). The placeholder shows generic names. |
| extensions/ops/scripts/prepare_tooling_docker.py | 70 (+1, at 96) | REPO | Legacy launcher code keys on a private repository name and branch. | Scrubbed (D0a-3 round 4, Verification ruling; the round-1 override is withdrawn): the scope-table key is the example key `example-repo` and the pinned ref a placeholder; the operator's real key and branch come from run-time configuration (PREPARE_TOOLING_DOCKER_COORDINATION_REPO and _REF), never from the tree. Legacy launcher code: a one-time migration with no other use in this repository; it ships scrubbed. No test pinned the old key. |

## 6. docs/work: orders, census, ARM-HEADER, work records

Each order's identity is the private merge commit that froze it, and many orders cite other private-history commits. The rows add only what is specific to each file.

| File | Line | Class | Why it matters | Principal decision |
| --- | --- | --- | --- | --- |
| docs/work/orders/ARM-HEADER.md | 1–44 | PROCESS, DESK | **Scrub, keep.** The rules every arm follows: internal desk names and the dispatch method. | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/ARM-HEADER.md | 17–22 | PROCESS, HASH | **Scrub, keep.** The 2026-10-04 blindness-lapse disclosure. It cites the off-tree record and the transcript checksums by hash, and relays a Principal ruling. | Kept, scrubbed (D0a-3 A2). The two off-tree checksums are removed. |
| docs/work/orders/D0a-1-release-hygiene.md | 1–82 | PROCESS, DESK | **Scrub, keep.** This order. | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/D0a-1-release-hygiene.md | 7–11 | DESK | **Scrub, keep.** Principal decisions quoted verbatim, plus a statement about the Principal's other accounts. | Kept, scrubbed (D0a-3 A2). The quotes stay verbatim (frozen-order reading, round 2); the PR number inside one is bracketed. The statement about the Principal's other accounts is kept as written (Principal decision, 2026-10-04: "Keep it"). |
| docs/work/orders/M1-model-gateway.md | 1–46 | PROCESS | **Scrub, keep.** | Kept, scrubbed (D0a-3 A2). The Principal's quote stays verbatim (frozen-order reading, round 2). |
| docs/work/orders/S1-knowledge-platform-coverage.md | 1–47 | PROCESS, PRIVATE-REF | **Scrub, keep.** Cites a commit of the private legacy source repository. | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/S2-product-image.md | 1–67 | PROCESS | **Scrub, keep.** | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/S3-manifest-installer.md | 1–117 | PROCESS | **Scrub, keep.** Describes the private live runtime. | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/S5-restore-extracted-tests.md | 1–41 | PROCESS, PRIVATE-REF, DESK | **Scrub, keep.** Cites the private legacy commit and internal desks. | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/T1-extension-seam.md | 1–62 | PROCESS, DESK | **Scrub, keep.** | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/T2-desk-registry-bindings.md | 1–69 | PROCESS, DESK | **Scrub, keep.** | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/T3-launch-binding-harness-profiles.md | 1–88 | PROCESS | **Scrub, keep.** | Kept, scrubbed: the D0a-3 scan found no value of a scrubbed class. |
| docs/work/orders/T4-host-adapter.md | 1–59 | PROCESS | **Scrub, keep.** | Kept, scrubbed: the D0a-3 scan found no value of a scrubbed class. |
| docs/work/orders/T5-refresh-efficiency.md | 1–58 | PROCESS | **Scrub, keep.** | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/T5b-retention-references.md | 1–37 | PROCESS | **Scrub, keep.** | Kept, scrubbed: the D0a-3 scan found no value of a scrubbed class. |
| docs/work/orders/T6a-images-and-ci.md | 1–63 | PROCESS | **Scrub, keep.** | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/T6b-docs-sweep.md | 1–36 | PROCESS | **Scrub, keep.** | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/T7a-role-readiness.md | 1–97 | PROCESS | **Scrub, keep.** | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/T7b-board-wiring.md | 1–63 | PROCESS | **Scrub, keep.** | Kept, scrubbed: the D0a-3 scan found no value of a scrubbed class. |
| docs/work/orders/T8-claim-note-budget.md | 1–42 | PROCESS | **Scrub, keep.** | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/T9-capture-continuity.md | 1–52 | PROCESS | **Scrub, keep.** | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/T9b-memory-store-volume.md | 1–317 | PROCESS, DESK | **Scrub, keep.** | Kept, scrubbed: the D0a-3 scan found no value of a scrubbed class. |
| docs/work/orders/T10-read-path-projection.md | 1–119 | PROCESS | **Scrub, keep.** | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/T10h-projection-hotfix.md | 1–82 | PROCESS, DESK | **Scrub, keep.** | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/T11a-leaf-consolidation.md | 1–267 | PROCESS, DESK | **Scrub, keep.** | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/T11a-census-1e74728.md | 1–851 | PROCESS | **Scrub, keep.** A census. | Kept, scrubbed (D0a-3 A2). The file name keeps its census label, which other files and tests cite. |
| docs/work/orders/T11b-leaf-behaviour.md | 1–175 | PROCESS, DESK | **Scrub, keep.** | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/T12-census-42a7c783.md | 1–531 | PROCESS, PRIVATE-REF | **Scrub, keep.** A census, at a private base commit. | Kept, scrubbed (D0a-3 A2). The file name keeps its census label, which other files cite. |
| docs/work/orders/T12a-capture-survives.md | 1–122 | PROCESS, DESK | **Scrub, keep.** | Kept, scrubbed (D0a-3 A2). |
| docs/work/orders/T12b-one-indexer-outbox.md | 1–164 | PROCESS, DESK | **Scrub, keep.** | Kept, scrubbed (D0a-3 A2). |
| docs/work/inventory/S8-module-inventory.csv | 1–150 | PROCESS, DESK | A module inventory, census-like, with internal desk names. AT-0006 does not name inventories, so this row has no marking. | Keep (override): its OPS values are module classifications naming the OPS extension (product vocabulary); no desk or tenant instance. |
| docs/work/inventory/T1-module-dispositions.csv | 1–150 | PROCESS, DESK | The same kind of inventory, with no marking. | Keep (override): as the S8 row; the file is also a frozen input pinned by sha256 in `tests/boundary/t1_contract.py`. |
| docs/work/ADR-PLANNING-DELIVERY.md | 5 (+3) | PROCESS, PRIVATE-REF | A work record with private-history baselines and an operator trial. | Scrubbed (D0a-3 A2). |
| docs/work/NATIVE-KANBAN-ADR-MAPPING.md | 58 | PROCESS, PRIVATE-REF, DESK | A work record citing a private legacy checkout revision. | Scrubbed (D0a-3 A2). |
| docs/work/NATIVE-KANBAN-ADR-RECONCILER.md | 1–145 | PROCESS | A work record. Its event id at line 75 is an example. | Keep: no value of a scrubbed class; the event id at line 75 is an example. |

## 7. ADRs (docs/adr)

The ADRs are design records. AT-0006 does not name them among the method pieces, so these rows carry no marking.

| File | Line | Class | Why it matters | Principal decision |
| --- | --- | --- | --- | --- |
| docs/adr/README.md | 8 | DESK | The index quotes Principal rulings. | Scrubbed (D0a-3 A2). The Principal's quotes stay verbatim (Principal decision, 2026-10-04: "Verbatim in ADRs too"); a desk or tenant instance name inside a quote is bracketed and generalised. |
| docs/adr/0012-fresh-user-onboarding.md | 1–106 | DESK, REPO, PRIVATE-REF | Links its original decision in a private repository; see also the rows in sections 3 and 5. | Scrubbed (D0a-3 A2). The Principal's quotes stay verbatim (Principal decision, 2026-10-04: "Verbatim in ADRs too"); a desk or tenant instance name inside a quote is bracketed and generalised. |
| docs/adr/AT-0001-native-planning-and-delivery.md | 18 | PRIVATE-REF, DESK | A merge commit and pull request of the private history. | Scrubbed (D0a-3 A2). The Principal's quotes stay verbatim (Principal decision, 2026-10-04: "Verbatim in ADRs too"); a desk or tenant instance name inside a quote is bracketed and generalised. |
| docs/adr/AT-0002-desk-profiles-and-memory-views.md | 5 | PRIVATE-REF, DESK | A merge commit and pull request of the private history. | Scrubbed (D0a-3 A2). The Principal's quotes stay verbatim (Principal decision, 2026-10-04: "Verbatim in ADRs too"); a desk or tenant instance name inside a quote is bracketed and generalised. |
| docs/adr/AT-0003-distributable-package.md | 1–65 | DESK, PRIVATE-REF | Principal rulings, the dispatch method and private pull requests. | Scrubbed (D0a-3 A2). The Principal's quotes stay verbatim (Principal decision, 2026-10-04: "Verbatim in ADRs too"); a desk or tenant instance name inside a quote is bracketed and generalised. |
| docs/adr/AT-0004-portable-kanban-suite.md | 12 | DESK, PRIVATE-REF | Principal rulings from an internal desk's session, and private-history references throughout. | Scrubbed (D0a-3 A2). The Principal's quotes stay verbatim (Principal decision, 2026-10-04: "Verbatim in ADRs too"); a desk or tenant instance name inside a quote is bracketed and generalised. |

## 8. records/

| File | Line | Class | Why it matters | Principal decision |
| --- | --- | --- | --- | --- |
| records/acceptance/independent-images-20260925.json | 1–117 | RECORD, PRIVATE-REF, HASH | Local image ids, private commits and private receipt hashes. | Removed (D0a-3 A3). |
| records/acceptance/kanban-container-import-20260925.json | 3 (+2, at 4, 14) | RECORD, PRIVATE-REF, HASH | A local image id, a private commit and a private receipt hash. | Removed (D0a-3 A3). |
| records/desk-menu/2026-09-27-live.json | 33 (+5) | RECORD, PRIVATE-REF, DESK | A private commit, a local image digest, a private CI run, a local port and internal desk names. | Removed (D0a-3 A3). |
| records/desk-menu/2026-09-29-capture-scope.json | 1–26 | RECORD, TENANT, REPO, HASH | The tenant and private repository names (lines 5, 9–15, 22), a catalog hash (line 6), and a Principal ruling quoted verbatim (line 17). | Removed (D0a-3 A3). |
| records/desk-menu/2026-09-29-workspace-capture-review.md | 1–26 | RECORD | An operator validation record. | Removed (D0a-3 A3). |
| records/extraction/portable-migration-rehearsal.json | 4 (+7) | RECORD, PRIVATE-REF, HASH, REPO | Private commits, hashes of private exports and archives, and private repository names (lines 35, 37). | Removed (D0a-3 A3). |
| records/extraction/source-files.json | 4 (+1, at 5) | RECORD, PRIVATE-REF | Origin revisions of the private legacy source repository and of the local Kanban branch. The per-file hashes are of this tree. | Removed (D0a-3 A3). |
| records/extraction/validation.json | 1–17 | RECORD | An extraction baseline record. | Removed (D0a-3 A3). |
| records/telemetry/core-consumer-handoff.json | 3 (+3) | RECORD, PRIVATE-REF, DESK | Private commits and branch, and an internal desk; see section 3 for line 8. | Removed (D0a-3 A3). |

## 9. apps/kanban/.plan

Every file in `apps/kanban/.plan` except three is byte-identical to upstream at the fork point.

| File | Line | Class | Why it matters | Principal decision |
| --- | --- | --- | --- | --- |
| apps/kanban/.plan/ | all upstream files | UPSTREAM | Upstream's internal planning notes. They are already public upstream, and this tree ships them unchanged. | Keep: byte-identical to upstream at the fork point (class default; checked by Git blob). |
| apps/kanban/.plan/docs/durable-inbox-live-trial.md | 1–28 | RECORD, PRIVATE-REF | Added by this fork: an operator trial record with a private baseline commit and local ports. | Kept, scrubbed (D0a-3 A2). A RECORD outside `records/` is scrubbed, not removed (D0a-3 AMBIGUITY). |
| apps/kanban/.plan/docs/durable-inbox.md | 1–17 | PROCESS | Added by this fork: a design note. | Keep: a fork design note with no value of a scrubbed class. |
| apps/kanban/.plan/docs/standalone-inbox-publisher.md | 1–40 | RECORD | Added by this fork: an operator trial record. | Kept, scrubbed (D0a-3 A2). A RECORD outside `records/` is scrubbed, not removed (D0a-3 AMBIGUITY). |
| apps/kanban/.plan/docs/CLI References/claude-code-cli-reference.md | 1–161 | 3P-DOC, UPSTREAM | A copy of Anthropic's Claude Code documentation. | Removed (D0a-3 A3): a copy of third-party documentation (class default). |
| apps/kanban/.plan/docs/CLI References/codex-cli-reference.md | 1–858 | 3P-DOC, UPSTREAM | A copy of the Codex CLI documentation. | Removed (D0a-3 A3): a copy of third-party documentation (class default). |
| apps/kanban/.plan/docs/CLI References/gemini-cli-reference.md | 1–126 | 3P-DOC, UPSTREAM | A copy of the Gemini CLI documentation. | Removed (D0a-3 A3): a copy of third-party documentation (class default). |
| apps/kanban/.plan/docs/CLI References/kiro-cli-reference.md | 1–2409 | 3P-DOC, UPSTREAM | A copy of the Kiro CLI documentation. | Removed (D0a-3 A3): a copy of third-party documentation (class default). |
| apps/kanban/.plan/docs/CLI References/opencode-cli-reference.md | 1–602 | 3P-DOC, UPSTREAM | A copy of the OpenCode documentation. | Removed (D0a-3 A3): a copy of third-party documentation (class default). |
| apps/kanban/.plan/docs/hooks-update/claude-code-hooks-docs.md | 1–1744 | 3P-DOC, UPSTREAM | A copy of Anthropic's Claude Code hooks documentation. | Removed (D0a-3 A3): a copy of third-party documentation (class default). |
| apps/kanban/.plan/docs/hooks-update/cline-cli-hooks-docs.md | 1–470 | 3P-DOC, UPSTREAM | A copy of the Cline CLI hooks documentation. | Removed (D0a-3 A3): a copy of third-party documentation (class default). |
| apps/kanban/.plan/docs/hooks-update/gemini-cli-configuration.md | 1–1683 | 3P-DOC, UPSTREAM | A copy of the Gemini CLI configuration documentation. | Removed (D0a-3 A3): a copy of third-party documentation (class default). |
| apps/kanban/.plan/docs/hooks-update/gemini-cli-hooks-docs.md | 1–163 | 3P-DOC, UPSTREAM | A copy of the Gemini CLI hooks documentation. | Removed (D0a-3 A3): a copy of third-party documentation (class default). |
| apps/kanban/.plan/docs/hooks-update/opencode-hooks-docs.md | 1–388 | 3P-DOC, UPSTREAM | A copy of the OpenCode hooks documentation. | Removed (D0a-3 A3): a copy of third-party documentation (class default). |
| apps/kanban/.plan/docs/ACP/ACP-docs.md | 1–4043 | 3P-DOC, UPSTREAM | A copy of the Agent Client Protocol documentation. | Removed (D0a-3 A3): a copy of third-party documentation (class default). |
| apps/kanban/.plan/docs/Skills/agent-skills-protocol.md | 1–1543 | 3P-DOC, UPSTREAM | A copy of the Agent Skills documentation. | Removed (D0a-3 A3): a copy of third-party documentation (class default). |
| apps/kanban/.plan/docs/Skills/claude-code-skills.md | 1–695 | 3P-DOC, UPSTREAM | A copy of Anthropic's Claude Code skills documentation. | Removed (D0a-3 A3): a copy of third-party documentation (class default). |
| apps/kanban/.plan/docs/blueprint-ui-docs.md | 1–2306 | 3P-DOC, UPSTREAM | A copy of the Blueprint UI documentation. | Removed (D0a-3 A3): a copy of third-party documentation (class default). |
| apps/kanban/.plan/02-kanban-to-kanban-rename/inventory.md | 103 (+1) | HOME, UPSTREAM | An upstream contributor's home path, public upstream. H6 counts it. | Keep: byte-identical to upstream at the fork point (class default; checked by Git blob). |
| apps/kanban/.plan/02-kanban-to-kanban-rename/plan.md | 196 (+1) | HOME, UPSTREAM | The same. | Keep: byte-identical to upstream at the fork point (class default; checked by Git blob). |
| apps/kanban/.plan/desktop-5-way-split-handoff.md | 9 (+2) | HOME, UPSTREAM | The same. | Keep: byte-identical to upstream at the fork point (class default; checked by Git blob). |
| apps/kanban/.plan/docs/cline-sdk-native-integration-plan.md | 46 | HOME, UPSTREAM | The same. | Keep: byte-identical to upstream at the fork point (class default; checked by Git blob). |
| apps/kanban/.plan/docs/hooks-update/codex-hooks-research.md | 12 (+2) | HOME, UPSTREAM | The same. | Keep: byte-identical to upstream at the fork point (class default; checked by Git blob). |
| apps/kanban/.plan/docs/hooks-update/hooks-update-research.md | 17 (+2) | HOME, UPSTREAM | The same. | Keep: byte-identical to upstream at the fork point (class default; checked by Git blob). |
| apps/kanban/.plan/docs/kanban-terminal-gap-analysis.md | 52 (+14) | HOME, UPSTREAM | The same. | Keep: byte-identical to upstream at the fork point (class default; checked by Git blob). |
| apps/kanban/.plan/docs/opencode-terminal-investigation-report.md | 21 (+19) | HOME, UPSTREAM | The same. | Keep: byte-identical to upstream at the fork point (class default; checked by Git blob). |

Outside `.plan`, upstream tests carry the same class. `apps/kanban/packages/desktop/test/window-state.test.ts:263`, `apps/kanban/test/runtime/terminal/codex-workspace-trust.test.ts:36` and `apps/kanban/test/runtime/update/auto-update.test.ts:77 (+41)` are byte-identical upstream, and H6 counts them. The Principal decides them with the `.plan` rows above. Applied (D0a-3): keep, as upstream has them.

## 10. Committed JSON under config/ and deploy/examples/ with a real name

| File | Line | Class | Why it matters | Principal decision |
| --- | --- | --- | --- | --- |
| config/desk-context/catalog.example.json | 6 | REPO | The example catalog's `repo_key` is a private repository name. | Scrubbed (D0a-3, Verification's ruling at the meet, overriding the kept row): a placeholder repository key, a synthetic revision and the binding keys the product derives from them; the T8 golden is regenerated by its generator under a named exception (`tests/d0a3_ruled_exceptions.py`). |
| config/desk-context/catalog.example.json | 7 | PRIVATE-REF | Its `source_revision` is a private commit. | Scrubbed (D0a-3, Verification's ruling at the meet, overriding the kept row): a placeholder repository key, a synthetic revision and the binding keys the product derives from them; the T8 golden is regenerated by its generator under a named exception (`tests/d0a3_ruled_exceptions.py`). |
| config/desk-context/catalog.example.json | 36 (+3, at 37, 52, 53) | HASH | Desk binding keys, derived from the coordinates above. | Scrubbed (D0a-3, Verification's ruling at the meet, overriding the kept row): a placeholder repository key, a synthetic revision and the binding keys the product derives from them; the T8 golden is regenerated by its generator under a named exception (`tests/d0a3_ruled_exceptions.py`). |
| config/desk-context/memory.example.json | 4 (+1, at 17) | HASH | The same binding keys. | Scrubbed (D0a-3, Verification's ruling at the meet, overriding the kept row): a placeholder repository key, a synthetic revision and the binding keys the product derives from them; the T8 golden is regenerated by its generator under a named exception (`tests/d0a3_ruled_exceptions.py`). |
| packages/tooling/src/kp_agent_tooling/assets/desk-context/catalog.example.json | 6 (+1, at 7); 36 (+3) | REPO, PRIVATE-REF, HASH | The packaged copy of the example catalog, which ships in every wheel and image. | Scrubbed (D0a-3, Verification's ruling at the meet, overriding the kept row): a placeholder repository key, a synthetic revision and the binding keys the product derives from them; the T8 golden is regenerated by its generator under a named exception (`tests/d0a3_ruled_exceptions.py`). |

## 11. Addendum (D0a-3, round 2): findings outside the original rows

Verification ruled at the D0a-3 meet that every private mention the D0a-3 scan found outside the rows above gets a row and the approved class defaults; named exceptions (`tests/d0a3_ruled_exceptions.py`) cover frozen fixtures and the pins bound to them. Line numbers here refer to the tree after round 2. Like the rows above, a row never repeats the value it found.

| File | Line | Class | Why it matters | Principal decision |
| --- | --- | --- | --- | --- |
| AGENTS.md | 3 | DESK, REPO | Names the legacy repository's checkout by its short name. | Scrubbed (D0a-3 round 2). |
| docs/PORTABLE-HOST-ENTRY.md | 4 | DESK | Names the retired legacy graph CLI by the legacy repository's short name. | Scrubbed (D0a-3 round 2). |
| docs/CONTAINER-IMPORT.md | 5 | DESK | Names the legacy service by the legacy repository's short name. | Scrubbed (D0a-3 round 2). |
| docs/CONTAINER-IMPORT.md | 93 | PRIVATE-REF | A runtime source commit of the private history. | Scrubbed (D0a-3 round 2). |
| docs/CONTAINER-IMPORT.md | 96 | RECORD | A link to a receipt removed with `records/`. | Scrubbed (D0a-3 A3; accepted at the meet): the link is replaced by a note. |
| apps/kanban/docs/ops-chat-projection.md | 28 | DESK | Names the legacy graph by the legacy repository's short name. | Scrubbed (D0a-3 round 2). |
| apps/kanban/scripts/start-ops-local-trial.sh | 21 | DESK | A refusal message names the legacy checkout. | Scrubbed (D0a-3 round 2). Message text only; the script's variables are unchanged. |
| extensions/ops/src/kp_agent_tooling_ops/desk_import_cli.py | 1 | DESK | The docstring, also the `--help` description, names the legacy desks. | Scrubbed (D0a-3 round 2). |
| packages/tooling/src/kp_agent_tooling/_impl/service/navigation_process.py | 3 | DESK | A docstring names the legacy CLI. | Scrubbed (D0a-3 round 2). |
| examples/telemetry/core-consumer/tracer_bootstrap.py | 3 | PRIVATE-REF | A commit of the consumer application's private repository. | Scrubbed (D0a-3 round 2). |
| .github/workflows/images.yml | 63 | PRIVATE-REF | A comment cites a private-history commit. | Scrubbed (D0a-3 round 2). |
| apps/kanban/test/runtime/launch/t3-launch-binding.test.ts | 24 (+1, at 61) | PRIVATE-REF | Comments cite an order's private merge commit. | Scrubbed (D0a-3 round 2). |
| tests/EXTRACTED-TEST-LEDGER.md | 3 (+25) | PRIVATE-REF, REPO, DESK | Commits of the legacy repository and of this repository's private history; desk instances at line 320; legacy paths follow the word OPS. | Keep (named exception, `tests/d0a3_ruled_exceptions.py`): the word OPS is the ledger's legacy-path qualifier, which `tests/docs/test_doc_paths.py` P3 parses, and the extension's public name. Its private commits and the desk instances at line 320 are scrubbed (D0a-3 round 2). |
| tests/docs/test_doc_paths.py | 11 | DESK | The P3 docstring explains the OPS qualifier. | Keep (named exception, `tests/d0a3_ruled_exceptions.py`): as the ledger row. |
| extensions/ops/tests/test_doc_code_reference_delivery.py | 119 (+7) | DESK | Skip reasons name the legacy graph with the ledger's word. | Keep (named exception, `tests/d0a3_ruled_exceptions.py`): as the ledger row. |
| extensions/ops/tests/test_launch_claude_desk_memory_ops_original.py | 1 (+2, at 71, 104) | PRIVATE-REF, DESK | A comment and xfail reasons cite the legacy extraction commit. | Keep (named exception, `tests/d0a3_ruled_exceptions.py`): the word OPS is the ledger's legacy-path qualifier, which `tests/docs/test_doc_paths.py` P3 parses, and the extension's public name. Its private commits are scrubbed (D0a-3 round 2). |
| extensions/ops/tests/test_reference_bounds_continuation.py | 21 (+4) | PRIVATE-REF, DESK | Xfail reasons cite the legacy extraction commit. | Keep (named exception, `tests/d0a3_ruled_exceptions.py`): the word OPS is the ledger's legacy-path qualifier, which `tests/docs/test_doc_paths.py` P3 parses, and the extension's public name. Its private commits are scrubbed (D0a-3 round 2). |
| tests/test_memory_budget_ops_original.py | 1 (+3) | PRIVATE-REF, DESK | A comment and xfail reasons cite the legacy extraction commit. | Keep (named exception, `tests/d0a3_ruled_exceptions.py`): the word OPS is the ledger's legacy-path qualifier, which `tests/docs/test_doc_paths.py` P3 parses, and the extension's public name. Its private commits are scrubbed (D0a-3 round 2). |
| tests/ops_repo_fixture.py | 1 | PRIVATE-REF, DESK | The docstring cites the legacy extraction commit. | Keep (named exception, `tests/d0a3_ruled_exceptions.py`): the word OPS is the ledger's legacy-path qualifier, which `tests/docs/test_doc_paths.py` P3 parses, and the extension's public name. Its private commits are scrubbed (D0a-3 round 2). |
| tests/test_operation_navigation.py | 9 | DESK | A comment names the legacy repository's fixture with the ledger's word. | Keep (named exception, `tests/d0a3_ruled_exceptions.py`): as the ledger row. |
| tests/test_serena_navigation.py | 52 (+9) | PRIVATE-REF | Xfail reasons cite a private-history commit. | Scrubbed (D0a-3 round 2). |
| tests/t11a_census.py | 1074 | PRIVATE-REF | A comment cites the census commit. | Scrubbed (D0a-3 round 2). The census file name in its docstring is kept with the census rows. |
| tests/test_claim_note_budget.py | 3 | PRIVATE-REF | The docstring cites the order's private merge commit. | Scrubbed (D0a-3 round 2). |
| tests/test_import_closure.py | 4 | PRIVATE-REF | The docstring cites the order's private merge commit. | Scrubbed (D0a-3 round 2). |
| extensions/ops/tests/test_knowledge_platform_coverage.py | 4 | PRIVATE-REF | The docstring cites the order's private merge commit. | Scrubbed (D0a-3 round 2). |
| tests/boundary/t1_contract.py | 3 | PRIVATE-REF | The docstring cites the order's private merge commit. | Scrubbed (D0a-3 round 2). |
| tests/boundary/t1_contract.py | 18 | PRIVATE-REF | `ORDER_MERGE_BASE`, which the T1 capture command diffs against. | Removed (D0b R4a): replaced by `PRE_MOVE_PRODUCT_SHA256`, the pre-move product's content hash. |
| tests/boundary/t1_harness.py | 410 | DESK | A placeholder key that the frozen pre-move capture's configurations use. | Keep (named exception, `tests/d0a3_ruled_exceptions.py`): bound to the frozen pre-move capture. |
| tests/boundary/fixtures/pre_move_tools_list.json | 1121 (+3); 6671 (+1) | DESK, PRIVATE-REF | The frozen capture of the pre-move tool surface: four tool descriptions and its merge base. | Keep (named exception, `tests/d0a3_ruled_exceptions.py`) for the DESK half: a frozen fixture; P3 compares names and schemas only, and the pre-move tree it needs is private history. The PRIVATE-REF half: Removed (D0b R4a), replaced by `pre_move_product_sha256`. |
| tests/refresh/t5_rig.py | 47 | PRIVATE-REF | A comment cites the order's base commit. | Scrubbed (D0a-3 round 2). |
| tests/refresh/t5b_rig.py | 3 | PRIVATE-REF | The docstring cites the order's private merge commit. | Scrubbed (D0a-3 round 2). |
| tests/t10_oracle.py | 7 | PRIVATE-REF | The docstring cites the T10 base commit. | Scrubbed (D0a-3 round 2). |
| tests/fixtures/t10-read-path-projection/generate.py | 4 (+1, at 37) | PRIVATE-REF | The T10 golden generator's base commit. | Removed (D0b R4a): replaced by `BASE_PRODUCT_SHA256`. |
| tests/fixtures/t10-read-path-projection/goldens.json | 1 | PRIVATE-REF | The frozen golden records its base commit (`meta.base_sha`). | Removed (D0b R4a): replaced by `meta.generator_sha256` and `meta.product_sha256`. |
| tests/test_t10_p5_identical_results.py | 36 | PRIVATE-REF | Pins the frozen golden's base commit. | Removed (D0b R4a): it asserts `meta.generator_sha256` and `meta.product_sha256` instead. |
| tests/fixtures/t11a/generate_identity_corpus.py | 3 | PRIVATE-REF | The docstring cites the T11a base commits. | Scrubbed (D0a-3 round 2). |
| tests/fixtures/t11a/identity_corpus.json | 8 (+1, at 17) | HASH | A binding key derived from the example catalog's private repository key. | Scrubbed (D0a-3 round 2): regenerated by its committed generator at the scrubbed example (named exception E6). |
| tests/fixtures/t8-claim-note-budget/base-capsule.json | whole file | PRIVATE-REF, HASH | The private base commit, and the binding and the ids derived from the example catalog. | Scrubbed (D0a-3 round 2): regenerated from the T8 base code by the committed `generate_base_capsule.py` (named exception E2). |
| tests/test_workspace_context.py | 73 | PRIVATE-REF | Pins the example catalog's private source revision. | Scrubbed (D0a-3 round 2): re-pinned to the synthetic revision (named exception E3). |
| tests/install/test_t10h_p3_image_sqlite.py | 3 | PRIVATE-REF | The docstring cites the order's private merge commit. | Scrubbed (D0a-3 round 2). |
| tests/test_t10h_p1_projection_key_order.py | 3 (+1, at 45) | PRIVATE-REF | The docstring and a comment cite private commits. | Scrubbed (D0a-3 round 2). |
| tests/test_t10h_p2_capture_never_backfills.py | 4 | PRIVATE-REF | The docstring cites the order's private merge commit. | Scrubbed (D0a-3 round 2). |
| tests/install/test_t12a_a2_roles_restart_and_refusals_wait_image.py | 3 | PRIVATE-REF | The docstring cites the order's private merge commit. | Scrubbed (D0a-3 round 2). |
| tests/install/test_t12a_a3_verify_names_volume_presence.py | 3 | PRIVATE-REF | The docstring cites the order's private merge commit. | Scrubbed (D0a-3 round 2). |
| tests/test_t12a_a1_capture_watch_survives.py | 3 | PRIVATE-REF | The docstring cites the order's private merge commit. | Scrubbed (D0a-3 round 2). |
| tests/test_t11b_regression_set.py | 53 | PRIVATE-REF | A comment cites a private commit. | Scrubbed (D0a-3 round 2). |
| tests/test_workspace_capture_continuity.py | 4 | PRIVATE-REF | The docstring cites the order's private merge commit. | Scrubbed (D0a-3 round 2). |
| tests/test_desk_write_scope.py | 12 | DESK, REPO | Test data uses a seat abbreviation and private repository keys. | Scrubbed (D0a-3 round 2): the roster role Deployment Engineering and the keys `app` and `lib` (named exception E4). |
| extensions/ops/tests/test_commit_rationale.py | 183 (+2, at 216, 283) | DESK | Test prose names a desk instance. | Scrubbed (D0a-3 round 2): an app repository (named exception E4). |
| tests/docs/test_release_hygiene.py | 28 (+1, at 32) | PRIVATE-REF | H6's docstring cites the path-scrub PR. | Scrubbed (D0a-3 round 2). |
| tests/docs/test_release_hygiene.py | 62 | PRIVATE-REF | `KANBAN_LOCAL_HEAD`, the private local branch head H3 checks NOTICE against. | Keep (named exception, `tests/d0a3_ruled_exceptions.py`): H3's guard value; removing it removes the check. |
| docs/work/orders/D0a-3-apply-public-readiness.md | 5 | PRIVATE-REF | The order's private base commit and PR number. | Kept, scrubbed (D0a-3 round 2). |
| docs/work/orders/D0f-board-without-agent-sdk.md | 5 | PRIVATE-REF | The order's private base commit and PR number. | Kept, scrubbed (D0a-3 round 2). Its verbatim Principal quote stays; it names no desk or tenant instance. |

**Recorded Principal decisions (2026-10-04, D0a-3 round 3).**
- **Repository keys `ats` and `core`: keep.** The lowercase keys in the OPS extension's code, its fixtures and the tests stay as they are: they are generic words and product behaviour (configured repository keys), not instance names in prose. No code changes.
- **Quotes in ADRs: verbatim.** As in the orders, the Principal's quotes in the ADRs and in `apps/kanban/docs/ops-desk-temporal.md` stay verbatim; only a desk or tenant instance name inside a quote is generalised, in brackets.
- **D0a-1's statement about the Principal's other accounts: keep**, as written.

## 12. Addendum (D0b): the private organisation's name

D0a-1's rows did not list these lines, so the private scan built from those rows did not count them (Verification, 2026-10-04). Line numbers refer to main after O2, D0b's base. Like the rows above, a row never repeats the value it found.

| File | Line | Class | Why it matters | Principal decision |
| --- | --- | --- | --- | --- |
| deploy/Dockerfile | 63 (+5, at 148, 173, 212, 237, 257) | REPO | Every target's `org.opencontainers.image.source` in deploy/Dockerfile names the private repository, so every image built from the tree carries it. D0b: each label reads the `SOURCE_URL` build argument, whose default is the public repository. | Replace (D0b) |
| docs/DOCKER.md | 92 | REPO, PRIVATE-REF | An image reference in the install-from-the-product-image example: a private package location. D0b: the public registry owner. | Replace (D0b) |
| docs/DOCKER.md | 93 | REPO, PRIVATE-REF | The same example's `--image` reference. D0b: the public registry owner. | Replace (D0b) |
| docs/DOCKER.md | 113 | REPO, PRIVATE-REF | The `plan` example's `--image` reference. D0b: the public registry owner. | Replace (D0b) |
| tests/install/s3_harness.py | 47 | REPO | A registry-shaped test fixture names the private owner. It never reaches the network. D0b: the public registry owner. | Replace (D0b) |
| tests/install/test_p3_refusals.py | 47 | REPO | A registry-shaped test fixture names the private owner. It never reaches the network. D0b: the public registry owner. | Replace (D0b) |
| docs/work/orders/ARM-HEADER.md | 13 | REPO | The arm header names the private repository as the arms' workspace origin. D0b: the public repository. | Replace (D0b) |

## Coverage notes

- **Checked; placeholders only.** The five files in `deploy/examples/` and `deploy/image/config.example.json` carry only placeholders (`REPLACE_WITH_…`, `/ABSOLUTE/HOST/PATH/TO/…`, `product`). The other tenant values in the tree are placeholders too (`workspace-demo`, `my-team`, `YOUR_TENANT`).
- **The H6 classes.** On this tree, the drive-mount class has no finding beyond the seven guard sites the path scrub kept. The three `local_` session ids are the section 3 rows. The home-directory and email findings are upstream Kanban content, npm registry metadata or placeholders. H6 allowlists each of these with a reason.
- **No person's name, no credential.** No tracked file named the operator when D0a-1 audited the tree. D0a-3 adds the copyright holder the Principal decided, in NOTICE, CONTRIBUTING.md and apps/kanban/NOTICE. A pattern scan for token and key shapes (GitHub, Anthropic, AWS, private keys) found only the guard regex in `tests/image/test_image_agents_and_credentials.py`. That is cleared, not measured: a scan for known shapes cannot exclude an unknown one.
- **Product vocabulary, not rows.** The role names in product code, tests and roster templates (Coordinator, Verification, Test Implementer and the rest) are listed only where they mirror the operator's own desk set (section 4).
- **Not audited.** The git history itself: the release uses a fresh public history. The contents of the images beyond what NOTICE lists. Bundle contents beyond `@anthropic-ai/claude-agent-sdk` and `@clinebot/*`, which were measured at the meet (section 2).
