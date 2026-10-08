"""Bounded public views of committed inventory and durable search cursors."""
from pathlib import Path


def _error(reason, message, args):
    return {'status': 'error', 'reason': reason, 'message': message,
            'repo_key': args.get('repo_key'), 'source_revision': args.get('target_revision'),
            'search_complete': False, 'absence_verdict': 'not-established'}


def call(config, operation, args):
    if operation == 'navigation.search_page':
        from kp_agent_tooling._impl.navigation_search_pages import search_pages, SearchPageError
        registry = config.get('navigation_registry_path')
        if not isinstance(registry, str) or not Path(registry).is_absolute():
            return _error('navigation_registry_unconfigured',
                          'Configure navigation_registry_path as an existing absolute persistent directory.', args)
        try:
            result = search_pages(config, registry, **args)
            if result.get('status') == 'ok' and not result.get('search_complete'):
                result['status'] = 'incomplete'
                result['absence_verdict'] = 'not-established'
                result['message'] = ('Search incomplete: more pages remain.' if not result.get('traversal_complete')
                                     else 'Search incomplete: excluded or failed files remain outside coverage.')
                token = result.get('continuation_token')
                result['next_call'] = ({'name': operation, 'arguments': dict(args, continuation_token=token)}
                                       if token else None)
                result['recovery'] = ('Follow next_call with unchanged search arguments before assessing absence.'
                                      if token else 'Inspect coverage, exclusions and failures; narrow or use another supported source reader.')
            return result
        except SearchPageError as error:
            return _error(error.reason, str(error), args)
        except (OSError, ValueError, RuntimeError):
            return _error('navigation_source_unavailable',
                          'The configured source or search registry could not be verified; no coverage established.', args)

    from kp_agent_tooling._impl.repository_manifest import build_manifest
    try:
        manifest = build_manifest(config, args['repo_key'], args['target_revision'])
    except (OSError, ValueError, RuntimeError):
        return _error('navigation_source_unavailable',
                      'The configured repository and exact commit could not be inventoried.', args)
    if not manifest.get('complete'):
        return manifest
    expected = args.get('expected_manifest_id')
    offset, limit = args.get('offset', 0), args.get('limit', 100)
    if (offset and not expected) or (expected and expected != manifest['manifest_id']):
        return _error('manifest_mismatch', 'Use the manifest identity returned by the first page.', args)
    entries = manifest['entries']
    if offset > len(entries):
        return _error('invalid_offset', 'Manifest offset exceeds the inventory size.', args)
    end = min(offset + limit, len(entries))
    next_call = None
    if end < len(entries):
        next_call = {'name': operation, 'arguments': {
            'repo_key': args['repo_key'], 'target_revision': args['target_revision'],
            'offset': end, 'limit': limit, 'expected_manifest_id': manifest['manifest_id']}}
    return dict(manifest, entries=entries[offset:end], offset=offset, returned=end-offset,
                remaining_entries=len(entries)-end, page_complete=end==len(entries),
                next_call=next_call, working_tree='excluded',
                semantic_coverage='not-assessed', runtime_coverage='not-assessed')
