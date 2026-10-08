# Composable deployment assessment

Status: recommendation following review of PR #3, not a deployed architecture.
The existing independent-image acceptance remains tied to its recorded image IDs;
merging new source does not update those images or host registrations.

Since this review, AT-0003 S3 replaced the Compose launcher described below with
the single manifest and installer ([DOCKER.md](DOCKER.md)), and AT-0004 T6a plans
the image targets (`runtime`, `product`, `agents`, `ops`). Statements below about
the `full` target and the PR #3 Compose file describe that earlier state.

## Recommended boundary

Ship one versioned release bundle, with independently startable services, rather
than one container supervising the whole product. Keep navigation and memory in
the existing tooling image initially: they already share an installed package and
CLI/MCP contracts. Run imports as a supervised process using that same image, not
as a new implementation or a second writer architecture. The small `runtime`
Docker target remains useful for memory-only installations; the `full` target adds
Serena, SCIP and local embeddings. Its current Compose launcher requires `full`;
a small-runtime launcher still needs acceptance before being advertised.

| Component | Default / optional | State and interface |
| --- | --- | --- |
| Tooling: navigation, memory, MCP | Default | Private memory and indexes; explicit repository inputs; stdio MCP |
| Import worker/service | Optional when capture/import is used | Same memory authority and existing job journal; supervised followers |
| Kanban browser UI and durable inbox | Optional | Own board/inbox state; narrow import client |
| OpenTelemetry collector | Optional | OTLP input; configured exporter, local or external backend |
| Tempo | Optional local trace storage | Own WAL/blocks and retention; unnecessary with an external backend |
| Host adapter | Only for local harness integration | Explicit native transcript access and exact-session identity; no implicit admission |

One tooling image suffices for headless tool/memory use. It does not suffice for
the complete browser plus telemetry experience without folding separate processes,
permissions and upgrade lifecycles into it. Reusing one image for several process
roles shares layers without requiring a monolithic container. Do not add another
database or graph service merely for packaging.

## Current implementation and gaps

`deploy/compose.yaml` already separates tooling, board and telemetry profiles,
uses explicit physical mounts, restricts the board to loopback, and retains the
board passcode and Host/Origin checks. Tooling health currently proves process
readiness, not provider/index/memory readiness. There is no public remote MCP
endpoint in this Compose contract: the accepted path is stdio through Docker exec.

PR #3's board import bridge launches an absolute, host-configured
`kp-agent-session-import` executable. The board runtime image contains Node and
Git, not Python or this executable. The Compose file does not configure the
bridge or mount native transcripts. Consequently the browser import feature is
**unconfigured in the PR #3 container bundle**, even though a host installation
and the real CLI bridge have passed tests. Do not mount the Docker socket into the
board to work around this, and do not silently borrow a host virtual environment.

The next integration should expose only the existing import actions over an
internal authenticated service boundary (a local Unix socket is sufficient for
single-host Compose). Reuse preview/apply/status/continue/stop/resume and their
receipts. The service, not browser input, selects allowed source roots and exact
session configuration. Browser/operator authentication and session admission remain
separate. Give the board access to the socket, not memory databases or unrestricted
shell execution. A service supervisor owns follower recovery, concurrency and
shutdown. Network deployment can add authenticated transport to this same narrow
contract after tenant isolation is tested; it is not provided today.

A remote container cannot read local Codex/Claude histories automatically. Use an
explicit host adapter with scoped read access, or a deliberate import/export flow.
Keep provider account credentials in the harness that owns them. Do not copy all
of a user's home directory into a hosted environment.

## Bare-install delivery sequence

1. Publish a release manifest containing exact source revisions, image digests per
   architecture, model artifacts/hashes, schema versions and supported upgrade
   paths. Current acceptance is linux/arm64 only. The existing base/telemetry tags
   and partially pinned provider dependency tree need locking for reproducible
   rebuilding; immutable published images give repeatable installation meanwhile.
2. Add an idempotent plan/apply bootstrap. Select components; validate physical
   storage, free space, UID/GID and port availability; provision private directories
   and configuration; download/verify the selected offline model; and prepare
   explicit repository mounts and desk catalog. Never create a local fallback
   when the selected drive is absent. Current manual marker/config provisioning
   and inactive-profile image-variable requirements belong inside this installer.
3. Implement and prove the narrow import connection above. Start with synthetic
   transcripts, absent config, unauthorized desk, source mutation, replay,
   restart, disconnect and persisted follower recovery. Reuse the CLI suite.
4. Add readiness diagnostics for identity, registered repositories, snapshot/index
   state, model availability, desk admission and import-service compatibility.
   Report an unavailable optional feature without making unrelated tools unusable.
5. Provide explicit refresh jobs with generation receipts and atomic activation.
   Source fetch, semantic indexing, maintained-document publication and memory
   capture have distinct triggers. Serena and OpenTelemetry do not automatically
   refresh all of these. Preserve old generations for rollback and expose stale,
   divergent, building and failed states honestly.
6. Test the release from an empty runtime root using only published artifacts,
   then restart and restore it. Verify CLI/MCP parity, scoped memory, browser
   import, inbox replay and optional telemetry. Promote the exact tested digests.
   Run this on amd64 as well before advertising both architectures.

The bootstrap should output a selected Compose configuration and ordinary
start/stop/upgrade commands; it need not become another long-running platform.
Host hook/MCP registration is an explicit optional setup step. Remote/public
hosting additionally needs transport identity, TLS, tenant boundaries and dependency
advisory review; current loopback trials do not establish that readiness.

## Sizing and cutover

The previous accepted full images and model cache displayed approximately 3.6 GB
in aggregate before repository snapshots, indexes, memory, traces and build caches.
Shared layers and Docker accounting mean this is not a fresh-install minimum.
The trial assigned 2 CPU/3 GiB to tooling, 1 CPU/1 GiB to the board, 256 MiB to the
collector and 512 MiB to Tempo. These are tested limits, not minimum hardware.
A reasonable initial evaluation allocation is 4 vCPU, 8 GiB RAM and 20 GB free
storage plus the selected repositories and retention budget; measure each workload
before publishing a supported minimum. No GPU is required for the accepted stack.

Before live cutover, freeze writers in the agreed quiet window, reconcile the
latest memory delta (including the coordinator's full-session import and delegate
claims), back up and restore-check state, build/test the merged source, update
registrations and exercise the same exact host session through CLI and MCP.
Retire legacy services only after replacement coverage and rollback pass. This
review and source merge perform none of those live changes.

## Local import packaging follow-up

The board image now installs the small portable memory CLI. The former opt-in
`compose.import.yaml` is folded into the manifest's `board` role (S3). That role
mounts the operator-selected config read-only, state, and the deliberately
selected sources read-only, so the existing bridge runs inside Docker for a local
single-operator installation. See CONTAINER-IMPORT.md and DOCKER.md. This local
packaging choice does not implement the proposed remote import-service boundary.
