"""OPS-owned question relations, projected at read time; not runtime call edges."""
import hashlib
import math
from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.source_citations import source

SLICES = ('ats-pool-lifecycle','ats-worker-performance','ats-pool-failures',
          'ats-auth-acquisition','ats-response-correctness','ats-constrained-pool')


def links(slice_id, identity, budget):
    targets={
        'ats-pool-lifecycle': [('ats-worker-performance','Does offload improve timing with pool size held constant?','compares_profiles'),
                               ('ats-pool-failures','What was exercised when execution or release failed?','tests_failure_obligation'),
                               ('ats-auth-acquisition','Were authorization and acquisition failures exercised?','tests_entry_obligation'),
                               ('ats-response-correctness','What response correctness was checked against real SQL?','tests_response_obligation')],
        'ats-worker-performance':[('ats-pool-lifecycle','What baseline and candidate paths do these measurements represent?','requires_source_scope'),
                                  ('ats-constrained-pool','What changes when only two connections are available?','compares_capacity_constraint')],
        'ats-auth-acquisition':[('ats-pool-lifecycle','Which route and candidate did these controlled checks exercise?','requires_source_scope')],
        'ats-response-correctness':[('ats-worker-performance','What are the comparable timings for the tested fixture?','compares_profiles')],
        'ats-constrained-pool':[('ats-worker-performance','What changes with a larger pool under the same callers?','compares_capacity_constraint')],
        'ats-pool-failures':[('ats-pool-lifecycle','Which candidate and binding did these assertions exercise?','requires_source_scope')]}
    rows=[{'target_slice':target,'question':question,'relation':relation,'role':'adjacent_obligation',
        'semantic_hops':1,'graph_hops':None,
        'status':'registered','next_call':{'tool':'verification.packet','arguments':{
            'slice_id':target,'mode':'plan','budget':budget}}} for target,question,relation in targets[slice_id]]
    rows.append({'question':'What happens under production cancellation or multiworker saturation?',
        'relation':'requires_observation','role':'unresolved_obligation','status':'observation_required','basis':'The retained workload explicitly excludes production cancellation and multiworker saturation.',
        'semantic_hops':1,'graph_hops':None})
    return rows


def role(key,item):
    targets={'entry','candidate','comparison','failure-assertions','response-correctness',
             'identity-denied','policy-denied','missing-tenant','acquisition-failure','duplicate-outage'}
    value={'role':'target' if key in targets else 'supporting_evidence'}
    symbol=item.get('symbol')
    if symbol: value['symbol_containment_depth']=symbol.count('.')
    return value


def assemble_adjacent(config, slice_id):
    from kp_agent_tooling_ops._impl.verification_packet import assemble,artifact,PROFILE
    base=assemble(config)  # existing source/model/candidate/runner validation
    repo,rev=config['evidence_repo'],config['evidence_revision']
    gate,gref=artifact(repo,rev,'records/lifecycle-30/lifecycle-gate.json.gz')
    if slice_id=='ats-auth-acquisition':
        ids=('identity-denied','policy-denied','missing-tenant','acquisition-failure','duplicate-outage')
        result={}
        for sid in ids:
            rows=[r for r in gate['rows'] if r['profile_id'] in {'baseline',PROFILE} and r['scenario_id']==sid]
            if len(rows)!=2 or {r['profile_id'] for r in rows}!={'baseline',PROFILE}: raise ValueError('entry_scenarios_missing')
            scenario=next(s for s in gate['model']['scenarios'] if s['id']==sid)
            result[sid]={'evidence_class':'controlled-executed-assertions','artifact':gref,'scenario':scenario,
                'rows':rows,'scope':gate['scope'],'not_established':gate['not_established']}
        return result
    if slice_id=='ats-response-correctness':
        from kp_agent_tooling_ops._impl.verification_correctness import correctness
        return correctness(config,base,gate)
    if slice_id=='ats-pool-failures':
        ids={'executor-failure','release-failure','executor-release-failure'}
        rows=[r for r in gate['rows'] if r['profile_id']==PROFILE and r['scenario_id'] in ids]
        if {r['scenario_id'] for r in rows}!=ids or len(rows)!=len(ids): raise ValueError('failure_scenarios_missing')
        return {'failure-assertions':{'evidence_class':'controlled-executed-assertions','artifact':gref,
            'profile':PROFILE,'rows':rows,'scenarios':[s for s in gate['model']['scenarios'] if s['id'] in ids],
            'scope':gate['scope'],'not_established':gate['not_established']},
            'candidate':base['candidate'],'transformation':base['transformation']}
    audit=base['collector']['receipt']
    load,lref=artifact(repo,rev,'records/lifecycle-30/load.json')
    summaries,sref=artifact(repo,rev,'records/lifecycle-30/summary.json')
    runner_hash=hashlib.sha256(source(repo,rev,'scripts/lifecycle_workload_server.py')[1].encode()).hexdigest()
    comparisons=[]; identities=[]; environments=[]
    pool=2 if slice_id=='ats-constrained-pool' else 8
    for label,profile in [(f'async-{pool}','ordered-pooled-candidate'),(f'thread-{pool}',PROFILE)]:
        runs=[r for r in load['runs'] if r['label'] in {label+'-a',label+'-b'}]
        if len(runs)!=2: raise ValueError('comparison_run_missing')
        clients=[];lags=[];waits=[];wall=0
        for run in runs:
            host,href=artifact(repo,rev,'records/lifecycle-30/'+run['label']+'.json.gz')
            if leaf.sorted_sha256(host)!=audit['receipts_sha256'][run['label']]:
                raise ValueError('comparison_receipt_mismatch')
            if host['profile']!=profile or host['pool_size']!=pool: raise ValueError('comparison_dimensions_mismatch')
            if host['candidate_sha256']!=hashlib.sha256(gate['candidate_sources'][profile].encode()).hexdigest():
                raise ValueError('comparison_candidate_mismatch')
            for key,pin in gate['source'].items():
                if any(host['source'][key][f]!=pin[f] for f in ('revision','path','blob_sha')): raise ValueError('comparison_source_mismatch')
            if host['runner_sha256']!=runner_hash:
                raise ValueError('comparison_runner_mismatch')
            environments.append(host['environment'])
            batches=[b for b in run['batches'] if b['concurrency']==8]
            if not batches: raise ValueError('comparison_batches_missing')
            phases={b['phase'] for b in batches}
            waits += [sum(r['stages']['pool-wait']) for r in host['rows'] if r['phase'] in phases]
            clients += [r['seconds'] for b in batches for r in b['rows']]
            lags += [r['seconds'] for r in host['event_loop_lag'] if r['phase'] in phases]
            wall += sum(b['wall_seconds'] for b in batches)
            identities.append({'label':run['label'],'artifact':href,'candidate_sha256':host['candidate_sha256'],
                'profile':profile,'pool_size':pool,'concurrency':8,'phases':sorted(phases),'runner_sha256':host['runner_sha256']})
        pct=lambda values: sorted(values)[math.ceil(.95*len(values))-1]*1000
        measured={'requests':len(clients),'throughput_rps':len(clients)/wall,
            'client_p95_ms':pct(clients),'event_loop_lag_p95_ms':pct(lags),'pool_wait_p95_ms':pct(waits)}
        matching=[s for s in summaries if s['profile']==label and s['concurrency']==8]
        if len(matching)!=1 or any(not math.isclose(measured[k],matching[0][k],rel_tol=1e-10) for k in measured):
            raise ValueError('comparison_summary_mismatch')
        comparisons.append({'label':label,'profile':profile,'pool_size':pool,'concurrency':8,**measured})
    if any(e!=environments[0] for e in environments): raise ValueError('comparison_environment_mismatch')
    if len({i['runner_sha256'] for i in identities})!=1: raise ValueError('comparison_runner_mismatch')
    return {'comparison':{'evidence_class':'recomputed-measurement-summary','rows':comparisons,
        'inputs':[lref,sref],'host_inputs':identities,'environment':environments[0],
        'method':'throughput=count/sum(batch wall seconds); p95=nearest-rank from retained raw samples; pool wait sums stage samples per request',
        'scope':'same pool size and caller count, both controls already pooled; not direct-connect baseline versus candidate',
        'limitations':['Host load/cache remain uncontrolled.','No production sizing, cancellation or multiworker proof.']},
        'collector':base['collector']}
