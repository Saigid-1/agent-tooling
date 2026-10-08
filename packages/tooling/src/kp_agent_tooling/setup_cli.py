#!/usr/bin/env python3
"""Configure local agent tools without running product hooks or changing a host."""
import argparse
import json
from pathlib import Path
import shutil
import sys

from kp_agent_tooling._impl.workspace_setup import apply, catalog, plan
from kp_agent_tooling._impl.workspace_setup_server import WorkspaceSetupServer


def _main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['catalog', 'plan', 'apply', 'serve'])
    parser.add_argument('--request')
    parser.add_argument('--repository', help='absolute root of a committed Git repository')
    parser.add_argument('--repo-key', help='name used in source tool calls')
    parser.add_argument('--output-root', help='private directory for setup bundles and search cursors')
    parser.add_argument('--launcher-command', help='absolute installed kp-agent-tooling executable')
    parser.add_argument('--launcher-arg', action='append', default=[], help='repeat for launcher arguments')
    parser.add_argument('--expected-plan-sha256')
    parser.add_argument('--port', type=int, default=0, help='loopback port for serve (default: random)')
    args = parser.parse_args()
    if args.action == 'serve':
        with WorkspaceSetupServer(args.port) as server:
            print(server.launch_url, flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        return
    if args.action == 'catalog':
        result = catalog()
    else:
        direct = any((args.repository, args.repo_key, args.output_root,
                      args.launcher_command, args.launcher_arg))
        if args.request and direct:
            parser.error('choose --request or direct repository options')
        if args.request:
            with Path(args.request).open('rb') as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                parser.error('request exceeds 16KiB')
            request = json.loads(raw)
        else:
            if not all((args.repository, args.repo_key, args.output_root)):
                parser.error('--repository, --repo-key and --output-root required')
            launcher = args.launcher_command or shutil.which('kp-agent-tooling')
            if not launcher:
                parser.error('kp-agent-tooling executable unavailable; supply --launcher-command')
            request = {'schema_version': 'ops.workspace-setup.v1',
                       'repository': args.repository, 'repo_key': args.repo_key,
                       'output_root': args.output_root,
                       'launcher': {'command': launcher, 'args': args.launcher_arg},
                       'capabilities': ['source-navigation']}
        if args.action == 'apply' and not args.expected_plan_sha256:
            parser.error('--expected-plan-sha256 required')
        result = plan(request) if args.action == 'plan' else apply(request, args.expected_plan_sha256)
    print(json.dumps(result, indent=2))


def main():
    try:
        _main()
        return 0
    except (ValueError, OSError) as error:
        print(f'workspace setup: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
