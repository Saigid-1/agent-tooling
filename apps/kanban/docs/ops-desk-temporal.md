# Desk attribution and temporal continuity pilot

Principal, 2026-09-23, current Codex session:
> let's continue with the planned work on that track and trial the desk attribution/temporal continuity work

This resumes the bounded read-only ADR0016 proposal. It does not implement an
admission service, reopen a run or authorize execution of queued ADR slices.

## Reuse

In the legacy source repository, navigation found
kp_ops/service/session_sources.py valid_from/valid_until. Inspection of metadata
and claim confirmed the historical session claim envelope, supersession and
retraction. DeskRun separately holds begin_at/end_at, provenance and summaries;
its reconstructed begin marker must not be mistaken for a measured start.

The new `task desk-history --source <metadata.json> --tenant <tenant>
--session <source-session-id>` reads a SessionSources.metadata export. Invoke via
`node node_modules/tsx/dist/cli.mjs src/cli.ts` until a candidate build is deployed.
It checks exact scope, bounds, predecessor integrity, forks/cycles and conflicts,
and returns a hash of the exact export. It does not query a graph, open a database,
resolve evidence references, authenticate exports or mutate claims. Run it only
with an operator-authorized export; matching a tenant string is not authentication.

No recording timestamp becomes an occupancy bound. Open bounds stay unknown.
Disjoint claims remain separate; potentially overlapping different desk owners
are flagged. Superseded/retracted claims remain visible. Current assertion means
historical claim head, not currently authorized seat. Intervals are half-open.
The projection ignores cached attribution summaries and derives from claim rows.

## Trial and limitations

Private evidence: kanban-observer-state/desk-temporal-trial-01 in the operator workspace.
An explicitly synthetic export was processed through the new CLI, then published
through existing task observe to the live isolated board on3485. Notification was
delivered; replay returned projection_saved=false. The card is explicitly FIXTURE.
No actual product-desk run was written, registered or modified. Neither server restarted.

21 affected tests passed, including 9 new temporal tests; root typecheck passed.
An initial store-suite attempt used the system temp directory and failed the
physical-path guard. Rerunning with TMPDIR on the external volume passed. Future runs must set it.

The user supplied a host session ID and relayed a
claimed interval ending with mechanical SessionEnd. A registered record export
or source path was not supplied. The default ~/.kp-ops/desk_memory.sock is absent.
These facts do not prove all memory services are unavailable. Real-history trial
remains unverified; source-gap.json retains the testimony boundary.

Next: obtain an authorized raw DeskRun/note export and map its actual shape to
historical evidence, retaining mechanical-end versus explicit retirement and
reconstructed-start qualifiers. Do not fabricate a SessionSources claim from
prose or confuse source hash integrity with authenticated registration. A real
session metadata export can be consumed now. DeskRun export adaptation and
automatic source retrieval are not implemented yet.

Approval-bound dispatch, ADR hook/reconciliation and durable dependency semantics
remain separate Kanban continuation slices. Coordination-desk tooling changes are left to the
parallel session. This patch adds no new global hooks or memory machinery.

## Raw desk-run export trial — 2026-09-24

The Principal supplied a read-only raw database export from another desk, with an
expected SHA256.
`scripts/project-desk-run-export.py` is a separate standard-library Python trial
adapter for that export shape; it does not masquerade DeskRuns as session claims.
It takes --source, --sha256, --tenant, --role, --repo and --host. No database or
memory client is invoked. Seven fixture tests run with
`python3 scripts/test_desk_run_export.py`.

The export hash, six record-export checksums, run/host identities and original
bookend/note content digests all matched. There are five runs, one active at
export and four retired with mechanical SessionEnd summaries. All five lack
binding_provenance. The exact end summary includes "or it ran later": it cannot
prove no explicit retirement ever followed. Five historical interval pairs
 overlap; no claim of simultaneous execution or exclusive occupancy follows.
The note names the subject host in prose and is later than the current begin
bookend. Its desk_authored class is preserved, not upgraded to an authenticated
grant. Export-only evidence does not establish live admission or process liveness.

Private receipts: a trial directory under kanban-observer-state in the operator workspace.
The actual derived report was projected to Review in the isolated live board;
notification delivered and exact replay saved no new projection. Original export
remained unchanged, with no writes to that desk, the legacy records, runs or claims. This
proves a read-only export-to-card path, not automatic capture or current seating.

## Desk discovery correction — 2026-09-24

Principal: multiple sessions may occupy the same desk concurrently; discovery and
orchestration, not exclusive occupancy, are the requirement. An unretired session
must not block another session. This correction supersedes occupancy-oriented
interpretation in earlier trial discussion, while preserving the evidence history.

Both adapters now return a desk-to-session directory. The claim adapter exposes
source/native session IDs and runtime; the raw adapter exposes host IDs, transcript
UUIDs and all associated run records. Neither invents a messaging address from an
ID. Reachability and contact route remain unverified/unknown until resolved via a
host adapter. Exported active/retired status is not live process state.

Raw adapter v1.1 calls interval overlaps concurrency_observations, never conflicts.
Its directory labels concurrency allowed, records export freshness/scope, and
keeps every session regardless of retirement. The claim adapter's conflicts apply
only to contradictory ownership claims on the same source session, never different
sessions sharing a desk. No exclusive seat lock or new registration store exists.

Eight Python checks and 22 affected TS tests passed; root typecheck passed. That desk's
five exported sessions were projected onto the existing card as observation 2;
replay saved no duplicate. Original export and observation 1 remain untouched.

Next orchestration boundary: resolve an exported session identity to its actual
harness contact route through that host's session registry, retaining last-observed
reachability separately. This is not yet delivered; no automatic discovery polling,
message delivery or new admission has been enabled by this read-only directory.
