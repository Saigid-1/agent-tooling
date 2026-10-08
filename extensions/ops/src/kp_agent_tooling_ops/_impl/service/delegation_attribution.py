"""Operator attribution of a verified Codex child session; never desk admission.

Native metadata proves the parent/child thread relationship. The assigned role
is a separate parent/operator assertion, with its own evidence reference and
effective time. Neither a task name nor an inherited environment ID assigns a
role or grants the child's session its parent's desk.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from contextlib import closing
import unicodedata

from kp_agent_tooling._impl.service.session_sources import SessionSources


_UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z')
_MAX_HEADER = 65536


def _required(value: str, label: str, limit: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'bounded {label} required')
    return value


def _header(path: str | Path) -> tuple[dict, str]:
    source = Path(path)
    if not source.is_absolute() or source.is_symlink() or not source.is_file() or source.suffix != '.jsonl':
        raise ValueError('existing absolute regular native JSONL file required')
    with source.open('rb') as stream:
        raw = stream.readline(_MAX_HEADER + 1)
    if not raw.endswith(b'\n') or len(raw) > _MAX_HEADER:
        raise ValueError('bounded complete native session header required')
    try:
        row = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('invalid native session header') from error
    if not isinstance(row, dict) or row.get('type') != 'session_meta' or not isinstance(row.get('payload'), dict):
        raise ValueError('native session_meta first row required')
    identity = row['payload'].get('id')
    if not isinstance(identity, str) or not _UUID.fullmatch(identity) or not source.stem.endswith(identity):
        raise ValueError('native session identity differs from filename or is invalid')
    return row, sha256(raw).hexdigest()


def _source_range(value: dict | None, *, identity: str, source_file: str | Path) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {'native_id', 'source_file', 'start_offset'}:
        raise ValueError('source range requires native_id, source_file and start_offset')
    path = Path(source_file)
    offset = value['start_offset']
    if (value['native_id'] != identity or value['source_file'] != str(path)
            or type(offset) is not int or offset < 0 or offset > path.stat().st_size):
        raise ValueError('source range must match selected native session file and byte boundary')
    if offset:
        with path.open('rb') as stream:
            stream.seek(offset - 1)
            if stream.read(1) != b'\n':
                raise ValueError('source range must begin at a native JSONL row boundary')
    return dict(value)


def plan_codex_delegation(*, parent_file: str | Path, child_file: str | Path,
                          agent_path: str, role_id: str, asserted_by: str,
                          assignment_ref: str, recorded_at: str,
                          approved_role_ids=(), child_source_range: dict | None = None,
                          parent_source_range: dict | None = None) -> dict:
    """Read only session_meta headers and calculate an attribution preview."""
    _required(agent_path, 'agent path', 256)
    _required(asserted_by, 'parent/operator actor')
    _required(assignment_ref, 'parent role-assignment evidence')
    _required(role_id, 'exact role identity', 128)
    if any(unicodedata.category(character).startswith('C') for character in role_id):
        raise ValueError('role identity contains control characters')
    if not isinstance(recorded_at, str):
        raise ValueError('timezone-aware assignment time required')
    try:
        when = datetime.fromisoformat(recorded_at.replace('Z', '+00:00'))
    except ValueError as error:
        raise ValueError('timezone-aware assignment time required') from error
    if when.tzinfo is None:
        raise ValueError('timezone-aware assignment time required')

    parent, parent_digest = _header(parent_file)
    child, child_digest = _header(child_file)
    parent_id = parent['payload']['id']
    child_id = child['payload']['id']
    if parent_id == child_id:
        raise ValueError('delegation requires distinct native parent and child sessions')
    source = child['payload'].get('source')
    spawn = source.get('subagent', {}).get('thread_spawn') if isinstance(source, dict) and isinstance(source.get('subagent'), dict) else None
    if (not isinstance(spawn, dict) or spawn.get('parent_thread_id') != parent_id
            or spawn.get('agent_path') != agent_path or type(spawn.get('depth')) is not int
            or spawn['depth'] < 1):
        raise ValueError('child native thread_spawn does not match the selected parent and agent path')
    for field in ('session_id', 'parent_thread_id'):
        inherited = child['payload'].get(field)
        if inherited is not None and inherited != parent_id:
            raise ValueError('conflicting inherited parent identity in child metadata')
    child_range = _source_range(child_source_range, identity=child_id, source_file=child_file)
    parent_range = _source_range(parent_source_range, identity=parent_id, source_file=parent_file)
    role_status = 'approved_catalog_role' if role_id in approved_role_ids else 'unregistered_role_claim'
    return {
        'schema_version': 'ops.delegation-attribution-preview.v1',
        'status': 'verified_native_relationship', 'runtime': 'codex',
        'parent_native_id': parent_id, 'child_native_id': child_id,
        'native_parent_verification_basis': 'parent session_meta ID and child thread_spawn parent_thread_id',
        'agent_path': agent_path, 'native_depth': spawn['depth'],
        'native_agent_role': spawn.get('agent_role'),
        'assigned_role_id': role_id, 'role_catalog_status': role_status,
        'asserted_by': asserted_by, 'assignment_ref': assignment_ref,
        'recorded_at': recorded_at,
        'child_source_range': child_range, 'parent_source_range': parent_range,
        'evidence': [f'native-session-meta:codex:{parent_id}:sha256:{parent_digest}',
                     f'native-session-meta:codex:{child_id}:sha256:{child_digest}',
                     assignment_ref],
        'authority': 'native parentage plus parent/operator role assertion; no desk admission',
    }


def apply_codex_delegation(*, store, tenant_id: str, parent_file: str | Path,
                           child_file: str | Path, agent_path: str, role_id: str,
                           asserted_by: str, assignment_ref: str, recorded_at: str,
                           approved_role_ids=(), supersedes_role_claim: str | None = None,
                           child_source_range: dict | None = None,
                           parent_source_range: dict | None = None) -> dict:
    """Append role and relationship claims after native verification.

    Contributor is historical participation in the parent's work. Ownership
    stays a distinct desk claim and is intentionally untouched here.
    """
    _required(tenant_id, 'tenant identity')
    preview = plan_codex_delegation(
        parent_file=parent_file, child_file=child_file, agent_path=agent_path,
        role_id=role_id, asserted_by=asserted_by, assignment_ref=assignment_ref,
        recorded_at=recorded_at, approved_role_ids=approved_role_ids,
        child_source_range=child_source_range, parent_source_range=parent_source_range,
    )
    sources = SessionSources(store)
    with closing(store._connect()) as db:
        if not sources.available(db):
            raise ValueError('session source catalog must already be initialized')
    parent = sources.register(tenant_id=tenant_id, runtime='codex', native_id=preview['parent_native_id'])
    child = sources.register(tenant_id=tenant_id, runtime='codex', native_id=preview['child_native_id'])
    common = dict(asserted_by=asserted_by, recorded_at=recorded_at,
                  evidence=preview['evidence'], valid_from=recorded_at)
    child_scope = ({'source_range': preview['child_source_range']}
                   if preview['child_source_range'] is not None else {})
    parent_scope = ({'source_range': preview['parent_source_range']}
                    if preview['parent_source_range'] is not None else {})
    try:
        role_claim = sources.claim(
            session_id=child, predicate='session.assigned_role',
            object={'kind': 'role', 'id': role_id},
            supersedes=supersedes_role_claim, **common, **child_scope,
        )
        parent_claim = sources.claim(
            session_id=child, predicate='session.delegated_from',
            object={'kind': 'source_session', 'id': parent}, **common, **child_scope,
        )
        contributor_claim = sources.claim(
            session_id=parent, predicate='session.contributor',
            object={'kind': 'source_session', 'id': child}, **common, **parent_scope,
        )
    except Exception as error:
        # SessionSources.claim is individually atomic. All identities and
        # claim payloads above are deterministic, so an exact retry completes
        # any interrupted sequence without duplicating prior claims.
        raise RuntimeError('delegation claims may be partially recorded; retry with identical arguments') from error
    return {**preview, 'status': 'claims_recorded', 'tenant_id': tenant_id,
            'parent_source_session_id': parent, 'child_source_session_id': child,
            'claim_ids': {'assigned_role': role_claim, 'delegated_from': parent_claim,
                          'contributor': contributor_claim},
            'owner_claim_created': False, 'desk_admitted': False}
