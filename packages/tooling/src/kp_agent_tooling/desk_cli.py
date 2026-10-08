"""Human/host desk selection; never expose this command as a model tool."""
import argparse
import json
import sys
import sqlite3
from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.episodic_memory import EpisodeUnavailable
from kp_agent_tooling._impl.service.desk_binding import DeskLaunchUnavailable
from kp_agent_tooling._impl.service.desk_memory_runtime import initialize, admit, components, read_context, persist_host_selection

def _main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', help='Private desk-memory configuration (every action; indexer: or --root)')
    p.add_argument('action', choices=['initialize','desks','admit','context','index-history','import-native-history','upgrade-sources','grant-write-scope','revoke-write-scope','indexer'])
    for key in ('desk-id','provider-id','model-id'):
        p.add_argument('--'+key)
    p.add_argument('--approval-ref', help='Exact operator ruling for role repository write scope')
    p.add_argument('--selection-output', help='Persist this operator-approved host selection for exact-session resume')
    p.add_argument('--apply', action='store_true', help='Apply an import-native-history preview')
    p.add_argument('--runtime', choices=['claude','codex'])
    p.add_argument('--tenant-id')
    p.add_argument('--source-folder')
    p.add_argument('--source-file', action='append', dest='source_files')
    p.add_argument('--project')
    p.add_argument('--date', dest='day')
    p.add_argument('--import-actor', default='operator:native-history')
    p.add_argument('--cursor-json', help='JSON mapping selected file paths to prior resume_cursor values')
    p.add_argument('--batch-rows', type=int, default=500,
                   help='upgrade-sources: rows projected per transaction in the projection backfill only '
                        '(1..100000, default 500); coverage marks are always MARK_BATCH episodes per transaction')
    # The indexer (T12b): the one drainer of every store's index outbox. Compose's `indexer` role runs
    # `indexer --root /state/memory --watch --interval 2`; its health check runs `indexer --root ... --health`.
    p.add_argument('--root', help='indexer: drain every store under this directory (the store volume)')
    p.add_argument('--watch', action='store_true', help='indexer: drain every --interval seconds until a signal')
    p.add_argument('--interval', type=int, help='indexer --watch: seconds between passes (1..3600)')
    p.add_argument('--max-passes', type=int, help='indexer --watch: stop after this many passes')
    p.add_argument('--batch', type=int, help='indexer: outbox rows per transaction (1..2000, default 500)')
    p.add_argument('--health', action='store_true', help='indexer: report the role health (exit 1: unhealthy)')
    # Claude's native project folder names begin with a single dash. Preserve
    # actual options (notably --apply/-h) while accepting --project -Users-... .
    argv=list(sys.argv[1:] if argv is None else argv)
    for i in range(len(argv)-1):
        if argv[i]=='--project' and argv[i+1].startswith('-') and not argv[i+1].startswith('--') and argv[i+1]!='-h':
            argv[i:i+2]=['--project='+argv[i+1]]
            break
    args=p.parse_args(argv)
    if args.action=='indexer':
        return _indexer(p, args)
    if not args.config:p.error('--config is required')
    if args.apply and args.action != 'import-native-history':p.error('--apply is supported only for import-native-history')
    if args.action=='import-native-history':
        if not args.runtime or not args.tenant_id:
            p.error('import-native-history requires --runtime and --tenant-id')
        if bool(args.source_files) == bool(args.source_folder):
            p.error('select --source-file (repeatable) or --source-folder')
        from kp_agent_tooling._impl.service.native_history_import import import_native_history
        cursors=json.loads(args.cursor_json) if args.cursor_json else None
        configuration,registry,ledger,store=components(args.config)
        admission=ledger.resolve(configuration['provider_session_id'],registry)
        if args.tenant_id != admission.tenant_id:
            raise ValueError('native import tenant must match the admitted session tenant')
        result=import_native_history(store=store,tenant_id=admission.tenant_id,
            runtime=args.runtime,files=args.source_files,source_folder=args.source_folder,
            project=args.project,day=args.day,apply=args.apply,import_actor=args.import_actor,
            cursors=cursors)
    elif args.action=='upgrade-sources':
        # Idempotent, bounded operator backfill of the read-scope projection; it also
        # records which episodes the store's search index covers.
        from kp_agent_tooling._impl.service.session_sources import SessionSources
        from kp_agent_tooling._impl.service.episodic_search import drain_after_seal
        store=components(args.config)[3]
        result=SessionSources(store).upgrade(batch_rows=args.batch_rows,
            coverage_index=leaf.store_path(store.path, leaf.SEARCH_INDEX_DB, sibling=True))
        if result.get('coverage',{}).get('reindex_requested'):
            # The one-time request for an older-writer store (T12b): a host install drains it now.
            drain_after_seal(store)
    elif args.action=='index-history':
        # A request (T12b): a reindex outbox row the drainer builds and swaps in. A host install drains
        # it now, never waiting; inside Compose the indexer role does, within its interval.
        from kp_agent_tooling._impl.service.episodic_search import drain_after_seal, reindex_counts, request_reindex
        store=components(args.config)[3]
        request_reindex(store)
        result=reindex_counts(drain_after_seal(store))
    elif args.action in ('grant-write-scope','revoke-write-scope'):
        from kp_agent_tooling._impl.service.desk_write_scope import record_scope
        config,_,_,store=components(args.config)
        result=record_scope(store,config['provider_session_id'],enabled=args.action=='grant-write-scope',approval_ref=args.approval_ref)
    elif args.action=='initialize': result=initialize(args.config)
    elif args.action=='context': result=read_context(args.config)
    elif args.action=='desks':
        from dataclasses import asdict
        result=[asdict(d) for d in components(args.config)[1].list_bindings()]
    else:
        values={key:getattr(args,key) for key in ('desk_id','provider_id','model_id')}
        if not all(values.values()):p.error('admit requires desk-id, provider-id and model-id')
        if args.selection_output:
            selection = persist_host_selection(args.config, **values,
                selection_path=args.selection_output)
            result={'status':'admitted','binding_key':selection['binding_key'],
                    'dispatch':False,'context':read_context(args.config),'selection':selection}
        else:
            result=admit(args.config,**values)
    print(json.dumps(result))
    if isinstance(result,dict) and (result.get('quarantined_count',0) or result.get('counts',{}).get('quarantined',0)):
        raise SystemExit(2)


def _indexer(p, args):
    """The indexer: --root (every store under it; Compose's role) or --config (that store), once or --watch."""
    from kp_agent_tooling._impl.service import episodic_search as search
    if bool(args.root) == bool(args.config):
        p.error('indexer needs exactly one of --root or --config')
    if args.health:
        if not args.root:
            p.error('indexer --health reads the role health under --root')
        code, record = search.health(args.root)
        print(json.dumps(record, sort_keys=True))
        return code
    batch = search.DRAIN_BATCH if args.batch is None else args.batch
    if not 1 <= batch <= 2000:
        p.error('--batch must be 1..2000')
    if args.watch:
        if not args.root:
            p.error('indexer --watch drains --root (the indexer role)')
        if args.interval is None or not 1 <= args.interval <= 3600:
            p.error('indexer --watch needs --interval 1..3600')
        if args.max_passes is not None and args.max_passes < 1:
            p.error('--max-passes must be at least 1')
        return search.watch(args.root, args.interval, max_passes=args.max_passes, batch=batch)
    stores = search.stores_under(args.root) if args.root else [components(args.config)[3]]
    stalls = {}
    lines = [search.drain_line(store, batch, stalls) for store in stores]
    for line in lines:
        print(json.dumps(line, sort_keys=True))
    return 1 if any(line['status'] == 'error' for line in lines) else 0


def main(argv=None):
    try:
        code = _main(argv)
        return 0 if code is None else code
    except (ValueError, OSError, sqlite3.Error, EpisodeUnavailable, DeskLaunchUnavailable) as error:
        print(f'desk: {error}', file=sys.stderr)
        return 1

if __name__=='__main__':raise SystemExit(main())
