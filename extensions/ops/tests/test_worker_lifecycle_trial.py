"""Worker candidate invariants and the retained independent runtime comparison."""
import ast
import gzip
import json
from pathlib import Path
import pytest
from kp_agent_tooling_ops._impl.lifecycle_matrix import route_source

ROOT = Path(__file__).resolve().parents[1]/'records/lifecycle-30'


def read(name):
    path = ROOT/name
    if path.exists(): return json.loads(path.read_text())
    with gzip.open(str(path)+'.gz','rt') as stream: return json.load(stream)


def test_refuses_to_thread_a_handler_containing_await():
    code='''async def graph_query():
    db = get_tenant_db(request)
    try:
        return await something()
    finally:
        db.close()
'''
    with pytest.raises(ValueError,match='async operations'):
        route_source(code,'worker-thread-candidate')


def test_worker_candidate_preserves_lifecycle_gate():
    if not (ROOT/'lifecycle-gate.json').exists() and not (ROOT/'lifecycle-gate.json.gz').exists():
        pytest.skip('controlled lifecycle execution not supplied')
    receipt=read('lifecycle-gate.json')
    rows=[r for r in receipt['rows'] if r['profile_id']=='worker-thread-candidate']
    assert len(rows)==14 and all(r['status']=='passed' for r in rows)
    source=receipt['candidate_sources']['worker-thread-candidate']
    fn=next(n for n in ast.parse(source).body if getattr(n,'name',None)=='graph_query')
    assert isinstance(fn,ast.FunctionDef)


def test_thread_execution_response_parity_and_bounded_ownership():
    if not (ROOT/'load.json').exists(): pytest.skip('real DB workload not supplied')
    load=read('load.json')
    hashes=set(); count=0
    for run in load['runs']:
        server=read(run['label']+'.json')
        by_trace={r['trace_id']:r for r in server['rows']}
        roots={s['context']['trace_id'][2:]:s for s in server['spans'] if s['name']=='ats.graph.request'}
        threaded=run['label'].startswith('thread')
        assert server['outstanding_leases']==0
        assert server['peak_leases'] <= server['pool_size']
        if threaded: assert server['peak_leases']>1
        for gate in run['gates']:
            assert gate['status']==gate['expected']
        for gate in run['gates'][:2]: assert by_trace[gate['trace_id']]['acquired']==0
        for batch in run['batches']:
            for client in batch['rows']:
                count+=1; hashes.add(client['body_sha256'])
                row=by_trace[client['trace_id']]
                assert row['status']==200 and row['acquired']==row['released']==1
                assert row['acquire_thread']==row['release_thread']
                assert (row['acquire_thread']!=row['event_loop_thread'])==threaded
                attrs=roots[client['trace_id']]['attributes']
                assert attrs['phase']==batch['phase']
                for repo in ('ats','core'):
                    assert attrs['source.'+repo+'.revision']==server['source'][repo]['revision']
    assert count==2560 and len(hashes)==1
