"""Bounded recovery of resolved references from one immutable serving view."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath

from .extraction import EXTRACTION_POLICY_VERSION
from .manifest import FrozenReferenceRetrieval
from .resolution import RESOLUTION_POLICY_VERSION
from .retrieval import _TreeMetadata, _chunk_id, _positive


def read_references(args, *, repositories, store, graph=None, navigation=None,
                    lifecycle=None):
    """Return a stable page of exact-generation resolved reference projections."""
    required = {'repo_key', 'path', 'blob_sha', 'byte_offset', 'byte_length',
                'generation_digest', 'code_revisions'}
    if (not isinstance(args, Mapping) or not required <= args.keys()
            or args.keys() - required - {'offset', 'limit'}):
        raise ValueError('exact document coordinate, target revisions and generation required')
    args = dict(args)
    if (not isinstance(args['repo_key'], str)
            or not isinstance(args['generation_digest'], str)
            or not args['generation_digest'] or len(args['generation_digest']) > 128):
        raise ValueError('repository key and generation identity required')
    repo = repositories.get(args['repo_key'])
    path = args['path']
    if (repo is None or not isinstance(path, str) or PurePosixPath(path).is_absolute()
            or '..' in PurePosixPath(path).parts or '\x00' in path or len(path) > 512
            or path not in repo['artifacts']
            or repo['artifacts'][path]['status'] != 'maintained'):
        raise ValueError('maintained document required')
    if (not isinstance(args['blob_sha'], str)
            or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', args['blob_sha'])):
        raise ValueError('exact document blob required')
    offset, limit = args.get('offset', 0), args.get('limit', 10)
    if (type(offset) is not int or offset < 0 or type(limit) is not int
            or not 1 <= limit <= 20 or type(args['byte_offset']) is not int
            or args['byte_offset'] < 0 or type(args['byte_length']) is not int
            or args['byte_length'] < 1):
        raise ValueError('invalid coordinate or page bound')
    revisions = args['code_revisions']
    if (not isinstance(revisions, dict) or not 1 <= len(revisions) <= 8
            or any(key not in repositories or not isinstance(value, str)
                   or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value)
                   for key, value in revisions.items())):
        raise ValueError('explicit registered target revisions required')

    result = {'schema_version': 'ops.reference-recovery.v1', 'status': 'unavailable',
              'source': {key: args[key] for key in
                         ('repo_key', 'path', 'blob_sha', 'byte_offset', 'byte_length')},
              'generation_digest': args['generation_digest'], 'references': [],
              'next_call': None}
    if lifecycle is not None and not lifecycle.permits(
            repo['corpus_scope'], path, args['blob_sha']):
        return dict(result, reason='document_ineligible')
    if not isinstance(store, FrozenReferenceRetrieval):
        return dict(result, reason='backend_not_configured')
    if args['generation_digest'] != store.generation_metadata['generation_digest']:
        return dict(result, reason='generation_mismatch', action_required='repeat_parent_read')

    chunk = _chunk_id(dict(result['source'], repo_key=repo['corpus_scope']))
    rows = []
    tree = _TreeMetadata(repositories, navigation, budget=.75)
    for target, revision in sorted(revisions.items()):
        shard = store.select(
            source_repo_key=args['repo_key'], document_path=path,
            document_blob_sha=args['blob_sha'], chunk_content_id=chunk,
            target_repo_key=target, target_revision=revision,
            extraction_policy_version=EXTRACTION_POLICY_VERSION,
            resolution_policy_version=RESOLUTION_POLICY_VERSION)
        if shard is None:
            return dict(result, reason='manifest_not_found')
        if (getattr(shard, 'published', True) is not True
                or getattr(shard, 'integrity_checked', True) is not True):
            return dict(result, reason='manifest_unpublished_or_unchecked')
        try:
            view = store.retrieval_view(shard, graph)
            if any('_target_symbol' not in edge or '_target_change' not in edge
                   for edge in view.get('positive_edges', ())):
                raise ValueError('positive edge lacks frozen endpoint metadata')
        except Exception:
            return dict(result, reason='manifest_graph_integrity_error')
        edges = {}
        for edge in view['positive_edges']:
            attrs = edge.get('attributes', edge)
            edges.setdefault(attrs.get('resolution_id'), []).append(edge)
        for outcome in view['outcomes']:
            if (outcome.get('status') != 'resolved'
                    or outcome.get('execution_state') != 'complete'):
                continue
            for edge in edges.get(outcome.get('resolution_id'), ()):
                rows.append(_positive(
                    edge, outcome, repo_key=target, requested_revision=revision,
                    selected_revision=revision, exact=True, repositories=repositories,
                    graph=graph, navigation=navigation, tree=tree, telemetry=None))
    rows.sort(key=lambda item: (item['ref_byte_offset'], item['target']['repo_key'],
                                item['target'].get('path') or '', item['resolution_id']))
    if offset > len(rows):
        raise ValueError('offset beyond references')
    result.update(status='ok', total=len(rows), offset=offset,
                  references=rows[offset:offset + limit])
    while True:
        count = len(result['references'])
        following = offset + count
        result.update(returned=count, complete=following == len(rows),
                      next_call=None if following == len(rows) else {
                          'name': 'knowledge.reference_recovery',
                          'arguments': dict(args, offset=following, limit=limit)})
        if len(json.dumps(result, ensure_ascii=True).encode()) <= 16000:
            return result
        if not result['references']:
            raise ValueError('reference metadata exceeds page budget')
        result['references'].pop()
