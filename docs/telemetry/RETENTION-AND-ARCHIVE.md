# Trace retention and on-demand historical analysis

Assessment, not an implemented archive/restore service or a retention-policy
change. The onboarding ADR does not authorize a telemetry policy change.

The shipped Compose definition pins Tempo 2.6.1. Its local configuration keeps
blocks for 24 hours under `/var/tempo/blocks` and the WAL under `/var/tempo/wal`,
both in the selected physical state volume. Existing acceptance proved persistence
across restart, not a full cold archive and restore. Tempo stores traces; application
logs, metrics and CPU profiles need their own capture/storage policy. The current
collector's logs/metrics debug exporter is not durable historical storage.

## Recommended tiers

- **Short hot trace window:** retain recent detailed traces for investigation.
  Keep the present 24-hour setting until a different window is selected.
- **Durable diagnostic receipts:** retain counts, errors, duration distributions,
  workload/concurrency, environment/resources, source/image identities, journey
  and instrumentation versions, sampling/drop information and exact query scope.
  Store histogram buckets/count/sum for recombination; do not average per-run p95s.
  Append new linked measurements rather than rewriting immutable old receipts.
- **Selected trace evidence and cold archives:** preserve baseline, regression,
  failure and accepted-journey traces; optionally archive all completed blocks
  for an explicit cold-retention period. SQLite can index archive manifests and
  receipts; it need not duplicate every span to enable a Tempo restore.

Trend measurements can survive detailed-trace expiry, but cannot explain an
unanticipated historical causal path. Retaining raw blocks permits later questions
about their recorded attributes, not reconstruction of unsampled/dropped spans,
uninstrumented code, logs or profiles. Prefer independent request metrics for
population rates when trace sampling is biased or its denominator is unknown.

## Candidate archive/restore contract

Tempo's native completed blocks can be retained with their tenant/block layout and
all required metadata/data objects. Restoring that backend layout for a compatible
Tempo reader avoids replaying spans through ingestion. This is a proposed operational
procedure to prove on our pinned version, not an already-tested product capability.

1. Archive before the hot retention deletes data. Quiesce ingestion and explicitly
   flush completed data; in 2.6.1 `flush_all_on_shutdown` defaults false. A copy of
   completed blocks alone can omit recent WAL/in-memory data. Obtain a consistent
   snapshot without racing compaction/deletion; include WAL for crash recovery if
   the snapshot is not fully drained and record that recovery requirement.
2. Seal a manifest with capture time, tenant namespace, block/time coverage,
   object hashes/counts, image digest, block encoding, schema/config identities,
   exclusions and trace-ID examples. Keep archives outside the active retention
   root and retain the image/config needed to read them. Do not claim complete
   coverage solely from successful file copying.
3. Restore to a disposable separate volume and pinned compatible Tempo instance,
   with no receiver traffic or deletion-capable maintenance against the archive.
   Protect the master archive; work from a copy. Ordinary 24-hour retention can
   delete old restored blocks because eligibility uses the original block end
   timestamp. Do not assume `block_retention: 0` disables deletion.
4. Query original timestamps using an explicit historical time range, respecting
   configured query-window limits or splitting searches. Compare known trace IDs,
   span counts/attributes and representative TraceQL results with the pre-archive
   receipt. Pin tenant headers where applicable. No timestamp rewriting.
5. Remove the disposable query instance when finished. Upgrades require a new
   compatibility/restore check, not an assumption that all block formats remain
   supported forever. Shared blocks may contain other sensitive traces; deleting
   a SQLite pointer is not erasure of that archive.

Next falsifiable trial: emit a synthetic journey, seal/flush and archive it, restore
into a second isolated instance with no original volume access, recover known traces
and query results, and prove the source archive remains unchanged. Also test an
incomplete archive and a retention misconfiguration. Only then advertise historical
restore through the tooling. No running Tempo settings were changed by this review.

## Primary references

- [Tempo 2.6.1 configuration](https://github.com/grafana/tempo/blob/v2.6.1/docs/sources/tempo/configuration/_index.md): local backend, WAL/flush, retention and query windows.
- [Tempo 2.6.1 retention implementation](https://github.com/grafana/tempo/blob/v2.6.1/tempodb/retention.go): block end-time cutoff and deletion.
- [Tempo CLI](https://grafana.com/docs/tempo/latest/operations/tempo_cli/): direct backend inspection; consult the pinned CLI version before selecting archive/inspection commands.
