# T1 — Extension seam: move the OPS and knowledge code out of the core

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

## Frozen inputs

- [`docs/work/inventory/T1-module-dispositions.csv`](../inventory/T1-module-dispositions.csv): one row per module (149), each marked `KEEP` (75), `MOVE` (71) or `REMOVE` (3). It is authoritative. A module missing from it is a new file and stays where it is.
- [`docs/work/inventory/S8-module-inventory.csv`](../inventory/S8-module-inventory.csv): the import-graph evidence. It records what reaches each module and which tests import it.

## Interface surface

- **Extension distribution.** `extensions/ops/` has its own `pyproject.toml`: distribution `kp-agent-tooling-ops`, import package `kp_agent_tooling_ops`, sources under `extensions/ops/src/`, tests under `extensions/ops/tests/`. It depends on `kp-agent-tooling` at the same version, and the core never depends on it.
- **Moved modules.** Each `MOVE` module now lives at `kp_agent_tooling_ops.<same relative path>`; for example, `kp_agent_tooling._impl.verification_packet` becomes `kp_agent_tooling_ops._impl.verification_packet`. `MOVE` rows under `scripts/` go to `extensions/ops/scripts/`.
- **Plugin seam.**
  - The navigation server (`kp_agent_tooling._impl.service.agent_tooling`) discovers tool providers through the entry-point group `kp_agent_tooling.tools`.
  - A provider contributes tool names, input schemas, a dispatch callable, and optional doctor/identity contributions.
  - Core tools are registered by the core itself. The extension registers:
    - `verification.*`, `lifecycle.evidence`, `knowledge.rationale`;
    - the portable and gateway `knowledge.*` tools served by `KnowledgeService`/`gateway_transport`.
  - The local `knowledge.platform` and `knowledge.symbol` (`local_knowledge` → `platform_snapshot`/`scip_entry`) remain core.
- **Core entry points.** `[project.scripts]` keeps only the entry points whose modules are `KEEP`. The extension declares the moved ones (`kp-agent-observations`, `kp-agent-capture`, `kp-agent-transcript`, `kp-agent-claude-capture`, `kp-agent-knowledge-publish`) under the same command names. Moved `-m` modules keep module entry points in the extension.
- **Core-to-extension edges.** Any such edge in the inventory is split so that the core half stays in the core and the extension half moves. The S8 hard cases:
  - `agent_tooling.py`'s OPS tool definitions and branches, `doctor` evidence walking, and the `gateway_transport` import;
  - `cli.py`'s `handoff` action;
  - `evidence_references.source_reference` (it moves into `source_citations`);
  - `desk_cli`'s `import-history` and `import-sessions`.
- **Default tool sets.** The installer (`runtime_install.ENABLED_TOOLS`), `workspace_setup`, `kp-agent-setup` and the image example config (`deploy/*/config.example.json`) enable no extension tool; `verification.guide` is removed from all defaults. OPS assets (`assets/verify-behavior.md`, `finding.schema.json`, `journey-registry.schema.json`) move with their modules.

## Properties and falsifiers

- **P1: the core is closed.** No module under `packages/tooling/src` imports `kp_agent_tooling_ops`, a `MOVE` module, or `kp_core`/`kp_ops`. This covers top-level, function-local and string (`-m`, `importlib`) forms. Every `MOVE` and `REMOVE` path is absent from the core.
  Falsifier: any such import, or a `MOVE` module still present in the core.
- **P2: the extension is complete.** Every `MOVE` module exists in the extension and imports cleanly with the core plus the extension installed. Every test that imported a `MOVE` module has moved to `extensions/ops/tests` and still runs. Its strict xfails, skips and assertions are unchanged apart from import paths.
  Falsifier: a missing module, a lost test, or a changed assertion.
- **P3: the tool surface is partitioned.** With the core alone, `tools/list` over stdio MCP (`kp-agent-tooling --config … serve`) returns exactly the core tools the config enables, and never loads an extension module while listing. With the extension installed and the same config, the names and input schemas of all tools the config enables equal what the order's merge-base tree, which is the pre-move code, returns for that config.
  Falsifier: any extra or missing tool, a schema difference, or an extension import during a core-only listing.
- **P4: defaults are core-only.** Every default enabled-tool list contains only core tools.
  Falsifier: an extension tool in any default.
- **P5: behaviour is preserved.**
  - The core suite (`tests/`) and the extension suite (`extensions/ops/tests/`) both pass.
  - The only deleted tests are those whose sole subject is a `REMOVE` module.
  - The Kanban seam commands (`kp-agent-session-import`, `kp-agent-desk-registry`, `assistant_host_cli`, `memory_cli`, `kp-agent-desk`) keep their existing tests unmodified.
  - Falsifier: a failing suite, a seam test edited, or an unlisted test deleted.

## Write scope

- **FEATURE:** `packages/tooling/**`, `extensions/ops/**` (new), moves out of `tests/` and `scripts/` into `extensions/ops/`, `deploy/**/config.example.json`, `pytest.ini`/`conftest` wiring so that both suites run, and README "Components" and install lines.
  - Do not edit `apps/kanban/**`.
  - Do not edit `refresh_cli.py`; T5 owns it.
  - Do not edit Dockerfiles or the compose manifest; T6 owns them.
- **TEST:** new tests only, under `tests/boundary/` for the core and `extensions/ops/tests/boundary/` if needed, covering P1, P3 and P4.
  - Build the pre-move tool-surface fixture from the merge base.
  - For P3, install the extension into a scratch venv under `TMPDIR`.
  - The TEST arm may read the frozen CSVs and the merge-base tree, never the FEATURE arm's branch.

## Acceptance at the meet

The Coordinator:
- runs both suites;
- re-runs the P1 guard with an injected core→extension import (it must fail);
- runs P3 with and without the extension against a real config;
- checks S5's ledger rows are still accounted for.
