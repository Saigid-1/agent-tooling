# S1 — Restore `knowledge.platform` and guard import closure

ADR: [AT-0003](../../adr/AT-0003-distributable-package.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

## Problem (observed at the order's base)

`kp_agent_tooling/_impl/service/platform_entry.py:21` does a function-local import of `kp_agent_tooling._impl.service.knowledge_coverage.platform_coverage`. That module does not exist in the package. Calling it raises `ModuleNotFoundError`. The `knowledge.platform` operation (`_impl/service/knowledge.py`, `operation == 'platform'`) therefore cannot produce a report. No test exercises the path.

Reference evidence (not a procedure): the legacy source repository, at the extraction commit, contains the module the extraction omitted, `kp_ops/service/knowledge_coverage.py`, and its test `tests/test_platform_entry.py`. Read them with `git -C <path to the legacy clone> show <extraction commit>:<path>`.

## Interface surface

`kp_agent_tooling._impl.service.knowledge_coverage.platform_coverage(config: dict, anchor: str) -> dict`

- **Inputs.**
  - `config` has the knowledge configuration shape: `platforms[anchor].sources` maps a repo key to `{revision}`, `repositories[key]` has `capabilities`, and there is an optional `scip_indexes[key]` list of `{revision, path, sha256}`.
  - `anchor` is a key of `config['platforms']`.
- **Return value.** A dict with exactly one entry per repo key in `config['platforms'][anchor]['sources']`. Each entry has the keys `context`, `retrieve`, `discover` and `symbol`:
  - `context.capability_ids`: the sorted capability ids of that repository.
  - `retrieve.eligible_path_count`: an int equal to `len(eligible_paths(repository))` from `_impl.service.knowledge_eligibility`.
  - `symbol.platform_key`: equals `anchor`.
  - `symbol.indexes`: one row per configured SCIP index spec for that repo, in configuration order.
    - A spec whose partitioned index loads, validates at the member revision, and matches its `revision`, `sha256` and `repo_key` yields `status: "digest-verified"` with `sha256` and `documents`.
    - Any other spec yields a row that has the key `gap: "index_unavailable_or_mismatched"` and does not have status `digest-verified`.
  - `symbol.next_call`: equals `{"tool": "knowledge.symbol", "arguments": {"repo_key": <key>, "target_revision": <member revision>}, "supply": ["path", "line"]}`.
- **Constraint.** No filesystem writes, no network access.

## Properties and falsifiers

- **P1: platform reports.** For a valid configuration, the `platform` operation returns `status` and `data.tool_coverage`, where `data.tool_coverage == platform_coverage(config, anchor)`.
  Falsifier: any configured anchor for which the operation raises, or omits `tool_coverage`.
- **P2: index honesty.** An index spec whose file is absent, whose digest differs, whose revision differs from the member revision, or whose `repo_key` differs is never reported `digest-verified`.
  Falsifier: any one of those four conditions produces `digest-verified`.
- **P3: import closure.** Every absolute import of a `kp_agent_tooling` module anywhere in the package source resolves to a module present in the package. That includes function-local and conditional imports, `import x` and `from x import y` forms, and imported names that are submodules.
  Falsifier: adding a function-local `from kp_agent_tooling._impl.nonexistent import thing` to any package module leaves the guard passing.

## Write scope

- FEATURE: `packages/tooling/src/kp_agent_tooling/_impl/service/knowledge_coverage.py` (new), plus the smallest necessary edits elsewhere in `packages/tooling/src`.
- TEST: new files under `tests/` only. Suggested names are `tests/test_knowledge_platform_coverage.py` and `tests/test_import_closure.py`.
- Both: do not modify `deploy/`, `apps/`, `scripts/`, `docs/` (except that FEATURE may add a one-line note to `docs/PORTABLE-KNOWLEDGE.md`), or other tests.

## Acceptance at the meet (the Coordinator runs these)

- The full suite passes with both arms merged.
- P3's falsifier is applied as a mutation and turns the guard RED.
- One live-shaped check: `platform_report` against a temporary configuration holding one real git repository and one deliberately mismatched index spec.
