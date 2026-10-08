"""Operator and launcher session bindings (``agent-tooling.session-binding.v1``).

A binding records its target (harness, provider, model) with the exact native
session, desk, source, workspace and parent session. Recording it admits that
exact session to the desk's one binding in the existing ``DeskSessionLedger``
table, in the same SQLite transaction. This is never a model tool: models and
hook payloads can neither select a desk nor grant admission.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone

from kp_agent_tooling._impl.service.desk_binding import (
    _ID, DeskLaunchConflict, DeskLaunchReceipt, DeskLaunchUnavailable,
)
from kp_agent_tooling._impl.service.desk_registry import is_registry
from kp_agent_tooling._impl import leaf

SCHEMA = 'agent-tooling.session-binding.v1'
SOURCES = ('board', 'host', 'operator', 'import')
FIELDS = ('harness', 'provider', 'model', 'native_session_id', 'desk_id', 'source', 'workspace',
          'parent_session_id')
_OPTIONAL = {'schema_version', 'recorded_at'}
_TABLE = '''CREATE TABLE IF NOT EXISTS session_bindings (
    instance TEXT NOT NULL, native_session_id TEXT NOT NULL, desk_id TEXT NOT NULL,
    binding_key TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
    PRIMARY KEY (instance, native_session_id))'''


def _digest(payload):
    return leaf.canonical_sha256(payload, ascii=True, allow_nan=True)


def _bounded(value, name, bound):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > bound:
        raise ValueError(f'{name} must contain 1..{bound} characters without surrounding space')
    return value


def validate(request):
    """Exact request fields; ``schema_version`` and ``recorded_at`` are optional."""
    if not isinstance(request, dict) or not set(FIELDS) <= set(request) <= set(FIELDS) | _OPTIONAL:
        raise ValueError('exact session binding fields required: ' + ', '.join(FIELDS))
    if request.get('schema_version', SCHEMA) != SCHEMA:
        raise ValueError('unsupported session binding schema')
    record = {name: _bounded(request[name], name, 256) for name in ('harness', 'provider', 'model')}
    for name in ('native_session_id', 'parent_session_id'):
        value = request[name]
        if value is None and name == 'parent_session_id':
            record[name] = None
        elif not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError(f'{name} must be a safe harness session ID')
        else:
            record[name] = value
    if record['parent_session_id'] == record['native_session_id']:
        raise ValueError('a session cannot be its own parent')
    desk_id = request['desk_id']
    if not isinstance(desk_id, str) or len(desk_id) != 41 or not desk_id.startswith('desk:'):
        raise ValueError('canonical desk:<uuid> required')
    if desk_id != 'desk:' + str(uuid.UUID(desk_id.removeprefix('desk:'))):
        raise ValueError('canonical desk:<uuid> required')
    record['desk_id'] = desk_id
    if request['source'] not in SOURCES:
        raise ValueError('source must be board, host, operator or import')
    record['source'] = request['source']
    record['workspace'] = _bounded(request['workspace'], 'workspace', 1024)
    recorded_at = request.get('recorded_at')
    if recorded_at is not None:
        try:
            moment = datetime.fromisoformat(recorded_at) if isinstance(recorded_at, str) and len(recorded_at) <= 64 else None
        except ValueError:
            moment = None
        if moment is None or moment.tzinfo is None:
            raise ValueError('recorded_at must be an ISO-8601 time with a zone')
    record['recorded_at'] = recorded_at or datetime.now(timezone.utc).isoformat()
    return {'schema_version': SCHEMA, **record}


def _same(prior, record):
    return all(prior.get(k) == record[k] for k in (*FIELDS, 'schema_version'))


def bind(config_path, request):
    """Record one binding and admit that exact session to the desk's binding."""
    from kp_agent_tooling._impl.service.desk_memory_runtime import components
    config, registry, ledger, _ = components(config_path)
    if not is_registry(registry):
        raise ValueError('bind requires an agent-tooling.desk-registry.v1 registry')
    record = validate(request)
    context = registry.context(record['desk_id'])
    binding = registry.resolve(tenant_id=context['project']['tenant_id'], role=context['desk']['role_id'],
                               repo_key=context['project']['repo_key'])
    receipt = DeskLaunchReceipt(record['native_session_id'], ledger.provider_instance, binding.binding_key,
                                binding.tenant_id, binding.role, binding.repo_key, record['provider'], record['model'])
    # The same admission bounds DeskSessionLedger.admit enforces.
    if any(not isinstance(v, str) or not v or len(v.encode()) > 512 for v in (
            receipt.binding_key, receipt.tenant_id, receipt.role, receipt.repo_key,
            receipt.provider_id, receipt.model_id)):
        raise ValueError('bounded nonempty admission coordinates required')
    ledger.assert_ready()
    admission = (receipt.binding_key, receipt.tenant_id, receipt.role, receipt.repo_key,
                 receipt.provider_id, receipt.model_id)
    idempotent = False
    try:
        with closing(leaf.sqlite_connect(ledger.path, mode='rw', resolve=True)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desk_contexts'").fetchone():
                raise DeskLaunchUnavailable('desk context table missing; initialize the registry state')
            db.execute(_TABLE)
            admitted = db.execute(
                'SELECT binding_key,tenant_id,role,repo_key,provider_id,model_id FROM desk_session_launches '
                'WHERE provider_instance=? AND provider_session_id=?',
                (ledger.provider_instance, receipt.provider_session_id)).fetchone()
            if admitted is not None and admitted != admission:
                raise DeskLaunchConflict('session is already admitted to another desk or target')
            prior = db.execute('SELECT payload,digest FROM session_bindings WHERE instance=? AND native_session_id=?',
                               (ledger.provider_instance, receipt.provider_session_id)).fetchone()
            if prior is not None:
                value = json.loads(prior[0])
                if _digest(value) != prior[1] or admitted is None:
                    raise DeskLaunchUnavailable('session binding integrity mismatch')
                if not _same(value, record):
                    raise DeskLaunchConflict('session binding already recorded with different coordinates')
                record, idempotent = value, True
            else:
                if admitted is None:
                    db.execute('INSERT INTO desk_session_launches '
                               '(provider_instance,provider_session_id,binding_key,tenant_id,role,repo_key,provider_id,model_id) '
                               'VALUES (?,?,?,?,?,?,?,?)',
                               (ledger.provider_instance, receipt.provider_session_id, *admission))
                db.execute('INSERT OR IGNORE INTO desk_contexts VALUES (?,?,?)',
                           (ledger.provider_instance, receipt.provider_session_id, json.dumps(context)))
                db.execute('INSERT INTO session_bindings VALUES (?,?,?,?,?,?)',
                           (ledger.provider_instance, receipt.provider_session_id, record['desk_id'],
                            receipt.binding_key, json.dumps(record), _digest(record)))
    except sqlite3.Error as error:
        raise DeskLaunchUnavailable('desk launch ledger unavailable') from error
    resolved = ledger.resolve(receipt.provider_session_id, registry)
    return {'status': 'bound', 'binding': record, 'binding_key': resolved.binding_key,
            'provider_instance': ledger.provider_instance, 'idempotent': idempotent, 'dispatch': False}


def list_records(ledger):
    """Verified session bindings recorded for this ledger's harness instance."""
    if not ledger.path.is_file():
        return []
    with closing(leaf.sqlite_connect(ledger.path, mode='ro', resolve=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_bindings'").fetchone():
            return []
        rows = db.execute('SELECT desk_id,binding_key,payload,digest FROM session_bindings WHERE instance=? '
                          'ORDER BY native_session_id', (ledger.provider_instance,)).fetchall()
    records = []
    for desk_id, key, raw, digest in rows:
        value = json.loads(raw)
        if _digest(value) != digest or value.get('desk_id') != desk_id:
            raise ValueError('session binding integrity mismatch')
        records.append({**value, 'binding_key': key})
    return records
