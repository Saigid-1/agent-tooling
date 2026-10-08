# Fresh workspace: navigation, desk selection and native history

Use the installed `kp-agent-tooling` package. No legacy application, platform service,
board, model provider or preconfigured journey is needed for this basic path.
Keep source repositories and private state separate. The commands create no host
registration or global hooks automatically. Keep all generated state on physical
storage the operator selects; never fall back to a local directory when it is absent.

## 1. Configure an unfamiliar repository

Commit the source you want to inspect. Keep the tracked working tree clean, or
use a separate checkout. The source revision is observed from Git, not guessed.

```sh
kp-agent-setup plan --repository '/absolute/repo' --repo-key project \
  --output-root '/absolute/private/tooling'
```

Review the returned source revision, gaps and `plan_sha256`. Repeat with `apply`
and `--expected-plan-sha256 '<returned hash>'`. The resulting bundle contains
`tooling.json`, the reviewed plan, and `mcp.json` for host registration. Paths with
spaces must be quoted. If the executable is not on PATH, supply
`--launcher-command '/absolute/venv/bin/kp-agent-tooling'`.

```sh
kp-agent-tooling --config '/absolute/bundle/tooling.json' doctor
kp-agent-tooling --config '/absolute/bundle/tooling.json' tools
kp-agent-tooling --config '/absolute/bundle/tooling.json' call \
  --tool navigation.search \
  --arguments '{"repo_key":"project","target_revision":"FULL_COMMIT","query":"entry point"}'
```

The basic profile enables source reading, paths, bounded literal search, manifest
and paged search, dependency/import reads, identity and delivery reads
(`delivery.read`); a reviewed Serena configuration adds `serena.inspect`. These are
core tools only.
`doctor.tool_availability` identifies optional providers that are unconfigured or
unavailable. Configuration readiness is not execution proof. Semantic and SCIP
index building, reviewed Serena configuration, maintained RAG documents and runtime
telemetry remain separate setup steps; no index is silently downloaded or invented.
`kp-agent-refresh` (the refresh role in `docs/DOCKER.md`) builds and publishes SCIP
and semantic indexes.

For scheduled refresh, declare platform membership and Python analysis dependencies
in the refresh request explicitly. An omitted `platforms` map creates no platform.
Each repository may supply `python_package_name`, `python_version_source`
(`revision` or `pyproject`), `analysis_dependencies` and `python_extra_paths` (the
last two are lists of configured repository keys). Defaults are the repository key,
revision version and empty lists. No topology between repositories is inferred from their names. Existing refresh configurations that
previously relied on implicit analysis of a shared library or on implicit platform membership must declare
those relationships before upgrading; otherwise the generic defaults remove them.

## 2. Prepare a reviewed catalog

Create private output and state directories (0700), and a doctrine file inside the
workspace (`--doctrine-source-ref` is its path relative to the workspace root).
Supply actual review references and reviewer identity. A generated hash proves the
selected bytes, not that a human reviewed them. An agent must not invent
approval values or reuse test-fixture approvals for a real workspace.

```sh
kp-agent-desk-setup catalog \
  --workspace-root '/absolute/repo' --catalog-path '/absolute/private/catalog.json' \
  --project-id project --tenant-id YOUR_TENANT --repo-key project \
  --source-revision FULL_COMMIT --team-approval-ref YOUR_REVIEW_REFERENCE \
  --doctrine-source-ref doctrine.md --doctrine-id project-doctrine \
  --doctrine-reviewed-by YOUR_REVIEWER --doctrine-reviewed-at ISO_TIMESTAMP \
  --role '{"role_id":"implementation","label":"Implementation","desk_id":"implementation-desk","desk_label":"Implementation desk","template_revision":"role:v1","approval_ref":"YOUR_ROLE_REVIEW_REFERENCE"}'
```

This previews a validated catalog with computed binding IDs and doctrine digest.
Repeat with `--apply` to write it privately. Repeat `--role` for each explicitly
selected role. An existing differing catalog is never overwritten.

## 3. Select one existing host session

Obtain the exact current native session ID from the harness. The operator asserts
that identity; setup does not independently verify host-session existence. Choose the provider instance,
desk, provider and model explicitly. Use the same arguments for each of these steps:

```sh
kp-agent-desk-setup session \
  --config-path '/absolute/private/SESSION.json' \
  --catalog-path '/absolute/private/catalog.json' --workspace-root '/absolute/repo' \
  --state-root '/absolute/private/state' --provider-instance claude-local \
  --provider-session-id EXACT_SESSION --desk-id implementation-desk \
  --provider-id anthropic --model-id YOUR_MODEL
```

First preview, then repeat with `--apply` to create the config. Initialize a new
store once with `kp-agent-desk --config ... initialize`; never initialize over
existing or partial state. Repeat the session command with `--admit`, optionally
adding `--selection-output '/absolute/private/selections/EXACT_SESSION.selection.json'`
(the selections directory must already be private). Admission and recovery affect
only this exact session. No standing permission for future sessions is implied.

`kp-agent-memory --config ... doctor` checks the resulting memory service.
Register `kp-agent-memory --config ... serve` with your harness using this session's
config. See [MEMORY.md](MEMORY.md) for the Claude resume launcher and capture hooks.
Model-facing tools cannot admit themselves or choose another session's config.

## 4. Import a selected native transcript

The operator CLI accepts native Claude and Codex JSONL files. Preview first:

```sh
kp-agent-desk --config '/absolute/private/SESSION.json' import-native-history \
  --runtime claude --tenant-id YOUR_TENANT \
  --source-file '/absolute/native/session-uuid.jsonl'
```

The configured session must be admitted. `--tenant-id` must match its admitted
tenant for both preview and apply; this command cannot import into another tenant.
Repeat with `--apply` only after reviewing counts, scope and rejection reasons.
For Codex use `--runtime codex` and its native rollout filename. Repeat
`--source-file` for a bounded selection. Folder mode requires `--source-folder`,
`--project` and `--date`; it is shallow and does not sweep every project's history.
Claude project means the native project-directory name; Codex project means the
exact session metadata `cwd`. Claude project values beginning with a dash work as
`--project -Users-example` or `--project=-Users-example`. Select subagent files explicitly.

Imports retain immutable visible source events and provenance. They do not assign
a desk, accept historical role claims, or authorize outbound summarization. Search
unattributed imported evidence with `memory.search`, `scope: "topic"`, from an
admitted session of the same tenant. Role-specific recall requires separately
reviewed attribution. Replays verify sealed coordinates and report conflicts rather
than overwriting history. Oversized files, malformed identities and partial rows
remain explicit gaps. Codex message rows that resemble another visible response
are retained and labeled as possible mirrors; equal text does not prove identity.
For incremental import, pass `--cursor-json` mapping each selected source path to
its returned `resume_cursor`. A changed prefix is refused. This import is not a substitute for a live compaction hook.

## 5. Attach a host/card observation (optional)

`kp-agent-host-card --config ... --event '/absolute/private/event.json'` previews a
host assertion. Add `--apply` to capture it and append a `session.tag` claim using
the existing memory store. A card is qualified by board and workspace, not its
short ID alone. The event must be an owned private file with these required fields:

```json
{"schema_version":"ops.host-card-event.v1","provider_instance":"claude-local","host_session_id":"EXACT_SESSION","board_id":"YOUR_BOARD","workspace_id":"YOUR_WORKSPACE","card_id":"CARD_ID","event_id":"UNIQUE_HOST_EVENT","recorded_at":"ISO_TIMESTAMP","asserted_by":"YOUR_HOST_ADAPTER"}
```

The configured session must already be admitted. Repeating the same event is safe;
changed bytes under the same event identity are refused. A successful apply refreshes the existing episode-search projection, including on
replay. If projection refresh fails after capture, repeat the same event to repair
it; immutable capture and claims are preserved. The receipt contains a
claim filter for memory queries. That filter selects the associated session, not
proof that every event in it concerns the card. This proves recorded attribution only. It does not
prove Kanban launched the session, establish commit linkage, grant permissions or
verify harness policy. Those remain the ADR0013 integration trial obligations.

If the same session has imported native history, add `native_runtime: "claude"`
or `"codex"` to the event. The bridge verifies that the exact native session is
already registered under the admitted tenant before capturing anything. It tags
both source identities, recording the mapping as operator asserted. It does not
assign desk ownership to the imported history. Without this explicit field, the
card filter covers the live provider-instance identity only.
