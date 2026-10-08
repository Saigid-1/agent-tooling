"""Trace-linked retained response checks; no inference from timings to correctness."""
import ast
import hashlib
from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.source_citations import source


def correctness(config,base,gate):
    from kp_agent_tooling_ops._impl.verification_packet import artifact
    repo,rev=config['evidence_repo'],config['evidence_revision']
    load,lref=artifact(repo,rev,'records/lifecycle-30/load.json')
    blob,client=source(repo,rev,'scripts/lifecycle_workload_cli.py')
    _,fixture=source(repo,rev,'records/lifecycle-30/fixture.sql')
    _,runner=source(repo,rev,'scripts/lifecycle_workload_server.py')
    if load['client_sha256']!=hashlib.sha256(client.encode()).hexdigest(): raise ValueError('correctness_client_mismatch')
    if load['fixture_sha256']!=hashlib.sha256(fixture.encode()).hexdigest(): raise ValueError('correctness_fixture_mismatch')
    tree=ast.parse(client)
    checks=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='one']
    loops=[n for n in ast.walk(tree) if isinstance(n,ast.For) and ast.unparse(n.target)=='(path, expected)']
    if len(checks)!=1 or len(loops)!=1: raise ValueError('correctness_checker_not_unique')
    cases=ast.literal_eval(loops[0].iter)
    audit=base['collector']['receipt'];hashes=set();total=0;gates=[];inputs=[]
    expected_labels={f'{mode}-{pool}-{order}' for mode in ('async','thread') for pool in (2,8) for order in ('a','b')}
    if {r['label'] for r in load['runs']}!=expected_labels or len(load['runs'])!=8: raise ValueError('correctness_runs_missing')
    for run in load['runs']:
        label=run['label'];mode,pool,_=label.split('-')
        profile='worker-thread-candidate' if mode=='thread' else 'ordered-pooled-candidate'
        host,href=artifact(repo,rev,'records/lifecycle-30/'+label+'.json.gz')
        if leaf.sorted_sha256(host)!=audit['receipts_sha256'][label]: raise ValueError('correctness_host_mismatch')
        if host['profile']!=profile or host['pool_size']!=int(pool): raise ValueError('correctness_profile_mismatch')
        if host['runner_sha256']!=hashlib.sha256(runner.encode()).hexdigest(): raise ValueError('correctness_runner_mismatch')
        if host['candidate_sha256']!=hashlib.sha256(gate['candidate_sources'][profile].encode()).hexdigest(): raise ValueError('correctness_candidate_mismatch')
        for key,pin in gate['source'].items():
            if any(host['source'][key][f]!=pin[f] for f in ('revision','path','blob_sha')): raise ValueError('correctness_source_mismatch')
        by_trace={r['trace_id']:r for r in host['rows']}
        if len(by_trace)!=len(host['rows']): raise ValueError('correctness_ambiguous_trace')
        if len(run['gates'])!=len(cases): raise ValueError('correctness_gates_missing')
        for (path,expected),record in zip(cases,run['gates']):
            observed=by_trace.get(record['trace_id'])
            if record['expected']!=expected or record['status']!=expected or observed is None or observed['status']!=expected:
                raise ValueError('correctness_gate_trace_mismatch')
            gates.append({'run':label,'request':path,**record})
        for batch in run['batches']:
            for row in batch['rows']:
                observed=by_trace.get(row['trace_id'])
                if observed is None or observed['status']!=200 or observed['phase']!=batch['phase']: raise ValueError('correctness_response_trace_mismatch')
                hashes.add(row['body_sha256']);total+=1
        inputs.append({'label':label,'artifact':href,'profile':profile,'candidate_sha256':host['candidate_sha256']})
    if len(hashes)!=1 or load['response_parity']!='passed' or total!=2560: raise ValueError('correctness_parity_mismatch')
    node=checks[0];text='\n'.join(client.splitlines()[node.lineno-1:node.end_lineno])
    return {'response-correctness':{'evidence_class':'trace-linked-client-checks','artifact':lref,
        'measured_responses':total,'body_sha256':next(iter(hashes)),'http_gates':gates,'host_inputs':inputs,
        'fixture_sha256':load['fixture_sha256'],'workload':load['workload'],
        'scope':'real SQL fixture, controlled identity/policy; retained client assertion results and identical response hashes',
        'limitations':['Raw response bodies were not retained; content assertions cannot be independently rerun from hashes.',
                        'Fixture checks do not establish general query correctness or production authorization.']},
        'response-checker':{'evidence_class':'source','citation':{'path':'scripts/lifecycle_workload_cli.py','revision':rev,'blob_sha':blob},
            'start_line':node.lineno,'end_line':node.end_lineno,'text':text},
        'http-gate-checker':{'evidence_class':'source','citation':{'path':'scripts/lifecycle_workload_cli.py','revision':rev,'blob_sha':blob},
            'start_line':loops[0].lineno,'end_line':loops[0].end_lineno,
            'text':'\n'.join(client.splitlines()[loops[0].lineno-1:loops[0].end_lineno])}}
