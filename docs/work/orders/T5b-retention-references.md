# T5b — Retention keeps generations the operator's knowledge catalog references

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

## Problem

T5 added fail-safe retention to refresh. Its keep-set is:
- the published profile and the catalog that profile names;
- generations the snapshot registry references;
- generations a process visible in the refresh container holds;
- generations without `profile.json`;
- the newest `retain - 1` others.

An operator's maintained knowledge catalog is not in that set. The live runtime's catalog, `config/knowledge.json` (named through `navigation.json` → `portable_knowledge_config` → `knowledge-runtime.json`), pins three older generations by absolute path. The processes that hold them run in the `tooling` container, which the refresh container cannot see.

The Coordinator's T7 migration rehearsal (2026-10-01, on copies) showed the result:
- the first retention pass with `retain=1` deleted all three as `beyond_retention`, which would break the knowledge tools;
- copying the catalog into the snapshot registry kept them, but that copy goes stale whenever the operator updates the catalog.

## Properties and falsifiers

- **P1: listed reference files are kept.** The refresh request gains an optional `retention_references`: up to 32 absolute paths to files under `/config` or `/state`.
  - Each run of retention reads the files as they are at that moment. Any generation a listed file mentions is kept, using the snapshot registry's mention rules: absolute paths under the generations root, in JSON values or text.
  - The retention receipt lists them as `referenced_by_reference_files`.

  Falsifier: a generation a listed file references is removed, or a stale copy of an earlier version of the file decides instead of the current one.
- **P2: fail-safe.** A listed file that is missing, unreadable or over the read budget means retention removes nothing in that pass. The receipt says why. Publication is never failed.
  Falsifier: a pruning pass with an unreadable reference file, or a refresh failure caused by retention.
- **P3: validated before work.** An invalid `retention_references` (a relative path, more than 32 entries, or a path outside `/config` or `/state`) is refused before any source work, like the other T5 options. When the field is absent, behaviour is exactly as today.
  Falsifier: a bad value accepted, or any change to the absent-field behaviour.
- **P4: documented.** The DOCKER.md refresh options table and the retention paragraph state the field and when to use it: a maintained knowledge catalog pinned separately from refresh. The T7 runbook uses it in place of a copied pin file.
  Falsifier: documentation the code contradicts.

## Write scope

- **FEATURE:** `packages/tooling/src/kp_agent_tooling/_impl/refresh_retention.py`, `refresh_cli.py` (request validation, passing the files to `prune`), `_impl/refresh_defaults.json` if it lists defaults, and `docs/DOCKER.md` (options table and retention paragraph). Every existing test stays unmodified and green.
- **TEST:** new files under `tests/` next to the existing T5 retention tests (find them with `grep -rl prune tests`), using temporary generation trees with `profile.json` files and reference files. No Docker and no network.
