#!/usr/bin/env python3
"""Plan, apply, prepare and verify an agent-tooling runtime root from the single manifest.

plan     prints the reviewed plan (with plan_sha256) and writes nothing.
apply    writes exactly that plan into the runtime root, or refuses before any write.
         It never calls Docker.
prepare  after apply, before up: creates the project's store volume <project>_memory,
         owned by the container user with mode 0700, and moves pre-T9b store directories
         into it once, in one-off containers of the runtime image (never pulled). It
         refuses while any container of the project runs (docs/DOCKER.md, "The memory store").
verify   re-hashes the root against its receipt.json and reports drift, and lists per
         selected role the operator files still absent, and the store (readiness).

Nothing is pulled, and no role is started or registered. Start with `docker compose`
afterwards.
"""
import argparse
import json
import os

from kp_agent_tooling._impl.runtime_install import ApplyFailed, InstallRefused, apply, plan, prepare, verify


def _parser():
    parser = argparse.ArgumentParser(prog='kp-agent-install', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    actions = parser.add_subparsers(dest='action', required=True)
    for name in ('plan', 'apply'):
        command = actions.add_parser(name)
        command.add_argument('--runtime-root', required=True,
                             help='existing physical directory: empty, or a previous installation')
        command.add_argument('--image', required=True,
                             help='digest only: registry/name@sha256:<64 hex> or sha256:<64 hex>')
        command.add_argument('--repository', action='append', default=[], metavar='KEY=ABSOLUTE_PATH',
                             help='repeatable; the root of a committed Git repository')
        command.add_argument('--components', default='tooling',
                             help='tooling[,refresh][,capture][,board][,telemetry][,summarizer] (default: '
                                  'tooling; summarizer is selected only when named)')
        command.add_argument('--uid', default=str(os.getuid()), help='container UID, never 0 '
                             '(default: the invoking user)')
        command.add_argument('--gid', default=str(os.getgid()), help='container GID, never 0 '
                             '(default: the invoking group)')
        command.add_argument('--board-port', default='3487', help='loopback board port, 1024..65535')
        command.add_argument('--project-name', default='agent-tooling', help='Compose project name')
        command.add_argument('--transcript-root', action='append', default=[], metavar='ABSOLUTE_PATH',
                             help='repeatable; native transcript root mounted read-only into capture')
        command.add_argument('--home', metavar='ABSOLUTE_PATH',
                             help='existing directory that ~/ in the harness profiles expands against; '
                                  'a plan input (inputs.home). Default: the invoking user\'s $HOME')
        command.add_argument('--board-agents', action='store_true',
                             help='for the agents image: mount every --repository read-write at its host '
                                  'path into the board role only, so board tasks can add worktrees there; '
                                  'requires the board component (inputs.board_agents)')
        if name == 'apply':
            command.add_argument('--expected-plan-sha256', required=True,
                                 help='plan_sha256 of the reviewed plan')
    command = actions.add_parser('prepare', help='create and own the store volume, and migrate pre-T9b '
                                 'store directories into it (after apply, before up)')
    command.add_argument('--runtime-root', required=True)
    command = actions.add_parser('verify')
    command.add_argument('--runtime-root', required=True)
    # Accepted so one argument set serves every action; verify reads only the receipt.
    for option in ('--image', '--components', '--uid', '--gid', '--board-port', '--project-name',
                   '--expected-plan-sha256', '--home'):
        command.add_argument(option, help=argparse.SUPPRESS)
    for option in ('--repository', '--transcript-root'):
        command.add_argument(option, action='append', help=argparse.SUPPRESS)
    command.add_argument('--board-agents', action='store_true', default=None, help=argparse.SUPPRESS)
    return parser


IGNORED_BY_VERIFY = ('image', 'components', 'uid', 'gid', 'board_port', 'project_name',
                     'expected_plan_sha256', 'repository', 'transcript_root', 'home', 'board_agents')


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.action == 'prepare':
            result = prepare(args.runtime_root)
        elif args.action == 'verify':
            result = verify(args.runtime_root)
            ignored = ['--' + name.replace('_', '-') for name in IGNORED_BY_VERIFY
                       if getattr(args, name) is not None]
            if ignored:
                result['ignored_options'] = ignored
        else:
            options = dict(runtime_root=args.runtime_root, image=args.image,
                           repositories=args.repository, components=args.components,
                           uid=args.uid, gid=args.gid, board_port=args.board_port,
                           project_name=args.project_name, transcript_roots=args.transcript_root,
                           home=args.home, board_agents=args.board_agents)
            result = (plan(**options) if args.action == 'plan'
                      else apply(args.expected_plan_sha256, **options))
    except (InstallRefused, ApplyFailed) as stopped:
        print(json.dumps(stopped.result(), indent=2, sort_keys=True))
        return 1
    except OSError as error:
        # Reading the root or a repository failed; apply had not started writing.
        print(json.dumps({'status': 'failed', 'action': args.action, 'error': type(error).__name__,
                          'detail': str(error)[:300], 'writes': 'none'}, indent=2, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result.get('status') == 'drift' else 0


if __name__ == '__main__':
    raise SystemExit(main())
