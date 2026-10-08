# AT-0003 — Distributable package: one image, one manifest

Declared status: Accepted, partly superseded by [AT-0004](AT-0004-portable-kanban-suite.md). Derived status: In progress.

Built so far: S1, S5 and S3. AT-0004 moves S1's module and S5's OPS-specific tests to the extension (T1), folds S2 and S4 into T6, and replaces S6 with T7.

Rulings: the Principal, in the Coordinator's session on 2026-09-30, assigned the Coordinator to "stabilize the implementation so this is a usable and distributable solution" and selected, when asked:

- agent launching from the board: "Opt-in agent target";
- the legacy source repository's workspace-dispatch branch: "Preserve now, port later";
- live runtime cutover: "Ask me for a quiet window";
- merges in this repository: "Yes, merge after review + CI".

No slice below is Built until its implementation merge is cited here.

## Context (observed 2026-09-30)

- The Python package and the Cline Kanban fork build from this repository alone. The Python suite passes: 435 passed, 1 skipped, Python 3.12.
- The live deployment is not reproducible from this repository:
  - its Compose file is a hand-rewritten copy outside Git;
  - its image is pinned by a raw image ID, two merged PRs behind;
  - its boards are host Node processes started by an untracked script.
- There are two Dockerfiles, `deploy/tooling` and `deploy/kanban`, with separate Python installations. There are three Compose files: `deploy/compose.yaml`, the legacy `deploy/tooling/compose.yaml`, and `deploy/compose.import.yaml`.
- `scripts/deploy_tooling_release.py` targets container names that no longer exist.
- CI runs only the Python tests and a wheel build. The Kanban application is never built or tested, and no image is built.
- `knowledge.platform` imports `_impl.service.knowledge_coverage`, which was not extracted. The call fails with `ModuleNotFoundError`.
- Tests for many extracted modules stayed behind in the legacy source repository.

## Decision

1. **One image, several roles.** One `deploy/Dockerfile` produces the distributable image. It has three targets:
   - `product`: navigation, memory, capture, refresh and the board;
   - `runtime`: a slim, memory-only build;
   - `agents`: opt-in, `product` plus the `claude` and `codex` CLIs.

   No target contains credentials. Provider logins happen inside a running `agents` container and persist only under `/state`.
2. **Separate containers per role, not one supervised container.** Capture stays network-less with read-only source mounts. Only refresh receives the source credential. The board publishes only to loopback. Roles share the image, so this costs nothing in image size.
3. **One manifest.** A single `deploy/compose.yaml` defines the roles `tooling` (the MCP exec target for navigation and memory), `refresh`, `capture` and `board`, plus optional `telemetry`. It has no host-specific paths, user IDs or image IDs; those come from rendered `.env` and workspace overlays.
4. **One installer.** A plan/apply installer renders a runtime root from an empty physical directory. Planning writes nothing. Apply requires the reviewed plan hash, never overwrites a differing file, and emits a receipt. It reuses `kp-agent-setup` and the preserved appliance renderer (kept on a preservation branch). Host registration artifacts (MCP entries, hook settings) are rendered for the operator to install explicitly. Nothing is registered implicitly.
5. **Boundary with the legacy source repository.** These stay there as consumers: the board and graph, feature intake, the dispatch orchestrator, grants, Studio, DPG, and the capability map data. Its legacy `~/.kp-ops` hooks are to be replaced by this package's portable hooks.

## Slices

| Slice | Order | Dispatch | Status |
|---|---|---|---|
| S1 Restore `knowledge.platform` and add the import-closure guard | [S1](../work/orders/S1-knowledge-platform-coverage.md) | FEATURE + TEST | Built; the module moves to the extension in AT-0004 T1 |
| S2 One product image | [S2](../work/orders/S2-product-image.md) | FEATURE + TEST | Met (43/43 image tests), unmerged; folded into AT-0004 T6 |
| S3 Single manifest and empty-root installer | [S3](../work/orders/S3-manifest-installer.md) | FEATURE + TEST | Built |
| S4 CI builds the image and checks Kanban | folded into AT-0004 T6 | single (mechanical; Coordinator discretion, recorded) | Superseded by T6 |
| S5 Restore the extracted modules' legacy tests | [S5](../work/orders/S5-restore-extracted-tests.md) | single (the tests are the contract; Coordinator discretion, recorded) | Built; OPS-specific tests move to the extension in T1 |
| S6 Live cutover to the rendered runtime | after an empty-root rehearsal, in a Principal-granted quiet window | Coordinator | Superseded by AT-0004 T7 |

Dispatch transport: implementer arms are Claude Code subagents working in dedicated worktrees on the external volume, not headless Sol. This is a recorded deviation from the 2026-08-29 transport ruling, because this Coordinator seat runs in Claude Code. §10 bifurcation, contract-by-path, and the arms' blindness to each other are preserved.

## Preserved on 2026-09-30

Work that existed only on one disk was preserved unchanged. It is not yet reviewed and not yet part of `main`.

- An appliance-bundle preservation branch: the appliance bundle renderer, its tests and its contract.
- A worktree-ignore preservation branch: the uncommitted Kanban shared-exclude fix.
- A workspace-dispatch branch of the legacy source repository: workspace dispatch, runs and browser UI. Porting is deferred.

## Acceptance of the whole

This decision reaches Proven when the following holds on a second directory beside the live runtime. First, a release built only from this repository and installed from an empty root passes MCP stdio parity (navigation and admitted memory), board HTTP and import, capture isolation, and restart persistence. Then the same release is cut over in a granted quiet window, with a rollback receipt.
