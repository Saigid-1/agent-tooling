"""Bounded per-member tool coverage for `knowledge.platform`.

Configuration is not corpus or runtime proof. The only verified claim made here is
that a configured SCIP index loaded, validated at the member's pinned revision and
matched its configured revision, digest and repository key. Read-only: no writes,
no network.
"""
from pathlib import PurePosixPath

from kp_agent_tooling._impl.scip_navigation import load_partitioned_index, validate_index
from kp_agent_tooling_ops._impl.service.knowledge_eligibility import eligible_paths

INDEX_GAP = 'index_unavailable_or_mismatched'
MAX_PATH_SCOPES = 20
# Absent/unreadable files (OSError), digest, revision and identity refusals and bad
# JSON (ValueError), and malformed specs or envelopes (KeyError, TypeError, ...).
_UNPROVEN = (ValueError, KeyError, OSError, TypeError, AttributeError, IndexError, RecursionError)


def _path_scopes(blobs):
    scopes = set()
    for path in blobs:
        parts = PurePosixPath(path).parts
        scopes.add(str(PurePosixPath(*parts[:2])) + ('/' if len(parts) > 2 else ''))
    return sorted(scopes)


def _index_row(spec, repo_key, member_revision):
    """One row per configured spec. `digest-verified` only after every check passed."""
    row = {'revision': spec.get('revision') if isinstance(spec, dict) else None, 'status': 'unverified'}
    try:
        if spec['revision'] != member_revision:
            raise ValueError('index revision differs from platform member')
        envelope = load_partitioned_index(spec['path'], expected_sha256=spec['sha256'], hydrate=False)
        data = validate_index(envelope, member_revision)
        if envelope['sha256'] != spec['sha256'] or data['repo_key'] != repo_key:
            raise ValueError('index identity mismatch')
        scopes = _path_scopes(data['blobs'])
        verified = {'status': 'digest-verified', 'sha256': envelope['sha256'],
                    'producer': data['producer'], 'documents': len(data['blobs']),
                    'path_scopes': scopes[:MAX_PATH_SCOPES],
                    'omitted_scopes': max(0, len(scopes) - MAX_PATH_SCOPES),
                    'scope_meaning': 'indexed path prefixes, not exhaustive subtree coverage'}
    except _UNPROVEN:
        # Any spec that cannot be proven is a named gap; one bad spec never hides the others.
        row['gap'] = INDEX_GAP
        return row
    row.update(verified)
    return row


def platform_coverage(config, anchor):
    """Return one coverage entry per configured source of platform `anchor`."""
    result = {}
    indexes_by_repo = config.get('scip_indexes', {})
    for key, member in config['platforms'][anchor]['sources'].items():
        repo = config['repositories'][key]
        revision = member['revision']
        result[key] = {
            'context': {'capability_ids': sorted(repo['capabilities']),
                        'rule': 'use only a listed capability'},
            'retrieve': {'eligible_path_count': len(eligible_paths(repo)),
                         'corpus_status': 'not-assessed',
                         'rule': 'source index import does not embed documents'},
            'discover': {'scope': 'scripts/*.py only; not general code search'},
            'symbol': {'indexes': [_index_row(spec, key, revision) for spec in indexes_by_repo.get(key, [])],
                       'platform_key': anchor,
                       'next_call': {'tool': 'knowledge.symbol',
                                     'arguments': {'repo_key': key, 'target_revision': revision},
                                     'supply': ['path', 'line']},
                       'rule': 'follow member definitions with their repo_key/revision; '
                               'unsupported paths return a gap'},
        }
    return result
