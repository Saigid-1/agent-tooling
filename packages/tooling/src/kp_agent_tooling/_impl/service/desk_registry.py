"""Portable desk registry: descriptor, operator role roster and catalog import.

A desk is data in the memory store. Its role comes from an operator-configured
roster; doctrine, approval and reviewer fields are never gates. Each desk has
exactly one binding. Nothing in this module admits a session: only an operator
or launcher ``bind`` does (``session_bindings``).
"""
from __future__ import annotations

import json
import uuid
from contextlib import closing
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.desk_binding import call_memo
from kp_agent_tooling._impl.service.desk_identity import binding_key

DESCRIPTOR_SCHEMA = 'agent-tooling.desk-registry.v1'
ROSTER_SCHEMA = 'agent-tooling.role-roster.v1'
IMPORTED_CATALOG_SCHEMA = 'ops.imported-desk-catalog.v1'
REGISTRY_SOURCE = 'agent-tooling.desk-registry'
ANY_REPOSITORY = '*'
MAX_ROLES = 200
MAX_REPOS = 32
CONTEXT_DOC_BYTES = 16384
_ROSTER_BYTES = 262144
# Fixed namespace: an imported binding always maps to the same desk identity,
# which is what makes a repeated import create nothing.
_IMPORT_NAMESPACE = uuid.UUID('6f1f8a52-3d0e-5b7c-9a4e-2c8d7f6b1e30')
_ROLE_FIELDS = ('role_id', 'label', 'purpose')


def is_registry(authority):
    return getattr(authority, 'kind', None) == DESCRIPTOR_SCHEMA


def _private_json(path):
    return leaf.read_private_json(path, memo=call_memo())


def load_descriptor(path):
    """Validate an ``agent-tooling.desk-registry.v1`` descriptor (private operator file).

    ``harness_profiles_path`` is optional: an absolute path to the operator harness
    profiles that launch binding reads (``harness_profiles``).
    """
    value = _private_json(path)
    required = {'schema_version', 'tenant_id', 'roster_path'}
    if (not isinstance(value, dict) or not required <= set(value) <= required | {'harness_profiles_path'}
            or value['schema_version'] != DESCRIPTOR_SCHEMA):
        raise ValueError('invalid desk registry descriptor')
    if 'harness_profiles_path' in value:
        profiles = value['harness_profiles_path']
        if not isinstance(profiles, str) or len(profiles) > 4096 or not Path(profiles).is_absolute():
            raise ValueError('registry harness_profiles_path must be an absolute path')
    tenant = value['tenant_id']
    if (not isinstance(tenant, str) or not tenant or tenant != tenant.strip()
            or len(tenant) > 512 or '|' in tenant):
        raise ValueError('registry tenant must contain 1..512 characters without | or surrounding space')
    roster = value['roster_path']
    if not isinstance(roster, str) or len(roster) > 4096 or not Path(roster).is_absolute():
        raise ValueError('registry roster_path must be an absolute path')
    return dict(value)


def _role(value):
    if not isinstance(value, dict) or set(value) != set(_ROLE_FIELDS):
        raise ValueError('a role has exactly role_id, label and purpose')
    role_id, label, purpose = (value[k] for k in _ROLE_FIELDS)
    if not isinstance(role_id, str) or not 1 <= len(role_id) <= 128 or role_id != role_id.strip():
        raise ValueError('role_id must contain 1..128 characters without surrounding space')
    if not isinstance(label, str) or not label.strip() or len(label) > 128:
        raise ValueError('role label must contain 1..128 characters')
    if not isinstance(purpose, str) or len(purpose) > 1000:
        raise ValueError('role purpose is bounded to 1000 characters')
    return {'role_id': role_id, 'label': label.strip(), 'purpose': purpose.strip()}


def validate_roster(value):
    if (not isinstance(value, dict) or value.get('schema_version') != ROSTER_SCHEMA
            or not {'schema_version', 'roles'} <= set(value) <= {'schema_version', 'roles', 'version', 'authority'}):
        raise ValueError('invalid role roster')
    version = value.get('version', 0)
    if type(version) is not int or version < 0:
        raise ValueError('role roster version must be a nonnegative integer')
    roles = value['roles']
    if not isinstance(roles, list) or not 1 <= len(roles) <= MAX_ROLES:
        raise ValueError('a role roster has 1..200 roles')
    clean = [_role(role) for role in roles]
    if len({role['role_id'] for role in clean}) != len(clean):
        raise ValueError('role_id values must be unique')
    return {'schema_version': ROSTER_SCHEMA, 'version': version, 'roles': clean}


def _asset(name):
    return validate_roster(json.loads(files('kp_agent_tooling').joinpath('assets/roles/' + name).read_text()))


def default_roster():
    """Small generic roster used until the operator saves one; no OPS role names.

    The nine OPS roles ship only as ``assets/roles/ops-roles.example.json``, a
    valid roster an operator may copy to ``roster_path``; nothing loads it.
    """
    return _asset('default.json')


class RoleRoster:
    """The operator role roster file named by the registry descriptor."""

    def __init__(self, path):
        self.path = leaf.as_path(path)

    def read(self):
        if not self.path.exists() and not self.path.is_symlink():
            return {**default_roster(), 'source': 'default'}
        return {**validate_roster(_private_json(self.path)), 'source': 'configured'}

    # Lock the directory itself: roster writes replace the file, so a file lock would not hold.
    def _locked(self):
        return leaf.owned_directory_lock(self.path, message='role roster needs an existing directory owned by the running user')

    def _write(self, roles, version):
        return leaf.replace_private_json(self.path, {
            'schema_version': ROSTER_SCHEMA, 'version': version,
            'authority': 'operator role roster; templates only, no session admission or write grant',
            'roles': roles}, max_bytes=_ROSTER_BYTES, too_large='role roster exceeds 256 KiB', temp_prefix='.roster-',
            result=lambda document: {**validate_roster(document), 'source': 'configured'},
            indent=2, ensure_ascii=False)

    def save_role(self, request):
        """Add or update one role under optimistic concurrency on the roster version."""
        if not isinstance(request, dict) or set(request) != {*_ROLE_FIELDS, 'expected_version'}:
            raise ValueError('exact role fields required: role_id, label, purpose, expected_version')
        version = request['expected_version']
        if type(version) is not int or version < 0:
            raise ValueError('expected version required')
        role = _role({k: request[k] for k in _ROLE_FIELDS})
        with self._locked():
            current = self.read()
            existing = next((r for r in current['roles'] if r['role_id'] == role['role_id']), None)
            if existing == role and current['version'] in (version, version + 1):
                return {**current, 'role': role, 'idempotent': True, 'authorization_changed': False}
            if current['version'] != version:
                raise ValueError('role roster changed; reload before saving')
            roles = ([role if r['role_id'] == role['role_id'] else r for r in current['roles']]
                     if existing else [*current['roles'], role])
            if len(roles) > MAX_ROLES:
                raise ValueError('a role roster has 1..200 roles')
            saved = self._write(roles, version + 1)
        return {**saved, 'role': role, 'idempotent': False, 'authorization_changed': False}

    def add_missing(self, role_ids, *, purpose):
        """Additive, idempotent: roles already listed are never changed."""
        with self._locked():
            current = self.read()
            known = {r['role_id'] for r in current['roles']}
            added = [_role({'role_id': r, 'label': r, 'purpose': purpose})
                     for r in sorted(set(role_ids)) if r not in known]
            if added:
                if len(current['roles']) + len(added) > MAX_ROLES:
                    raise ValueError('a role roster has 1..200 roles')
                self._write([*current['roles'], *added], current['version'] + 1)
        return [r['role_id'] for r in added]


def new_desk_binding(tenant_id, desk_id):
    """A new desk's one binding: (tenant, 'desk:<uuid>', '*')."""
    return {'binding_key': binding_key(tenant_id=tenant_id, role=desk_id, repo_key=ANY_REPOSITORY),
            'tenant_id': tenant_id, 'role': desk_id, 'repo_key': ANY_REPOSITORY, 'source': REGISTRY_SOURCE}


def desk_binding(profile, tenant_id):
    """Stored for imported and registry-created desks; derived for older profiles."""
    return dict(profile['binding']) if profile.get('binding') is not None else new_desk_binding(tenant_id, profile['desk_id'])


def registry_desks(store_path, tenant_id):
    """Latest verified registry desks with their single verified binding."""
    from kp_agent_tooling._impl.service.desk_profiles import latest_profiles
    path = leaf.as_path(store_path)
    if path.is_symlink():
        raise ValueError('registry store must not be a symbolic link')
    if not path.is_file():
        return []
    with closing(leaf.sqlite_connect(path, mode='ro', resolve=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desk_profiles'").fetchone():
            return []
        profiles = latest_profiles(db, tenant_id)
    result, seen = [], set()
    for profile in profiles:
        binding = desk_binding(profile, tenant_id)
        if (set(binding) != {'binding_key', 'tenant_id', 'role', 'repo_key', 'source'}
                or binding['tenant_id'] != tenant_id
                or binding['binding_key'] != binding_key(tenant_id=binding['tenant_id'], role=binding['role'],
                                                         repo_key=binding['repo_key'])):
            raise ValueError('registry binding integrity mismatch')
        if binding['binding_key'] in seen:
            raise ValueError('registry binding is held by more than one desk')
        seen.add(binding['binding_key'])
        result.append((profile, binding))
    return result


def imported_desk_id(key):
    return 'desk:' + str(uuid.uuid5(_IMPORT_NAMESPACE, key))


def _import_differences(profile, binding, row):
    expected = {'tenant_id': row['tenant_id'], 'role': row['role'], 'repo_key': row['repo_key']}
    differences = [k for k, v in expected.items() if binding[k] != v]
    if profile['role'] != row['role']:
        differences.append('desk role')
    if row['repo_key'] not in profile.get('repos', []):
        differences.append('repos')
    if profile['name'] != row['desk_label'].strip():
        differences.append('label')
    if profile.get('memory_write') is not row['memory_write_allowed']:
        differences.append('memory_write')
    return differences


def import_catalog(profiles, catalog_path):
    """Import an ``ops.imported-desk-catalog.v1`` file into the registry; idempotent.

    Each binding becomes one registry desk keeping its ``binding_key``, role and
    label, with the original ``repo_key`` in ``repos``. The source file is only
    read. Existing desks are never changed; differences are reported.
    """
    from kp_agent_tooling._impl.service.desk_memory_runtime import ImportedDeskAuthority
    from kp_agent_tooling._impl.service.desk_profiles import latest_profiles
    if not is_registry(profiles.registry):
        raise ValueError('import-catalog requires an agent-tooling.desk-registry.v1 registry')
    catalog = ImportedDeskAuthority(catalog_path)._catalog()
    conflicting, eligible = [], []
    for row in catalog['bindings']:
        reason = None
        if row['tenant_id'] != profiles.tenant:
            reason = 'binding tenant differs from the registry tenant'
        elif len(row['role']) > 128 or row['role'] != row['role'].strip():
            reason = 'role exceeds the roster role bound'
        elif not row['desk_label'].strip() or len(row['desk_label'].strip()) > 120:
            reason = 'label exceeds the desk name bound'
        if reason:
            conflicting.append({'binding_key': row['binding_key'], 'desk_id': None, 'reason': reason})
        else:
            eligible.append(row)
    roles_added = RoleRoster(profiles.registry.roster_path).add_missing(
        [row['role'] for row in eligible], purpose='Added by import from an ops.imported-desk-catalog.v1 catalog.')
    created, unchanged = [], []
    now = datetime.now(timezone.utc).isoformat()
    with closing(profiles.store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        current = latest_profiles(db, profiles.tenant)
        by_key = {desk_binding(p, profiles.tenant)['binding_key']: p for p in current}
        ids = {p['desk_id'] for p in current}
        for row in eligible:
            key = row['binding_key']
            profile = by_key.get(key)
            if profile is not None:
                differences = _import_differences(profile, desk_binding(profile, profiles.tenant), row)
                entry = {'binding_key': key, 'desk_id': profile['desk_id']}
                if differences:
                    conflicting.append({**entry, 'reason': 'existing desk differs: ' + ', '.join(differences)})
                else:
                    unchanged.append(entry)
                continue
            desk_id = imported_desk_id(key)
            if desk_id in ids:
                conflicting.append({'binding_key': key, 'desk_id': desk_id,
                                    'reason': 'imported desk identity is held by another binding'})
                continue
            payload = {'desk_id': desk_id, 'tenant_id': profiles.tenant, 'version': 1,
                       'name': row['desk_label'].strip(),
                       'description': f"Imported from an {IMPORTED_CATALOG_SCHEMA} binding (source: {row['source']}).",
                       'role': row['role'], 'repos': [row['repo_key']], 'capture': True,
                       'memory_write': row['memory_write_allowed'], 'context_doc': None,
                       'binding': {'binding_key': key, 'tenant_id': row['tenant_id'], 'role': row['role'],
                                   'repo_key': row['repo_key'], 'source': row['source']},
                       'recorded_at': now,
                       'authority': 'imported desk binding; registry record, not session admission'}
            db.execute('INSERT INTO desk_profiles VALUES (?,?,?,?,?)',
                       (profiles.tenant, desk_id, 1, leaf.canonical_bytes(payload, ascii=True, allow_nan=True),
                        leaf.content_id('desk-profile', payload, ascii=True, allow_nan=True)))
            ids.add(desk_id)
            by_key[key] = payload
            created.append({'binding_key': key, 'desk_id': desk_id})
    return {'status': 'imported', 'created': created, 'unchanged': unchanged, 'conflicting': conflicting,
            'roles_added': roles_added,
            'counts': {'created': len(created), 'unchanged': len(unchanged), 'conflicting': len(conflicting)},
            'sessions_admitted': 0, 'source_modified': False}
