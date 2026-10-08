# Independent image acceptance

The acceptance environment is isolated from the live legacy containers, host registrations and catalogs. Its purpose is to prove the extracted package can run without a legacy checkout on Python's import path or the legacy graph service. Analyzed Git repositories remain explicit read-only inputs, not runtime dependencies.

## Build and run (the recorded 2026-09-25 procedure)

This section records how the 2026-09-25 acceptance was run. It predates the single manifest and installer (AT-0003 S3, see [DOCKER.md](DOCKER.md)) and the multi-target image planned in AT-0004 T6a (`runtime`, `product`, `agents`, `ops`). A new acceptance should use those, not the files described here.

From the physical `agent-tooling` checkout, the trial built the then-current tooling image with target `full`, and the separate Kanban image. `SOURCE_REVISION` was the exact committed source SHA. The tooling `acceptance` target added tests and pytest to a derivative image; it was not the production image.

The Compose file of that time composed tooling with optional `board` and `telemetry` profiles. It took immutable `AGENT_TOOLING_IMAGE` and `AGENT_KANBAN_IMAGE`, a physical `AGENT_ROOT`, offline `AGENT_MODELS`, and the owner UID/GID. Each trial used a unique Compose project name. No fixed container names were imposed.

Before launch, the trial provisioned private directories `state/tmp`, `state/snapshots`, `state/navigation`, `config`, `board/tmp`, `board/kanban`, and `tempo` under `AGENT_ROOT`, and created `state/.ops-tooling-volume` containing `ops-tooling-state-v1`. The navigation config, `$AGENT_ROOT/config/navigation.json`, named the container paths of the snapshot and navigation registries. Keep private configuration mode 0600 and mount it read-only. Add explicit read-only repository mounts for the configured target repositories. No directory fallback is created when a mount is missing.

Only the board is published to a loopback host port (3487 by default). It retains its native passcode gate. `KANBAN_PUBLIC_ORIGIN` names exactly the browser-facing origin, so mapped Docker ports pass the Host/Origin checks without broadening them to arbitrary sites. Tool access remains stdio through the installed `kp-agent-tooling` command. Native host admission must use the exact session config; starting a container does not admit any session. Provider CLIs and their account credentials are optional separate integrations, not included or copied into the board image.

The telemetry profile routes OTLP to the collector and persists Tempo WAL/blocks under `AGENT_ROOT/tempo`; retention is 24 hours. It does not install application instrumentation. The consumer-application telemetry handoff remains owned by that application's maintainers.

## Prepared document publication

`kp-agent-knowledge-publish --request /private/request.json` (installed by the OPS extension, `extensions/ops`) reuses the existing full-input embedding, chunk identity, extraction and resolution rules. The request schema is `agent-tooling.knowledge-publication.v1` and requires: `runtime_config`, a fresh physical `destination`, `source_repo_key`, `code_revisions` (registered repo keys to full commit SHAs), `paths` (the complete maintained document selection for this source), and `registry_path` (a literal operation-binding registry path or null). Optional `registry_kind` selects `bindings` (the default `CODE_REFERENCE_OPERATION_BINDINGS` map) or `operation_declarations` (the current `KNOWLEDGE_OPERATION_DECLARATIONS` tuple); neither executes repository code.

The command retains historical chunks, marks prior source chunks non-current, reuses unchanged vectors, and publishes a new corpus plus materialized reference generation. It never imports repository code. Python declaration coverage is explicit; other language declarations and missing operation registries stay unresolved. Changed/withdrawn sources are never silently upgraded to proven behavior.

Publication writes `receipt.json` last. A failed run is not a completed generation; retry into a new directory. Review the receipt and point the operator-owned runtime configuration at the prepared `runtime.json`; then restart the affected stdio worker. Existing workers reject changed configuration rather than quietly serve mixed generations. Scheduling publication after a successful source refresh is supported by invoking this command, but automatic activation is deliberately not enabled in the live deployment. This separates source refresh from approval of the maintained document selection.

## Acceptance scope

Check CLI/native MCP parity, pinned source recovery, document retrieval with the offline real model, exact-session memory admission and scope isolation, capture/replay/search, reference generation, browser/inbox durability across restart, and collector-to-Tempo trace retrieval. Record exact image IDs, source labels, exclusions and private receipt locations. The source suite's fixture tests are not a substitute for these image checks.

Archive restore requires PostgreSQL 16 with `vector` and `pg_trgm` in public before restoring the exported `org_ops` schema. Use a disposable network-isolated database, never a live target. Archived transcripts remain historical evidence with unresolved desk ownership unless an explicit attribution record supplies that scope; restoring them does not make them current desk memory or authorize recall.

## Resource envelope

Configured trial limits are tooling 2 CPU/3 GiB RAM, board 1 CPU/1 GiB, collector 256 MiB and Tempo 512 MiB. These are acceptance limits, not measured minimums or production sizing promises. Reserve space for image layers, model weights, retained source/index generations, private memory and Tempo retention; archive restore additionally requires the database's expanded size. No GPU is required. Browser-only clients need no model runtime. Image size and measured acceptance results are recorded separately.

The Kanban dependency install currently reports dependency advisories. The trial is loopback-only; this is not approval for public Internet exposure. Adjudicate the production dependency paths before a hosted release rather than applying an unreviewed major-version audit fix.

## Observed baseline (2026-09-25 UTC)

The independent trial packet is ready. The installed tooling image passed 349 tests (one legacy-only skip); Cline's focused tests passed 15. Separate actual-provider probes exercised offline Serena, Python and TypeScript SCIP, TypeScript compiler context, real model retrieval, CLI/native MCP citation parity, authenticated inbox restart/replay/acknowledgement, and trace retrieval after Tempo restart. The image and receipt record is kept with the operator's records (not published); see the [reviewer trial](TRIAL-INDEPENDENT-IMAGES.md).

Docker displays 2.33 GB for the tooling image, 589 MB for Kanban, 331 MB for the collector and 165 MB for Tempo. The preprovisioned model cache occupies 183,226,368 allocated bytes. Their displayed sum is approximately 3.6 GB including that cache, before repository snapshots, indexes, memory, trace retention and build caches; shared layers mean this is not a fresh-install disk minimum. The local acceptance builder image is additional test/build overhead. Only linux/arm64 was exercised here.

The tooling and board image source revisions are recorded separately; the board runtime did not change in the final publisher-only revision. Later documentation/receipt commits do not relabel these images. Use the recorded immutable IDs for independent review.
