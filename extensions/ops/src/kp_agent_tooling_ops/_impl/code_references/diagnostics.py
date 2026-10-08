"""Bounded recovery of text-free diagnostics from one immutable serving view."""
import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from .manifest import FrozenReferenceRetrieval
from .retrieval import _chunk_id
from .extraction import EXTRACTION_POLICY_VERSION
from .resolution import RESOLUTION_POLICY_VERSION


def read_diagnostics(args, *, repositories, store, lifecycle=None):
    required = {'repo_key','path','blob_sha','byte_offset','byte_length','generation_digest','code_revisions'}
    if not isinstance(args, Mapping) or not required <= args.keys() or args.keys() - required - {'offset','limit'}:
        raise ValueError('exact document coordinate, target revisions and generation required')
    args = dict(args)
    if not isinstance(args['repo_key'],str) or not isinstance(args['generation_digest'],str) or len(args['generation_digest'])>128:
        raise ValueError('repository key and generation identity required')
    repo = repositories.get(args['repo_key'])
    path = args['path']
    if (repo is None or not isinstance(path,str) or PurePosixPath(path).is_absolute()
            or '..' in PurePosixPath(path).parts or '\x00' in path
            or len(path)>512 or path not in repo['artifacts']
            or repo['artifacts'][path]['status'] != 'maintained'):
        raise ValueError('maintained document required')
    if not isinstance(args['blob_sha'],str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',args['blob_sha']):
        raise ValueError('exact document blob required')
    offset, limit = args.get('offset',0), args.get('limit',10)
    if (type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=20
            or type(args['byte_offset']) is not int or args['byte_offset']<0
            or type(args['byte_length']) is not int or args['byte_length']<1):
        raise ValueError('invalid coordinate or page bound')
    revisions=args['code_revisions']
    if (not isinstance(revisions,dict) or not 1<=len(revisions)<=8 or
            any(k not in repositories or not isinstance(v,str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',v)
                for k,v in revisions.items())):
        raise ValueError('explicit registered target revisions required')
    result={'schema_version':'ops.reference-diagnostics.v1','status':'unavailable',
        'source':{k:args[k] for k in ('repo_key','path','blob_sha','byte_offset','byte_length')},
        'generation_digest':args['generation_digest'],'diagnostics':[], 'next_call':None}
    if lifecycle is not None and not lifecycle.permits(repo['corpus_scope'],path,args['blob_sha']):
        return dict(result,reason='document_ineligible')
    if not isinstance(store,FrozenReferenceRetrieval):
        return dict(result,reason='backend_not_configured')
    if args['generation_digest'] != store.generation_metadata['generation_digest']:
        return dict(result,reason='generation_mismatch',action_required='repeat_parent_read')
    chunk = _chunk_id(dict(result['source'],repo_key=repo['corpus_scope']))
    rows=[]
    for target,revision in sorted(revisions.items()):
        shard=store.select(source_repo_key=args['repo_key'],document_path=path,
            document_blob_sha=args['blob_sha'],chunk_content_id=chunk,target_repo_key=target,
            target_revision=revision,extraction_policy_version=EXTRACTION_POLICY_VERSION,
            resolution_policy_version=RESOLUTION_POLICY_VERSION)
        if shard is None:
            return dict(result,reason='manifest_not_found')
        for outcome in store.retrieval_view(shard,None)['outcomes']:
            if outcome.get('status')=='resolved' and outcome.get('execution_state')=='complete':
                continue
            row={k:outcome[k] for k in ('occurrence_id','resolution_id','kind','ref_byte_offset',
                'ref_byte_length','literal_digest','status','reason','match_count','truncated','execution_state') if k in outcome}
            rows.append(dict(row,target_repo_key=target,resolved_code_revision=revision))
    rows.sort(key=lambda x:(x['target_repo_key'],x.get('ref_byte_offset',0),x.get('occurrence_id','')))
    if offset>len(rows):raise ValueError('offset beyond diagnostics')
    result.update(status='ok',total=len(rows),offset=offset,diagnostics=rows[offset:offset+limit])
    while True:
        count=len(result['diagnostics']);following=offset+count
        result.update(returned=count,complete=following==len(rows),
            next_call=None if following==len(rows) else {'name':'knowledge.reference_diagnostics',
                'arguments':dict(args,offset=following,limit=limit)})
        if len(json.dumps(result,ensure_ascii=True).encode())<=16000:return result
        if not result['diagnostics']:raise ValueError('diagnostic metadata exceeds page budget')
        result['diagnostics'].pop()
