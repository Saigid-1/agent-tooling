# Cutover ledger

Requested by the Principal: move the entire memory suite, Cline work, navigation tooling, portable MCP gateway and telemetry to this repository; retire the legacy runtime.

This ledger records the 2026-09 extraction and the first portable cutover. Under [AT-0004](adr/AT-0004-portable-kanban-suite.md) the legacy source repository is retired in favour of this one: the OPS-specific code lives in the `extensions/ops` extension, and the remaining step, an empty-root rehearsal and a cutover, is AT-0004 T7. T7 ran on 2026-10-02; see [T7 cutover](#t7-cutover-2026-10-02).

## Required gates

1. Native package sources build without the legacy source tree; retained source origin and licensing notices.
2. Portable CLI/MCP parity, isolated memory scopes, capture/consolidation, source navigation and Cline inbox tests.
3. Export immutable legacy memory evidence and binding metadata privately; preserve existing portable SQLite state and indexes; account for every imported, already-present, excluded or quarantined row. Source retirement requires no unexplained lost records.
4. Replace the remaining gateway-backed knowledge operations, including maintained document retrieval and reference enrichment. Do not equate literal episodic search with semantic document RAG.
5. Build independent images, prove them in an isolated deployment, move active host registrations, and run live read-only checks.
6. Graveyard the legacy runtime and its startup services only after the replacement proves its data and capability coverage. Preserve historical Git revisions, exports and rollback receipts; never delete private memory to clean source.

## Initial observed boundaries

Portable source: the legacy source repository at its extraction commit. Cline fork: the local fork's branch head. Legacy inventory at extraction: 9 bindings, 1,022 notes, 99 runs, 541 document chunks. Counts are observations at a time, not a freeze of concurrent writers.

The previous wheel still forwarded several `knowledge.*` calls to the legacy HTTP gateway. Those are explicit migration work, not evidence that the package is already independent end-to-end. Existing portable memory has its own admitted sessions, immutable episodes and literal index; graph claims are historical attribution, not portable admission.

## Isolated continuation (2026-09-25)

The portable provider now composes the existing knowledge service, schema contracts, withdrawal ledger and frozen reference readers in-process. Real offline document queries passed against the exported corpus. Desk-history import and replay are reconciled in isolated copies. See [PORTABLE-KNOWLEDGE.md](PORTABLE-KNOWLEDGE.md) and [MEMORY-RECONCILIATION.md](MEMORY-RECONCILIATION.md).

Portable document/reference publication and the isolated restore rehearsal are now implemented. Archived transcript history is preserved as historical evidence, excluded from desk recall until explicit scope mapping. Independent image acceptance is recorded separately. These were the remaining gates before the live cutover recorded below.


## Live portable cutover (2026-09-25)

The Principal authorized the quiet window after Claude stopped. A recorded build is running from a recorded local image. The physical runtime is a private directory on the operator's physical storage; its private Compose file starts/stops tooling, refresh and independent telemetry. Models, state and configuration reside on that storage. Refresh alone mounts the existing operator credential read-only; no credential is baked into the image.

The legacy HTTP gateway launch service is disabled and unloaded, and the old tooling/refresh containers are stopped and retained for rollback. Claude and Codex tooling registrations now select the portable container. Claude's exact-session memory launcher and two product desks' resume/end hooks use the portable runtime. The old HTTP gateway registration is removed in Claude and disabled in Codex. No new desk admission or grant was created.

Final reconciliation accounted for 1,136 historical desk records: 65 imported versions, 1,071 already present, zero excluded/quarantined. Replay imported zero. The rebuilt episodic search index contains 3,925 indexed episodes and 208,062 events; SQLite integrity checks passed. Historical graph evidence does not become session authority.

Live MCP checks passed for literal search, committed source, SCIP symbol resolution, Serena language-server overview, semantic navigation, document retrieval and context. Context correctly returned `review_required` for changed map references. Both existing Claude session admissions passed exact CLI/MCP search parity and the actual resume hook. New host session IDs still require explicit admission; a resumed existing ID preserves its admission.

Portable OTLP HTTP is on loopback port 14319; Tempo queries are on loopback 13200. One labelled synthetic trace was exported and retrieved successfully. This proves the new pipeline, not automatic rerouting of every existing project's instrumentation. Other projects' collectors and the existing Kanban import trial at port 3488 were left running unchanged. The latter still contains synthetic trial data, not a newly activated production board.

### Explicit remaining boundaries

* Navigation refresh reports all three registered repositories current and runs every 300 seconds. The maintained knowledge catalog is separately pinned to the cutover revisions; document/reference publication is not automatically activated by a navigation refresh. Its SCIP/platform catalog is also pinned. Refreshing those pins remains a reviewed publication step.
* Each of the three registered repositories is pinned at its cutover commit. The new agent-tooling source repository is not yet registered as a navigation target.
* Retained reference enrichment remains its historical legacy generation; cutover does not establish new reference coverage. Archived transcripts remain historical until explicitly scoped.
* Global Codex navigation registration was switched; this does not create a memory binding for arbitrary Codex tasks. Existing configured portable memory state was preserved.
* Desktop-native reconnect has to occur when the user restarts. The acceptance calls used real MCP stdio transport to the exact deployed container, not the old cached desktop connection.

Private rollback material and raw receipts are under `agent-tooling-cutover-20260925` beside the runtime. The final archive includes legacy public FK dependencies, was inspected/checksummed, and follows the earlier successful restore rehearsal. It was not freshly restored during the cutover. Retain the old state and reconcile any post-cutover writes before rolling back.


## Post-cutover admission follow-up (one product desk)

One product desk's Coordinator reported a working legacy `desk_memory_cli.py` claim but no portable MCP admission. The initial cutover verified two existing admitted IDs; it did not admit arbitrary resumed/new IDs or promote historical claims. The Principal subsequently approved the specific reported Claude session and portable writes for that Coordinator desk. The private catalog, exact-session config, ledger admission and durable host selection were updated accordingly. Host launchers and the product desks' lifecycle hooks now share the selection directory for same-ID config recovery. The exact model version was not established and was not guessed.

The actual configured Claude launcher passed MCP recall, a clearly labelled synthetic cited `memory.propose` write, and `memory.handoff` recovery. The actual resume hook resolved the approved desk. The synthetic record expressly disclaims project doctrine and Coordinator authorship. Two post-snapshot legacy record versions were imported, with no quarantine or exclusion. A CLI claim remains a separate legacy write; use portable `memory.search` and `memory.propose` going forward. Free-form legacy write/ledger commands are not aliases for those tools, and the retired gateway should not be re-enabled to restore them.

A direct blob audit at the cutover pins found four outdated corpus documents in that desk's repository (two under its `docs/` directory and two at its root), not 42 distinct documents in this corpus. The reproduced retrieval excluded the stale backlog blob and supplied a pinned navigation fallback. This audit did not republish or claim fresh document coverage; publication remains separate from navigation refresh. Private approval, capture and verification receipts are under the runtime's `state/cutover-followup` directory.

## Second admission follow-up (2026-09-26)

The Principal explicitly approved another product desk's reported Coordinator Claude session and
portable desk writes. Its saved host selection existed but its session config and
ledger admission were absent. The selected binding/provider/model metadata were
preserved, the missing admission was completed, and writes were enabled for that
Coordinator desk only. A real MCP connection through the configured Claude
launcher returned 13 tools, ready status and desk-scoped recall; the actual resume
hook also passed. This does not assert the already-running desktop conversation
has refreshed its cached tool list. Private receipts are in the runtime's
`state/cutover-followup` directory.

That desk's startup index still described directory-default graph bootstrap and
legacy CLI re-keying. The current host entry notice explicitly supersedes those
memory-registration commands while preserving the legacy charter's role content.
No legacy source/history was deleted and no release or push authority was granted.


## T7 cutover (2026-10-02)

The Principal ruled for a full migration in one window, with the long-running coordination sessions restarting fresh afterwards. The window opened on the Principal's word, with every session idle.

**Rehearsed first, on copies (2026-10-01).**
- **Empty-root rehearsal.** Its findings became T7a (role readiness) and T7b (board wiring).
- **Migration rehearsal.**
  - The installer-rendered root ran with copies of the live configuration and state.
  - Parity with live: `knowledge.capabilities`, `knowledge.context` (zero differing fields) and `navigation.search` were identical, and `memory.search` returned the same hits.
  - `import-catalog` created all 57 desks with their original binding keys, and the imported desks searched their full history.
  - It found that T5 retention would delete the three generations the maintained knowledge catalog pins. T5b (`retention_references`) fixes that.

**Cutover.**
- **Install.** `kp-agent-install` built a new runtime root (project `agent-tooling`; image `ops` at a recorded commit; roles `tooling`, `capture`, `board` and `refresh`; nine repositories; both native transcript roots). The old project's roles were stopped and kept for rollback.
- **State.** The state was moved into the new root by rename, recorded in a manifest. Lock files were preserved.
- **Configuration.**
  - The configuration was carried over; the old `navigation.json` became the operator file `$root/config/navigation.ops.json`.
  - The refresh credential was moved into the new root's `secrets/`, unread.
- **Registry.**
  - The portable registry (`$root/config/launch/desks.json`, roster under `/state/registry`) imported the catalog: 57 desks, 8 roles, 0 conflicts.
  - The imported desks were renamed `<role> · <repository>`.
- **Verification on the live root.**
  - 34 tools; `tooling.identity` reports the image revision.
  - The knowledge, navigation and memory parity calls passed.
  - The board's Desks menu lists all 67 desks over HTTP.
  - A real Claude Code launch through `kp-agent-host` (docker mode) was spooled, ingested, bound (`source: host`), captured, and found by desk-scope `memory.search`.
- **Host registrations repointed (each file backed up first).**
  - The Claude and Codex `ops-agent-tooling` servers now run in `agent-tooling-tooling` with `navigation.ops.json`.
  - The desk-memory launcher and the SessionStart and SessionEnd desk hooks were repointed to the new container and roots.
  - The legacy per-session capture hooks were removed.

**Deliberate deviations and open decisions.**
- **Per-project hooks.** `kp-agent-host install-hooks` was not applied to the coordination repositories. A per-project policy binds every session in a repository to one desk, but those repositories host several desks. The per-session admission model remains for desktop sessions; host launches use the adapter.
- **Refresh.** Refresh is stopped. The request's product platform still lists the legacy source repository, which the earlier ruling dropped from refresh. Navigation reads repositories from the published profile, so a profile without that repository would remove its navigation. Navigation serves the last published profile until the Principal chooses.
- **Workspace capture.** It refuses transcripts whose source identity changed with the new container mount (nothing is imported twice). New sessions are captured normally.
