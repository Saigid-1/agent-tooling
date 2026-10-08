# Core telemetry handoff

## Ownership and integration state

Agent-tooling owns the portable tooling source, optional consumer tracer example
and collector/Tempo configuration. Core owns instrumentation in its application
and database paths. The consumer application's maintainers review and integrate
that work. No Core application source, patch, vendored package or base dependency
is introduced here. This is a handoff onto the extracted tooling baseline, not a
Git rebase of unrelated Core application history.

The source awaiting Core review is a reviewed and pushed feature branch of the
consumer application's own repository (not published), on an integration base
that already landed earlier instrumentation.

That integration base already landed request/database/evidence instrumentation and Core's
thread-context propagation fixes. The remaining branch preserves those fixes
and their tests exactly and adds slices 4–5. The head is a feature commit, not
a released consumer pin. Use Core's normal review/release process; recheck its
current state before applying or publishing anything. This tooling commit does
not claim Core PR creation, merge, deployment or consumer activation.

## What the remaining Core slices measure

| Scope | Fixed span names |
| --- | --- |
| Enrichment callback and publish callback | `core.external.attempt`, `core.worker.publish` |
| NATS transport | `core.broker.publish`, `core.broker.consume` |
| Resolver invocation | `core.resolver.resolve`, `core.resolver.resolve_source` |
| Reads and processing | `core.resolver.evidence.read`, `core.resolver.map`, `core.resolver.identity` |
| Projection and relationships | `core.resolver.project`, `core.resolver.corroborate`, `core.resolver.link` |

Only `traceparent` crosses the broker. Missing, malformed, duplicate or unreadable
parents start a fresh trace. Existing callbacks, five-second publish wait,
ack/nak behavior and SQL are preserved. A publish timeout does not prove that
its background send failed. Projection/corroboration phases mark handled record
failures as errors while an outer run can complete successfully. Spans contain
bounded outcomes, not payloads, tenant identifiers, SQL or exception text.
Durations are inclusive; do not sum parent and child spans. Per-record mapping,
identity and link spans require workload-appropriate sampling.

## Wiring after Core integration

Use `examples/telemetry/core-consumer/tracer_bootstrap.py` as the optional process
adapter. The owning process supplies identity, exporter endpoint, sampling and
shutdown. For Core versions containing the respective features:

```python
from kp_core.diagnostics import diagnostic_scope
from kp_core.connectors.kp_nats_client import NATSClient

# Long-lived subscriptions retain the explicitly supplied process observer.
client = NATSClient(servers=servers, tenant_id=tenant_id,
                    diagnostics=tracing.diagnostics)
with diagnostic_scope(tracing.diagnostics):
    resolver.resolve()
```

Core HTTP assembly supports `app.state.core_diagnostics`; consumer ASGI apps
can install `CoreTelemetryMiddleware` once. Existing store injection remains
available, and stores without an explicit observer inherit the operation's scope.
Check the target Core version before using these APIs.

Collector and Tempo configurations live under `deploy/telemetry`. Traces are
stored in Tempo; the current metrics/logs pipelines use the debug exporter and
are not persistent query stores. Configure listener reachability, data volumes
and credentials in the owning deployment. Host loopback does not establish
container or remote reachability. This handoff changes no live collector,
host registration, memory store or service retirement state.

## Evidence and limits

Core's reconciled head passed 91 focused integration checks and two complete
isolated database-enabled gates: 6,157 tests and 387 subtests, with 42 skips and
zero failures each. Studio and Core boundary checks passed. Historical synthetic
rehearsal at the preceding working tree retrieved 38/38 spans across two Python
processes with NATS, PostgreSQL evidence and a MemoryAdapter resolver; replay
created no duplicate evidence/assertion/link. It had zero observer/export failures.
That trial's source hashes remain in the Core branch receipts. It was not rerun
as a live trace trial on the integration commit; the new full gates cover that
integration. No performance improvement or production/load acceptance is claimed.

This repository retains source identities and validation summaries, not private
exports or transcripts. The Cline fork is already in the extraction baseline;
this handoff does not recopy it. The extraction owner retains responsibility for
legacy knowledge-operation parity, memory reconciliation and deployment cutover.
