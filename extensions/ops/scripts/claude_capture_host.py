#!/usr/bin/env python3
"""Private operator-scoped Claude Desktop capture bridge; Python stdlib only."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.launch_claude_desk_memory import LaunchError, _read_private_json, command_for_session

EVENTS = ('Stop', 'PreCompact', 'SessionEnd')


def physical_file(value):
    if not isinstance(value, str):
        raise LaunchError('configured source path must be a string')
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path or not path.is_file():
        raise LaunchError('configured source must be an existing physical absolute file')
    if path.stat().st_uid != os.getuid():
        raise LaunchError('configured source must be owned by this user')
    return path


def container_path(value):
    if not isinstance(value, str) or not PurePosixPath(value).is_absolute() or str(PurePosixPath(value)) != value or '..' in PurePosixPath(value).parts:
        raise LaunchError('container paths must be canonical absolute paths')
    return value


def capture_command(config_path, payload, *, environment=None):
    config = _read_private_json(physical_file(str(config_path)))
    required = {'schema_version','host_session_id','native_session_id','registry_path','cwd',
                'host_transcript_path','container_transcript_path','session_config_root',
                'docker_command','container','container_config_root','capture_ledger','queue','telemetry'}
    if not isinstance(config, dict) or set(config) != required or config['schema_version'] != 'ops.claude-capture-host.v1':
        raise LaunchError('invalid operator capture configuration')
    if not isinstance(payload, dict) or payload.get('hook_event_name') not in EVENTS:
        raise LaunchError('unsupported capture lifecycle event')
    host_id, native_id = config['host_session_id'], config['native_session_id']
    if not isinstance(native_id, str) or not native_id or len(native_id) > 128:
        raise LaunchError('invalid configured native identity')
    values = os.environ if environment is None else environment
    observed_host = values.get('CLAUDE_CODE_HOST_SESSION_ID')
    if not observed_host:
        raise LaunchError('host session environment is missing; capture/index completion is not established')
    if observed_host != host_id:
        return None  # Shared project settings do not admit or capture other sessions.
    registry_path = physical_file(config['registry_path'])
    if registry_path.name != host_id + '.json' or registry_path.stat().st_size > 262144:
        raise LaunchError('registry file differs from configured host session')
    registry = json.loads(registry_path.read_bytes())
    if not isinstance(registry, dict) or any(registry.get(key) != value for key, value in
            (('sessionId',host_id), ('cliSessionId',native_id), ('cwd',config['cwd']))):
        raise LaunchError('host registry identity differs from operator mapping')
    transcript = physical_file(config['host_transcript_path'])
    if transcript.name != native_id + '.jsonl':
        raise LaunchError('transcript filename differs from native identity')
    if payload.get('session_id') != native_id or payload.get('transcript_path') != str(transcript):
        raise LaunchError('hook source differs from verified native session')
    if payload.get('cwd') != config['cwd']:
        raise LaunchError('hook working directory differs from verified registry')
    docker, base = command_for_session(session_config_root=config['session_config_root'],
        docker_command=config['docker_command'], container=config['container'],
        container_config_root=config['container_config_root'], environment=values)
    argv = [docker, 'exec', '-i', config['container'], 'kp-agent-claude-capture',
            '--memory-config', base[-2], '--native-session-id', native_id,
            '--hook-transcript-path', str(transcript)]
    for flag, key in (('transcript','container_transcript_path'), ('capture-ledger','capture_ledger'),
                      ('queue','queue'), ('telemetry','telemetry')):
        argv.extend(['--'+flag, container_path(config[key])])
    return argv + ['hook']


def run_hook(config_path, payload, *, environment=None, runner=subprocess.run):
    argv = capture_command(config_path, payload, environment=environment)
    if argv is None:
        return {'suppressOutput':True, 'status':'not_applicable', 'authorization_changed':False}
    try:
        result = runner(argv, input=json.dumps(payload), capture_output=True, text=True,
                        timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LaunchError('capture backend unavailable or timed out; capture/index completion is not established') from error
    if result.returncode:
        raise LaunchError('capture backend failed; capture/index completion is not established; inspect scoped backend status')
    try:
        receipt = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise LaunchError('capture backend returned an invalid receipt') from error
    if not isinstance(receipt, dict) or receipt.get('suppressOutput') is not True or receipt.get('status') == 'error':
        raise LaunchError('capture backend did not acknowledge the hook')
    return {'suppressOutput':True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--settings', action='store_true', help='Print scoped settings to merge; never installs them')
    args = parser.parse_args(argv)
    payload = None
    try:
        if args.settings:
            physical_file(str(args.config))
            command = shlex.join([sys.executable, str(Path(__file__).resolve()), '--config', str(args.config)])
            print(json.dumps({'hooks':{event:[{'hooks':[{'type':'command','command':command,'timeout':60}]}]
                                       for event in EVENTS}}, indent=2))
            return 0
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise LaunchError('hook payload exceeds 64 KiB')
        payload = json.loads(raw)
        print(json.dumps(run_hook(args.config, payload)))
        return 0
    except (LaunchError, OSError, ValueError, TypeError) as error:
        # Raw backend output and source content never enter hook output.
        detail = str(error) if isinstance(error, LaunchError) else 'capture configuration or payload is unavailable or invalid'
        print(json.dumps({'status':'error','category':'claude_capture_host_refused',
                          'message':detail,'authorization_changed':False}), file=sys.stderr)
        return 2 if isinstance(payload, dict) and payload.get('hook_event_name') == 'PreCompact' else 1


if __name__ == '__main__':
    raise SystemExit(main())
