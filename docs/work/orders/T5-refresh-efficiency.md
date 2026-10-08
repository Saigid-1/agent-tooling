# T5 — Refresh efficiency

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

## Problem (telemetry, 2026-09-30, the live runtime)

`kp-agent-refresh --watch --interval 300` sustained 150–190% CPU. Observed causes:

1. **Dependency pins invalidate every repository.** All three configured repositories declare `analysis_dependencies: ["lib"]`, naming a shared library repository. Every `lib` commit therefore changes each repository's SCIP `build_identity` (via `analysis_dependency_pins`), so all three are re-cloned and re-indexed.
2. **Semantic reuse is gated on SCIP reuse.** `refresh_cli.refresh` only considers `reusable_semantic` for keys in `reused`. Two repositories (41,550 and 6,763 chunks) are re-embedded even when their own revision and semantic identity are unchanged.
3. **A failing semantic build retries on every change.** The `lib` repository's semantic build fails with `build_deadline`, and `recent_semantic_gap` only suppresses a retry while the SCIP identity is unchanged, so it retries on every `lib` commit.
4. **The index timeout is fixed.** `run(cmd, timeout=300)` for `scip-python` is hard-coded. Under contention it fails (`refresh_failed … python_index TimeoutExpired`), and the next cycle rebuilds again.
5. **Generations are never pruned.** 244 generations (82 GB) have accumulated.
6. **Repository layout is hard-coded** around `refresh_cli.py:480-483`: `studio/package.json`, `studio/package-lock.json`, tenant `platform-trial`.

## Interface surface

These are new optional fields in the refresh request (`/config/refresh.json`). An absent field takes the default, and existing requests stay valid.

- Per repository:
  - `python_index_timeout_seconds`: an integer from 60 to 3600, default 300.
  - `typescript_package_files`: a list of paths relative to the repository root, default `["<typescript_prefix>/package.json", "<typescript_prefix>/package-lock.json"]` when `typescript_prefix` is set.
- Top level:
  - `min_rebuild_interval_seconds`: an integer from 0 to 86400, default 0, which preserves current behaviour.
  - `retain_generations`: an integer from 1 to 50, default 3.
  - `semantic_retry_after_seconds`: an integer from 300 to 604800, default 86400.
  - `tenant_id`: a string, default `"platform-trial"` for compatibility.

## Properties and falsifiers

- **P1: semantic reuse follows its own identity.** A repository keeps its published semantic index when its own source revision and semantic build identity are unchanged, even if its SCIP identity changed (for example, a dependency pin moved).
  Falsifier: a dependency-only change that re-embeds an unchanged repository.
- **P2: failure backoff follows its own identity.** A semantic build that failed at (revision R, semantic identity I) is not retried while R and I are unchanged and less than `semantic_retry_after_seconds` has elapsed. Dependency or SCIP changes do not reset the backoff. The published readiness reports the gap and its retry time.
  Falsifier: a retry inside the window caused by a dependency-only change, or a gap that is not reported.
- **P3: generations are retained and bounded.** After each successful publish, the only generations left are:
  - the published one;
  - any referenced by the snapshot registry or live handles;
  - at most `retain_generations − 1` newer or most recent unreferenced ones.

  Removals are atomic and listed in a receipt. A referenced or published generation is never removed, and a failed build's partial directory is removed.
  Falsifier: unreferenced generations beyond the bound, a referenced one removed, or partial directories left behind.
- **P4: rebuilds are debounced.** With `min_rebuild_interval_seconds = N`, at most one rebuild starts per N seconds. A cycle that sees new revisions inside the window reports `status: "pending_rebuild"` with the next eligible time, and does no clone and no index.
  Falsifier: two rebuilds inside N, or a clone or index during a pending cycle.
- **P5: the timeout is configurable.** `scip-python` runs with that repository's `python_index_timeout_seconds`.
  Falsifier: the configured value is ignored.
- **P6: no hard-coded layout.** TypeScript package files come from `typescript_package_files` (or `typescript_prefix`), and the tenant comes from `tenant_id`. `studio` and `platform-trial` do not appear as code literals.
  Falsifier: a repository whose TypeScript prefix is not `studio` fails, or the code still names those literals.
- **P7: compatibility.** An existing request with none of the new fields produces the same published profile schema and the same rebuild decisions as before, apart from P1, P2 and P3. All existing refresh tests pass.
  Falsifier: a schema or behaviour regression outside P1, P2 and P3.

## Write scope

- **FEATURE:** `packages/tooling/src/kp_agent_tooling/refresh_cli.py`, plus small helpers beside it under `_impl/` if needed, and `docs/DOCKER.md` (the refresh settings section only).
- **TEST:** new tests under `tests/refresh/`. Use temporary git repositories and stub indexers or embedders injected through the existing seams (monkeypatch). Do not run real `scip-python` or torch in the default suite; mark any real-toolchain check `@pytest.mark.image`. Never touch the live runtime.

## Acceptance at the meet

The Coordinator runs both suites. The Coordinator then replays a dependency-only change against a copied, small published state and confirms that no re-embed and no retry happen.
