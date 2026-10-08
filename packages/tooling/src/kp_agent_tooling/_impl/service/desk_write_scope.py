"""Operator-only, revocable same-role repository write scope for an exact session."""
from contextlib import closing
from datetime import datetime, timezone
import json

from kp_agent_tooling._impl import leaf


def _digest(payload):
    return leaf.canonical_sha256(payload, ascii=True, allow_nan=True)


def record_scope(store, session, *, enabled, approval_ref):
    if type(enabled) is not bool or not isinstance(approval_ref,str) or not 1 <= len(approval_ref) <= 4096:
        raise ValueError('explicit operator approval reference required')
    seat=store.sessions.resolve(session,store.registry)
    payload={'session':session,'instance':store.sessions.provider_instance,'binding_key':seat.binding_key,
             'tenant_id':seat.tenant_id,'role':seat.role,'all_role_repositories':enabled,
             'approval_ref':approval_ref,'recorded_at':datetime.now(timezone.utc).isoformat()}
    digest=_digest(payload)
    with closing(leaf.sqlite_connect(store.sessions.path, mode='rw', resolve=True)) as db,db:
        db.execute('CREATE TABLE IF NOT EXISTS desk_write_scopes (sequence INTEGER PRIMARY KEY, instance TEXT, session TEXT, payload TEXT, digest TEXT)')
        db.execute('INSERT INTO desk_write_scopes(instance,session,payload,digest) VALUES (?,?,?,?)',
                   (payload['instance'],session,json.dumps(payload),digest))
    return {'scope_receipt':digest,**payload,'admission_changed':False}


def scope_enabled(store, session):
    seat=store.sessions.resolve(session,store.registry)
    with closing(leaf.sqlite_connect(store.sessions.path, mode='ro', resolve=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='desk_write_scopes' AND type='table'").fetchone():
            return False
        row=db.execute('SELECT payload,digest FROM desk_write_scopes WHERE instance=? AND session=? ORDER BY sequence DESC LIMIT 1',
                       (store.sessions.provider_instance,session)).fetchone()
    if row is None:return False
    p=json.loads(row[0])
    expected={'session':session,'instance':store.sessions.provider_instance,'binding_key':seat.binding_key,
              'tenant_id':seat.tenant_id,'role':seat.role}
    if _digest(p)!=row[1] or any(p.get(k)!=v for k,v in expected.items()) or type(p.get('all_role_repositories')) is not bool:
        raise PermissionError('write scope receipt integrity mismatch')
    return p['all_role_repositories']


def write_targets(store,session):
    seat=store.sessions.resolve(session,store.registry)
    expanded=scope_enabled(store,session)
    permits=getattr(store.registry,'allows_memory_write',lambda key:True)
    return tuple(r.binding_key for r in store.registry.list_bindings()
                 if r.tenant_id==seat.tenant_id and r.role==seat.role
                 and (expanded or r.binding_key==seat.binding_key) and permits(r.binding_key))
