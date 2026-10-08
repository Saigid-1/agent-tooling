# Optional Core consumer adapter

These two files are a process-composition example, not part of the installed
`kp-agent-tooling` package. They import Core only when the consumer explicitly
invokes the example. The tooling package has no Core dependency.

The consuming process must install a reviewed Core version containing
`kp_core.diagnostics.CoreDiagnostics`. See the source-pinned handoff in
`docs/telemetry/CORE-CONSUMER-HANDOFF.md` at the repository root before relying
on additional spans. This repository carries no Core application source or patch.

Install the optional exporter into the consumer's normal environment:

```text
opentelemetry-sdk==1.44.0
opentelemetry-exporter-otlp-proto-http==1.44.0
```

Copy/adapt `tracer_bootstrap.py` into the process composition layer. Build one
runtime after fork, with explicit configuration:

```python
from tracer_bootstrap import start_tracing

tracing = start_tracing(
    enabled=settings.tracing_enabled,
    service_name=settings.service_name,
    service_version=settings.build_revision,
    endpoint=settings.otlp_traces_endpoint,  # full /v1/traces URL
    sample_ratio=settings.trace_sample_ratio,
)
```

Pass `tracing.diagnostics` at the supported Core injection points. Initialize
before accepting work. Drain work before calling `tracing.close()` once. In an
async lifespan, use the process's existing thread/offload mechanism for close.
The supervisor owns the hard termination deadline; SDK shutdown can exceed its
nominal flush budget. Report `tracing.health()` and close results through normal
operational diagnostics without replacing application errors.

The adapter sets no global provider. Disabled startup imports no SDK; enabled
startup configuration/import failures surface to the process owner. Counters
report observer/export failures, not sampled-out or queue-dropped spans. A
successful flush is not proof of backend persistence: retrieve a known trace.

Run the existing executable check from a prepared consumer checkout with Core
and the optional SDK installed, using an absolute path to `check_stub.py`:

```sh
PYTHONPATH=/absolute/reviewed-core-checkout .venv/bin/python \
  /absolute/agent-tooling/examples/telemetry/core-consumer/check_stub.py
```

It uses in-memory/failing exporters and no database or network. It verifies
resource identity, parentage, disabled imports, unchanged global provider,
shutdown and exporter-failure isolation. It is not a production/load trial.

The existing tooling runtime in `packages/tooling` remains independently
composable. This consumer example does not replace that runtime or activate it.
