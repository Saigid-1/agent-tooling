"""Controlled ASGI lifecycle experiment. Run in a dedicated process; no real DB."""
import ast
from contextlib import contextmanager, ExitStack
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import types
from unittest.mock import patch
from kp_agent_tooling_ops._impl.behavior_model import digest, validate
from kp_agent_tooling._impl.source_citations import source

PROFILES = ('baseline', 'eager-negative-control', 'ordered-pooled-candidate')
# name, Given, Then, expected HTTP status (None means raised exception), acquire count
SCENARIOS = [
 ('success', 'valid request', '200; one lease returned', 200, 1),
 ('duplicate', 'duplicate scalar input', '422 before acquisition', 422, 0),
 ('duplicate-outage', 'duplicate input and unavailable pool', '422 before acquisition', 422, 0),
 ('missing-query', 'missing query name', '422 before acquisition', 422, 0),
 ('identity-denied', 'identity denied', '401 before policy or acquisition', 401, 0),
 ('policy-denied', 'ATS PersonVisibility denied', '403 before acquisition', 403, 0),
 ('missing-tenant', 'no verified tenant schema', 'refusal before physical checkout', None, 1),
 ('acquisition-failure', 'unavailable resource', 'exception with no held lease', None, 1),
 ('unknown-query', 'unregistered query', '404 after acquisition and release', 404, 1),
 ('bad-registry-params', 'registered query missing root', '422 after acquisition and release', 422, 1),
 ('executor-failure', 'executor raises', 'exception and one release attempt', None, 1),
 ('serialization-failure', 'envelope conversion raises', 'exception and one release attempt', None, 1),
 ('release-failure', 'resource return raises', 'exception; unresolved ownership disclosed', None, 1),
 ('executor-release-failure', 'executor and return raise', 'release masks executor; retain both', None, 1),
]

def model(profiles=PROFILES):
    ids = [s[0] for s in SCENARIOS]
    value = {'schema_version': 'ops.behavior-model.v1', 'owner': 'OPS',
      'features': [{'id': 'ats-graph-resource-lifecycle', 'spec_ids': ['lifecycle-parity']}],
      'specs': [{'id': 'lifecycle-parity', 'contract_ids': ['GET /graph/query']}],
      'contracts': [{'id': 'GET /graph/query', 'operation_key': 'GET /graph/query',
                    'invariants': ['preserve failure precedence', 'disclose unresolved ownership'], 'scenario_ids': ids}],
      'scenarios': [{'id': sid, 'given': given, 'event': 'execute one ASGI request', 'then': then,
                     'assertion_id': 'lifecycle:'+sid, 'expected_status': status, 'expected_acquisitions': count}
                    for sid, given, then, status, count in SCENARIOS],
      'implementations': [{'id': p, 'contract_id': 'GET /graph/query',
                          'symbol': 'product.routers.graph.graph_query', 'scenario_ids': ids} for p in profiles],
      'profiles': [{'id': p, 'guard': {'composition': p}, 'implementation_ids': [p]} for p in profiles]}
    validate(value)
    return value

def pinned_checkout(root, revision, entry):
    root = Path(root).resolve()
    head = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if head != revision: raise ValueError('checkout HEAD must equal declared revision')
    if subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain', '--untracked-files=normal']):
        raise ValueError('clean source checkout required, including untracked files')
    blob, text = source(root, revision, entry)
    return {'revision': revision, 'path': entry, 'blob_sha': blob, 'checkout': str(root)}, text

def route_source(text, profile):
    tree = ast.parse(text)
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'graph_query')
    assignments = [n for n in fn.body if isinstance(n, ast.Assign) and ast.unparse(n) == 'db = get_tenant_db(request)']
    cleanup = [n for n in ast.walk(fn) if isinstance(n, ast.Try) and n.finalbody and ast.unparse(n.finalbody[0]) == 'db.close()']
    if len(assignments) != 1 or len(cleanup) != 1: raise ValueError('unsupported source shape; review transformation')
    if profile in {'ordered-pooled-candidate', 'worker-thread-candidate'}:
        assignments[0].value = ast.parse('_acquire(user)', mode='eval').body
        cleanup[0].finalbody = ast.parse('_release(db)').body
    elif profile == 'eager-negative-control':
        fn.args.args.append(ast.arg(arg='db'))
        fn.args.defaults.append(ast.parse('Depends(_eager)', mode='eval').body)
        fn.body.remove(assignments[0])
        outer = cleanup[0]
        index = fn.body.index(outer)
        fn.body[index:index+1] = outer.body
    elif profile != 'baseline': raise ValueError('unknown profile')
    if profile == 'worker-thread-candidate':
        if any(isinstance(n, (ast.Await, ast.AsyncFor, ast.AsyncWith)) for n in ast.walk(fn)):
            raise ValueError('cannot offload a route containing async operations')
        sync = ast.FunctionDef(**{name: getattr(fn, name) for name in fn._fields})
        ast.copy_location(sync, fn)
        tree.body[tree.body.index(fn)] = sync
    return ast.unparse(ast.fix_missing_locations(tree))

@contextmanager
def candidate_module(module):
    # Restore only this entry: snapshotting all sys.modules races SDK exporter imports.
    previous = sys.modules.get(module.__name__)
    sys.modules[module.__name__] = module
    try:
        yield
    finally:
        if previous is None:
            sys.modules.pop(module.__name__, None)
        else:
            sys.modules[module.__name__] = previous


class Observation:
    def __init__(self, tracer, histogram, profile, scenario):
        self.tracer, self.histogram = tracer, histogram
        self.labels = {'lifecycle.profile': profile, 'lifecycle.scenario': scenario}
        self.events, self.origin = [], time.perf_counter_ns()
    @contextmanager
    def stage(self, name):
        start = time.perf_counter_ns()
        self.events.append({'stage': name, 'event': 'start', 'offset_ns': start-self.origin})
        with self.tracer.start_as_current_span(name, attributes=self.labels):
            outcome = 'ok'
            try: yield
            except Exception:
                outcome = 'error'
                raise
            finally:
                end = time.perf_counter_ns()
                self.events.append({'stage': name, 'event': 'end', 'outcome': outcome,
                                    'offset_ns': end-self.origin, 'duration_ns': end-start})
                if self.histogram:
                    self.histogram.record((end-start)/1e9, {**self.labels, 'lifecycle.stage': name, 'lifecycle.outcome': outcome})

class ResourceFault(RuntimeError): pass

class ControlledPool:
    def __init__(self, obs, scenario):
        self.obs, self.scenario = obs, scenario
        self.held = self.checkouts = self.returns = 0
    def getconn(self):
        with self.obs.stage('physical-checkout'):
            self.checkouts += 1
            if self.scenario in {'acquisition-failure', 'duplicate-outage'}: raise ResourceFault('controlled acquisition fault')
            self.held += 1
            return ControlledConnection(self)
    def putconn(self, conn, close=False):
        with self.obs.stage('resource-return'):
            self.returns += 1
            if self.scenario in {'release-failure', 'executor-release-failure'}: raise ResourceFault('controlled release fault')
            self.held -= 1

class ControlledConnection:
    def __init__(self, pool): self.pool = pool
    def rollback(self):
        with self.pool.obs.stage('rollback'): pass
    def cursor(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def execute(self, sql):
        if not sql.startswith('SET search_path = '): raise AssertionError('unexpected SQL in controlled harness')
        with self.pool.obs.stage('schema-pin'): pass
    def commit(self):
        with self.pool.obs.stage('commit'): pass
    def close(self): self.pool.putconn(self)

def execute_row(graph, db, text, profile, scenario, tracer, histogram, trace_attributes=None):
    from fastapi import FastAPI, Depends, HTTPException, Request
    from fastapi.testclient import TestClient
    from kp_core.api.auth_context import VerifiedUser
    from product.projections.visibility import PersonVisibility
    from product.graph.envelope import GraphEnvelope
    sid, _, _, expected_status, expected_acquire = scenario
    obs = Observation(tracer, histogram, profile, sid)
    pool, sema = ControlledPool(obs, sid), threading.BoundedSemaphore(1)
    def identity(request: Request):
        with obs.stage('identity'):
            if sid == 'identity-denied': raise HTTPException(401, 'controlled denial')
            schema = '' if sid == 'missing-tenant' else 'lifecycle_test'
            request.state.tenant_schema = schema
            return VerifiedUser(kp_user_id='fixture', auth0_sub='fixture', email='fixture@example.test',
              email_verified=True, name='fixture', role='recruiter', tenant_id='fixture', tenant_schema=schema)
    def policy(user=Depends(identity)):
        with obs.stage('policy'):
            if sid == 'policy-denied': raise HTTPException(403, 'controlled denial')
            return PersonVisibility(scope='all')
    def acquire(user):
        with obs.stage('acquire'): return db.acquire_tenant(user.tenant_schema)
    def release(conn):
        with obs.stage('release'): db.release(conn)
    def eager(user=Depends(identity), visibility=Depends(policy)):
        conn = acquire(user)
        try: yield conn
        finally: release(conn)
    def baseline(request):
        with obs.stage('acquire'): return db.get_tenant_db(request)
    def dispatch(name, **kwargs):
        with obs.stage('registry-dispatch'):
            if sid in {'unknown-query', 'bad-registry-params'}: return graph.run_query(name, **kwargs)
            with obs.stage('executor'):
                if sid in {'executor-failure', 'executor-release-failure'}: raise ResourceFault('controlled executor fault')
            class Envelope:
                def as_dict(self):
                    with obs.stage('envelope-conversion'):
                        if sid == 'serialization-failure': raise ResourceFault('controlled serialization fault')
                        return GraphEnvelope(query=name, params=kwargs['params'], roots=[], nodes={},
                                             edges=[], omitted=[], unresolved=[]).as_dict()
            return Envelope()
    transformed = route_source(text, profile)
    module = types.ModuleType('_ops_lifecycle_candidate')
    module.__dict__.update(_acquire=acquire, _release=release, _eager=eager)
    with candidate_module(module):
        exec(compile(transformed, '<ops-lifecycle-candidate>', 'exec'), module.__dict__)
        module.get_tenant_db, module.run_query = baseline, dispatch
        app = FastAPI()
        app.include_router(module.router)
        app.dependency_overrides[graph.require_user] = identity
        app.dependency_overrides[graph.require_scope] = policy
        url = '/graph/query?q=reports_to_tree&root=fixture'
        if sid.startswith('duplicate'): url += '&root=duplicate'
        elif sid == 'missing-query': url = '/graph/query?root=fixture'
        elif sid == 'unknown-query': url = '/graph/query?q=unknown'
        elif sid == 'bad-registry-params': url = '/graph/query?q=reports_to_tree'
        wire = {}
        async def observed_app(scope, receive, send):
            async def observed_send(message):
                if message['type'] == 'http.response.start':
                    wire['status'] = message['status']
                    obs.events.append({'stage': 'response-start', 'event': 'observed',
                                       'offset_ns': time.perf_counter_ns()-obs.origin})
                await send(message)
            await app(scope, receive, observed_send)
        with ExitStack() as stack:
            for key, value in {'_pool': pool, '_pool_sema': sema, '_pool_pid': os.getpid(),
                               '_connect': lambda schema: pool.getconn()}.items():
                stack.enter_context(patch.object(db, key, value))
            stack.enter_context(patch.object(db.psycopg2, 'connect', side_effect=AssertionError('real DB forbidden')))
            stack.enter_context(patch.object(db.psycopg2.pool, 'ThreadedConnectionPool', side_effect=AssertionError('real pool forbidden')))
            error_chain, status = [], None
            with tracer.start_as_current_span('lifecycle.request', attributes={**obs.labels, **(trace_attributes or {})}) as span:
                trace_id = format(span.get_span_context().trace_id, '032x')
                with TestClient(observed_app) as client:
                    try:
                        with obs.stage('asgi-request'): response = client.get(url)
                        status = response.status_code
                    except Exception as exc:
                        while exc is not None:
                            error_chain.append({'type': type(exc).__name__, 'message': str(exc)})
                            exc = exc.__context__
            slot_free = sema.acquire(blocking=False)
            if slot_free: sema.release()
    acquisitions = sum(e['stage'] == 'acquire' and e['event'] == 'start' for e in obs.events)
    starts = [e['stage'] for e in obs.events if e['event'] == 'start']
    held = int(sid in {'release-failure', 'executor-release-failure'})
    checkout = int(expected_acquire == 1 and sid != 'missing-tenant')
    returns = int(checkout and sid != 'acquisition-failure')
    checks = {'public_status': status == expected_status, 'wire_status': wire.get('status') == (expected_status or 500), 'exception_presence': bool(error_chain) == (expected_status is None),
      'acquisition_count': acquisitions == expected_acquire, 'physical_checkout_count': pool.checkouts == checkout,
      'return_attempt_count': pool.returns == returns, 'ownership_accounted': pool.held == held,
      'semaphore_restored': slot_free,
      'policy_before_acquire': not acquisitions or ('policy' in starts and starts.index('policy') < starts.index('acquire')),
      'dual_failure_retained': sid != 'executor-release-failure' or [e['message'] for e in error_chain][:2] ==
                              ['controlled release fault', 'controlled executor fault']}
    return {'profile_id': profile, 'scenario_id': sid, 'trace_id': trace_id,
      'candidate_sha256': hashlib.sha256(transformed.encode()).hexdigest(), 'checks': checks,
      'status': 'passed' if all(checks.values()) else 'failed', 'http_status': status, 'wire_status': wire.get('status'), 'exception_chain': error_chain,
      'events': obs.events, 'resource': {'checkouts': pool.checkouts, 'return_attempts': pool.returns, 'unresolved_leases': pool.held},
      'risk': 'return failure leaves physical ownership unresolved' if held else None}

def run(ats, ats_revision, core, core_revision, repeat=1, endpoint=None, profiles=PROFILES):
    if not 1 <= repeat <= 100: raise ValueError('repeat must be 1..100')
    ats_pin, text = pinned_checkout(ats, ats_revision, 'product/routers/graph.py')
    core_pin, _ = pinned_checkout(core, core_revision, 'kp_core/api/db.py')
    sys.path[:0] = [str(Path(ats).resolve()), str(Path(core).resolve())]
    graph, db = importlib.import_module('product.routers.graph'), importlib.import_module('kp_core.api.db')
    for prefix, root in [('product', ats), ('kp_core', core)]:
        for name, module in list(sys.modules.items()):
            if name == prefix or name.startswith(prefix+'.'):
                path = getattr(module, '__file__', None)
                if path and not Path(path).resolve().is_relative_to(Path(root).resolve()): raise ValueError('import provenance mismatch: '+name)
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    runtime = None
    if endpoint:
        from kp_agent_tooling_ops._impl.service.telemetry_runtime import local_otlp
        runtime = local_otlp(endpoint, service_name='ops-lifecycle-matrix', service_version='1')
    provider = runtime.traces if runtime else TracerProvider()
    memory = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    tracer = provider.get_tracer('ops.lifecycle')
    histogram = runtime.metrics.get_meter('ops.lifecycle').create_histogram('ops.lifecycle.stage.duration', unit='s') if runtime else None
    authored, rows = model(profiles), []
    try:
        for iteration in range(repeat):
            for profile in profiles:
                for scenario in SCENARIOS:
                    row = execute_row(graph, db, text, profile, scenario, tracer, histogram,
                      {'lifecycle.model_digest': digest(authored), 'source.ats.revision': ats_revision,
                       'source.core.revision': core_revision, 'lifecycle.evidence_kind': 'controlled-asgi'})
                    row.update(iteration=iteration, model_digest=digest(authored),
                      scenario_digest=digest(next(s for s in authored['scenarios'] if s['id'] == scenario[0])))
                    rows.append(row)
        spans = [json.loads(span.to_json()) for span in memory.get_finished_spans()]
        flushed = runtime.flush() if runtime else provider.force_flush()
    finally:
        runtime.shutdown() if runtime else provider.shutdown()
    # Lazy imports and source edits during execution must not silently escape the boundary.
    pinned_checkout(ats, ats_revision, 'product/routers/graph.py')
    pinned_checkout(core, core_revision, 'kp_core/api/db.py')
    return {'candidate_sources': {p: route_source(text, p) for p in profiles},
      'schema_version': 'ops.lifecycle-execution.v1', 'model': authored, 'model_digest': digest(authored),
      'source': {'ats': ats_pin, 'core': core_pin}, 'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
      'environment': {'python': sys.version, 'fastapi': importlib.metadata.version('fastapi'),
         'installed_kp_core_metadata': importlib.metadata.version('kp-core'), 'imported_core': db.__file__, 'imported_ats': graph.__file__},
      'scope': 'actual ASGI and pinned Core checkout code; controlled identity, policy, executor and database resources',
      'not_established': ['live identity/policy behavior', 'SQL results', 'real pool cleanup', 'concurrency/backpressure',
                          'production latency or capacity', 'deployed candidate'],
      'telemetry': {'sdk_flush_completed': flushed, 'collector_delivery': 'unverified', 'spans': spans}, 'rows': rows}


def render_markdown(receipt):
    """Bounded journey view; validate identity before joining rows to traces/model."""
    import statistics
    authored = receipt['model']
    groups = validate(authored)
    if receipt['model_digest'] != digest(authored): raise ValueError('model digest mismatch')
    roots = {s['context']['trace_id'].removeprefix('0x'): s for s in receipt['telemetry']['spans']
             if s['name'] == 'lifecycle.request'}
    for row in receipt['rows']:
        scenario = groups['scenarios'][row['scenario_id']]
        root = roots.get(row['trace_id'])
        if (row['model_digest'] != digest(authored) or row['scenario_digest'] != digest(scenario)
            or row['candidate_sha256'] != hashlib.sha256(receipt['candidate_sources'][row['profile_id']].encode()).hexdigest()
            or root is None or root['attributes'].get('lifecycle.scenario') != row['scenario_id']
            or root['attributes'].get('lifecycle.profile') != row['profile_id']
            or root['attributes'].get('lifecycle.model_digest') != digest(authored)
            or any(root['attributes'].get('source.'+repo+'.revision') != receipt['source'][repo]['revision']
                   for repo in ('ats', 'core'))):
            raise ValueError('incompatible lifecycle evidence')
    lines = ['# Executed ATS/Core lifecycle matrix', '',
      'Feature: `ats-graph-resource-lifecycle` → spec: `lifecycle-parity` → contract: `GET /graph/query`.', '',
      'Evidence: [execution receipt](execution.json). Every row retains scenario/model/candidate hashes, source pins, events and trace ID.', '',
      '| Scenario | '+' | '.join(groups['profiles'])+' |',
      '| --- | '+' | '.join('---' for _ in groups['profiles'])+' |']
    for sid in groups['scenarios']:
        values = []
        for profile in groups['profiles']:
            rows = [r for r in receipt['rows'] if r['scenario_id'] == sid and r['profile_id'] == profile]
            if not rows: raise ValueError('missing matrix row')
            values.append('PASS' if all(r['status'] == 'passed' for r in rows) else 'FAIL')
        lines.append('| '+sid+' | '+' | '.join(values)+' |')
    lines += ['', 'PASS means the declared baseline-parity observations matched. It does not mean the behavior is safe for production.', '',
      'Return faults retain unresolved physical ownership; simultaneous executor/return faults retain the masked exception chain. '
      'The eager dependency can start a 200 response before cleanup fails.', '',
      '## Controlled timing observations', '',
      'ASGI request duration includes in-process transport and instrumentation. Resources and policy are simulated. '
      'These samples are harness diagnostics, not a performance ranking or production sizing estimate.', '',
      '| Profile | Requests | Median ms | p95 ms (nearest rank) |', '| --- | ---: | ---: | ---: |']
    import math
    for profile in groups['profiles']:
        samples = sorted(e['duration_ns']/1e6 for r in receipt['rows'] if r['profile_id'] == profile
                         for e in r['events'] if e['stage'] == 'asgi-request' and e['event'] == 'end')
        lines.append(f'| {profile} | {len(samples)} | {statistics.median(samples):.3f} | {samples[math.ceil(.95*len(samples))-1]:.3f} |')
    lines += ['', 'Do not compare these aggregate medians as speedups: scenario work differs by profile. '
      'For optimization, compare the same scenario and real workload; inspect stage spans, wait time, retries, errors and ownership together.', '',
      '## Source boundary', '']
    for repo, pin in receipt['source'].items():
        lines.append(f"- {repo}: `{pin['revision']}`, `{pin['path']}`, blob `{pin['blob_sha']}`.")
    lines += ['', 'Candidate compositions are generated in OPS; ATS/Core sources were not changed. '
      'This is a local evidence view, not a compiler fact or a published graph assertion.', '',
      'Unverified: '+', '.join(receipt['not_established'])+'.', '']
    return '\n'.join(lines)
