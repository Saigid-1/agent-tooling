# T6a — Product and `-ops` images on the slim core; CI

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

This slice absorbs AT-0003's S2 (one product image) and S4 (CI).

## Inputs

- **S2's meet artifact:** S2's meet branch. Its `deploy/Dockerfile` has the targets `runtime`, `product`, `agents` and `acceptance`, and `deploy/image/*` holds the `kanban` board command, `container.py` board health and the pinned agent CLI lock. It was met at 43/43 image contract tests and was never merged. Its `tests/image/` suite and the `image` pytest marker came with it.
- **Main since S2:** T1 (core/extension split; `extensions/ops`, distribution `kp-agent-tooling-ops`, entry point `kp_agent_tooling.tools`), T2 (desk registry), T5 (refresh) and T3 (launch binding; `kp-agent-launch`), if T3 merges first.
- **S4 draft:** see "CI properties" below. It needs these Kanban baseline fixes:
  - `TMPDIR` must be a physical path;
  - the 2 stale K1 `agent-registry` expectations;
  - 2 integration tests that fail only under load.

  One intermittent Python test, `test_runner_bounds_and_deadline` (now in `extensions/ops/tests/test_navigation_process.py`), fails with `os.killpg` EPERM.

## Interface surface

- **Dockerfile.** `deploy/Dockerfile` on current main has the targets `runtime`, `product`, `agents`, `acceptance` and a new `ops`.
  - `ops` is `product` plus the extension installed from `extensions/ops`, with its OPS assets and console scripts.
  - `product` and `agents` contain no `kp_agent_tooling_ops`.
  - Every target is labelled with `org.opencontainers.image.revision`.
- **Old files.** `deploy/tooling/Dockerfile` and `deploy/kanban/Dockerfile` are removed, and references to them are updated. T1's removal of `verification.guide` from the image example config is preserved, at its S2 location `deploy/image/config.example.json`.
- **CI.** `.github/workflows/*.yml` runs:
  - core `tests` on Python 3.11 and 3.12, with the core only installed;
  - `extensions/ops/tests` with core plus extension installed;
  - Kanban `npm ci`, the typecheck, the full vitest suite and `npm run build` (including the no-telemetry verification);
  - the web-ui tests;
  - an image job that builds `product`, `runtime`, `agents` and `ops` for the runner architecture, then runs `tests/image -m image`, `tests/install -m image` and an `ops` check.

  Workflows use `permissions: contents: read`, read no secrets, push nothing and publish nothing, and every job has a timeout. The image job runs on pull requests that touch `deploy/`, `packages/`, `extensions/`, `apps/kanban/`, `tests/image/` or `tests/install/`, and on pushes to `main`.

## Properties and falsifiers

- **P1: one tree yields both variants.** In `product`, every core `[project.scripts]` entry point resolves and `kp_agent_tooling_ops` is not importable. In `ops`, every core and extension entry point resolves. `kp-agent-tooling … serve` then lists the extension tools when the config enables them, and the core tools when it doesn't.
  Falsifier: extension code in `product`, a missing entry point in either variant, or an `ops` server that won't list configured extension tools.
- **P2: S2's contract still holds.** The S2 image contract (`tests/image`, 43 tests) passes against `product`, `runtime` and `agents` built from this tree.
  Falsifier: any S2 image property regresses.
- **P3: CI is effective.** CI fails when:
  - a default Python test fails, on either Python version;
  - an extension test fails;
  - a Kanban type error appears, a Kanban or web-ui test fails, or the build fails (including an emitted telemetry string);
  - an image contract fails (for example, an agent CLI added to `product`).

  The two stale K1 expectations are updated to the K1 contract (only `claude` and `codex` launchable), with a comment citing `apps/kanban/docs/ops-local-delta-manifest.md`. Load-sensitive integration files run serially, or with at most one bounded retry that is reported in the log; they are never skipped. The intermittent Python test gets a diagnosed fix, or a bounded retry that still fails on a real regression; it is never skipped or xfailed.
  Falsifier: any of those failures leaving CI green, a skipped test, or a weakened assertion.
- **P4: CI is safe.** No workflow references `secrets.`, pushes an image, publishes anything, or lacks a timeout.
  Falsifier: any of them.

## Write scope

- **FEATURE:** `deploy/**`, `.github/workflows/**`, `pytest.ini` markers, `apps/kanban/test/runtime/terminal/agent-registry.test.ts` (the K1 expectations only), `apps/kanban/vitest.config.ts` and package scripts (only to serialize integration), and `extensions/ops/tests/test_navigation_process.py` or the module it tests (only for the intermittent test).
  - Do not edit `packages/tooling/src` except to package deploy assets.
  - Do not edit `apps/kanban/src` or `apps/kanban/web-ui/src`.
- **TEST:** new tests under `tests/image/` only, for P1's `ops`/`product` variant properties (marked `image`; they read `AGENT_TOOLING_TEST_IMAGE_OPS`). Also a CI-structure test under `tests/ci/` that parses the workflow YAML for P4 and for the presence of each P3 job. The Coordinator proves P3's falsifiers on GitHub at the meet.

## Acceptance at the meet

The Coordinator:
- builds all targets and runs the image suites;
- pushes the branch and observes CI green;
- pushes throwaway commits, never merged, that each break one P3 condition, and observes CI go red.
