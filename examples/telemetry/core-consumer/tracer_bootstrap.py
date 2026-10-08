"""Integration stub: the consuming process owns startup and shutdown.

Requires a Core version that includes the diagnostic spans.
No SDK import until enabled; no global tracer registration.
"""
from dataclasses import dataclass
from threading import Lock


@dataclass
class TracingRuntime:
    diagnostics: object
    provider: object = None
    exporter: object = None

    def health(self):
        counters = self.exporter.counts() if self.exporter else {}
        return {"enabled": self.provider is not None,
                "observer_failures": self.diagnostics.failures, **counters}

    def close(self):
        """Call once after draining application work; never on each request.

        Flush has a 5s budget. SDK shutdown is NOT a hard wall-clock bound;
        the process supervisor must enforce its own termination deadline.
        """
        if self.provider is None:
            return {"flush_completed": True, **self.health()}
        flushed = False
        errors = []
        try:
            flushed = self.provider.force_flush(timeout_millis=5000)
        except Exception as error:
            errors.append(type(error).__name__)
        try:
            self.provider.shutdown()
        except Exception as error:
            errors.append(type(error).__name__)
        return {"flush_completed": flushed, "shutdown_errors": errors, **self.health()}


def start_tracing(*, enabled, service_name, service_version, endpoint,
                  sample_ratio=1.0, exporter_override=None):
    """Build once per worker, AFTER fork. Pass explicit operator configuration.

    endpoint is a full OTLP/HTTP traces URL, not the Tempo query URL.
    Initialization errors surface to startup: the host owns any fallback policy.
    exporter_override is only for local tests.
    """
    from kp_core.diagnostics import CoreDiagnostics
    if not enabled:
        return TracingRuntime(CoreDiagnostics())
    if not service_name or not service_version or not endpoint:
        raise ValueError("service identity and OTLP traces endpoint are required")
    if isinstance(sample_ratio, bool) or not 0 <= sample_ratio <= 1:
        raise ValueError("sample_ratio must be between 0 and 1")

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    class CountedExporter(SpanExporter):
        def __init__(self, inner):
            self.inner = inner
            self.lock = Lock()
            self.sent = self.failed = 0

        def export(self, spans):
            try:
                result = self.inner.export(spans)
            except Exception:
                result = SpanExportResult.FAILURE
            with self.lock:
                if result == SpanExportResult.SUCCESS:
                    self.sent += len(spans)
                else:
                    self.failed += len(spans)
            return result

        def counts(self):
            with self.lock:
                return {"exported_spans": self.sent, "failed_export_spans": self.failed}

        def shutdown(self):
            self.inner.shutdown()

    exporter = CountedExporter(exporter_override if exporter_override is not None else
                              OTLPSpanExporter(endpoint=endpoint, headers={}, timeout=1))
    provider = TracerProvider(
        resource=Resource({"service.name": service_name, "service.version": service_version}),
        sampler=ParentBased(TraceIdRatioBased(sample_ratio)), shutdown_on_exit=False)
    provider.add_span_processor(BatchSpanProcessor(
        exporter, max_queue_size=512, max_export_batch_size=64,
        schedule_delay_millis=100, export_timeout_millis=1000))
    return TracingRuntime(CoreDiagnostics(provider.get_tracer("core.evidence")), provider, exporter)
