import pytest

pytest.importorskip('opentelemetry.sdk')
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from kp_agent_tooling_ops._impl.service.knowledge_telemetry import KnowledgeTelemetry


def instruments(exporter=None):
    exporter = exporter or InMemorySpanExporter()
    resource = Resource.create({'service.name':'ops-trial','service.version':'test'})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meters = MeterProvider(resource=resource, metric_readers=[reader])
    observer = KnowledgeTelemetry(provider.get_tracer('ops'), meters.get_meter('ops'))
    return observer, exporter, reader, provider, meters


def test_trace_metrics_and_parent_without_sensitive_arguments():
    observer, exporter, reader, provider, meters = instruments()
    try:
        with provider.get_tracer('test').start_as_current_span('request'):
            expected = {'status':'ok','data':{'snapshot_id':'source-digest'}}
            assert observer.run('platform', lambda: expected) is expected
        spans = exporter.get_finished_spans()
        assert spans[0].parent.span_id == spans[1].context.span_id
        assert spans[0].attributes['ops.source.snapshot_id'] == 'source-digest'
        metrics = reader.get_metrics_data().resource_metrics[0].scope_metrics[0].metrics
        assert {m.name for m in metrics} == {'ops.knowledge.calls','ops.knowledge.duration'}
        for metric in metrics:
            assert dict(metric.data.data_points[0].attributes) == {'operation':'platform','outcome':'ok'}
    finally:
        provider.shutdown(); meters.shutdown()


def test_exporter_failure_and_business_exception_remain_separate():
    class Broken(InMemorySpanExporter):
        def export(self, spans):
            raise RuntimeError('export unavailable')
    observer, _, _, provider, meters = instruments(Broken())
    try:
        assert observer.run('platform', lambda: {'status':'ok'}) == {'status':'ok'}
        def fail():
            raise ValueError('secret request data')
        with pytest.raises(ValueError, match='secret request data'):
            observer.run('platform', fail)
    finally:
        provider.shutdown(); meters.shutdown()


def test_business_exception_is_not_copied_to_trace():
    observer, exporter, _, provider, meters = instruments()
    try:
        with pytest.raises(ValueError):
            observer.run('arbitrary-secret-operation', lambda: (_ for _ in ()).throw(ValueError('secret')))
        span = exporter.get_finished_spans()[0]
        assert span.name == 'knowledge.unknown'
        assert span.attributes['ops.outcome'] == 'error'
        assert not span.events
    finally:
        provider.shutdown(); meters.shutdown()
