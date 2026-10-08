# AT-0004 — Portable Kanban suite: core, extension, desks and bindings

Declared status: Accepted. Derived status: Accepted.

It supersedes parts of [AT-0003](AT-0003-distributable-package.md):
- S1's module and the OPS-specific share of S5's tests move to the extension (T1).
- S2 (image) and S4 (CI) are folded into T6.
- S6 (cutover) becomes T7.

AT-0003's one-image, role-per-container and single-manifest decisions stand.

## Rulings (the Principal, in the Coordinator's session, 2026-09-30)

- **Purpose.** Agent-tooling is "a light, portable suite of tools that would integrate with the cline kanban and UI (not the desktop native app)". It must not fold in the legacy framework.
- **Keep set.** The Kanban-core keep set from the S8 inventory "is accurate".
- **Host CLIs.** Session hooks, transcript capture and delegation are retained for users of the Claude and Codex CLIs, including computer use: "Claude/Codex for computer use for now". Swapping in another harness must be "merely a binding target swap and not a new build".
- **Desk abstraction.** The OPS schema coupling is abstracted out, "to allow desk definition, capture, and binding without introducing the dependency on core". Work builds on the existing desk-definition UI and binding.
- **Extension.** The plugin lives in this repository as an extension. "The legacy [source] repo will be retired in favor of this one."
- **Kanban.** "Kanban replaces the board/dispatch/intake (as intake is now through ADR's and the cline kanban dispatch)."
- **Out of scope.** The Electron desktop shell stays out until the runtime is stable.
- **Model routing.** Media and other modality work goes through a model-gateway MCP with a capability routing table: "exactly what I was gesturing at". The Cline bot keeps its own distinctly scoped memory.
- **Deployment.** The suite is portable, in one image or several, depending on how configurability is enabled.

## Decision

1. **Core.** `packages/tooling` holds only the keep set: navigation (search, SCIP, Serena, semantic, local code lookup), desk memory, session import, the desk registry, assistant memory, transcript capture, refresh, setup and the installer.
   - The core imports nothing from OPS or `kp_core`, and loads no OPS schema, role or verification asset.
   - [T1-module-dispositions.csv](../work/inventory/T1-module-dispositions.csv) freezes the split. It is derived from the import-graph inventory [S8-module-inventory.csv](../work/inventory/S8-module-inventory.csv).
2. **Extension.** `extensions/ops` is a separately built distribution that depends on the core, never the reverse. It holds:
   - the OPS verification, lifecycle, rationale, review and observation tools;
   - the knowledge and document stack;
   - legacy imports and the legacy desktop launchers.

   It plugs into the navigation server through a tool-provider entry-point group. An `-ops` image variant, which is the product image plus the extension, serves existing deployments during the transition.
3. **Portable desks.** A desk is data in the memory store:
   - fields: id, name, role, description, zero or more repositories, memory scope, capture setting;
   - the role comes from an operator-configurable roster, and the nine OPS roles become an importable example;
   - doctrine, approval references and reviewer fields are no longer gates;
   - the Desks UI (AT-0002) edits the registry.
4. **Bindings.**
   - **Target.** A session binding records its target as (harness, provider, model), plus the native session id, desk, source, workspace and parent session.
   - **Harness profiles.** Harnesses are configuration profiles. Each declares its executable and allowed arguments, how MCP and hooks are injected, how the session id is obtained, and its capture mode. Adding a harness that uses an existing capture mode requires no rebuild; a new transcript format is a mountable parser plugin.
   - **Enabled now.** Claude and Codex are enabled first.
   - **Authority.** Bindings made at launch by the board or the host adapter are operator acts. Models and hook payloads can never select a desk or grant admission.
5. **Launch binding and host adapter.**
   - The sidebar's `prepare` is generalized to every harness and wired into board task launch.
   - `kp-agent-host` (`launch`, `hook`, `bind`, `install-hooks`) attaches host-run Claude and Codex, which keeps computer use native. Its hooks append to a spool that capture ingests.
   - Codex child threads inherit their parent's binding.
6. **Kanban is the control surface.** The board runs in a container by default. Host sessions appear on it through their bindings.
7. **Phase 2.**
   - **Model gateway:** an MCP server exposing capability tools (image, audio, others). A routing table maps each capability to a provider and model, with per-capability cost caps and a mounted OpenRouter secret.
   - **Harness profiles:** Cline native (OpenRouter) and OpenCode, each with a capture adapter.
   - **Preferences:** a preferences and style section in the Cline bot's isolated assistant memory.

## Slices

| Slice | Scope | Dispatch | Status |
|---|---|---|---|
| T1 | Extension seam; move the OPS and knowledge code out; default tool set is core only | FEATURE + TEST ([order](../work/orders/T1-extension-seam.md)) | Built |
| T2 | Portable desk registry, configurable roster, session-binding schema, catalog importer, Desks UI | FEATURE + TEST ([order](../work/orders/T2-desk-registry-bindings.md)) | Built |
| T3 | Launch binding and harness profiles; board task launch binds (Claude, Codex) | FEATURE + TEST ([order](../work/orders/T3-launch-binding-harness-profiles.md)) | Built |
| T4 | Host adapter: launch, hook, bind, spool capture, parent/child attribution | FEATURE + TEST ([order](../work/orders/T4-host-adapter.md)) | Built |
| T5 | Refresh efficiency: independent semantic reuse, backoff, retention, debounce, configurable timeouts, no hard-coded repository layouts | FEATURE + TEST ([order](../work/orders/T5-refresh-efficiency.md)) | Built |
| T5b | Retention keeps generations an operator's knowledge catalog references (`retention_references`), fail-safe | FEATURE + TEST ([order](../work/orders/T5b-retention-references.md)) | Built |
| T6a | Product and `-ops` images on the slim core (rebase S2); CI builds the images and checks Kanban and the extension (S4) | FEATURE + TEST ([order](../work/orders/T6a-images-and-ci.md)) | Built |
| T6b | Documentation sweep for the slim core | single ([order](../work/orders/T6b-docs-sweep.md)) | Built |
| T7a | Role readiness from an empty root: no role fails at creation, documented steps work verbatim, field errors, image identity, `--home`, capture boundary | FEATURE + TEST ([order](../work/orders/T7a-role-readiness.md)) | Built |
| T7b | Board wiring in Docker: Desks menu and desk launch binding on the runtime registry; `--board-agents` writable workspaces for in-container agents; assistant memory wiring | FEATURE + TEST ([order](../work/orders/T7b-board-wiring.md)) | Built |
| T7 | Empty-root rehearsal; cutover in a Principal-granted quiet window; live refresh without the legacy source repository | Coordinator | Done (cutover 2026-10-02) |
| T8 | Claim notes cite ordinary episodes (qualifiers from cited events, omissions recorded); bounded refusals name the bound | FEATURE + TEST ([order](../work/orders/T8-claim-note-budget.md)) | Built |
| T9 | Workspace capture continues across container restarts (prefix digest, not inode) and directory moves; refusals recorded | FEATURE + TEST ([order](../work/orders/T9-capture-continuity.md)) | Built |
| T9b | The memory store lives on a named volume, where POSIX locks work; one-time lossless migration; no host access to store files | FEATURE + TEST ([order](../work/orders/T9b-memory-store-volume.md)) | Built (deployed 2026-10-03) |
| T10 | Memory reads: one roundtrip per call; scope from an indexed projection written at seal/claim; no scans; identical results | FEATURE + TEST ([order](../work/orders/T10-read-path-projection.md)) | Built (deployed 2026-10-03; hotfix T10h) |
| T10h | Projection hotfix: projection DDL passes the image's SQLite quick_check (key columns first, in-place rebuild); capture never backfills or re-checks the whole store; an image-marked test on the runtime's SQLite | FEATURE + TEST ([order](../work/orders/T10h-projection-hotfix.md)) | Built (deployed 2026-10-03) |
| T11a | Leaf module, consolidation with no observable change: one home each for canonical JSON and identity digests, private files, SQLite open, the store-path rule and store filenames; one `_page`; dead code removed | FEATURE + TEST ([order](../work/orders/T11a-leaf-consolidation.md)) | Built |
| T11b | Leaf module, ruled behaviour: store paths refused outside the volume at open in the Docker runtime; private writes created with their mode and publications fsynced; one SQLite profile; assistant template moves to /config | FEATURE + TEST ([order](../work/orders/T11b-leaf-behaviour.md)) | Built |
| T12a | Capture survives: the watch loop never ends on an error (bounded backoff); roles restart unless stopped; store-preflight refusals wait instead of exiting | FEATURE + TEST ([order](../work/orders/T12a-capture-survives.md)) | Built |
| T12b | One indexer: an outbox in the store appended in the seal transaction, one leased indexer from a watermark, build-and-swap reindex, the five pending states retired | FEATURE + TEST ([order](../work/orders/T12b-one-indexer-outbox.md), [census](../work/orders/T12-census-42a7c783.md)) | Accepted (T12 split by Verification, 2026-10-03); order frozen 2026-10-04 (r5) |
| T12c | Per-desk postings: desk-scope search cost independent of other desks' matching text (T10 P3(c)); reassignments visible immediately | FEATURE + TEST (order drafted; freezes after T12b) | Accepted (T12 split by Verification, 2026-10-03) |
| D0a-1 | Release hygiene for the public v0.4.0: root LICENSE (AGPL-3.0-only), NOTICE, CONTRIBUTING with the DCO, pyproject licence fields, the path scrub and a tree test against private paths, a public-readiness audit for the Principal's decisions | One arm ([order](../work/orders/D0a-1-release-hygiene.md)) | Built |
| D0a-3 | Apply the public-readiness decisions: audit class defaults, Apache-2.0 for apps/kanban's own changes, the copyright holder, Serena and Debian source offers, setuptools>=77 | One arm ([order](../work/orders/D0a-3-apply-public-readiness.md)) | Accepted (Principal, 2026-10-04); order frozen 2026-10-04 |
| D0f | The board without bundled claude-agent-sdk: external SDK, a user-initiated install action with a no-warranty/no-licence notice, installed under /state | FEATURE + TEST ([order](../work/orders/D0f-board-without-agent-sdk.md)) | Accepted (Principal, 2026-10-04); order frozen 2026-10-04 |
| M1 | Model-gateway MCP with a capability routing table | FEATURE + TEST ([order](../work/orders/M1-model-gateway.md)) | Built |
| M2–M3 | Cline/OpenCode harness profiles with capture adapters; assistant preferences memory | FEATURE + TEST | Accepted |

## Live-state note

The live refresh indexer was paused on 2026-09-30, by Principal ruling, after telemetry attributed 150–190% sustained CPU to it. The causes were:
- every commit to the shared library repository (`lib` here) invalidates every repository's SCIP identity through `analysis_dependencies: [lib]`;
- semantic reuse is gated on SCIP reuse;
- the shared library's semantic build never completes;
- 244 unpruned generations (82 GB) churn the filesystem.

T5 fixes the causes. T7 restarts refresh without the legacy source repository.
