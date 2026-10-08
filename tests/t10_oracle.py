"""Reference oracle: ``SessionSources.select`` exactly as it is at the T10 base SHA.

The T10 order keeps ``SessionSources.select`` in the tests as a reference
oracle ("on every seeded state, the new scope must equal select's"). The
product's own ``select`` is in the FEATURE write scope and may change, so this
module is a frozen, standalone port of the base implementation
(``_impl/service/session_sources.py`` at the T10 base, ``select``, ``metadata``,
``_at_episode``, ``_session`` and ``_claims``). It reads the sealed tables with
plain SQL, the catalog JSON and the admission ledger directly; it imports
nothing from the code under test.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path


class OracleUnavailable(RuntimeError):
    """The base implementation would raise ``EpisodeUnavailable``."""


def _id(kind, value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    return kind + ':sha256:' + hashlib.sha256(raw).hexdigest()


def catalog_bindings(catalog_path):
    """``[(binding_key, tenant_id)]`` of an imported desk catalog."""
    catalog = json.loads(Path(catalog_path).read_text())
    return [(row['binding_key'], row['tenant_id']) for row in catalog['bindings']]


def admission(ledger_path, instance, session):
    with closing(sqlite3.connect(Path(ledger_path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        row = db.execute('SELECT binding_key,tenant_id FROM desk_session_launches '
                         'WHERE provider_instance=? AND provider_session_id=?', (instance, session)).fetchone()
    if row is None:
        raise OracleUnavailable('session has no desk admission')
    return row


def _available(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='source_sessions'").fetchone() is not None


def _session(db, identity, tenant=None):
    row = db.execute('SELECT tenant,payload FROM source_sessions WHERE id=?', (identity,)).fetchone()
    if row is None or (tenant is not None and row[0] != tenant):
        raise OracleUnavailable('source session unavailable to this tenant')
    payload = json.loads(row[1])
    if _id('source-session', payload) != identity or payload['tenant_id'] != row[0]:
        raise OracleUnavailable('source session integrity mismatch')
    return payload


def _claims(db, session_id):
    result = []
    for identity, raw in db.execute('SELECT id,payload FROM session_claims WHERE session_id=? ORDER BY rowid',
                                    (session_id,)):
        p = json.loads(raw)
        if _id('session-claim', p) != identity or p['session_id'] != session_id:
            raise OracleUnavailable('session claim integrity mismatch')
        result.append(dict(p, claim_id=identity))
    return result


def metadata(db, session_id, tenant=None):
    payload = _session(db, session_id, tenant)
    claims = _claims(db, session_id)
    superseded = {r['supersedes'] for r in claims if r['supersedes']}
    active = [r for r in claims if r['claim_id'] not in superseded and not r['retracted']]
    desks = sorted({r['object']['id'] for r in active
                    if r['predicate'] == 'session.owner' and r['object']['kind'] == 'desk'})
    return dict(payload, session_id=session_id, claims=claims, active_claims=active,
                desk_bindings=desks, attribution_status='unresolved' if not desks else
                'resolved' if len(desks) == 1 else 'conflicting',
                authority='historical claims; no admission or authentication grant')


def at_episode(meta, coordinates):
    if meta is None:
        return None
    active = []
    for claim in meta['active_claims']:
        boundary = claim.get('source_range')
        if boundary is None:
            active.append(claim)
        elif (isinstance(coordinates, dict) and
              boundary['native_id'] == meta['native_id'] and
              coordinates.get('path') == boundary['source_file'] and
              type(coordinates.get('start')) is int and
              coordinates['start'] >= boundary['start_offset']):
            active.append(claim)
    desks = sorted({claim['object']['id'] for claim in active
                    if claim['predicate'] == 'session.owner' and claim['object']['kind'] == 'desk'})
    return {**meta, 'active_claims': active, 'desk_bindings': desks,
            'attribution_status': ('unresolved' if not desks else
                                   'resolved' if len(desks) == 1 else 'conflicting')}


def select(store_path, bindings, admitted_tenant, admitted_binding, *, scope='desk', binding_key=None,
           attribution='any', claims=None, session_id=None):
    """The base ``SessionSources.select`` over ``bindings = [(binding_key, tenant)]``."""
    if scope not in ('desk', 'topic') or attribution not in ('any', 'resolved', 'unresolved', 'conflicting'):
        raise ValueError('invalid search scope or attribution filter')
    if scope == 'topic' and binding_key is not None:
        raise ValueError('binding_key selects desk scope; use desk scope with a query')
    registered = dict(bindings)
    if scope == 'desk':
        if binding_key is None:
            binding = admitted_binding
        else:
            if binding_key not in registered:
                raise ValueError('requested binding is not registered')
            if registered[binding_key] != admitted_tenant:
                raise OracleUnavailable('requested binding unavailable to this tenant')
            binding = binding_key
    else:
        binding = None
    tenant_bindings = {key for key, tenant in bindings if tenant == admitted_tenant}
    claims = [] if claims is None else claims
    selected = {}
    meta_by_session = {}
    excluded_unresolved = 0
    with closing(sqlite3.connect(Path(store_path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        links = dict(db.execute('SELECT episode_id,session_id FROM session_episodes')) if _available(db) else {}
        for identity, sid in links.items():
            if sid not in meta_by_session:
                try:
                    meta_by_session[sid] = metadata(db, sid, admitted_tenant)
                except OracleUnavailable:
                    meta_by_session[sid] = None
        for identity, original_binding in db.execute('SELECT id,binding FROM episodes'):
            if original_binding in tenant_bindings or meta_by_session.get(links.get(identity)) is not None:
                selected[identity] = {'storage_binding': original_binding,
                                      'metadata': at_episode(meta_by_session.get(links.get(identity)), None),
                                      'source_session_id': links.get(identity)}
        if _available(db):
            for identity, sid, coordinates_raw in db.execute(
                    "SELECT id,session_id,json_extract(payload,'$.source_provenance.source_coordinates') "
                    'FROM source_episodes WHERE tenant=?', (admitted_tenant,)):
                coordinates = json.loads(coordinates_raw) if coordinates_raw else None
                selected[identity] = {'storage_binding': None,
                                      'metadata': at_episode(meta_by_session.get(sid), coordinates),
                                      'source_session_id': sid}
        for identity, item in list(selected.items()):
            meta = item['metadata']
            if item['source_session_id'] and meta is None:
                raise OracleUnavailable('source session metadata unavailable')
            desks = meta['desk_bindings'] if meta else [item['storage_binding']]
            status = meta['attribution_status'] if meta else 'resolved'
            item.update(binding_key=desks[0] if len(desks) == 1 else None, attribution_status=status)
            if scope == 'desk' and status == 'unresolved':
                excluded_unresolved += 1
            matching = (scope == 'topic' or binding in desks) and (attribution == 'any' or status == attribution)
            matching = matching and (session_id is None or session_id == item['source_session_id'])
            matching = matching and all(meta and any(r['predicate'] == c['predicate'] and r['object'] == c['object']
                                                     for r in meta['active_claims']) for c in claims)
            if not matching:
                del selected[identity]
    return selected, {'scope': scope, 'binding_key': binding, 'attribution': attribution,
                      'claim_filters': claims, 'session_id': session_id,
                      'excluded_unresolved_episodes': excluded_unresolved,
                      'unresolved_included': scope == 'topic' and attribution in ('any', 'unresolved')}


def select_for(world, session, **filters):
    """The oracle scope for one admitted session of a ``t10_world.World``."""
    from t10_world import INSTANCE
    binding, tenant = admission(world.ledger_path, INSTANCE, session)
    return select(world.store_path, catalog_bindings(world.catalog), tenant, binding, **filters)
