"""Run with the prepared Core venv and Core checkout on PYTHONPATH; no DB/network."""
import json
import sys
from tracer_bootstrap import start_tracing

disabled = start_tracing(enabled=False, service_name='', service_version='', endpoint='')
assert not any(name.startswith('opentelemetry') for name in sys.modules)
assert disabled.close()['enabled'] is False

from opentelemetry import trace
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

global_before = trace.get_tracer_provider()
memory = InMemorySpanExporter()
runtime = start_tracing(enabled=True, service_name='stub-check', service_version='test',
                        endpoint='http://unused.invalid/v1/traces', exporter_override=memory)
with runtime.provider.get_tracer('host').start_as_current_span('host.operation') as parent:
    with runtime.diagnostics.span('core.evidence.record'):
        with runtime.diagnostics.span('core.evidence.commit'):
            pass
result = runtime.close()
assert result['flush_completed'] and result['exported_spans'] == 3
assert not result['shutdown_errors'] and result['observer_failures'] == 0
spans = {span.name: span for span in memory.get_finished_spans()}
assert spans['core.evidence.record'].parent.span_id == parent.context.span_id
assert spans['core.evidence.commit'].parent.span_id == spans['core.evidence.record'].context.span_id
assert spans['core.evidence.record'].resource.attributes['service.name'] == 'stub-check'
assert trace.get_tracer_provider() is global_before

class BrokenExporter(SpanExporter):
    def export(self, spans):
        raise RuntimeError('private failure')
    def shutdown(self):
        pass

runtime = start_tracing(enabled=True, service_name='stub-check', service_version='test',
                        endpoint='http://unused.invalid/v1/traces', exporter_override=BrokenExporter())
with runtime.diagnostics.span('core.evidence.record'):
    application_result = 'succeeded'
result = runtime.close()
assert application_result == 'succeeded'
assert result['failed_export_spans'] == 1 and result['exported_spans'] == 0
assert 'private failure' not in json.dumps(result)
print(json.dumps({'status': 'passed', 'checks': [
    'disabled SDK import isolation', 'parentage and resource identity',
    'no global provider replacement', 'flush and shutdown',
    'exporter failure isolation and counters']}))
