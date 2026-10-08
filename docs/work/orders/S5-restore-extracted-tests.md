# S5 — Restore the extracted modules' legacy tests

ADR: [AT-0003](../../adr/AT-0003-distributable-package.md). Header: [ARM-HEADER](ARM-HEADER.md).

Dispatch: SINGLE. Coordinator discretion, recorded: the contract is the existing legacy tests themselves. The arm does not derive tests from an implementation.

## Problem

The extraction source is a commit of the legacy source repository, in an operator-local clone. It copied modules into `packages/tooling/src/kp_agent_tooling/_impl` with `kp_ops.` renamed to `kp_agent_tooling._impl.`. Many legacy tests that exercise those modules were not carried over. Examples of affected modules: `scip_navigation`, `verification_*`, `population`, `server_identity`, `navigation_discovery`.

The missing tests are why a missing module went unnoticed (see S1).

## Contract

- **Scope.** For each test file in the legacy repository's `tests/` at the extraction commit that imports at least one module that now exists under `kp_agent_tooling._impl`, the file is in scope unless it is excluded under the next rule.
- **Exclusions.** A file is excluded when it depends on a module that exists only in the legacy repository: the graph and board, the legacy gateway (`mcp_gateway`, `mcp_transport`, `knowledge_mcp`), `desk_memory*` legacy, Postgres, or `kp_core`. Each excluded file is listed in `tests/EXTRACTED-TEST-LEDGER.md` with its reason.
- **Porting.** Each in-scope file is ported to `tests/` with imports renamed. Its assertions are kept as they were in the legacy repository.
- **Failures.** A ported test that fails is a finding, not an edit target.
  - Never weaken, delete or rewrite an assertion to make it pass.
  - Mark the test `@pytest.mark.xfail(strict=True, reason="S5 divergence: <one line>")`.
  - Record it in the ledger under DIVERGENCES, with the failing assertion.
- **S1's territory.** The legacy `tests/test_platform_entry.py`, and any test that needs `knowledge_coverage`, belong to S1, which is dispatched in parallel. Record each as `covered-by-S1` and do not port it.
- **Deduplication.** Where agent-tooling already has an equivalent test with the same assertions, record it as `already-covered: <path>` and don't duplicate it.
- **Fixtures.** Fixtures that legacy tests read from `tests/fixtures/` are copied as well. No fixture may contain credentials or transcript content.

## Properties and falsifiers

- **P1: the ledger is complete.** Every legacy test file at the extraction commit that names an extracted module appears in the ledger exactly once, as `ported`, `excluded`, `already-covered` or `covered-by-S1`.
  Falsifier: a legacy test file that names an extracted module and is absent from the ledger.
- **P2: assertions are faithful.** For every `ported` file, the assertions match the legacy originals after the mechanical import rename.
  Falsifier: an assertion that is weakened, removed or rewritten without an `xfail` divergence record.
- **P3: the suite is green, and honest about divergences.** The default suite exits 0. Every divergence is a strict `xfail`.
  Falsifier: a non-zero exit, or an unrecorded skip or xfail.

## Write scope

New files under `tests/` (including `tests/fixtures/`), plus `tests/EXTRACTED-TEST-LEDGER.md`. Do not modify `packages/`, `apps/`, `deploy/`, `scripts/` or existing tests. If a test cannot run without a product change, record that under DIVERGENCES. Do not make the change.

## Report addition

Counts: in-scope, ported, excluded, already-covered, divergences. Also report the default-suite command, its exit code and its summary line.
