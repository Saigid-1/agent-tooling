"""Append-only content-addressed observations. Identity is not authority or truth."""
import json
from pathlib import Path
import re

from kp_agent_tooling._impl import leaf

MAX_BYTES=1_000_000
MAX_DELIVERY_BYTES=100_000


def encoded(value):
    return leaf.canonical_bytes(value, ascii=True, allow_nan=False)


def digest(value):
    return leaf.canonical_sha256(value, ascii=True, allow_nan=False)


def identity(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):
        raise ValueError('invalid observation identity')
    return value


def validate_scope(scope):
    if set(scope)!={'sources','binding_sha256'} or not scope['sources']:
        raise ValueError('explicit sources and binding required')
    identity(scope['binding_sha256'])
    if any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}',v) for v in scope['sources'].values()):
        raise ValueError('full source revisions required')


def make_record(provider,invocation_id,subject,kind,scope,original,assertions,references,limitations):
    validate_scope(scope)
    if kind not in {'source','static','controlled-test','runtime'}:
        raise ValueError('unknown evidence kind')
    if any(not isinstance(s,str) or not s for s in (provider,invocation_id,subject)) or not limitations:
        raise ValueError('provider invocation subject and limitations required')
    for assertion in assertions:
        if set(assertion)!={'predicate','value'} or not isinstance(assertion['predicate'],str) or not assertion['predicate']:
            raise ValueError('explicit predicate/value required')
    for ref in references:identity(ref)
    return {'schema_version':'ops.observation.v1','provider':provider,'invocation_id':invocation_id,
        'subject':subject,'kind':kind,'scope':scope,'original_sha256':digest(original),
        'assertions':assertions,'references':references,'limitations':limitations,
        'authorization':'not-granted','authenticity':'adapter-recorded; unsigned',
        'original':original}


class ObservationStore:
    def __init__(self,root):
        self.root=Path(root)

    def _read(self,group,key):
        identity(key)
        path=self.root/group/(key+'.json')
        if path.is_symlink():raise ValueError('symlink object refused')
        if path.stat().st_size>MAX_BYTES:raise ValueError('object exceeds bound')
        value=json.loads(path.read_bytes())
        if digest(value)!=key:raise ValueError('object integrity mismatch')
        return value

    def _put(self,group,value):
        data=encoded(value)
        if len(data)>MAX_BYTES:raise ValueError('object exceeds bound')
        key=digest(value);folder=self.root/group;folder.mkdir(parents=True,exist_ok=True)
        path=folder/(key+'.json')
        # Atomic publication without overwriting an existing immutable object.
        leaf.publish_once(path, data, temp_prefix='.pending-', temp_dir=folder,
                          on_exists=lambda: self._read(group, key), cleanup='always')
        return key

    def append(self,value):
        # Reconstruct through the public constructor, rejecting invented authority fields.
        rebuilt=make_record(*(value[k] for k in ('provider','invocation_id','subject','kind','scope','original','assertions','references','limitations')))
        if value!=rebuilt:raise ValueError('noncanonical observation')
        for ref in value['references']:
            try:self.read(ref)
            except (OSError,ValueError) as error:raise ValueError('unresolved reference') from error
        raw=self._put('originals',value['original'])
        record={k:v for k,v in value.items() if k!='original'}
        if raw!=record['original_sha256']:raise ValueError('original mismatch')
        return self._put('records',record)

    def read(self,key):
        record=self._read('records',key)
        original=self._read('originals',record['original_sha256'])
        return {'id':key,'record':record,'original':original}

    def read_bounded(self,key,budget=20000):
        if type(budget) is not int or budget>MAX_DELIVERY_BYTES or budget<0:
            raise ValueError('budget must be 0..100000')
        value={'status':'ready','reference':{'kind':'observation','id':key},**self.read(key)}
        size=len(encoded(value))
        if size>budget:
            result={'status':'split_required','id':key,'required_bytes':size,
                'reason':'Indivisible original; explicitly request the required budget or leave unread'}
            if size<=MAX_DELIVERY_BYTES:
                result['next_call']={'tool':'verification.observations',
                    'arguments':{'mode':'read','id':key,'budget':max(4000,size)}}
            else:
                result['reason']='Indivisible original exceeds the maximum read budget; no public continuation is available'
            return result
        return value

    def list(self,subject=None,limit=20,after=None,budget=20000):
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('limit must be 1..100')
        if type(budget) is not int or not 4000<=budget<=MAX_DELIVERY_BYTES:raise ValueError('budget must be 4000..100000')
        if after:identity(after)
        rows=[]
        more=False
        ordering='content identity; not recency or eligibility'
        snapshot='not pinned; restart enumeration if concurrent imports matter'
        for path in sorted((self.root/'records').glob('*.json')):
            if after and path.stem<=after:continue
            record=self._read('records',path.stem)
            if subject is None or record['subject']==subject:
                original=self._read('originals',record['original_sha256'])
                read_size=len(encoded({'status':'ready','reference':{'kind':'observation','id':path.stem},'id':path.stem,'record':record,'original':original}))
                row={'id':path.stem,**{k:record[k] for k in ('provider','subject','kind','scope')},
                    'original_read_bytes':read_size}
                if read_size<=MAX_DELIVERY_BYTES:
                    row['next_call']={'tool':'verification.observations',
                        'arguments':{'mode':'read','id':path.stem,'budget':max(4000,read_size)}}
                else:
                    row['next_call']=None
                    row['read_unavailable_reason']='Original exceeds the maximum public read budget'
                candidate=rows+[row]
                result={'records':candidate,'has_more':True,'next_after':path.stem,
                    'ordering':ordering,'snapshot':snapshot}
                if len(candidate)>limit or len(encoded(result))>budget:
                    if not rows:
                        return {'status':'split_required','required_bytes':len(encoded(result)),
                            'reason':'One metadata row exceeds the list budget; narrow the subject or explicitly increase budget'}
                    more=True
                    break
                rows=candidate
        return {'records':rows,'has_more':more,
            'next_after':rows[-1]['id'] if more else None,
            'ordering':ordering,'snapshot':snapshot}

    def assess(self,ids,expected_scope):
        validate_scope(expected_scope)
        if len(ids)>30 or len(set(ids))!=len(ids):raise ValueError('bounded unique selection required')
        records=[self.read(key)['record'] for key in ids]
        incompatible=[key for key,r in zip(ids,records) if r['scope']!=expected_scope]
        claims={}
        for key,r in zip(ids,records):
            if key in incompatible:continue
            for a in r['assertions']:
                claims.setdefault((r['subject'],a['predicate']),{}).setdefault(encoded(a['value']).decode(),[]).append(key)
        conflicts=[{'subject':s,'predicate':p,'alternatives':v} for (s,p),v in claims.items() if len(v)>1]
        return {'status':'incompatible' if incompatible else 'conflicting' if conflicts else 'compatible' if records else 'unknown',
            'incompatible':incompatible,'conflicts':conflicts,
            'execution':'recorded-runtime-evidence' if not incompatible and any(r['kind']=='runtime' for r in records) else 'unknown',
            'authorization':'not-assessed','semantic_verdict':'not-assessed',
            'limits':'Exact predicate conflicts only; compatibility does not prove causal path, completeness or truth.'}
