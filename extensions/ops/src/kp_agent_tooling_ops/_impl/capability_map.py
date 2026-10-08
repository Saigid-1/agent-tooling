"""Validate a curated map and compare its immutable file references."""
from __future__ import annotations

import re
from typing import Protocol


class ReferenceSource(Protocol):
    revision: str

    def entry(self, path: str) -> tuple[str, str] | None: ...


ROLES = {'architecture', 'entrypoint', 'composition', 'service', 'index',
         'source_resolution', 'verification'}
SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')


def _text(value: object, maximum: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _sha(value: object) -> bool:
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def validate_manifest(value: object) -> dict:
    if not isinstance(value, dict) or value.get('schema_version') != 'ops.capability-map.v1':
        raise ValueError('unsupported capability map schema')
    if not _text(value.get('capability_id'), 80) or not _text(value.get('summary'), 500):
        raise ValueError('invalid capability id or summary')
    if not _sha(value.get('source_revision')):
        raise ValueError('source_revision must be a full lowercase commit id')
    refs = value.get('references')
    if not isinstance(refs, list) or not 1 <= len(refs) <= 32:
        raise ValueError('references must contain 1..32 entries')
    ids, paths = set(), set()
    for ref in refs:
        if not isinstance(ref, dict) or not _text(ref.get('id'), 80):
            raise ValueError('invalid reference id')
        if not isinstance(ref.get('role'), str) or ref['role'] not in ROLES:
            raise ValueError('invalid reference role')
        path = ref.get('path')
        if (not _text(path, 512) or '\\' in path
                or any(part in {'', '.', '..'} for part in path.split('/'))
                or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in path)):
            raise ValueError('reference path must be a normalized relative POSIX path')
        if not _sha(ref.get('blob_sha')):
            raise ValueError('invalid reference blob id')
        if ref['id'] in ids or path in paths:
            raise ValueError('duplicate reference id or path')
        ids.add(ref['id']); paths.add(path)
    return value


def compare_references(manifest: dict, baseline: ReferenceSource, current: ReferenceSource) -> dict:
    rows = []
    for ref in manifest['references']:
        pinned = baseline.entry(ref['path'])
        if pinned is None or pinned[0] not in {'100644', '100755'} or pinned[1] != ref['blob_sha']:
            raise ValueError('baseline reference missing, unsupported or inconsistent with its pin')
        observed = current.entry(ref['path'])
        if observed is None:
            status = 'missing'
        elif observed[0] not in {'100644', '100755'}:
            status = 'unsupported'
        else:
            status = 'unchanged' if observed[1] == ref['blob_sha'] else 'changed'
        rows.append({key: ref[key] for key in ('id', 'role', 'path', 'blob_sha')} | {
            'status': status, 'observed_blob_sha': observed[1] if observed else None})
    return {
        'schema_version': 'ops.capability-map-report.v1',
        'capability_id': manifest['capability_id'], 'summary': manifest['summary'],
        'source_revision': baseline.revision, 'checked_revision': current.revision,
        'status': 'current' if all(r['status'] == 'unchanged' for r in rows) else 'review_required',
        'working_tree': 'excluded', 'conformance': 'not-assessed', 'references': rows,
        'limitations': ['This curated map covers only its listed committed file references.',
                        'Changed or missing references require review, not a claim of architectural violation.',
                        'Unchanged references do not establish correctness, runtime readiness or semantic completeness.'],
    }
