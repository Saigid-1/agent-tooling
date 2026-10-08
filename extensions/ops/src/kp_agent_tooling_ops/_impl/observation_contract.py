"""Mode-specific registry inputs and bounded, non-invented recovery instructions."""
import re

HASH={'type':'string','pattern':'^[0-9a-f]{64}$'}
SCOPE={'type':'object','properties':{
    'sources':{'type':'object','minProperties':1,'additionalProperties':{'type':'string','pattern':'^([0-9a-f]{40}|[0-9a-f]{64})$'}},
    'binding_sha256':HASH},'required':['sources','binding_sha256'],'additionalProperties':False}


def input_schema():
    props={'mode':{'type':'string','enum':['list','read','assess']},'subject':{'type':'string'},
        'id':{**HASH,'description':'One observation ID; required for read.'},
        'ids':{'type':'array','items':HASH,'maxItems':30,'uniqueItems':True},
        'expected_scope':{**SCOPE,'description':'Exact sources and binding identity to assess; not a subject filter.'},
        'budget':{'type':'integer','minimum':4000,'maximum':100000,
                  'description':'Serialized response byte ceiling, 4000..100000. For read, use original_read_bytes from list metadata or the split_required next_call.'},
        'after':HASH,'limit':{'type':'integer','minimum':1,'maximum':100}}
    return {'type':'object','properties':props,'required':['mode'],'additionalProperties':False,
        'description':'Modes: list(subject?,after?,limit?,budget?); read(id,budget?); assess(ids,expected_scope). Server checks mode-specific requirements.'}


def mode_errors(args):
    """Additional mode-specific JSON Schema errors after the host-friendly schema."""
    from jsonschema import Draft202012Validator
    modes={'list':(['mode'],['mode','subject','after','limit','budget']),
           'read':(['mode','id'],['mode','id','budget']),
           'assess':(['mode','ids','expected_scope'],['mode','ids','expected_scope'])}
    mode=args.get('mode') if isinstance(args,dict) else None
    if mode not in modes:return []
    required,allowed=modes[mode]
    schema={'type':'object','required':required,'properties':{k:True for k in allowed},
            'additionalProperties':False}
    return list(Draft202012Validator(schema).iter_errors(args))


def argument_error(args,errors):
    calls=[{'tool':'verification.observations','arguments':{'mode':'list'}}]
    if args.get('mode')=='read' and isinstance(args.get('ids'),list):
        ids=args['ids']
        if 0<len(ids)<=30 and all(isinstance(x,str) and re.fullmatch('[0-9a-f]{64}',x) for x in ids):
            budget=args.get('budget',20000)
            if type(budget)!=int or not 4000<=budget<=100000:budget=20000
            calls=[{'tool':'verification.observations','arguments':{'mode':'read','id':x,'budget':budget}}
                   for x in dict.fromkeys(ids)]
    return {'status':'invalid_arguments','errors':[{'path':'/'+ '/'.join(map(str,e.absolute_path)),
        'code':e.validator} for e in errors[:20]],'next_calls':calls,
        'guidance':'list accepts subject/after/limit/budget; read requires one id and optional budget. assess requires ids and expected_scope containing only sources and binding_sha256. Inspect listed scopes; choose the intended boundary, never invent or replace a binding to force compatibility. A successful read does not recover a failed assessment.'}
