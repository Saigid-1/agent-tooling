# T2 — Portable desk registry, role roster and session bindings

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

## Problem (observed at the order's base)

Desk definition and memory access are coupled to the legacy governance model.

- **Role list.** `DeskProfiles.save` (`_impl/service/desk_profiles.py`) accepts only the nine OPS role IDs from the bundled `assets/desk_roles.json`.
- **Desk authority.** Memory access requires a `DeskAuthority` from `desk_memory_runtime.components()`, which is one of:
  - `ImportedDeskAuthority`: `ops.imported-desk-catalog.v1`, with a mandatory `approval_ref`;
  - `WorkspaceDeskAuthority`: a workspace context catalog whose setup (`desk_catalog_setup`) requires a doctrine file, reviewer, review time and team approval reference.
- **Binding keys.** Binding keys derive from (tenant, role, `repo_key`), so a desk is tied to one repository.
- **Two separate models.** The Desks UI (AT-0002) edits repository-independent profiles, but those can never grant memory access.

The Kanban seam (`desk-registry-bridge.ts` → `kp-agent-desk-registry {list, save, annotate, search}`) and the sidebar binding (`assistant_host_cli`) are the surfaces to build on.

## Interface surface

- **Role roster.**
  - A private JSON file named by the registry descriptor (below), with schema `agent-tooling.role-roster.v1`. Each role has `{role_id, label, purpose}`; there are 1..200 roles, and `role_id` values are unique strings of 1..128 characters.
  - The default roster is small and generic: at least one role, and no OPS role names.
  - The nine OPS roles ship only as an example asset that can be imported.
  - Registry CLI actions: `roles` (list) and `save-role` (add or update, using stdin JSON and `expected_version`).
- **Desk profile.** Fields: `desk_id` (canonical `desk:<uuid>`), `name`, `description`, `role` (must be in the configured roster), `repos` (0..32 bounded strings), `capture` (bool), `memory_write` (bool), `context_doc` (optional, ≤ 16 KiB text), `expected_version`. None of doctrine, approval or reviewer is required.
- **Registry descriptor.** A catalog file with schema `agent-tooling.desk-registry.v1`: `{schema_version, tenant_id, roster_path}`.
  - When a memory config's `catalog_path` names one, `components()` selects a new `RegistryDeskAuthority`, which implements the existing `DeskAuthority` protocol (`list_bindings`, `resolve`, `allows_memory_write`, `context`) over the registry's desks.
  - Each desk has one binding. A new desk's `binding_key` is `binding_key(tenant_id=<tenant>, role='desk:<uuid>', repo_key='*')`.
  - An imported desk keeps its original `binding_key`.
- **Session binding.** Schema `agent-tooling.session-binding.v1`: `{harness, provider, model, native_session_id, desk_id, source, workspace, parent_session_id|null, recorded_at}`.
  - `source` is one of `board`, `host`, `operator` or `import`.
  - `harness`, `provider` and `model` are bounded free strings here; T3 adds profile validation.
  - `bind` (a registry CLI action, stdin JSON) records the binding and admits that exact `native_session_id` to the desk's binding through the existing `DeskSessionLedger`, in one transaction.
  - `bind` is an operator or launcher action only. No MCP tool may create or alter a binding or an admission.
- **Importer.** The registry CLI action `import-catalog --catalog <path>` reads an `ops.imported-desk-catalog.v1` file. It is idempotent. It:
  - creates one registry desk per binding, preserving `binding_key`, role and label, with the original `repo_key` kept in `repos`;
  - adds any missing roles to the roster;
  - reports created, unchanged and conflicting desks.
- **Kanban.**
  - The desk-registry contract (`apps/kanban/src/core/desk-registry-contract.ts`), the bridge, the tRPC router and the web-ui Desks components gain the new desk fields, the configured roster, and actions to add a role and bind a session.
  - The desk dialogue keeps its one-concern-per-screen flow.
  - The existing annotate, search and list behaviours are unchanged.
- **Compatibility.** `ImportedDeskAuthority`, `WorkspaceDeskAuthority`, `ops.desk-memory.local.v1` and `ops.assistant-memory.local.v1` configs keep working unchanged.

## Properties and falsifiers

- **P1: desks are portable.** A desk whose role is in the configured roster, but not among the OPS nine, can be created, bound and used for memory write and search. It needs no doctrine, approval or reviewer anywhere in that flow. A role absent from the configured roster is refused.
  Falsifier: an OPS-only role list is enforced, a governance field is required, or an unlisted role is accepted.
- **P2: a binding admits exactly one session.** After `bind`, the `memory_cli` MCP with a config for that exact `native_session_id` can call `memory.search` and `memory.propose` for that desk. A config for any other session ID, or a desk the session is not bound to, is refused. No MCP tool (`tools/list` of `memory_cli` and of `kp-agent-tooling`) creates bindings.
  Falsifier: a cross-session or cross-desk admission, or an MCP route to binding.
- **P3: history is preserved.** Importing an `ops.imported-desk-catalog.v1` catalog yields registry desks with identical `binding_key` values. Episodes recorded under the old catalog are returned by desk-scoped search through the registry authority. Re-importing creates nothing.
  Falsifier: a changed key, orphaned history, or duplicate desks.
- **P4: compatibility.** Every existing test of the imported, workspace and assistant authorities, the Kanban seam CLIs and the sidebar binding passes unmodified.
  Falsifier: an edited or failing existing test.
- **P5: the UI works.** In Kanban web-ui component tests, the role picker lists the configured roster (not a hard-coded list), and creating a desk sends the new fields. The bridge rejects fields the contract does not define.
  Falsifier: a hard-coded roster, or unvalidated extra fields.

## Write scope

- **FEATURE:**
  - `_impl/service/desk_profiles.py`, `desk_memory_runtime.py` (a new authority plus selection in `components()` only), new `_impl/service/desk_registry.py` and `session_bindings.py`, `desk_registry_cli.py`, and `assets/` (new roster files; the OPS nine move to an example file);
  - `apps/kanban/src/core/desk-registry-contract.ts`, `src/server/desk-registry-bridge.ts`, the desks router in `src/trpc/`, and `web-ui/src/components/desk-*.tsx` with their tests;
  - `docs/DESK-MENU.md`.
  - Do not edit `desk_cli.py`, `assistant_host_cli.py`, `agent_tooling.py` or `refresh_cli.py`. T1 and T5 run in parallel, and T3 owns the launch path.
- **TEST:** new Python tests under `tests/desks/`, and new Kanban tests under `apps/kanban/test/runtime/desks/` and `apps/kanban/web-ui/src/components/__tests__/` (or beside the components, following the repository's convention).

## Acceptance at the meet

The Coordinator runs both suites plus the Kanban web-ui tests. The Coordinator also imports a copy of a real imported catalog from the live runtime's private config into a scratch store, without modifying the source, and checks that its binding keys and search results are preserved.
