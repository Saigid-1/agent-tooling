"""Import legacy desk history or session catalogs into an initialized desk store.

The former ``kp-agent-desk import-history`` and ``import-sessions`` actions; run as
``python -m kp_agent_tooling_ops.desk_import_cli --config C import-history --export-path P
[--dry-run]`` or ``... import-sessions --export-path P``. Operator use only; never
expose this command as a model tool.
"""
import argparse
import json
import sys
import sqlite3
from kp_agent_tooling._impl.service.episodic_memory import EpisodeUnavailable
from kp_agent_tooling._impl.service.desk_binding import DeskLaunchUnavailable
from kp_agent_tooling._impl.service.desk_memory_runtime import components

def _main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    p.add_argument('action', choices=['import-history','import-sessions'])
    p.add_argument('--export-path')
    p.add_argument('--dry-run', action='store_true')
    args=p.parse_args(argv)
    if args.dry_run and args.action != 'import-history':p.error('--dry-run is supported only for import-history')
    if args.action=='import-history':
        if not args.export_path:p.error('import-history requires --export-path')
        from kp_agent_tooling_ops._impl.service.legacy_desk_import import import_export
        result=import_export(args.export_path,components(args.config)[3],dry_run=args.dry_run)
    else:
        if not args.export_path:p.error('import-sessions requires --export-path')
        from kp_agent_tooling_ops._impl.service.session_catalog_import import import_catalog
        result=import_catalog(args.export_path,components(args.config)[3])
    print(json.dumps(result))
    if isinstance(result,dict) and (result.get('quarantined_count',0) or result.get('counts',{}).get('quarantined',0)):
        raise SystemExit(2)
def main(argv=None):
    try:
        _main(argv)
        return 0
    except (ValueError, OSError, sqlite3.Error, EpisodeUnavailable, DeskLaunchUnavailable) as error:
        print(f'desk: {error}', file=sys.stderr)
        return 1

if __name__=='__main__':raise SystemExit(main())
