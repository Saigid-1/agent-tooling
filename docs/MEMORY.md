# Desk context and memory, independent of dispatch

Version 0.4.0. For the Docker lifecycle and host registration, use [DOCKER.md](DOCKER.md).
Building/installing the wheel alone changes no host registrations or existing stores. Navigation remains available through
`kp-agent-tooling`. Memory is a second MCP server/CLI in the same distribution.

## What is carried forward

- The recently verified `workspace_context` builder: approved team/desks, reviewed
  doctrine with content digest, declared source revision and bounded context.
- The existing immutable episode store, exact citation validation, loss-aware
  handoffs, cross-session evidence recovery, durable consolidation queue,
  model-budget contracts, summarizer interface and Claude capture/telemetry.
- Session admission is a small SQLite ledger extracted from the old launch module.
  It starts no process, creates no worktree and needs no card, dispatcher or graph.
- Stable desk identity remains tenant + role + repository. Provider instance/session
  identify the admitted session; model/provider fields record the selection only.
- Serena, SCIP, telemetry and CLI source tools retain their existing configuration.
  RAG documents remain an explicit configured corpus/service; memory does not
  silently become a new document index. Source semantic ranking requires the
  optional semantic extra and a compatible pinned index/model.

The heavy OPS binding registry, graph-backed desk facts/promotion and board app are
retired from this product, not prerequisites. Historical notes and runs require
an explicit operator import with the OPS extension
(`python -m kp_agent_tooling_ops.desk_import_cli --config C import-history --export-path P`);
they are not automatically migrated.
No code from the old graph services is included in the portable wheel. The canonical key
format and a small receipt/ledger are the dependencies retained for compatibility.

## Setup (human or trusted host)

1. Install `packages/tooling` with Python 3.11+ (see the README's Development section).
   Keep the existing navigation configuration. Install `[telemetry]` or `[semantic]`
   only when those features are needed. Serena and SCIP indexers remain external.
2. Choose an existing persistent private state directory (0700) on physical storage
   the operator selects. Never create a local fallback. In the Docker runtime it must
   be under `/state/memory`, the store volume: there, every tool refuses to open a store
   whose path does not resolve under it (`store_outside_volume`, naming the path); see
   [DOCKER.md](DOCKER.md#the-store-volume). A host install is not affected.
3. Copy `config/desk-context/catalog.example.json` and `doctrine.md` as a schema
   example. Replace the synthetic project, roles, approvals and doctrine with
   reviewed workspace values. Set catalog mode 0600. Keep only approved desks.
   `binding_id` and `memory_binding_id` are both `binding:` plus SHA256 of the UTF-8
   string `tenant_id|role_id|repo_key`; coordinates cannot contain `|`.
   Doctrine revision is `doctrine:` plus the SHA256 of exact doctrine bytes.
4. Create a session configuration (0600), using absolute paths:

```json
{
  "schema_version": "ops.desk-memory.local.v1",
  "state_root": "/your/private/state",
  "catalog_path": "/your/private/workspace/catalog.json",
  "workspace_root": "/your/private/workspace",
  "provider_instance": "claude-local",
  "provider_session_id": "the-existing-host-session-id"
}
```

5. Once per new state store, initialize. Then select an approved desk for the
   existing host session; this explicit operator invocation grants its binding:

```sh
kp-agent-desk --config '/your/session.json' initialize
kp-agent-desk --config '/your/session.json' desks
kp-agent-desk --config '/your/session.json' admit \
  --desk-id 'implementation-desk' --provider-id 'anthropic' --model-id 'your-model'
kp-agent-desk --config '/your/session.json' context
kp-agent-memory --config '/your/session.json' doctor
```

For Claude resume, the trusted host can retain the exact selection in a separate
private directory (0700). On the original operator admission, add
`--selection-output '/state/selections/HOST_SESSION_ID.selection.json'`
to `kp-agent-desk ... admit`. The file is written mode 0600 and names the exact
host session, selected tenant, role, repository, provider and model. Create the
host directory `$root/state/selections` (in the Docker runtime root) mode 0700
first; `/state` is the writable bind mount in the Docker deployment, while
`/config` is read-only to the container. The host launcher (shipped with the OPS
extension in `extensions/ops/scripts/`) can then use the host paths:

```sh
python extensions/ops/scripts/launch_claude_desk_memory.py \
  --session-config-root "$root/config/sessions" \
  --selection-root "$root/state/selections" \
  --docker-command '/absolute/path/to/docker'
```

It checks the exact host session,
recreates a missing config atomically, asks the portable desk service to verify
admission against the live catalog and ledger, and emits a binding receipt before
starting MCP. An existing config with a missing or corrupt ledger still refuses;
the launcher never initializes storage. No selection file, a conflicting claim,
catalog withdrawal or a different host session requires an explicit operator
admission. Printed SessionStart ledgers and recalled facts do not grant one.

Admission freezes the context in SQLite, including its digest. Repeated admission
cannot change the session's desk or model selection. A successor session gets a new
configuration/session id and may use another model/harness while recalling the same
desk's episodes. Do not reinitialize shared state. Current catalog withdrawal denies
subsequent memory calls; a frozen context is evidence, not perpetual authorization.

The context's initial memory field explicitly says no resolver was supplied; this is
not an empty-corpus verdict. Invoke model-facing memory tools to select source episodes
and an explicit capsule. No implicit 'latest is authoritative' selection occurs.

## Legacy claim versus portable admission

The OPS `desk_memory_cli.py claim` command corrects a legacy desk-run attribution record.
It does not admit a portable session or add a verified desk binding to an existing
gateway credential. The old gateway's `memory_recall`/`memory_write` require the
`desk_binding` claim in their verified principal; `desk_binding_required` is not
proof that the run ledger failed to update. Restarting that gateway does not turn
a historical claim into authorization.

For this product, use `kp-agent-desk-setup session --admit` (or `kp-agent-desk admit`)
with the current native session ID and reviewed catalog, then register
`kp-agent-memory --config /exact/session.json serve` or the existing Claude
exact-session launcher below. Check `memory.status` and use `memory.search` for
imported history. Reconnect that memory server after changing its registration.
Do not reuse another session's config or silently promote a legacy claim to a grant.
Navigation's memory pointers do not register this second MCP server automatically.

## Host tool binding

Register this command as a separate stdio MCP server in the chosen host:

```json
{
  "command": "/your/venv/bin/kp-agent-memory",
  "args": ["--config", "/your/session.json", "serve"]
}
```

Tool names: memory.connection_status, memory.status/list/search/session/bindings,
memory.episode_directory/read_event, memory.handoff/evidence_directory,
memory.resume/handoff_page and memory.propose.
The host may require a reconnect after registration. CLAUDE.md cannot install tools.
`kp-agent-desk` is operator-only; do not expose admission as a model tool. A session
cannot select a tenant, role or repository through memory tool arguments.

This single-owner composition trusts the host and OS account. It is not isolation
from an adversarial process sharing the same user's filesystem access. Multi-user
cloud authentication is not established here.

## Capture and consolidation

The packaged `ClaudeEpisodeCapture`, `HookTelemetry`, `EpisodeStore` and
`ConsolidationQueue` are the existing implementations, not a new summarizer.
Initialize the queue after desk admission (`kp-agent-claude-capture` is installed by
the OPS extension, `kp-agent-tooling-ops`):

```sh
kp-agent-memory-queue --config '/your/session.json' \
  --queue '/your/private/state/queue.sqlite3' initialize
kp-agent-claude-capture --memory-config '/your/session.json' \
  --capture-ledger '/your/private/state/capture.sqlite3' \
  --queue '/your/private/state/queue.sqlite3' \
  --telemetry '/your/private/state/hooks.sqlite3' \
  --transcript '/exact/host/session.jsonl' initialize
```

Use the same capture arguments with `settings` to print the scoped Stop/PreCompact/
SessionEnd hook configuration; install it in that host session/project after checking
its transcript binding. `status` reports capture/queue evidence. Printing settings
does not install them. The package never rewrites global host settings.

For a Claude host using the Docker image, `settings` accepts
`--host-command-prefix '/absolute/docker exec -i <project>-tooling'` so the printed
hook command runs in the container. When Claude's hook payload reports a host path
different from the container's read-only transcript mount, add
`--hook-transcript-path '/exact/host/transcript.jsonl'`; keep `--transcript` set to
the mounted container path. The operator-fixed mapping checks the payload path and
the adapter still reads only its configured file. Mount the specific transcript or
private transcript directory first and verify container read permission. These
options belong in scoped host settings; a generated command is not proof it fired.

The queue's `work-once` retains the existing explicit model profile, source-digest
approval and key-environment requirements. Outbound summarization is optional. With the
optional `summarizer` role selected and a desk in its standing approval, every sealed
episode of that desk that reaches the queue is sent for summarization, under the
approved model and the model gateway's budget, without a per-episode digest approval
([DOCKER.md](DOCKER.md#optional-roles), `summarizer`). Without the role, or for a desk not in the approval, nothing changes:
install, binding, capture and reads never trigger an outbound call. Ambiguous outbound
attempts require review rather than automatic retry. A request the gateway refused
before the network (a budget cap, a required confirmation, a refused key file) was not
sent: it is not ambiguous, and its job stays queued. Native harness compaction remains
owned by that harness; these hooks preserve external evidence/handoffs, not hidden context.

Codex and other harnesses may bind and use the memory tools now. Automated capture
requires their own verified visible-event adapter; do not pretend a Claude hook
captures Codex. Reuse EpisodeStore.capture for trusted adapter events, preserving
source identity and explicit coverage rather than inventing missing events.

## Evidence boundary

Package tests prove local storage, source recovery, cross-harness desk identity and
scope isolation with controlled fixtures. They do not prove a newly installed host
hook fired, a production RAG corpus is complete, or doctrine is correct. Keep those
claims separate in the ADR lifecycle. Existing graph notes use the additive
history import of the OPS extension (`import-history` of
`kp_agent_tooling_ops.desk_import_cli`). Import, index rebuild and reconciliation
stay operator actions; no destructive migration runs during admission or reads.

Summary attribution, preserved negative evidence, review obligations and bounded
recovery: the summary evidence contract
(`packages/tooling/src/kp_agent_tooling/_impl/service/summary_evidence.py`).
These checks do not turn source assertions or static navigation into verified
execution.

Outbound summary routing defaults to provider fallback with `zdr: true` and
`data_collection: deny`, retaining `require_parameters: true`. Fallback cannot
weaken privacy filters; no eligible endpoint is an explicit failure, not permission
to retry without them. The queue CLI exposes `--no-allow-provider-fallbacks` and
an explicit operator `--no-require-zdr` override; privacy is never downgraded by a
retry. This affects the summary adapter/queue CLI, not account-wide settings or
other dispatch clients. The `summarizer` role sends the same routing on every request,
through the model gateway to the route's provider only; it has no `--no-require-zdr`, a
route `params` entry that would weaken the routing or add a fallback model list is
refused before the network, and a response whose reported model is not the requested
model goes to review with no capsule. OpenRouter's ZDR classification allows transient in-memory
caching. The review of the verified wire request (`nemotron-private-01`) is not
carried in this repository.

## Claim notes: bounds, source qualifiers and errors

`memory.propose` stores a claim note (a cited handoff). These bounds are refusals:

| Bound | Limit |
|---|---|
| episodes | 1..32 distinct source episodes |
| items | at most 32 handoff items |
| item text size | 1..2000 UTF-8 bytes (and at most 2000 characters) per item |
| citations | 1..8 per item |
| citation range | `0 <= start < end <=` the cited event's length in characters; the quote is at most 8000 characters and equals those exact characters |
| unresolved questions | at most 16, each 1..1000 UTF-8 bytes (and at most 1000 characters) |
| handoff budget | `budget_bytes` 1000..24000 (default 12000); the stored handoff must fit it |

Source qualifiers are negated source sentences ("not", "never", "without" and similar
English words) kept beside the note, so that a summary does not hide a nearby "not".
They are a review signal, not admission control. `memory.propose` takes them from
the cited events only: the `(episode_id, event_id)` pairs its citations name, not
every event of every cited episode. The handoff records the scan in
`source_qualifier_scan`: `scope: "cited_events"`, the scanned events grouped by
episode, the budget and the sentence counts. `evidence_review.qualifier_scope`
is `cited_events`.

A capsule carries at most 64 qualifier sentences of at most 2000 bytes each, inside
its handoff budget. When the cited events hold more, `memory.propose` still succeeds:
- it keeps qualifiers within the budget: sentences that overlap a cited range first,
  then the rest in source order; the kept ones are listed in source order;
- `source_qualifier_scan.omissions` records, per cited event, how many sentences were
  omitted and by which bound (`sentence_count`, `sentence_bytes`, `handoff_bytes`);
  omitted sentence text is not copied into the capsule;
- `evidence_review.status` is `review_required` with `reasons:
  ["source_qualifiers_omitted"]`. `memory.resume` and `memory.handoff_page` carry
  that review. Reopen the cited events with `memory.read_event` before relying on
  the note.

The summarizer consolidation paths (the queue's `work-once`) keep their pre-flight
over every event of the source episodes. Before any paid proposal call it refuses a
source that holds more than 64 qualifier sentences, or one longer than 2000 bytes;
split such sources explicitly. The capsule they then write takes its qualifiers from
the cited events, as `memory.propose` does, so the same items yield the same capsule.
Capsules stored before this change have no `source_qualifier_scan`;
their qualifiers came from every event of the cited episodes. They read back and
verify unchanged.

Memory tool errors carry only `status`, `category` and `guidance`. They never include
source text, a source path, a desk identifier or a binding key the caller did not
supply.

| Category | Meaning |
|---|---|
| `bound_exceeded` | A declared input bound was exceeded. The guidance names the bound and its limit, for example "Bound exceeded: items, at most 32 handoff items per proposal." Bounds include those above, page offsets, the resume budget (1500..24000) and the search query (1..16 words). |
| `invalid_arguments` | Schema shape: a missing or unknown field, a wrong type, pattern or enum, or duplicate episodes. |
| `source_conflict` | A quote that differs from the exact source characters, or a citation of an event that is not in the cited episodes. |
| `unknown_binding` | A `binding_key` that is not registered. |
| `operation_not_permitted` | A write outside this admission's scope. |
| `memory_unavailable` | Admission, record availability or service health. |
| `projection_incomplete` | The store holds rows sealed without a read-scope projection (an older writer, or a store not yet upgraded). Reads refuse rather than scan; the operator runs `upgrade-sources` (see [Memory reads](#memory-reads-one-roundtrip-per-call)). |
| `unknown_operation` | A tool name that is not listed. |


## Accepted summarization baseline

Use GLM 5.3 Flash through OpenRouter for new summarization profiles. The profile
builder defaults to `z-ai/glm-5.3-flash` and provider `openrouter`; an explicit
`--model-id` remains supported. The queue selects low reasoning and JSON output
for that exact model, validates both against fresh metadata, and uses a 120-second
deadline. Explicit reasoning/JSON overrides remain available. Existing profiles
are not rewritten or silently redirected.

Create a fresh receipt (metadata expires after 24 hours), using the tested reserves:

```sh
python scripts/memory_model_profile.py --fetch \
  --system 4096 --tool 1024 --reasoning 4096 --output-tokens 8192 --safety 2048 \
  --output '/your/private/state/glm-profile.json'
```

Supply that receipt to the existing queue `work-once --profile` invocation along
with the admitted session configuration, approved source digests and key environment
reference. This only selects the summarizer: install and capture do not authorize
outbound calls; only the `summarizer` role's standing approval does, for its approved
desks. Fallback remains restricted by ZDR and denied data collection;
an unavailable model or unsupported parameter fails without selecting a different
model or weakening privacy.

The accepted trial matched Hindsight's ten-fact coverage at $0.00040685 per
synthetic episode. Its review (`glm-hindsight-cost-01`), with scope, citation gaps
and cost caveats, is not carried in this repository. Hindsight integration is
parked for this slice.
Successor models require an explicit evaluation and selection; no automatic
quality or price equivalence is assumed. This source configuration change is not
a shared Docker deployment receipt.

## Explicit reads across desks

Memory writes remain bound to the admitted session. Read tools accept optional
`binding_key` for an approved desk in the same tenant; omit it for your own desk.
Use `memory.bindings` to discover registered keys rather than inventing them.
Returned records identify the selected binding and source sessions, label another
desk's claim, and leave absent actor names or observation times unknown. A valid
empty desk is reported as zero results; an invalid binding is an error. Resume
and handoff recovery keep the explicit read selection. This portable catalog has
no shared-audience designation, so default reads do not include a shared scope.

Commit attribution is a separate consequence checkpoint, documented outside this
repository. A deliberately captured commit declaration can be
cited through a summary; commits are not automatically ingested by this release.

## Historical session sources and claim filters

Historical import need not assign a desk. The session source contract is
`packages/tooling/src/kp_agent_tooling/_impl/service/session_sources.py`.
`memory.search` defaults to the admitted desk. Use `scope: "topic"` to include all
same-tenant sources, including unresolved attribution; `attribution: "unresolved"`
selects only those sources. `claims` takes conjunctive `{predicate, object: {kind, id}}`
filters, for example `session.tag` / `tag` / `performance`. Follow each result's
`next_call` for exact source recovery. `memory.session` returns the full additive
claim history. Ownership and contribution never grant admission or write authority.

### Workspace capture is unresolved evidence

Workspace capture (`kp-agent-workspace-capture`, the Docker `capture` role) records
**unresolved evidence**. It verifies that a native transcript belongs to an approved
repository, not that it belongs to a desk, so its source sessions carry no
`session.owner` claim. That holds even for a session that `bind` attached to a
desk: a binding admits the session, it does not attribute workspace-captured
transcripts. Desk-scope `memory.search` therefore excludes those episodes and
reports them in `excluded_unresolved_episodes`; `scope: "topic"` (with `attribution`
`any` or `unresolved`) reaches them.

Desk-attributed capture comes from the bound session's own capture path: the launch
hooks of a bound launch (T3, [LAUNCH-BINDING.md](LAUNCH-BINDING.md)) and the host
adapter's spool ingestion (T4, [HOST-ADAPTER.md](HOST-ADAPTER.md#spool-ingestion)).
Those capture under the bound desk's binding, so desk-scope search finds them.

## Memory reads: one roundtrip per call

Every memory tool call costs the same number of SQLite statements whatever its
result or citation count, and its cost does not grow with unrelated history.

- **Once per call.** Admission (including the ledger `PRAGMA quick_check`), the
  registry read (descriptor, catalog or roster file, each opened once) and the
  read scope are resolved at most once per call. Nothing from the store is
  cached between calls, so a catalog change, claim or seal shows on the next read.
- **Scope projection.** `episodes.sqlite3` holds a projection of every sealed
  episode's read scope: `episode_scope` (storage table and binding, order
  sequence, source session and tenant, payload digest, type-guarded source
  coordinates, attribution status and single desk), `episode_desks` (desk-scope
  membership), `episode_claims` (the active claims that apply to the episode,
  for claim filters) and `scope_counts` (maintained totals per desk, storage
  binding and tenant, with index coverage). Seal, claim and link write it in
  their own transaction; a claim re-projects only that session's episodes.
  Registry membership is not projected: whether a storage binding belongs to
  the tenant comes from the call's registry read.
- **The projection grants nothing.** Each record a call returns or cites is
  reopened from its sealed row in one batched read with its link, session and
  claims; its scope is re-derived from the content-addressed claims. A record
  the re-derived scope excludes is dropped. A session whose sealed metadata or
  claims fail their ID check refuses (`memory_unavailable`) only the reads whose
  results, citations or session filter include it; other reads in the tenant
  are served.
- **Search.** The search index is attached read-only to the store connection and
  one statement joins the full-text match to the projection, ordered by rank,
  over a fixed 201-row candidate window. Candidates are checked against the
  indexed text; only returned results are reopened and checked against the
  sealed source. Until the indexer owns per-desk postings (T12), a desk search
  for a word common in other desks costs time in proportion to those matches.
- **Coverage.** `covered_episodes` comes from per-episode coverage marks that
  the index writers (`rebuild`, `upsert_episodes`) record in the store, tagged
  with the index file's coverage token. A reassignment moves coverage between
  desks without opening the index; a replaced or recreated index starts from no
  coverage. `rebuild` also optimizes the index to one segment.
- **Integrity.** Opening the memory tools checks that the store is readable; the
  whole-store `PRAGMA quick_check` (whose cost grows with the store) runs in
  `upgrade-sources` (and on a store just initialized, which is empty), never on
  a call or a capture pass.

### Upgrading an existing store

A store written before this release, or a row written by an older writer, has
no projection. Reads that resolve episode scope (search, list, status, episode
directory, read event, propose, and capsule reads that attribute their cited
episodes) then refuse with `projection_incomplete`, never falling back to a scan,
and `memory.connection_status` reports `status: not_ready` with the same
category. `memory.session`, `memory.bindings` and own-desk `memory.resume` and
`memory.handoff_page`, which resolve no episode scope, still answer. The
operator runs, once:

```sh
kp-agent-desk --config '/your/session.json' upgrade-sources [--batch-rows 500]
```

It first reads the completion marker and the per-table marks. When the
projection is complete and no table is behind, it projects nothing and runs no
`quick_check`. Otherwise it checks the store with `PRAGMA quick_check`, then
projects every unprojected row in bounded batches (each its own transaction),
verifying each sealed row's content address first; a mismatch stops it with the
store still marked incomplete. It records which episodes the store's search index
(`episode-search.sqlite3`) covers, and ends with the completion marker and a
report (`status: complete`, rows `projected` and `already_projected` per table,
`batches`, `verified_episodes`, `coverage`, the per-table `marks`, and `rebuilt`).
It is idempotent (a second run changes nothing) and safe beside running writers of
this release, which project their own rows. A trigger counts every row each
table receives, so a row an older writer adds later is detected on the next
read without scanning.

Every projection table declares its primary-key columns first: on the
runtime image's SQLite (3.40.1), `quick_check` reports false NULL values in a
`WITHOUT ROWID` table with a column declared between its key columns. A store
whose `episode_desks` was created by the first projection release (`tenant`
declared second, found by `PRAGMA table_info`) is rebuilt key-first by
`upgrade-sources` in one transaction (rows unchanged, its index recreated),
reported as `rebuilt: {"episode_desks": <rows>}`, and then checked with
`quick_check`.

The workspace capture worker (`kp-agent-workspace-capture`, `once` and each
`watch` pass) never installs, rebuilds or backfills the projection and never
runs `quick_check`; it only creates the catalog tables its own writes need. On a
store without the projection its rows are sealed unprojected and counted once
`upgrade-sources` installs the marks. On an incomplete projection each write
projects only the rows it seals, so reads keep refusing with
`projection_incomplete` until the operator runs `upgrade-sources`. On a
complete projection the same per-write projection keeps it complete.

## A connected server reporting missing setup

The host launcher now keeps a diagnostic-only MCP connection when startup setup
is refused. `memory.connection_status` reports the startup reason, exact host
identity/configuration path when known, and operator recovery guidance. It cannot
read memory, select a desk or admit the session. Fix setup through the existing
operator flow, then reconnect `ops-desk-memory` to rerun admission verification.
An existing board-memory claim or a working legacy `desk_memory_cli.py` command
does not configure portable memory. Properly admitted sessions continue to use
the original native MCP server; no memory tool behavior changes.

Preliminary admission/context checks do not read MCP stdin. Only the final native
memory server receives initialize and tool-call frames, preventing the resume
preflight from consuming the handshake.

## Bounded Codex history jobs

See [session import and attribution](memory/SESSION-IMPORT.md) for the Cline dialog,
local slash command, full-snapshot and forward scopes, and operator CLI.
[Delegation attribution](memory/DELEGATION-ATTRIBUTION.md) records verified native
parentage separately from an asserted work role and desk admission.
