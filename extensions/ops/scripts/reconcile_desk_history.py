#!/usr/bin/env python3
"""Reconcile an immutable desk export into an isolated copy of portable state.

No live writes, admission or model calls. Never point destination at live state.
SQLite backups are individually consistent, not a coordinated multi-store freeze.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

from kp_agent_tooling._impl.service.desk_memory_runtime import components, private_json
from kp_agent_tooling_ops._impl.service.legacy_desk_import import import_export
from kp_agent_tooling._impl.service.episodic_search import index_of, lease_of, reindex


def reconcile(source_state, catalog, export, destination):
    paths = [Path(value) for value in (source_state,catalog,export,destination)]
    source_state,catalog,export,destination = paths
    for path in paths:
        if not path.is_absolute() or path.resolve() != path:
            raise ValueError('absolute physical paths required')
    if destination.exists() or destination.is_relative_to(source_state):
        raise ValueError('fresh isolated destination outside live state required')
    if not source_state.is_dir() or source_state.stat().st_mode & 0o077:
        raise ValueError('private source state required')
    if private_json(catalog).get("schema_version") != "ops.imported-desk-catalog.v1":
        raise ValueError("this legacy rehearsal requires an approved imported desk catalog")
    if export.stat().st_mode & 0o077 or export.stat().st_uid != os.getuid():
        raise ValueError('private owned export required')
    destination.mkdir(mode=0o700)
    state = destination/'state';state.mkdir(mode=0o700)
    if export.stat().st_size > 64*1024*1024:
        raise ValueError('export exceeds 64 MiB')
    frozen_export = destination/'source-export.json'
    shutil.copyfile(export, frozen_export)
    frozen_export.chmod(0o400)
    receipt = {'status':'in_progress','authority':'no session admission or grant change',
               'scope':'isolated copies only; live source may still advance','backups':[]}
    for path in sorted(source_state.glob('*.sqlite3')):
        if path.is_symlink():raise ValueError('database symlink refused')
        target=state/path.name
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as source, sqlite3.connect(target) as db:
            source.backup(db)
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise ValueError('SQLite snapshot integrity failure')
        target.chmod(0o600)
        receipt['backups'].append({'name':path.name,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
    if not all((state/name).exists() for name in ('sessions.sqlite3','episodes.sqlite3')):
        raise ValueError('existing portable session and episode databases required')
    target_catalog=destination/'catalog.json';shutil.copyfile(catalog,target_catalog);target_catalog.chmod(0o600)
    receipt['catalog_sha256']=hashlib.sha256(target_catalog.read_bytes()).hexdigest()
    config=destination/'operator.json'
    config.write_text(json.dumps({'schema_version':'ops.desk-memory.local.v1','state_root':str(state),
        'catalog_path':str(target_catalog),'workspace_root':str(destination),
        'provider_instance':'reconciliation-only','provider_session_id':'operator-import-no-admission'}))
    config.chmod(0o600)
    store=components(config)[3]
    receipt['preflight']=import_export(frozen_export,store,dry_run=True)
    receipt['apply']=import_export(frozen_export,store)
    receipt['replay']=import_export(frozen_export,store)
    # T12b: the copy's index is built by the one build function (build-and-swap under the lease), opened
    # through the leaf as the store's sibling (a marked path); never an in-place rebuild.
    index=index_of(store)
    receipt['index']=reindex(store,index,lease_of(index))
    receipt['status']=('reconciled' if not receipt['apply']['quarantined_count']
        and not receipt['apply']['excluded_total'] and receipt['replay']['imported']==0
        and len({receipt[k]['export_sha256'] for k in ('preflight','apply','replay')})==1
        and receipt['replay']['already_present']==receipt['apply']['imported']+receipt['apply']['already_present']
        else 'review_required')
    output=destination/'receipt.json';output.write_text(json.dumps(receipt,indent=2)+'\n');output.chmod(0o600)
    return receipt


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source-state','catalog','export','destination'):parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    try:
        result=reconcile(args.source_state,args.catalog,args.export,args.destination)
        print(json.dumps({'status':result['status'],'imported':result['apply']['imported'],
            'already_present':result['apply']['already_present'],
            'quarantined_count':result['apply']['quarantined_count']}))
        return 0 if result['status']=='reconciled' else 1
    except Exception as error:
        print(json.dumps({'status':'error','category':type(error).__name__,
            'message':'Reconciliation incomplete; live state unchanged. Inspect private inputs and isolated destination.'}))
        return 1

if __name__=='__main__':raise SystemExit(main())
