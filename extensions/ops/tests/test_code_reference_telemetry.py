"""Telemetry contract for document-to-code reference indexing and enrichment."""

import asyncio
import time

import pytest

from kp_agent_tooling_ops._impl.service.knowledge_telemetry import (
    CODE_REFERENCE_SPAN_NAMES,
    KnowledgeTelemetry,
    telemetry_stage,
    validate_code_reference_attributes,
)
from kp_agent_tooling_ops._impl.service.telemetry_runtime import TelemetryRuntime


@pytest.fixture
def observer():
    class Id:
        def __init__(self, span_id):
            self.span_id = span_id

    class Span:
        def __init__(self, name, parent, attributes):
            self.name, self.parent = name, parent
            self.context = Id(id(self))
            self.attributes, self.events, self.end_time = dict(attributes), [], None

        def set_attribute(self, key, value):
            self.attributes[key] = value

    class Scope:
        def __init__(self, tracer, name, attributes):
            parent = tracer.current.context if tracer.current is not None else None
            self.tracer, self.span = tracer, Span(name, parent, attributes)

        def __enter__(self):
            self.tracer.current = self.span
            return self.span

        def __exit__(self, *_):
            self.span.end_time = time.monotonic_ns()
            self.tracer.finished.append(self.span)
            self.tracer.current = self.span.parent

    class Tracer:
        def __init__(self):
            self.current, self.finished, self.flushes = None, [], 0

        def start_as_current_span(self, name, attributes=None, **_):
            return Scope(self, name, attributes or {})

        def force_flush(self):
            self.flushes += 1
            raise AssertionError("hot-path synchronous flush")

    class Instrument:
        def add(self, *_):
            pass

        def record(self, *_):
            pass

    class Meter:
        def create_histogram(self, *_args, **_kwargs):
            return Instrument()

        def create_counter(self, *_args, **_kwargs):
            return Instrument()

    class Exporter:
        def __init__(self, tracer):
            self.tracer = tracer

        def get_finished_spans(self):
            return tuple(self.tracer.finished)

    tracer = Tracer()
    telemetry = KnowledgeTelemetry(tracer, Meter())
    yield telemetry, Exporter(tracer), tracer


def test_f15_catalog_nested_context_and_bounded_attributes(observer):
    """GREEN-IF: fixed stage names nest and expose only bounded safe attributes."""
    telemetry, exporter, tracer = observer
    with tracer.start_as_current_span("knowledge.retrieve"):
        with telemetry.stage("code_references.enrich", status="ok", rows=2):
            with telemetry.stage(
                "code_references.manifest_lookup",
                status="ok",
                cache_state="hit",
                batch_size=2,
            ):
                pass
    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert CODE_REFERENCE_SPAN_NAMES == frozenset({
        "code_references.index", "code_references.document",
        "code_references.document_read", "code_references.extract",
        "code_references.registry_read", "code_references.symbol_index",
        "code_references.resolve", "code_references.graph_lookup",
        "code_references.graph_write", "code_references.checkpoint",
        "code_references.manifest_publish", "code_references.enrich",
        "code_references.revision_selection", "code_references.manifest_lookup",
        "code_references.baseline_ancestry", "code_references.tree_freshness",
        "code_references.navigation_readiness", "code_references.response_enrichment",
    })
    assert spans["code_references.manifest_lookup"].parent.span_id == spans[
        "code_references.enrich"
    ].context.span_id
    assert spans["code_references.enrich"].parent.span_id == spans[
        "knowledge.retrieve"
    ].context.span_id
    assert spans["code_references.manifest_lookup"].attributes == {
        "status": "ok",
        "cache_state": "hit",
        "batch_size": 2,
    }
    assert tracer.flushes == 0


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("path", "docs/secret.md"),
        ("revision", "a" * 40),
        ("query", "private query"),
        ("reason", "arbitrary exception text"),
        ("rows", -1),
        ("rows", True),
    ],
)
def test_f15_validator_rejects_prohibited_or_unbounded_attributes(attribute, value):
    """GREEN-IF: unsafe fields and values cannot enter feature span attributes."""
    with pytest.raises(ValueError):
        validate_code_reference_attributes({attribute: value})


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (RuntimeError("adapter failed"), "error"),
        (TimeoutError(), "timeout"),
        (asyncio.CancelledError(), "cancelled"),
    ],
)
def test_f15_spans_close_and_classify_failures_without_exception_payload(
    observer, error, status
):
    """GREEN-IF: error, timeout and cancellation close spans without payload events."""
    telemetry, exporter, _ = observer
    with pytest.raises(type(error)):
        with telemetry.stage("code_references.resolve", resolver="symbol"):
            raise error
    span = exporter.get_finished_spans()[-1]
    assert span.attributes["status"] == status
    assert not span.events
    assert span.end_time is not None


def test_f17_invalid_or_broken_telemetry_never_changes_application_behavior(observer):
    """GREEN-IF: observer failure falls back to a null context and preserves results."""
    telemetry, exporter, _ = observer
    before = telemetry.failures
    expected = object()
    with telemetry_stage(telemetry, "generated.secret", path="secret"):
        result = expected
    assert result is expected
    assert telemetry.failures > before
    assert not exporter.get_finished_spans()
    with telemetry_stage(None, "code_references.enrich"):
        assert result is expected


def test_rehearsal_observer_suppresses_source_identity_on_parent_request_span(observer):
    """GREEN-IF: reference rehearsal request spans keep revision identity in receipts only."""
    telemetry, exporter, _ = observer
    telemetry.suppress_source_identity = True
    telemetry.run("retrieve", lambda: {
        "status": "ok", "repo_key": "private-repo", "source_revision": "a" * 40,
        "data": {"snapshot_id": "private-snapshot"},
    })
    attributes = exporter.get_finished_spans()[-1].attributes
    assert attributes == {"ops.outcome": "ok"}


def test_stage_duration_uses_monotonic_elapsed_time(observer):
    """GREEN-IF: every completed stage records a nonnegative monotonic duration."""
    telemetry, _, _ = observer
    with telemetry.stage("code_references.extract", kind="symbol"):
        time.sleep(0.001)
    assert telemetry.completed_stages == 1
    assert telemetry.stage_duration_seconds >= 0.001


def test_f17_rehearsal_flush_is_finite_and_reconciles_provider_failures():
    """GREEN-IF: shutdown flushing passes one finite deadline to every provider."""
    class Provider:
        def __init__(self, result):
            self.result, self.deadlines = result, []

        def force_flush(self, timeout_millis):
            self.deadlines.append(timeout_millis)
            return self.result

        def shutdown(self):
            pass

    providers = [Provider(True), Provider(False), Provider(True)]
    runtime = TelemetryRuntime(object(), *providers)
    assert runtime.flush(timeout_millis=250) is False
    assert [provider.deadlines for provider in providers] == [[250], [250], [250]]
    assert runtime.flush_failures == 1
