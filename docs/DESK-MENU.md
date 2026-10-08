# Configure desks and memory views

Open the Kanban **Desks** menu (or `/?view=desks`). Choose **Create desk** and complete Name → Description → Role → Review. The review screen shows the optional settings, each on its own screen: **Change repositories** (0–32 repository names), **Change memory** (capture, and whether bound sessions may propose memory; a new desk has both on, and either can be switched off here) and **Change context** (a context document of at most 16 KiB that a bound session receives with the desk). A desk is a persistent working identity; it does not need its own repository. Creating one neither starts a session nor grants memory access.

The role picker lists the roster the registry is configured with. With a portable registry, **Add role** (in the menu header, or **Add a role** on the role screen) adds a role to that roster. Roles are templates only: no doctrine, approval reference or reviewer is required anywhere, and a role grants nothing.

Select a desk and **Link session context** to associate an already captured source session with repositories, ADR/card references and optional account/provider/model metadata. Leave unknown values blank. Versions are retained; concurrent changes require a reload. These operator annotations are separate from source admission and identity verification.

With a portable registry, select a desk and **Bind session** to admit one exact native session to that desk's memory. The binding records the target (harness, provider, model), the native session ID, the workspace and an optional parent session, with source `operator`. A session stays bound to one desk and target; binding it to another desk or model is refused. Binding is an operator or launcher act only: no MCP tool can create or change a binding or an admission.

Search **This desk**, **Repository**, or **Global**. Global means the current tenant's already eligible topic-search sources. Unassigned sessions appear there under existing rules but are excluded from narrower views until annotated. Results retain episode/event references and coverage counts. Missing results do not establish absence.

## Portable registry

A portable registry keeps desks as data in the memory store. Point a memory configuration's `catalog_path` at a private (mode 0600) registry descriptor:

```json
{"schema_version": "agent-tooling.desk-registry.v1", "tenant_id": "my-team", "roster_path": "/config/roles.json"}
```

The descriptor may also carry an optional absolute `harness_profiles_path`, naming the operator harness profiles that launch binding reads ([LAUNCH-BINDING.md](LAUNCH-BINDING.md)). No other key is accepted.

The roster (`agent-tooling.role-roster.v1`, 1–200 roles of `{role_id, label, purpose}`) lives at `roster_path`. Until a role is saved, the small generic default roster applies. The nine OPS roles ship only as the example roster `assets/roles/ops-roles.example.json`; copy it to `roster_path` (mode 0600) to use them.

Each desk has exactly one binding. A new desk's binding key is `binding_key(tenant_id, role='desk:<uuid>', repo_key='*')`. Its `memory_write` setting decides whether a bound session may call `memory.propose`; its repositories are metadata and do not restrict memory.

Registry actions (`kp-agent-desk-registry --config CONFIG ACTION`):

| Action | Input | Effect |
|---|---|---|
| `initialize` | — | Creates the memory stores (`sessions.sqlite3`, `episodes.sqlite3`) in the configuration's `state_root` if absent (never resets partial state) and the registry tables. It does not create `state_root`: that directory must already exist, mode 0700, owned by the user the registry runs as. |
| `list` | — | Desks, roster, recorded session bindings, contexts and sessions. |
| `roles` | — | The configured roster and its version. |
| `save-role` | stdin `{role_id, label, purpose, expected_version}` | Adds or updates one role; `expected_version` is the roster version. |
| `save` | stdin `{desk_id, name, description, role, repos, capture, memory_write, context_doc?, expected_version}` | Creates or updates a desk; the role must be in the roster. The older five-field shape `{desk_id, name, description, role, expected_version}` is still accepted: it creates a desk with no repositories, capture off and memory write off, and on update keeps the stored settings. |
| `bind` | stdin `{harness, provider, model, native_session_id, desk_id, source, workspace, parent_session_id}` | Records an `agent-tooling.session-binding.v1` binding and admits that exact session, in one transaction. `source` is `board`, `host`, `operator` or `import`. Optional `schema_version` (`agent-tooling.session-binding.v1`) and `recorded_at` (ISO-8601 with a zone) are accepted; without `recorded_at` the registry records the current time. |
| `import-catalog --catalog PATH` | — | Imports an `ops.imported-desk-catalog.v1` file; see below. |
| `annotate`, `search` | stdin | Unchanged. |

A malformed request to `save`, `save-role`, `bind` or `annotate` (a missing or
`null` field, a non-canonical `desk:<uuid>`, a missing `expected_version`, a value
of the wrong type) is refused with exit 1 and `{"status": "error", "category":
"ValueError", "message": ...}`, where the message names the field. `"Registry
unavailable; inspect the operator configuration."` is reserved for faults in the
registry itself, never for a request's fields.

The session admitted by `bind` uses the configuration's `provider_instance`. Its memory configuration is the same file with `provider_session_id` set to the bound native session ID. A configuration for any other session is refused.

`search` still runs as the configuration's own admitted session. To search from the Desks menu with a portable registry, bind the registry configuration's `provider_session_id` to a desk first.

### Importing an existing catalog

`import-catalog` reads (never modifies) a private `ops.imported-desk-catalog.v1` file. For each binding in the registry's tenant it creates one desk that keeps the original `binding_key`, role and label, with the original `repo_key` in `repos` and `memory_write` from `memory_write_allowed`. Missing roles are added to the roster. Episodes recorded under the old catalog remain searchable through the imported desk, and sessions admitted under it stay admitted. The report lists created, unchanged and conflicting desks. Re-importing creates nothing; an existing desk that differs, or a binding from another tenant, is reported as conflicting and left unchanged.

## In the Docker runtime

In the Docker runtime ([DOCKER.md](DOCKER.md#first-run-from-an-empty-root), steps
3 and 4) the registry runs in the `tooling` role through `docker exec -i`, and the
board's Desks menu runs it in the `board` role: the compose `board` role sets
`KANBAN_DESK_REGISTRY_COMMAND` to
`["/usr/local/bin/kp-agent-desk-registry", "--config", "/config/launch/registry.json"]`.
Both read and write the same registry state in the project's memory volume
(`/state/memory`, [DOCKER.md](DOCKER.md#the-store-volume)), so a desk saved
or a session bound in the menu is what `tooling`, `capture` and host launches see.
Until the config below is written, the menu reports that the desk registry is not
configured and the board keeps serving; once it is written, the next request uses
it, with no restart. The registry operator config is the same file host launches use:

| Runtime root file | Container path | Content |
|---|---|---|
| `$root/config/launch/registry.json` | `/config/launch/registry.json` | `ops.desk-memory.local.v1`: `state_root` `/state/memory/registry`, `catalog_path` `/config/launch/desks.json`, your `provider_instance` and the registry's own `provider_session_id`. |
| `$root/config/launch/desks.json` | `/config/launch/desks.json` | The descriptor: `tenant_id`, `roster_path` `/state/memory/registry/roles.json` and `harness_profiles_path` `/config/launch/harness-profiles.json`. |
| (memory volume) | `/state/memory/registry` | `state_root`, in the project's memory volume. Create it inside the runtime before `initialize`: `docker compose --project-directory "$root" run --rm -T tooling sh -c 'mkdir -m 700 -p /state/memory/registry'`. The host cannot write into the volume. |
| (memory volume) | `/state/memory/registry/roles.json` | The roster. `/config` is read-only in every role, so a roster that **Add role** or `save-role` can write lives in the memory volume. Until a role is saved, the default roster applies. |

Both configuration files are 0600. Then, with `registry` set to the argv
`docker exec -i <project>-tooling kp-agent-desk-registry --config /config/launch/registry.json`:

```sh
"${registry[@]}" initialize
printf '{"desk_id": "%s", "name": "Product", "description": "Work on the product repository.", "role": "general", "repos": ["product"], "capture": true, "memory_write": true, "expected_version": 0}' "$desk" | "${registry[@]}" save
printf '{"harness": "operator", "provider": "none", "model": "none", "native_session_id": "registry-operator", "desk_id": "%s", "source": "operator", "workspace": "%s", "parent_session_id": null}' "$desk" "$product" | "${registry[@]}" bind
"${registry[@]}" list
```

`desk` is a new `desk:<uuid>` in canonical (lowercase) form. Binding the registry's
own `provider_session_id` lets `search` and workspace capture run as that session.

## Operator configuration

Existing `ops.desk-memory.local.v1` configurations with an imported or workspace catalog keep working unchanged. For those, use an existing admitted portable memory config. Initialize once:

```sh
kp-agent-desk-registry --config /config/sessions/SESSION.json initialize
```

`KANBAN_DESK_REGISTRY_COMMAND` is a JSON argv array set before Kanban starts. In the Docker deployment the compose `board` role sets it to `["/usr/local/bin/kp-agent-desk-registry", "--config", "/config/launch/registry.json"]`, which runs in the board container itself; the board's images (`product`, `agents`) carry no `docker` binary, and the board never reaches the registry through `docker exec`. For a Kanban run outside a container, set it the same way with the absolute path of a host-installed `kp-agent-desk-registry` and the absolute host path of its configuration. When the command runs `kp-agent-desk-registry` itself and names no `--config`, or one that does not exist yet, the menu reports the registry as not configured without running it, and uses it from the first request after the file is written. Kanban appends only one of `list`, `roles`, `save`, `save-role`, `annotate`, `search`, `bind`, and refuses any request field the desk contract does not define. Keep this on the local operator interface; existing remote access controls still apply.

`memory.search` also accepts `view: {kind: "agent", value: "desk:UUID"}`, `view: {kind: "repo", value: "repository-name"}`, or `view: {kind: "global"}`. A view cannot be combined with an explicit binding. Profile metadata never authorizes a memory write; with a portable registry only a desk's `memory_write` setting, for a bound session, does.

Automatic capture enrollment remains exact-session based. Install the verified host/native transcript mapping, read-only transcript mount, and Stop/PreCompact/SessionEnd hooks. A stopped memory container or absent external drive produces a visible capture error. On recovery a later hook resumes from the durable cursor. Large source files remain subject to the capture service's explicit bounds; inspect pending bytes and refusal diagnostics.
