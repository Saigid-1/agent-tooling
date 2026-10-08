# T6b — Documentation sweep for the slim core

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md).

Dispatch: SINGLE. Coordinator discretion, recorded: the change is mechanical documentation and the frozen property list below is the contract.

## Problem

The documentation still describes the pre-AT-0004 world:
- the legacy `ops-tooling` deployment and `portable_tooling/` paths;
- operator-specific absolute host paths (external-volume mounts);
- `scripts/` paths for launchers that moved to `extensions/ops/scripts/`;
- OPS-only tools in the defaults;
- the S5 ledger's `tests/<file>` rows for files that moved to `extensions/ops/tests/`;
- "Extraction is in progress" in the README.

## Properties and falsifiers

- **P1: no stale paths in maintained docs.**
  - Scope: maintained docs are `README.md`, `docs/*.md` (excluding `docs/adr/`, `docs/work/` and historical `records/`), and `packages/tooling/src/kp_agent_tooling/assets/*.md`.
  - They contain no host-specific absolute paths (`/Volumes/`, `/Users/`), no `portable_tooling/`, no `deploy/tooling/`, no `deploy/kanban/Dockerfile`, and no `scripts/<file>` reference to a file that has moved.
  - Every repository path a maintained doc names exists in the tree.
  - Falsifier: any such path, or a named path that does not exist.
- **P2: the README reflects the product.**
  - It describes the core (Kanban-centric), the `extensions/ops` extension, the image targets (`product`, `runtime`, `agents`, `ops`), the installer (`kp-agent-install`), and the desk registry and launch binding.
  - It states that the legacy source repository is retired in favour of this one.
  - It keeps the licensing section.
  - Falsifier: a missing component, or a claim the code does not support.
- **P3: the ledger is accurate.** Every path in `tests/EXTRACTED-TEST-LEDGER.md` exists, updated to the moved locations under `extensions/ops/tests/`. The rows for deleted tests (from removed modules) say so.
  - Falsifier: a row naming a missing path.
- **P4: historical records are untouched.** Nothing under `records/`, `docs/adr/` or `docs/work/` changes except the ADR index.
  - Falsifier: any edit there.

## Write scope

Maintained docs (as defined in P1), `README.md`, `tests/EXTRACTED-TEST-LEDGER.md` and `extensions/ops/README.md` (new). Also add a small checker, `tests/docs/test_doc_paths.py`, that enforces P1 and P3. Do not edit code.
