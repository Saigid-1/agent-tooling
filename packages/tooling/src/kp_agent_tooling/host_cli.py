"""kp-agent-host: run the Claude or Codex CLI on the host, bound to a desk; spool its hooks.

  kp-agent-host [--config ADAPTER] launch <harness> --desk <desk_id> [--provider P] [--model M] [-- <cli args>]
  kp-agent-host [--config ADAPTER] hook --launch <id>
  kp-agent-host [--config ADAPTER] bind <harness> --native-session-id <id> --desk <desk_id>
  kp-agent-host [--config ADAPTER] install-hooks --project <repo> --harness claude|codex --desk <desk_id>
  kp-agent-host [--config ADAPTER] uninstall-hooks --project <repo> --harness claude|codex
  kp-agent-host runtime            (runtime side of docker mode; one JSON request on stdin)

ADAPTER is an agent-tooling.host-adapter.v1 file (--config, else KP_AGENT_HOST_CONFIG).
launch prepares a T3 launch with source "host", then execs the CLI with your arguments
unchanged plus the session, memory MCP and hook injections. hook only appends its
payload to the launch's spool file; it prints {} on stdout and never blocks the harness.
bind, install-hooks and uninstall-hooks print JSON on stdout; a refusal exits 1.
"""
import argparse
import json
import os
import sys

_HARNESS_UNAVAILABLE = ('harness_unavailable', 'HarnessUnavailable')


def _parser():
    parser = argparse.ArgumentParser(prog='kp-agent-host', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', help='host adapter config (default: $KP_AGENT_HOST_CONFIG)')
    actions = parser.add_subparsers(dest='action', required=True)

    def action(name, help_text):
        command = actions.add_parser(name, help=help_text)
        command.add_argument('--config', dest='action_config', help=argparse.SUPPRESS)
        return command

    launch = action('launch', 'prepare a bound launch and exec the CLI')
    launch.add_argument('harness')
    launch.add_argument('--desk', required=True)
    launch.add_argument('--provider')
    launch.add_argument('--model')
    hook = action('hook', 'append one hook payload (stdin) to the launch spool')
    hook.add_argument('--launch', required=True)
    bind = action('bind', 'operator bind of a session not launched through the adapter')
    bind.add_argument('harness')
    bind.add_argument('--native-session-id', required=True)
    bind.add_argument('--desk', required=True)
    bind.add_argument('--provider')
    bind.add_argument('--model')
    bind.add_argument('--workspace', help='default: the current directory')
    install = action('install-hooks', 'record a project policy and merge its hooks')
    install.add_argument('--project', required=True)
    install.add_argument('--harness', required=True, choices=('claude', 'codex'))
    install.add_argument('--desk', required=True)
    uninstall = action('uninstall-hooks', 'revoke the project policy and restore the original file')
    uninstall.add_argument('--project', required=True)
    uninstall.add_argument('--harness', required=True, choices=('claude', 'codex'))
    uninstall.add_argument('--desk', help='refuse unless the installed policy names this desk')
    actions.add_parser('runtime', help='runtime-side operation (used through docker exec)')
    return parser


def _error(error):
    from kp_agent_tooling._impl.service.host_adapter import HostRefused, RuntimeUnavailable
    category = getattr(error, 'category', None) or type(error).__name__
    shown = isinstance(error, (ValueError, RuntimeUnavailable))
    return {'status': 'error', 'category': category,
            'message': str(error)[:500] if shown else 'Host adapter unavailable; inspect the adapter configuration.'}


def _hook(config, launch_id):
    from kp_agent_tooling._impl.service.host_adapter import READ_BYTES, append_event
    raw = sys.stdin.buffer.read(READ_BYTES + 1)
    try:
        result = append_event(config, launch_id, raw)
    except Exception as error:  # never exit 2: a hook refusal must not block the harness
        print('{}')
        print(json.dumps(_error(error)), file=sys.stderr)
        return 1
    print('{}')
    print(json.dumps(result), file=sys.stderr)
    return 0


def _runtime():
    from kp_agent_tooling._impl.service.host_adapter import runtime_call
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError('runtime request exceeds 64 KiB')
        result = runtime_call(json.loads(raw))
    except Exception as error:
        print(json.dumps(_error(error)))
        return 1
    print(json.dumps(result))
    return 0


def _action_index(argv):
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == '--config':
            index += 2
        elif token.startswith('-'):
            index += 1
        else:
            return index
    return None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cli_args = []
    # `launch ... -- <cli args>`: everything after the first `--` goes to the CLI verbatim.
    position = _action_index(argv)
    if position is not None and argv[position] == 'launch' and '--' in argv[position:]:
        index = argv.index('--', position)
        argv, cli_args = argv[:index], argv[index + 1:]
    args = _parser().parse_args(argv)
    config = getattr(args, 'action_config', None) or args.config
    if args.action == 'hook':
        return _hook(config, args.launch)
    if args.action == 'runtime':
        return _runtime()
    from kp_agent_tooling._impl.service import host_adapter
    if args.action == 'launch':
        try:
            executable, cli_argv, env, _ = host_adapter.plan_launch(
                config, args.harness, args.desk, provider=args.provider, model=args.model, user_args=cli_args)
        except Exception as error:
            report = _error(error)
            print(json.dumps(report), file=sys.stderr)
            return 3 if report['category'] in _HARNESS_UNAVAILABLE else 1
        os.execve(executable, cli_argv, env)
    try:
        if args.action == 'bind':
            result = host_adapter.operator_bind(config, args.harness, args.native_session_id, args.desk,
                                                provider=args.provider, model=args.model, workspace=args.workspace)
        elif args.action == 'install-hooks':
            result = host_adapter.install_hooks(config, args.project, args.harness, args.desk)
        else:
            result = host_adapter.uninstall_hooks(config, args.project, args.harness, args.desk)
    except Exception as error:
        report = _error(error)
        print(json.dumps(report))
        return 3 if report['category'] in _HARNESS_UNAVAILABLE else 1
    print(json.dumps(result))
    return 1 if result.get('status') == 'revoked_not_restored' else 0


if __name__ == '__main__':
    raise SystemExit(main())
