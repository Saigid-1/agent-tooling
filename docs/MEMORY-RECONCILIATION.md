# Memory reconciliation before cutover

Work remains isolated until the operator selects a quiet window. Neither an
import receipt nor a historical claim grants a session admission. Concurrent
sessions on one desk remain supported.

The 2026-09-25 rehearsal copied portable SQLite databases with SQLite's backup
API, retained the approved catalog, imported an immutable read-only PostgreSQL
export, rebuilt the literal episode index, and repeated the import. All 1,121
notes/runs across nine bindings were accounted for: 50 new/updated immutable
records, 1,071 already present, zero quarantined or excluded; replay added zero.
These are source-row versions, not 50 newly authored notes. Live stores were not
changed. The source remained active; separate SQLite backups are not an atomic
snapshot across every store.

The private rollback archive preserves `org_ops`, including old transcript
history and graph relationships. A disposable full restore succeeded, including 213,794 messages, 747 transcripts,
44,211 transcript chunks and all nine bindings/1,022 notes/99 runs. The restore
requires public extensions `vector` and `pg_trgm`, plus a separately retained
dependency archive for `public.connector_definitions` and `public.endpoint_configs`.
Those dependency rows were captured later; this is a demonstrated rollback
rehearsal, not an atomic freeze of concurrent source writes. The restored database
occupied 2,708,814,871 bytes; its isolated container is stopped and state retained. Raw data, credentials and
transcripts are excluded from this repository.

The document export preserved 541 chunks and 552 vectors across two embedding
revisions. No vectors lack a corresponding chunk. Ten chunks have no vector;
all ten are non-current. All nonempty session-memory lineage/grant/tier tables
must be mapped before retirement: this inventory found those tables empty except
for one tier-composition guard row. The old 44,211 transcript chunks are preserved
in the archive, not automatically promoted into desk memory. A reviewed scope
mapping is required before their import/recall can be claimed.

## Repeatable isolated rehearsal

The script ships with the OPS extension and imports both the core and the extension.
With `packages/tooling` and `extensions/ops` installed, run from the repository root:

```sh
python extensions/ops/scripts/reconcile_desk_history.py \
  --source-state /private/live/desk-history \
  --catalog /private/approved-catalog.json \
  --export /private/desk-history-export.json \
  --destination /private/new-reconciliation-directory
```

The copy's search index is built by the one build function (T12b): a new file beside
the copied index, built from every sealed row under the copy's index lease
(`state/index.lock`), verified and renamed into place. The script never rebuilds in
place, and opens the copy's index through the store-path leaf as its store's sibling.
The receipt's `index` reports the episodes and events the build indexed.

This legacy rehearsal requires an approved `ops.imported-desk-catalog.v1` catalog.
All paths must be physical and absolute; the destination must not exist and may
not be nested under the live state. The existing legacy export format is
`ops.desk-history-export.v1`; raw rows carry their original digests, authored
coordinates and binding metadata. Source export must be read-only and saved with
0600 permissions. Keep the output and its private detailed receipt outside Git.

Before switching: stop only the relevant writers in the agreed quiet window,
repeat source inventories and export, reconcile the final delta, verify the same
host session through CLI and native MCP, verify withdrawal/reference/corpus
identities, and preserve rollback state. Do not replace a running SQLite database
or a host registration with a rehearsal copy while writers are active.

A second isolated reconciliation retained the newly approved agent-tooling Coordinator catalog entry, exact-session admission and two portable captures reported by the parallel coordinator. It again accounted for all 1,121 historical rows with 50 imported and 1,071 already present. New portable state and historical graph rows remain distinct; no imported claim becomes a new grant. Final writer-freeze reconciliation remains mandatory.
