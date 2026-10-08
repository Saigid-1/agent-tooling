"""OPS authored behavioral models, typed graph projection and evidence joins.

A compiler fact never manufactures a requirement or a successful test result.
Projection is for caller-owned isolated graphs; no public admission is added.
"""
from __future__ import annotations

from kp_agent_tooling._impl import leaf

KINDS={'specs':'BehaviorSpec','contracts':'BehaviorContract','scenarios':'BehaviorScenario',
       'implementations':'BehaviorImplementation','profiles':'BehaviorProfile'}

def digest(value):
    return leaf.canonical_sha256(value, ascii=True, allow_nan=False)

def validate(model):
    if model.get('schema_version')!='ops.behavior-model.v1' or not model.get('owner'):
        raise ValueError('owned behavioral model required')
    groups={}
    for group in ['features',*KINDS]:
        rows=model.get(group)
        if not isinstance(rows,list) or not rows:raise ValueError('nonempty '+group+' required')
        if any(not isinstance(r,dict) or not isinstance(r.get('id'),str) or not r['id'] for r in rows):raise ValueError('record ID required')
        groups[group]={r['id']:r for r in rows}
        if len(groups[group])!=len(rows):raise ValueError('duplicate '+group+' IDs')
    for src,field,dst in [('features','spec_ids','specs'),('specs','contract_ids','contracts'),('contracts','scenario_ids','scenarios'),('profiles','implementation_ids','implementations')]:
        for row in groups[src].values():
            refs=row.get(field)
            if not isinstance(refs,list) or not refs or len(set(refs))!=len(refs) or any(x not in groups[dst] for x in refs):raise ValueError('invalid '+field)
    for scenario in groups['scenarios'].values():
        if any(not isinstance(scenario.get(k),str) or not scenario[k] for k in ['given','event','then','assertion_id']):raise ValueError('executable scenario requires explicit expectations')
    for contract in groups['contracts'].values():
        if not contract.get('operation_key') or not contract.get('invariants'):raise ValueError('contract identity and invariants required')
    for impl in groups['implementations'].values():
        contract=groups['contracts'].get(impl.get('contract_id'))
        if contract is None or not impl.get('symbol'):raise ValueError('invalid implementation binding')
        if not isinstance(impl.get('scenario_ids'),list) or not set(impl['scenario_ids'])<=set(contract['scenario_ids']):raise ValueError('invalid scenario binding')
    for profile in groups['profiles'].values():
        if not isinstance(profile.get('guard'),dict) or not profile['guard']:raise ValueError('explicit profile guard required')
        contracts=[groups['implementations'][i]['contract_id'] for i in profile['implementation_ids']]
        if len(contracts)!=len(set(contracts)):raise ValueError('ambiguous implementation selection')
    return groups
