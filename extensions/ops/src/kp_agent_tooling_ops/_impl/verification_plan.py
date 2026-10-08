"""Small question-to-evidence plan; metadata never asserts that evidence was read."""
import json
from kp_agent_tooling._impl import leaf
from kp_agent_tooling_ops._impl.verification_packet import packet
from kp_agent_tooling_ops._impl.evidence_references import packet_reference, resolve_reference
from kp_agent_tooling_ops._impl.journey_registry import load_registry

QUESTIONS = {
    'baseline': {'ats-pool-lifecycle':['entry','entry-import','tenant','connect']},
    'lifecycle': {'ats-pool-lifecycle':['transformation','acquire-binding','release-binding',
                                     'binding-assignment','candidate','scenario','runtime']},
    'performance': {'ats-worker-performance':['comparison']},
    'deployment': {'ats-pool-lifecycle':['candidate','runtime']},
}


def plan(config, questions, supplied=None, budget=12000):
    registry=load_registry(config)
    registered=dict(QUESTIONS)
    if registry:
        for qid, definition in registry['data']['questions'].items():
            mapping={}
            for requirement in definition['requires']:
                mapping.setdefault(requirement['slice_id'],[])
                mapping[requirement['slice_id']]+=requirement['evidence_ids']
            registered[qid]=mapping
    if not questions or len(set(questions))!=len(questions) or any(q not in registered for q in questions):
        raise ValueError('select unique registered questions')
    if not 4000<=budget<=12000:raise ValueError('plan budget must be 4000..12000')
    supplied=supplied or []
    if len(supplied)>60:raise ValueError('too many supplied references')
    provided=set(); invalid=[]
    key=lambda ref:leaf.canonical_json(ref, ascii=True, allow_nan=True)
    for ref in supplied:
        try:resolve_reference(config,ref);provided.add(key(ref))
        except (ValueError,OSError,KeyError,TypeError) as error:
            invalid.append({'reference':ref,'reason':str(error)[:100]})
    desired={}
    for question in questions:
        for sid,ids in registered[question].items():
            desired.setdefault(sid,[])
            desired[sid]=list(dict.fromkeys(desired[sid]+ids))
    rows=[];calls=[];remaining=[]
    for sid,ids in desired.items():
        metadata=packet(config,sid,mode='plan',budget=12000)
        if metadata['status']!='ready':
            rows.append({'slice_id':sid,'status':'unavailable','detail':metadata});continue
        refs=[packet_reference(sid,id,metadata['packet_identity']) for id in ids]
        unread=[ref for ref in refs if key(ref) not in provided]
        remaining.extend(unread)
        row={'slice_id':sid,'packet_identity':metadata['packet_identity'],
             'required_references':refs,'already_supplied':[ref for ref in refs if key(ref) in provided],
             'unread':unread}
        if unread:
            arguments={'slice_id':sid,'mode':'read','evidence_ids':[ref['id'] for ref in unread],
                       'budget':budget,'expected_packet_identity':metadata['packet_identity']}
            sizing=packet(config,**arguments)
            if sizing['status']=='ready':calls.append({'tool':'verification.packet','arguments':arguments})
            elif sizing['status']=='split_required':
                calls.extend(sizing['next_calls'])
                for item in sizing.get('oversized',[]):
                    if item['next_call']:calls.append(item['next_call'])
                    else:row.setdefault('unavailable',[]).append(item['id'])
            else:row['delivery_gap']=sizing['status']
        rows.append(row)
    result={'schema_version':'ops.verification-plan.v1','status':'ready',
        'sources':{k:v['revision'] for k,v in config['repos'].items()},
        'identity_scope':'Configured source pins only; build/deployment not assessed',
        'questions':questions,'coverage':rows,'invalid_supplied':invalid,'next_calls':calls,
        'evidence_reads_planned':len(calls),'recovery_reserve':2,
        'remaining_references':remaining,
        'sufficiency':'Registered obligations only; not proof of semantic completeness or agent receipt',
        'reuse':'Supplied references are identity-checked; caller claims prior complete delivery. No inferred equivalence between different providers.',
        'deployment_gaps':['Target deployment identity, cancellation and multiworker behavior require separate scoped evidence'] if 'deployment' in questions else [],
        'guidance':'Choose only questions relevant to the request. Read next_calls separately. Stop on adequate evidence; keep recovery capacity instead of spending all calls on orientation.'}
    if registry and any(question in registry['data']['questions'] for question in questions):
        result['registry_identity']=registry['sha256']
        result['registry_product']=registry['data']['product']
    if len(json.dumps(result,ensure_ascii=True).encode())>budget:
        return {'status':'split_required','reason':'Plan metadata exceeds bound; request fewer questions',
                'next_calls':[{'tool':'verification.plan','arguments':{'questions':[q],'budget':budget}} for q in questions]}
    return result
