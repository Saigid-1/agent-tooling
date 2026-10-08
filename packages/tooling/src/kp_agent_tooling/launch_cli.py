"""kp-agent-launch: bind one harness launch to one registry desk; no model calls.

prepare (an operator or launcher act) reads one JSON request on stdin:
  {harness, provider, model, desk_id, workspace, task_id, source, parent_session_id}
and prints {receipt_path, native_session_id, argv_additions, env_additions, files}.
An unknown or disabled harness exits 3 with category harness_unavailable; any
other refusal exits 1. Errors are printed as JSON on stdout.

hook (run by the harness) reads one hook payload on stdin. It prints {} on
stdout, which every supported harness treats as no instruction, and its result
or refusal as JSON on stderr. A refusal exits 1.

ingest-spool --root <spool> applies hook semantics to the events host hooks
(kp-agent-host) appended under the spool, once (--once, the default), or with --watch every
--interval-seconds. With --watch, a command after `--` runs as a supervised
child and the watcher exits with its status. Each pass prints a JSON summary.
"""
import argparse
import json
import sys
from pathlib import Path

from kp_agent_tooling._impl import leaf

_SHOWN = ('ValueError', 'DeskLaunchUnavailable', 'ClaudeCaptureConflict', 'RolloutCaptureConflict',
          'HookCaptureIncomplete', 'EpisodeUnavailable')


def _read():
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        raise ValueError('request exceeds 64 KiB')
    return json.loads(raw)


def _error(error, category=None):
    # Messages of these classes carry no paths or transcript text.
    shown = isinstance(error, ValueError) or type(error).__name__ in _SHOWN
    return {'status': 'error', 'category': category or leaf.error_category(error),
            'message': str(error) if shown else 'Launch binding unavailable; inspect the operator configuration.'}


def _ingest(parser, args, command):
    if command and not args.watch:
        parser.error('a supervised command after -- requires --watch')
    if not 0.1 <= args.interval_seconds <= 3600:
        parser.error('--interval-seconds must be in 0.1..3600')
    from kp_agent_tooling._impl.service.spool_ingest import ingest, watch
    if args.watch:
        return watch(args.root, interval=args.interval_seconds, command=command or None)
    try:
        result = ingest(args.root)
    except Exception as error:
        print(json.dumps(_error(error)))
        return 1
    print(json.dumps(result))
    return 1 if result['status'] == 'error' else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    command = []
    if '--' in argv:
        index = argv.index('--')
        argv, command = argv[:index], argv[index + 1:]
    parser = argparse.ArgumentParser(prog='kp-agent-launch', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', type=Path, help='registry operator configuration (prepare)')
    parser.add_argument('--receipt', type=Path, help='launch receipt (hook)')
    parser.add_argument('--root', type=Path, help='host adapter spool root (ingest-spool)')
    parser.add_argument('--watch', action='store_true', help='keep ingesting (ingest-spool)')
    parser.add_argument('--once', action='store_true', help='one pass, the default (ingest-spool)')
    parser.add_argument('--interval-seconds', type=float, default=2.0, help='watch interval (ingest-spool)')
    parser.add_argument('action', choices=('prepare', 'hook', 'ingest-spool'))
    args = parser.parse_args(argv)
    if (args.action == 'prepare') != (args.config is not None):
        parser.error('--config is required by, and only valid with, prepare')
    if (args.action == 'hook') != (args.receipt is not None):
        parser.error('--receipt is required by, and only valid with, hook')
    if (args.action == 'ingest-spool') != (args.root is not None):
        parser.error('--root is required by, and only valid with, ingest-spool')
    if args.action != 'ingest-spool' and (args.watch or args.once or command):
        parser.error('--watch, --once and a supervised command are only valid with ingest-spool')
    if args.watch and args.once:
        parser.error('--watch and --once exclude each other')
    if args.action == 'ingest-spool':
        return _ingest(parser, args, command)
    from kp_agent_tooling._impl.service.harness_profiles import HarnessUnavailable
    from kp_agent_tooling._impl.service.launch_binding import hook, prepare
    if args.action == 'prepare':
        try:
            result = prepare(args.config, _read())
        except HarnessUnavailable as error:
            print(json.dumps(_error(error, 'harness_unavailable')))
            return 3
        except Exception as error:
            print(json.dumps(_error(error)))
            return 1
        print(json.dumps(result))
        return 0
    try:
        result = hook(args.receipt, _read())
    except Exception as error:
        print(json.dumps(_error(error)), file=sys.stderr)
        return 1
    print('{}')
    print(json.dumps(result), file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
