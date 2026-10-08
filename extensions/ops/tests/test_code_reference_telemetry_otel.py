"""OpenTelemetry SDK integration for document-reference feature spans."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

import pytest

pytest.importorskip("opentelemetry.sdk")
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from kp_agent_tooling_ops._impl.service.knowledge_telemetry import KnowledgeTelemetry


def test_f15_concurrent_stages_keep_explicitly_propagated_parent_context():
    """GREEN-IF: concurrent children retain the supported copied request context."""
    exporter = InMemorySpanExporter()
    traces = TracerProvider()
    traces.add_span_processor(SimpleSpanProcessor(exporter))
    metrics = MeterProvider()
    observer = KnowledgeTelemetry(traces.get_tracer("feature"), metrics.get_meter("feature"))
    tracer = traces.get_tracer("request")
    try:
        with tracer.start_as_current_span("knowledge.context") as root:
            contexts = [copy_context(), copy_context()]

            def work(kind):
                with observer.stage("code_references.resolve", kind=kind, status="ok"):
                    return kind

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(context.run, work, kind)
                           for context, kind in zip(contexts, ("path", "symbol"))]
                assert [future.result() for future in futures] == ["path", "symbol"]
        children = [span for span in exporter.get_finished_spans()
                    if span.name == "code_references.resolve"]
        assert len(children) == 2
        assert all(span.parent.span_id == root.context.span_id for span in children)
        assert all(span.end_time is not None and not span.events for span in children)
    finally:
        traces.shutdown()
        metrics.shutdown()


def test_f15_business_exception_adds_no_automatic_exception_event():
    """GREEN-IF: exception text and stack payloads never enter feature spans."""
    exporter = InMemorySpanExporter()
    traces = TracerProvider()
    traces.add_span_processor(SimpleSpanProcessor(exporter))
    metrics = MeterProvider()
    observer = KnowledgeTelemetry(traces.get_tracer("feature"), metrics.get_meter("feature"))
    try:
        with pytest.raises(RuntimeError, match="private payload"):
            with observer.stage("code_references.resolve", resolver="git"):
                raise RuntimeError("private payload")
        span = exporter.get_finished_spans()[0]
        assert span.attributes["status"] == "error"
        assert not span.events
    finally:
        traces.shutdown()
        metrics.shutdown()
