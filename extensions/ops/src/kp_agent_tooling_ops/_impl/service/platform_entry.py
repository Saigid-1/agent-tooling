"""Read-only platform entry using operator-configured sources, never caller paths."""
from pathlib import Path

from kp_agent_tooling._impl.platform_snapshot import inspect_platform


def platform_report(config, anchor, *, requested_repo=None, installed_provider=None):
    requested_repo = requested_repo or anchor
    manifest = config['platforms'][anchor]
    checkouts = {key: config['repositories'][key]['path'] for key in manifest['sources']}
    report = inspect_platform(manifest, checkouts, installed_provider=installed_provider)
    # Host paths are operator configuration, not agent-facing source identity.
    for observation in report['checkouts'].values():
        observation.pop('path', None)
    nav = config.get('navigation')
    navigation = {'status': 'not-configured', 'execution': 'not-assessed'}
    if nav:
        paths = [nav['command'], nav['python']] if nav.get('provider') == 'serena' else [nav['tool_root']]
        navigation = {'status': 'paths-present' if all(Path(p).exists() for p in paths) else 'paths-unavailable',
                      'execution': 'not-assessed'}
    from kp_agent_tooling_ops._impl.service.knowledge_coverage import platform_coverage
    report['tool_coverage'] = platform_coverage(config, anchor)
    report['platform_anchor'] = anchor
    report['requested_repo_key'] = requested_repo
    report['source_revisions'] = {key: row['revision'] for key, row in report['snapshot']['sources'].items()}
    report['diagnostics'] = {
        'navigation': navigation,
        'source_access': {'status': 'partial' if report['gaps'] else 'read-verified'},
        'credentials': {'status': 'not-assessed'},
        'runtime': {'status': 'not-assessed'},
        'retrieval': {'status': 'not-assessed'},
    }
    return {'schema_version': 'ops.knowledge.v1', 'operation': 'platform',
            'repo_key': requested_repo, 'source_revision': report['source_revisions'].get(requested_repo),
            'status': report['status'], 'data': report, 'omitted': 0,
            'limitations': report['limitations'] + [
                'Provider paths being present does not verify provider startup or volume mount identity.',
                'Transport authorization is separate from downstream credential and runtime readiness.']}
