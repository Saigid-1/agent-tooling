"""Optional injected OpenTelemetry observer. No global SDK/exporter configuration.

Operation/status metric labels are finite. Source/snapshot identity belongs on
spans, never on metric labels. Request arguments and exception text are excluded.
"""
from contextlib import nullcontext
import asyncio
import math
from time import perf_counter


CODE_REFERENCE_SPAN_NAMES = frozenset({
    "code_references.index",
    "code_references.document",
    "code_references.document_read",
    "code_references.extract",
    "code_references.registry_read",
    "code_references.symbol_index",
    "code_references.resolve",
    "code_references.graph_lookup",
    "code_references.graph_write",
    "code_references.checkpoint",
    "code_references.manifest_publish",
    "code_references.enrich",
    "code_references.revision_selection",
    "code_references.manifest_lookup",
    "code_references.baseline_ancestry",
    "code_references.tree_freshness",
    "code_references.navigation_readiness",
    "code_references.response_enrichment",
})

_CATEGORICAL_VALUES = {
    "kind": frozenset({"path", "symbol", "operation", "document", "manifest"}),
    "status": frozenset({
        "ok", "partial", "error", "timeout", "cancelled", "unavailable",
        "resolved", "ambiguous", "candidate", "unresolved", "not_indexed",
        "historical_only", "not_checked", "ready", "stale", "complete",
    }),
    "resolver": frozenset({"path", "symbol", "operation", "registry", "git", "graph", "navigation"}),
    "stage": frozenset({"wait", "compute", "read", "write", "publish", "verify"}),
    "cache_state": frozenset({"hit", "miss", "cold", "warm", "bypass"}),
    "reason": frozenset({
        "adapter_unavailable", "backend_unavailable", "deadline_exceeded",
        "invalid_input", "missing_revision", "incomplete_coverage",
        "export_failed", "queue_full", "shutdown_timeout", "unknown",
    }),
}
_NUMERIC_ATTRIBUTES = frozenset({
    "candidates", "bytes", "rows", "batch_size", "references", "documents",
    "omitted", "errors", "exported", "dropped",
})


def validate_code_reference_attributes(attributes):
    """Return a safe copy of fixed categorical values and nonnegative counters."""
    safe = {}
    for key, value in attributes.items():
        if key in _CATEGORICAL_VALUES:
            if value not in _CATEGORICAL_VALUES[key]:
                raise ValueError(f"unsupported code-reference telemetry {key}")
        elif key in _NUMERIC_ATTRIBUTES:
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                    not math.isfinite(value) or value < 0):
                raise ValueError(f"invalid code-reference telemetry counter {key}")
        else:
            raise ValueError(f"prohibited code-reference telemetry attribute {key}")
        safe[key] = value
    return safe


class _Stage:
    def __init__(self, observer, name, attributes):
        self.observer, self.name, self.attributes = observer, name, attributes
        self.started, self.scope, self.span = None, None, None

    def __enter__(self):
        self.started = perf_counter()
        self.scope = self.observer._attempt(lambda: self.observer.tracer.start_as_current_span(
            self.name, attributes=self.attributes, record_exception=False,
            set_status_on_exception=False))
        self.span = self.observer._attempt(self.scope.__enter__) if self.scope is not None else None
        return self.span

    def __exit__(self, exc_type, exc, traceback):
        elapsed = max(0.0, perf_counter() - self.started)
        status = self.attributes.get("status", "ok")
        if exc_type is not None:
            if issubclass(exc_type, TimeoutError):
                status = "timeout"
            elif issubclass(exc_type, asyncio.CancelledError):
                status = "cancelled"
            else:
                status = "error"
        self.observer.completed_stages += 1
        self.observer.stage_duration_seconds += elapsed
        labels = {key: self.attributes[key] for key in
                  ("kind", "status", "resolver", "stage", "cache_state", "reason")
                  if key in self.attributes}
        labels["status"] = status
        self.observer._attempt(lambda: self.observer.feature_calls.add(1, labels))
        self.observer._attempt(lambda: self.observer.feature_duration.record(elapsed, labels))
        if self.span is not None:
            self.observer._attempt(lambda: self.span.set_attribute("status", status))
            self.observer._attempt(lambda: self.scope.__exit__(None, None, None))
        return False


def telemetry_stage(telemetry, name, **attributes):
    """Return a safe feature-stage context; absent or broken observers are no-ops."""
    if telemetry is None:
        return nullcontext()
    try:
        return telemetry.stage(name, **attributes)
    except Exception:
        try:
            telemetry.failures += 1
        except Exception:
            pass
        return nullcontext()


class KnowledgeTelemetry:
    def __init__(self, tracer, meter, logger=None, *, suppress_source_identity=False):
        self.tracer = tracer
        self.logger = logger
        self.suppress_source_identity = suppress_source_identity
        self.meter = meter
        self.duration = meter.create_histogram('ops.knowledge.duration', unit='s')
        self.calls = meter.create_counter('ops.knowledge.calls', unit='{call}')
        self.failures = 0
        self.completed_stages = 0
        self.stage_duration_seconds = 0.0
        self.feature_duration = None
        self.feature_calls = None

    def _attempt(self, fn):
        try:
            return fn()
        except Exception:
            self.failures += 1
            return None

    def run(self, operation, invoke):
        name = operation if operation in {'symbol', 'platform', 'context', 'discover', 'retrieve', 'check_references'} else 'unknown'
        started = perf_counter()
        scope = self._attempt(lambda: self.tracer.start_as_current_span(
            'knowledge.' + name, record_exception=False, set_status_on_exception=False))
        span = self._attempt(scope.__enter__) if scope is not None else None
        outcome = 'error'
        try:
            result = invoke()
            status = result.get('status')
            outcome = status if status in {'ok', 'partial', 'error', 'review_required', 'no_results'} else 'other'
            if span is not None:
                self._attempt(lambda: span.set_attribute('ops.outcome', outcome))
                if not self.suppress_source_identity:
                    snapshot = result.get('data', {}).get('snapshot_id')
                    if snapshot:
                        self._attempt(lambda: span.set_attribute('ops.source.snapshot_id', snapshot))
                    for key in ('repo_key', 'source_revision'):
                        value = result.get(key)
                        if isinstance(value, str) and len(value) <= 128:
                            self._attempt(lambda key=key, value=value: span.set_attribute('ops.' + key, value))
            return result
        finally:
            labels = {'operation': name, 'outcome': outcome}
            self._attempt(lambda: self.calls.add(1, labels))
            self._attempt(lambda: self.duration.record(perf_counter() - started, labels))
            if self.logger is not None:
                self._attempt(lambda: self.logger.emit(body='knowledge.request.completed', attributes=labels))
            if span is not None:
                self._attempt(lambda: span.set_attribute('ops.outcome', outcome))
                self._attempt(lambda: scope.__exit__(None, None, None))

    def stage(self, name, **attributes):
        """Instrument one fixed document-reference stage without payload identity."""
        if name not in CODE_REFERENCE_SPAN_NAMES:
            raise ValueError("unsupported code-reference telemetry span")
        if self.feature_duration is None:
            self.feature_duration = self.meter.create_histogram(
                'ops.code_references.stage.duration', unit='s')
            self.feature_calls = self.meter.create_counter(
                'ops.code_references.stage.calls', unit='{call}')
        return _Stage(self, name, validate_code_reference_attributes(attributes))
