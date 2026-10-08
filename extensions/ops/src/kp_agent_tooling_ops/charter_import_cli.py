"""Operator charter import. Profiles/aliases are metadata, never admission."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import uuid

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.desk_memory_runtime import components, private_json
from kp_agent_tooling._impl.service.desk_profiles import DeskProfiles


def reconcile(registry, manifest, *, apply=False):
    if manifest.get('schema_version') != 'agent-tooling.charter-import.v1' or manifest.get('tenant_id') != registry.tenant:
        raise ValueError('charter import tenant/schema mismatch')
    encoded, digest = leaf.canonical_bytes_sha256(manifest, ascii=True, allow_nan=True)
    sources=manifest['sources']
    for source in sources:
        if hashlib.sha256(source['text'].encode()).hexdigest()!=source['sha256']:
            raise ValueError('charter source digest mismatch')
    profiles=manifest['profiles']
    if not 1<=len(profiles)<=100:raise ValueError('bounded profiles required')
    aliases={}; ids=set(); names=set(); actions=[]
    roles={r['role_id'] for r in registry.roles()}
    with closing(registry.store._connect()) as db:
        ready=db.execute("SELECT 1 FROM sqlite_master WHERE name='desk_charter_aliases'").fetchone()
        existing=dict(db.execute('SELECT alias,desk_id FROM desk_charter_aliases WHERE tenant=?',(registry.tenant,))) if ready else {}
    for item in profiles:
        p=item['profile']
        if set(p)!={'desk_id','name','description','role','expected_version'}:raise ValueError('exact profile fields required')
        did=p['desk_id']
        if p['name'] in names:raise ValueError('duplicate profile name')
        names.add(p['name'])
        if did in ids:raise ValueError('duplicate desk identity')
        ids.add(did)
        if did!='desk:'+str(uuid.UUID(did.removeprefix('desk:'))):raise ValueError('canonical desk UUID required')
        if p['role'] not in roles:raise ValueError('unregistered charter role')
        if not isinstance(p['expected_version'],int) or p['expected_version']<0:raise ValueError('expected version required')
        for key,limit in [('name',120),('description',4000)]:
            if not isinstance(p[key],str) or not p[key].strip() or len(p[key])>limit:raise ValueError('invalid profile '+key)
        for alias in item['aliases']:
            if not isinstance(alias,str) or not alias or len(alias)>1024:raise ValueError('invalid charter alias')
            if aliases.get(alias,did)!=did or existing.get(alias,did)!=did:raise ValueError('charter alias conflict')
            aliases[alias]=did
        old=registry.get(did)
        same=old and all(old[k]==p[k] for k in ('name','description','role'))
        if not same and (old['version'] if old else 0)!=p['expected_version']:
            raise ValueError('desk changed; regenerate import plan')
        if not old and any(x['name']==p['name'] for x in registry.list()):raise ValueError('existing name needs explicit identity mapping')
        actions.append({'desk_id':did,'action':'unchanged' if same else 'update' if old else 'create'})
    if apply:
        registry.initialize()
        for item,action in zip(profiles,actions):
            if action['action']!='unchanged':registry.save(item['profile'])
        with closing(registry.store._connect()) as db,db:
            db.executescript('''CREATE TABLE IF NOT EXISTS desk_charter_imports
            (digest TEXT PRIMARY KEY, tenant TEXT NOT NULL, payload BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS desk_charter_aliases
            (tenant TEXT, alias TEXT, desk_id TEXT, import_digest TEXT, PRIMARY KEY(tenant,alias));''')
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT payload FROM desk_charter_imports WHERE digest=?',(digest,)).fetchone()
            if prior and prior[0]!=encoded:raise ValueError('charter import integrity mismatch')
            db.execute('INSERT OR IGNORE INTO desk_charter_imports VALUES (?,?,?)',(digest,registry.tenant,encoded))
            for alias,did in aliases.items():
                prior=db.execute('SELECT desk_id FROM desk_charter_aliases WHERE tenant=? AND alias=?',(registry.tenant,alias)).fetchone()
                if prior and prior[0]!=did:raise ValueError('charter alias changed during import')
                db.execute('INSERT OR IGNORE INTO desk_charter_aliases VALUES (?,?,?,?)',(registry.tenant,alias,did,digest))
    return {'status':'applied' if apply else 'preview','manifest_digest':digest,'actions':actions,'alias_count':len(aliases),'authorization_changed':False,'historical_claims_changed':False}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True);p.add_argument('--manifest',required=True);p.add_argument('--expected-sha256');p.add_argument('action',choices=['preview','apply']);a=p.parse_args()
    try:
        raw=Path(a.manifest).read_bytes()
        if len(raw)>2_000_000:raise ValueError('manifest exceeds bound')
        manifest=private_json(a.manifest)
        if a.action=='apply' and hashlib.sha256(raw).hexdigest()!=a.expected_sha256:raise ValueError('reviewed manifest digest required')
        cfg,_,_,store=components(a.config)
        result=reconcile(DeskProfiles(store,cfg['provider_session_id']),manifest,apply=a.action=='apply')
        print(json.dumps(result));return 0
    except (ValueError,KeyError,OSError) as error:
        print(json.dumps({'status':'error','reason':str(error)}));return 1


if __name__=='__main__':raise SystemExit(main())
