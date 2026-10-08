"""Explicit local OTLP composition; no global SDK provider or environment discovery."""
from dataclasses import dataclass
from urllib.parse import urlparse
from kp_agent_tooling_ops._impl.service.knowledge_telemetry import KnowledgeTelemetry


@dataclass
class TelemetryRuntime:
    observer: object
    traces: object
    metrics: object
    logs: object
    flush_failures: int = 0

    def flush(self, *, timeout_millis=5000):
        if (isinstance(timeout_millis, bool) or not isinstance(timeout_millis, int)
                or timeout_millis <= 0):
            raise ValueError('positive integer flush timeout required')
        results = []
        for provider in (self.traces, self.metrics, self.logs):
            try:
                results.append(bool(provider.force_flush(timeout_millis=timeout_millis)))
            except Exception:
                results.append(False)
        self.flush_failures += results.count(False)
        return all(results)

    def shutdown(self):
        self.traces.shutdown(); self.metrics.shutdown(); self.logs.shutdown()


def local_otlp(endpoint, *, service_name, service_version):
    url=urlparse(endpoint)
    if url.scheme!='http' or url.hostname not in {'127.0.0.1','localhost','::1'} or url.username or url.password or url.query or url.fragment:
        raise ValueError('explicit loopback OTLP endpoint required')
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    resource=Resource({'service.name':service_name,'service.version':service_version})
    base=endpoint.rstrip('/')
    traces=TracerProvider(resource=resource)
    traces.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=base+'/v1/traces',timeout=1),max_queue_size=512,max_export_batch_size=128))
    metrics=MeterProvider(resource=resource,metric_readers=[PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=base+'/v1/metrics',timeout=1),export_interval_millis=5000)])
    logs=LoggerProvider(resource=resource)
    logs.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=base+'/v1/logs',timeout=1),max_queue_size=512,max_export_batch_size=128))
    observer=KnowledgeTelemetry(traces.get_tracer('ops.knowledge'),metrics.get_meter('ops.knowledge'),
        logs.get_logger('ops.knowledge'),suppress_source_identity=True)
    return TelemetryRuntime(observer,traces,metrics,logs)
