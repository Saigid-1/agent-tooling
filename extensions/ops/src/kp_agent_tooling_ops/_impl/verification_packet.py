"""Claim slices composed from committed source and compatible retained receipts."""
import ast
import gzip
import hashlib
import io
import json
import subprocess
from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.source_citations import source
from kp_agent_tooling_ops._impl.behavior_model import digest
from kp_agent_tooling_ops._impl.lifecycle_matrix import render_markdown

PROFILE = 'worker-thread-candidate'
SLICE = 'ats-pool-lifecycle'


def encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True).encode()


def excerpt(text, symbol):
    node = ast.parse(text)
    for name in symbol.split('.'):
        hits=[n for n in node.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and n.name==name]
        if len(hits)!=1: raise ValueError('symbol_not_unique')
        node=hits[0]
    start=min([node.lineno]+[d.lineno for d in getattr(node,'decorator_list',[])])
    value='\n'.join(text.splitlines()[start-1:node.end_lineno])
    return {'symbol':symbol,'start_line':start,'end_line':node.end_lineno,'text':value,
            'excerpt_sha256':hashlib.sha256(value.encode()).hexdigest()}


def artifact(repo, revision, path):
    # Existing source() validates regular Git files, but compressed receipts need bytes.
    entry=subprocess.check_output(['git','ls-tree',revision,'--',path],cwd=repo,timeout=15).decode().strip()
    meta,actual=entry.split('\t'); mode,kind,blob=meta.split()
    if actual!=path or mode not in {'100644','100755'} or kind!='blob': raise ValueError('invalid_receipt_source')
    size=int(subprocess.check_output(['git','cat-file','-s',blob],cwd=repo,timeout=15))
    if size>16_000_000: raise ValueError('receipt_budget_exceeded')
    raw=subprocess.check_output(['git','cat-file','blob',blob],cwd=repo,timeout=15)
    if path.endswith('.gz'):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as file: payload=file.read(32_000_001)
    else: payload=raw
    if len(payload)>32_000_000: raise ValueError('decoded_receipt_budget_exceeded')
    return json.loads(payload),{'revision':revision,'path':path,'blob_sha':blob,
        'payload_sha256':hashlib.sha256(payload).hexdigest()}


def validate_join(gate, host, audit, sources, runner_text, transform_text):
    # Reuse the lifecycle validator: model, scenario, candidate and trace roots.
    render_markdown(gate)
    for receipt in (gate,host):
        for key, pin in sources.items():
            if any(receipt['source'][key].get(field)!=pin[field] for field in ('revision','path','blob_sha')):
                raise ValueError('receipt_source_mismatch')
    candidate=gate['candidate_sources'][PROFILE]
    candidate_hash=hashlib.sha256(candidate.encode()).hexdigest()
    if host['profile']!=PROFILE or host['candidate_sha256']!=candidate_hash:
        raise ValueError('candidate_profile_mismatch')
    if host['runner_sha256']!=hashlib.sha256(runner_text.encode()).hexdigest():
        raise ValueError('workload_runner_mismatch')
    if gate['runner_sha256']!=hashlib.sha256(transform_text.encode()).hexdigest():
        raise ValueError('gate_runner_mismatch')
    if audit['receipts_sha256']['thread-8-a']!=leaf.sorted_sha256(host):
        raise ValueError('collector_receipt_mismatch')
    return candidate


def assemble(config):
    evidence_repo, rev=config['evidence_repo'],config['evidence_revision']
    items={}; pins={}
    def add_source(key, repo_key, path, symbol):
        spec=config['repos'][repo_key]
        blob,text=source(spec['path'],spec['revision'],path)
        pin={'repo_key':repo_key,'revision':spec['revision'],'path':path,'blob_sha':blob}
        items[key]={'evidence_class':'source','citation':pin,**excerpt(text,symbol)}
        return pin,text
    pins['ats'],entry_text=add_source('entry','ats','product/routers/graph.py','graph_query')
    from kp_agent_tooling._impl.service.import_context import import_context
    spec=config['repos']['ats']
    items['entry-import']={'evidence_class':'source-syntax','context':import_context(spec['path'],spec['revision'],'ats','product/routers/graph.py','get_tenant_db',[])}
    pins['core'],_=add_source('tenant','core','kp_core/api/db.py','get_tenant_db')
    add_source('connect','core','kp_core/api/db.py','_connect')
    add_source('pool-acquire','core','kp_core/api/db.py','acquire_tenant')
    add_source('pool-release','core','kp_core/api/db.py','release')
    def ops_source(key,path,symbol):
        blob,text=source(evidence_repo,rev,path)
        items[key]={'evidence_class':'source','citation':{'repo_key':'ops','revision':rev,'path':path,'blob_sha':blob},**excerpt(text,symbol)}
        return text
    transform=ops_source('transformation','kp_ops/lifecycle_matrix.py','route_source')
    runner=ops_source('acquire-binding','scripts/lifecycle_workload_server.py','main.acquire')
    ops_source('release-binding','scripts/lifecycle_workload_server.py','main.release')
    gate,gref=artifact(evidence_repo,rev,'records/lifecycle-30/lifecycle-gate.json.gz')
    host,href=artifact(evidence_repo,rev,'records/lifecycle-30/thread-8-a.json.gz')
    audit,aref=artifact(evidence_repo,rev,'records/lifecycle-30/collector-audit.json')
    candidate=validate_join(gate,host,audit,pins,runner,transform)
    # Show the binding assignment as source too; a wrapper declaration alone is insufficient.
    lines=runner.splitlines(); anchor='    module.__dict__.update(_acquire=acquire, _release=release)'
    if lines.count(anchor)!=1: raise ValueError('binding_assignment_not_unique')
    i=lines.index(anchor)
    items['binding-assignment']={'evidence_class':'source','citation':items['acquire-binding']['citation'],
        'start_line':i+1,'end_line':i+2,'text':'\n'.join(lines[i:i+2])}
    items['candidate']={'evidence_class':'generated-source','artifact':gref,
        'selector':['candidate_sources',PROFILE],'candidate_sha256':hashlib.sha256(candidate.encode()).hexdigest(),
        **excerpt(candidate,'graph_query')}
    scenario=next(s for s in gate['model']['scenarios'] if s['id']=='success')
    rows=[r for r in gate['rows'] if r['profile_id']==PROFILE and r['scenario_id']=='success']
    if len(rows)!=1: raise ValueError('scenario_row_not_unique')
    items['scenario']={'evidence_class':'executed-assertions','artifact':gref,'profile':PROFILE,
        'scenario':scenario,'model_digest':gate['model_digest'],'row':rows[0],
        'scope':gate['scope'],'not_established':gate['not_established']}
    observations=[r for r in host['rows'] if r['phase']!='warmup' and r['status']==200]
    if not observations: raise ValueError('runtime_observation_missing')
    row=observations[0]
    spans=[s for s in host['spans'] if s['context']['trace_id'].removeprefix('0x')==row['trace_id']]
    roots=[s for s in spans if s['name']=='ats.graph.request']
    if len(roots)!=1: raise ValueError('runtime_trace_missing_or_ambiguous')
    root=roots[0]
    if root['attributes'].get('profile')!=PROFILE or any(root['attributes'].get('source.'+key+'.revision')!=pin['revision'] for key,pin in pins.items()):
        raise ValueError('runtime_root_identity_mismatch')
    if not {'acquire','release'} <= {s['name'] for s in spans}: raise ValueError('runtime_stage_missing')
    items['runtime']={'evidence_class':'observed-runtime','artifact':href,'profile':PROFILE,
        'candidate_sha256':host['candidate_sha256'],'row':row,'spans':spans,
        'environment':host['environment'],'scope':'one selected successful request; controlled identity/policy, real SQL; not all paths',
        'selector':{'phase':row['phase'],'trace_id':row['trace_id']}}
    items['collector']={'evidence_class':'delivery-audit','artifact':aref,'receipt':audit,
        'scope':'retained audit matched to host payload; collector log not reprocessed by this packet'}
    return items


def packet(config, slice_id, mode='plan', evidence_ids=None, budget=12000, expected_packet_identity=None, *, _internal=False):
    from kp_agent_tooling_ops._impl.verification_adjacency import SLICES, links, role, assemble_adjacent
    from kp_agent_tooling_ops._impl.journey_registry import load_registry, assemble_registered, links_registered
    registry=load_registry(config)
    generic=registry is not None and slice_id in registry['slices']
    if slice_id not in SLICES and not generic: raise ValueError('unknown_verification_slice')
    if type(budget)!=int or not 4000<=budget<=100000:
        return {'status':'invalid_budget','reason':'Budget is serialized bytes, not tokens or thousands. Use 4000..100000 or omit for 12000.',
                'evidence':[], 'next_call':{'tool':'verification.packet','arguments':{'slice_id':slice_id,'mode':'plan','budget':12000}}}
    if mode not in {'plan','read'}: raise ValueError('invalid_mode')
    # Public multi-item reads cannot defeat the transport ceiling with a large budget.
    # Only a deliberately selected single indivisible item may use a larger bound.
    requested_budget = budget
    if mode == 'read' and not _internal and (evidence_ids is None or len(evidence_ids) != 1):
        budget = min(budget, 12000)
    base={'schema_version':'ops.verification-slice.v1','slice_id':slice_id,
          'semantic_verdict':'requires_evidence_review','memory_promotion':'not-performed',
          'budget':{'ceiling':budget,'requested':requested_budget,'unit':'serialized ASCII JSON bytes; not model tokens'}}
    if generic:
        specification=registry['slices'][slice_id]
        base['registry_identity']=registry['sha256']
        base['source_scope']=registry['data']['sources']
        base['associations']={'feature_ids':specification.get('feature_ids',[]),
                              'spec_ids':specification.get('spec_ids',[])}
    try:
        items=(assemble_registered(config,registry,slice_id) if generic else
               assemble(config) if slice_id==SLICE else assemble_adjacent(config,slice_id))
    except (ValueError,KeyError,StopIteration,TypeError,OSError,SyntaxError,subprocess.SubprocessError) as error:
        return dict(base,status='unverified',gap={'reason':str(error)[:160]},evidence=[])
    base['packet_identity']=digest({'registry':registry['sha256'],'slice':specification,'items':items}) if generic else digest(items)
    if expected_packet_identity is not None and expected_packet_identity!=base['packet_identity']:
        return dict(base,status='unverified',gap={'reason':'packet_identity_changed'},evidence=[])
    def call(ids, amount=budget):
        return {'tool':'verification.packet','arguments':{'slice_id':slice_id,'mode':'read',
            'evidence_ids':ids,'budget':amount,'expected_packet_identity':base['packet_identity']}}
    def response(ids):
        return dict(base,status='ready',join_status='identity_checks_passed',
            evidence={key:items[key] for key in ids},references={key:{'kind':'packet','slice_id':slice_id,'id':key,'packet_identity':base['packet_identity']} for key in ids},omitted=[key for key in items if key not in ids])
    def groups_for(ids):
        # Whole artifacts only; metadata never stands in for a body.
        groups=[]; current=[]; oversized=[]
        for key in ids:
            if len(encoded(response([key])))>budget:
                if current: groups.append(current);current=[]
                minimum=len(encoded(response([key])))+32  # budget-digit growth margin
                oversized.append({'id':key,'minimum_budget':minimum,
                    'next_call':call([key],minimum) if minimum<=100000 else None})
            elif len(encoded(response(current+[key])))<=budget: current.append(key)
            else: groups.append(current);current=[key]
        if current: groups.append(current)
        return groups, oversized
    registered_roles={entry['id']:entry.get('role','supporting_evidence') for entry in specification['evidence']} if generic else {}
    catalog=[{'id':key,'evidence_class':item['evidence_class'],'bytes':len(encoded(item)),
              **({'role':registered_roles[key]} if generic else role(key,item))} for key,item in items.items()]
    if mode=='plan':
        plan=dict(base,status='ready',join_status='identity_checks_passed',catalog=catalog,
            adjacency=links_registered(registry,slice_id,budget) if generic else links(slice_id,base['packet_identity'],budget),
            navigation_semantics='Question distance is not execution depth.')
        if budget <= 12000:
            groups, oversized = groups_for(list(items))
            plan['read_groups'] = [call(group) for group in groups]
            plan['oversized'] = oversized
            plan['selection_guidance'] = ('Select only groups needed for the claim; read separately. Follow registered adjacent obligations.'
                if generic else 'Select only groups needed for the claim; read separately. For optimization questions follow the performance adjacency, not just the lifecycle trace.')
        required=len(encoded(plan))
        if required>budget:
            return dict(base,status='split_required',requested_bytes=required,reason='Planning metadata exceeds budget; no catalog truncated.',next_calls=[{'tool':'verification.packet','arguments':{'slice_id':slice_id,'mode':'plan','budget':required+32,'expected_packet_identity':base['packet_identity']}}])
        return plan
    ids=list(items) if evidence_ids is None else evidence_ids
    if not ids or len(ids)!=len(set(ids)) or any(key not in items for key in ids):
        invalid=dict(base,status='invalid_selection',reason='Use packet catalog IDs, not lifecycle.evidence IDs.',
            catalog=catalog,evidence=[],next_call={'tool':'verification.packet','arguments':{'slice_id':slice_id,'mode':'plan','budget':budget}})
        if len(encoded(invalid))>budget: invalid.pop('catalog')
        return invalid
    result=response(ids)
    required=len(encoded(result))
    if required<=budget: return result
    groups, oversized = groups_for(ids)
    split=dict(base,status='split_required',requested_bytes=required,
        reason='Follow whole-evidence continuations; oversized items require an explicit larger budget.',
        next_calls=[call(group) for group in groups],oversized=oversized,
        omitted=[],evidence_returned=False)
    if len(encoded(split))>budget:
        return dict(base,status='split_required',requested_bytes=required,
            reason='Selection metadata exceeds budget; plan at a larger budget, then select fewer IDs.',
            next_calls=[{'tool':'verification.packet','arguments':{'slice_id':slice_id,
                'mode':'plan','budget':12000,'expected_packet_identity':base['packet_identity']}}],
            evidence_returned=False)
    return split
