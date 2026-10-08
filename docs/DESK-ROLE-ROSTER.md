# Desk role roster

`kp-agent-desk-setup roles` lists the starter roster for the reviewed catalog builder: Coordinator, Test Implementer, Feature Implementer, Verification, UX Coordinator, Deployment Engineering, Security, Analyst/Researcher and Auditor. The JSON is packaged with `kp-agent-tooling` (`packages/tooling/src/kp_agent_tooling/assets/desk_roles.json`). It is a menu of templates, not an admission or an inferred team assignment. Additional roles remain supported by the existing catalog builder.

The portable desk registry behind the Kanban **Desks** menu uses its own operator-configured roster instead; there the same roles are only an importable example. See [DESK-MENU.md](DESK-MENU.md).

A desk is scoped by tenant, repository and exact role ID. Sessions and models do not own the desk; multiple admitted sessions may use the same desk concurrently. Test and feature implementers have distinct memory scopes. Existing role IDs must be reused verbatim to retain their memory; display labels and friendly aliases must not silently create new bindings. In particular the existing identities are `Deployment Engineering` and `UX Coordinator`.

For a new workspace, select templates from `roles`, remove the presentation-only `key` and `purpose` fields, and add each actual human `approval_ref`. Supply those role objects to the existing `kp-agent-desk-setup catalog --role` interface together with the project and reviewed doctrine. Preview before applying. A template revision is not an approval reference.

For an existing imported catalog, add missing role/repository choices using the existing binding-key function. Preserve all existing rows, write permissions, source references and session admissions. New menu choices begin read-only. Record the human selection before enabling writes and admitting an exact session. A config file alone is not admission. Verify the host registry's session ID and repository; cwd is evidence, not permission to guess a cross-repository desk.

Use `kp-agent-desk ... admit --selection-output ...` to retain the selected desk for exact-session recovery. Confirm `memory.connection_status`, then exercise read/write through the session's MCP connection. Automatic transcript capture is a separate mapping: adding a desk or admitting a session does not install its capture hooks or map its native transcript.

## One role working across repositories

An operator may authorize an exact admitted session to write to its own role across registered repositories in the same tenant:

```sh
kp-agent-desk --config /config/sessions/EXACT_SESSION.json grant-write-scope --approval-ref 'Exact human ruling reference'
```

The append-only scope receipt is bound to the original session, provider instance, tenant, role and default binding. `revoke-write-scope` with an approval reference withdraws it. Other sessions do not inherit this grant. Catalog write permission remains an additional live check; merely registering a new repository does not enable its writes.

The model calls `memory.bindings` and selects an entry with `writable: true`. It passes that exact `target_binding_key` to `memory.propose`. Without a target, the default desk is used. A capsule has one destination; work relevant to two product repositories (for example `app` and `lib`) can produce one scoped capsule for each, citing the same immutable evidence. Citations may come from resolved source records belonging to any desk within this granted same-role scope. Conflicting/unresolved ownership and another role or tenant remain refused. Capsule attribution preserves source sessions and distinguishes the author desk from its destination.

For the approved Deployment Engineering session, the default is `Deployment Engineering @ deploy-receipts` (an example repository). Product work explicitly selects that role's product repository (and a shared library repository separately when relevant). This does not change filesystem permissions, grant source capture, infer a destination from cwd, or authorize writes into Coordinator/Verification memories.
