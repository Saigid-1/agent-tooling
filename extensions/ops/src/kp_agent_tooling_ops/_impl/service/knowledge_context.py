"""Compose existing knowledge evidence without conflating its revision boundaries."""
from collections import Counter
import json
from pathlib import Path

from kp_agent_tooling_ops._impl.capability_map import compare_references, validate_manifest
from kp_agent_tooling_ops._impl.tool_discovery import GitSource, search

LIMITATIONS = [
    'Working-tree edits are excluded; target commit is not a live deployment claim.',
    'Artifact owner/lifecycle are configured declarations, not independently verified.',
    'Static navigation is source evidence, not runtime traces or complete topology.',
    'Indexed guidance has its own blob identity; ranking does not establish authority.',
    'Behavioral tests, runtime graph trace, corpus completeness and conformance are not assessed.',
]


def _size(report):
    return len((json.dumps(report, ensure_ascii=True) + '\n').encode())


def _bound(report):
    data = report['data']
    enriched = any('code_reference_coverage' in row for row in data.get('guidance', []))
    baseline = None
    if enriched:
        from copy import deepcopy
        baseline = deepcopy(report)
        baseline_data = baseline['data']
        baseline_data.pop('code_reference_context', None)
        for row in baseline_data.get('guidance', []):
            row.pop('code_reference_coverage', None)
            row.pop('code_references', None)
        baseline = _bound(baseline)
        from kp_agent_tooling_ops._impl.code_references.retrieval import trim_code_reference_enrichment
        trim_code_reference_enrichment(report, 32768)
    # Retrieval already bounded its opt-in payload, but the context envelope adds
    # other evidence. Trim only reference enrichment again before any original
    # guidance row can be omitted.
    while _size(report) > 32768:
        changed = False
        for row in reversed(data.get('guidance', [])):
            coverage = row.get('code_reference_coverage', {})
            if coverage.get('diagnostics'):
                coverage['diagnostics'].pop()
                coverage['diagnostics_omitted_count'] = (
                    coverage.get('diagnostics_omitted_count', 0) + 1)
                changed = True
                break
            if row.get('code_references'):
                row['code_references'].pop()
                coverage['omitted_count'] = coverage.get('omitted_count', 0) + 1
                coverage['returned_count'] = len(row['code_references'])
                changed = True
                break
        if not changed:
            break
    groups = [(data.get('discovery', {}), 'results'), (data, 'excluded_evidence'),
              (data, 'guidance'), (data.get('references', {}), 'references'), (data, 'next_steps')]
    for owner, key in groups:
        while _size(report) > 32768 and owner.get(key):
            owner[key].pop()
            report['omitted'] += 1
            if 'output_omitted' not in data['gaps']:
                data['gaps'].append('output_omitted')
    if _size(report) > 32768 and 'report' in data.get('navigation', {}):
        del data['navigation']['report']
        data['navigation']['report_omitted'] = True
        report['omitted'] += 1
        if 'output_omitted' not in data['gaps']:
            data['gaps'].append('output_omitted')
    discovery = data.get('discovery', {})
    if 'available' in discovery:
        discovery['omitted'] = discovery['available'] - len(discovery.get('results', []))
    if 'output_omitted' in data['gaps'] and report['status'] == 'ok':
        report['status'] = 'partial'
    if _size(report) > 32768:
        raise ValueError('context metadata exceeds budget')
    if baseline is not None and len(data.get('guidance', [])) < len(
            baseline['data'].get('guidance', [])):
        baseline['data']['code_reference_context'] = {
            'state': 'omitted', 'reason': 'response_budget_exhausted'}
        if _size(baseline) <= 32768:
            return baseline
        return {'schema_version': report.get('schema_version', 'ops.knowledge.v1'),
                'operation': report.get('operation', 'context'),
                'repo_key': report.get('repo_key'),
                'source_revision': report.get('source_revision'), 'status': 'error',
                'data': {'error': {'code': 'response_budget_exhausted',
                    'message': 'Reference enrichment could not fit while preserving baseline guidance.'}},
                'omitted': report.get('omitted', 0),
                'limitations': list(report.get('limitations', LIMITATIONS))}
    return report


def context_report(service, repo_key, capability_id, target_revision, query,
                   *, reference_options=None, reference_prepared=None):
    repo = service._config['repositories'][repo_key]
    data = {'capability_id': capability_id, 'target_revision': target_revision,
            'catalog_revision': None, 'working_tree': 'excluded',
            'runtime_trace': 'not-assessed', 'behavioral_tests': 'not-run',
            'conformance': 'not-assessed', 'gaps': [], 'guidance': [],
            'excluded_evidence': []}
    report = {'schema_version': 'ops.knowledge.v1', 'operation': 'context',
              'repo_key': repo_key, 'source_revision': None, 'status': 'error',
              'data': data, 'omitted': 0, 'limitations': list(LIMITATIONS)}
    try:
        target = GitSource(Path(repo['path']), target_revision)
        if target.revision != target_revision:
            raise ValueError('target must name the commit itself')
        report['source_revision'] = target.revision
    except Exception:
        data['gaps'].append('target_unavailable')
        return report
    try:
        catalog = GitSource(Path(repo['path']), repo['ref'])
        data['catalog_revision'] = catalog.revision
        entry = catalog.entry(repo['capabilities'][capability_id])
        if entry is None or entry[0] not in {'100644', '100755'}:
            raise ValueError('manifest unavailable')
        manifest = validate_manifest(json.loads(catalog.read_bounded(entry[1], 65536)))
        if manifest['capability_id'] != capability_id:
            raise ValueError('wrong capability manifest')
        baseline = GitSource(Path(repo['path']), manifest['source_revision'])
        if baseline.revision != manifest['source_revision']:
            raise ValueError('baseline must name the commit itself')
        references = compare_references(manifest, baseline, target)
        data['references'] = references
        data['reference_summary'] = dict(Counter(row['status'] for row in references['references']))
    except Exception:
        data['gaps'].append('manifest_unavailable')
        return report
    try:
        discovery = search(target, query)
        data['discovery'] = {'status': 'ok' if discovery['available'] else 'no_results',
                             'results': discovery['results'][:3],
                             'available': discovery['available'],
                             'omitted': max(0, discovery['available'] - 3),
                             'absence_verdict': 'not-established',
                             'source_revision': target.revision,
                             'limitations': discovery['limitations']}
        entrypoints = {row['path'] for row in manifest['references'] if row['role'] == 'entrypoint'}
        for candidate in data['discovery']['results']:
            candidate['classification'] = ('mapped_entrypoint' if candidate['source']['path'] in entrypoints else 'unclassified_operator')
            candidate['runtime_verification'] = 'not-performed'
        report['omitted'] += data['discovery']['omitted']
    except Exception:
        data['discovery'] = {'status': 'unavailable'}
        data['gaps'].append('discovery_unavailable')
    architecture = {r['path'] for r in manifest['references'] if r['role'] == 'architecture'}
    paths = tuple(sorted(p for p in architecture if repo.get('artifacts', {}).get(p, {}).get('status') == 'maintained'))
    try:
        if not paths:
            raise ValueError('no maintained architecture paths')
        default, _ = service._default_branch_source(repo)
        retrieved = service._retrieve_data(repo_key, query, 5, target=target, catalog=catalog,
                                           default=default, paths=paths,
                                           reference_options=reference_options,
                                           reference_prepared=reference_prepared)
        retrieval = {'status': retrieved['status'], 'data': retrieved, 'omitted': 0}
    except Exception:
        retrieval = {'status': 'error', 'data': {}, 'omitted': 0}
    data['retrieval_status'] = retrieval['status']
    data['corpus_revision_status'] = retrieval['data'].get('corpus_revision_status', 'unknown')
    data['configured_catalog_lineage'] = retrieval['data'].get('configured_catalog_lineage', 'unknown')
    data['default_revision'] = retrieval['data'].get('default_revision')
    data['configured_catalog_default_lineage'] = retrieval['data'].get('configured_catalog_default_lineage', 'unknown')
    data['guidance_scope'] = retrieval['data'].get('guidance_scope', 'requested_revision_only')
    data['source_fallbacks'] = retrieval['data'].get('source_fallbacks', [])
    data['guidance_exclusions'] = retrieval['data'].get('excluded_revision_examples', [])
    data['guidance_excluded_count'] = retrieval['data'].get('excluded_revision_count', 0)
    if 'code_reference_context' in retrieval['data']:
        data['code_reference_context'] = retrieval['data']['code_reference_context']
    if retrieval['status'] == 'error':
        data['gaps'].append('retrieval_unavailable')
    else:
        for hit in retrieval['data'].get('results', []):
            try:
                path = hit['path']
                declaration = repo.get('artifacts', {}).get(path, {})
                classification = declaration.get('status', 'unclassified')
                if classification != 'maintained' or path not in architecture:
                    data['excluded_evidence'].append({
                        'path': path, 'blob_sha': hit['blob_sha'],
                        'classification': classification,
                        'reason': 'Not declared maintained architectural guidance for this capability.'})
                    continue
                observed = target.entry(path)
                status = ('missing' if observed is None else 'unsupported'
                          if observed[0] not in {'100644', '100755'} else 'unchanged'
                          if observed[1] == hit['blob_sha'] else 'changed')
                data['guidance'].append({**hit, 'classification': classification,
                                         'owner': declaration['owner'], 'target_status': status,
                                         'target_blob_sha': observed[1] if observed else None})
            except Exception:
                if 'guidance_evidence_unavailable' not in data['gaps']:
                    data['gaps'].append('guidance_evidence_unavailable')
        report['omitted'] += retrieval['omitted']
    if not data['guidance']:
        data['gaps'].append('maintained_guidance_unavailable')
        if retrieval['data'].get('reason') == 'indexed_bytes_not_at_target':
            data['gaps'].append('indexed_guidance_stale_for_target')
            data['next_source_action'] = 'Read maintained guidance at the exact target revision or index those source bytes.'
    symbol = repo.get('entry_symbols', {}).get(capability_id)
    try:
        if service._navigation_provider is None or symbol is None:
            raise ValueError('navigation not configured')
        navigation = service._navigation_provider.inspect(target.repository, target.revision, symbol)
        if navigation.get('status') != 'ok' or navigation.get('source_revision') != target.revision:
            data['navigation'] = {key: navigation[key] for key in ('status', 'reason', 'provider', 'evidence_kind', 'failed_stage', 'published_revision', 'index_gaps') if key in navigation}
            data['navigation']['status'] = 'unavailable'
            if navigation.get('source_revision') != target.revision:
                data['navigation']['reason'] = 'navigation_revision_mismatch'
            data['navigation']['source_revision'] = target.revision
            raise ValueError('navigation unavailable or wrong revision')
        data['navigation'] = navigation
    except Exception:
        data.setdefault('navigation', {'status': 'unavailable', 'reason': ('navigation_provider_failed' if service._navigation_provider is not None and symbol is not None else 'navigation_not_configured'),
                                       'source_revision': target.revision})
        data['gaps'].append('navigation_unavailable')
    data['next_steps'] = [
        {'action': 'review_reference', 'path': row['path'], 'target_revision': target.revision}
        for row in references['references'] if row['status'] != 'unchanged']
    changed = references['status'] != 'current' or any(h['target_status'] != 'unchanged' for h in data['guidance'])
    report['status'] = 'review_required' if changed else 'partial' if data['gaps'] else 'ok'
    return _bound(report)
