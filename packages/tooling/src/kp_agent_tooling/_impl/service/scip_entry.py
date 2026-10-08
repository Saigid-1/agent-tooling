"""SCIP navigation inside the existing platform tenant/source boundary."""
import json
from pathlib import Path
from kp_agent_tooling._impl.scip_navigation import validate_index, lookup, definitions, load_partitioned_index


def symbol_report(config, anchor, revision, path, line, *, repo_key=None):
    repo_key = repo_key or anchor
    result={'schema_version':'ops.knowledge.v1','operation':'symbol','repo_key':repo_key,
            'source_revision':revision,'status':'partial','data':{'results':[],'gaps':[], 'platform_key':anchor,
            'platform_key_role':'configured_platform_membership', 'requested_repo_key':repo_key},
            'omitted':0,'limitations':['SCIP symbols are static source candidates, not executed calls.',
                'Only explicitly configured indexed platform members are searched; no result is not absence proof.']}
    manifest=config['platforms'][anchor]
    if manifest['sources'][repo_key]['revision'] != revision:
        result['data']['gaps'].append('target_not_in_platform_snapshot')
        return result
    catalogs=[]
    for key,member in manifest['sources'].items():
        for spec in config.get('scip_indexes',{}).get(key,[]):
            try:
                if spec['revision'] != member['revision']:
                    raise ValueError('index revision differs from platform')
                envelope=load_partitioned_index(spec['path'], expected_sha256=spec['sha256'], hydrate=False)
                data=validate_index(envelope,member['revision'])
                if data['repo_key']!=key:
                    raise ValueError('index repository mismatch')
                catalogs.append({'repo':config['repositories'][key]['path'],
                    'revision':member['revision'],'index':envelope})
            except (ValueError,KeyError,OSError):
                result['data']['gaps'].append({'repo_key':key,'reason':'index_unavailable_or_mismatched'})
    covered = any(item['index']['data']['repo_key'] == repo_key and path in item['index']['data']['blobs'] for item in catalogs)
    result['data']['indexed_scope']=[]
    import hashlib as _hashlib, json as _json
    for item in sorted(catalogs,key=lambda row:row['index']['data']['repo_key']):
        paths=sorted(item['index']['data']['blobs'])
        # Identity and size of the scope, not the scope itself: the full path list made a
        # one-line resolution a 26 KB answer. `paths_sha256` lets two answers be compared.
        result['data']['indexed_scope'].append({'repo_key':item['index']['data']['repo_key'],
            'revision':item['revision'],'indexed_paths':len(paths),
            'paths_sha256':_hashlib.sha256(_json.dumps(paths).encode()).hexdigest(),
            'covers_requested_path': item['index']['data']['repo_key']==repo_key and path in item['index']['data']['blobs'],
            'top_level_dirs':sorted({p.split('/',1)[0] for p in paths})[:40]})
    if not covered:
        result['data']['gaps'].append('path_not_indexed')
    for item in catalogs:
        data=item['index']['data']
        if data['repo_key']!=repo_key or path not in data['blobs']:
            continue
        try:
            located=lookup(item['index'],revision=revision,repo=item['repo'],path=path,line=line,limit=20)
            result['omitted']+=located['omitted']
            for occurrence in located['occurrences']:
                target={'status':'local_symbol','definitions':[]} if occurrence['local'] else definitions(occurrence['symbol'],catalogs,limit=5)
                for definition in target['definitions']:
                    definition['next_call'] = {'tool':'knowledge.symbol', 'arguments':{
                        'repo_key':definition['repo_key'], 'target_revision':definition['revision'],
                        'path':definition['path'], 'line':definition['line']}}
                result['data']['results'].append({'occurrence':occurrence,'resolution':target})
        except (ValueError,KeyError,OSError):
            result['data']['gaps'].append('source_coordinate_unverified')
    result['data']['available']=len(result['data']['results'])
    result['data']['absence_verdict']='not-established'
    result['status']='partial' if result['data']['gaps'] else 'ok' if result['data']['results'] else 'no_results'
    return result
