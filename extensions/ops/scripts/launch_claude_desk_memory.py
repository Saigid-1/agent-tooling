#!/usr/bin/env python3
"""Launch the portable memory MCP for the current Claude host session.

The host provides CLAUDE_CODE_HOST_SESSION_ID. This launcher selects only the
matching operator-owned session config; it cannot create a desk admission or
select another session. Docker then performs the normal admission check.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile

_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_MAX_CONFIG_BYTES = 262144


class LaunchError(ValueError):
    pass


def _private_root(value: str) -> Path:
    root = Path(value)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise LaunchError("session config root must be an existing physical absolute directory")
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise LaunchError("session config root must be owned by this user and private (0700)")
    return root


def _private_config(path: Path) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise LaunchError("no operator session config exists for this Claude host session") from error
    except OSError as error:
        raise LaunchError("operator session config is unavailable") from error
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > _MAX_CONFIG_BYTES):
            raise LaunchError("operator session config must be an owned private regular file (0600, at most 256 KiB)")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read(_MAX_CONFIG_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > _MAX_CONFIG_BYTES:
        raise LaunchError("operator session config exceeds 256 KiB")
    try:
        config = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise LaunchError("operator session config is invalid JSON") from error
    if not isinstance(config, dict) or config.get("schema_version") != "ops.desk-memory.local.v1":
        raise LaunchError("operator session config has an unsupported schema")
    return config


def command_for_session(*, session_config_root: str, docker_command: str,
                        container: str, container_config_root: str,
                        environment: dict[str, str] | None = None) -> tuple[str, list[str]]:
    values = os.environ if environment is None else environment
    session = values.get("CLAUDE_CODE_HOST_SESSION_ID", "")
    if not isinstance(session, str) or not _SESSION.fullmatch(session):
        raise LaunchError("a valid CLAUDE_CODE_HOST_SESSION_ID is required before memory can start")
    root = _private_root(session_config_root)
    path = root / (session + ".json")
    config = _private_config(path)
    if config.get("provider_session_id") != session:
        raise LaunchError("operator config belongs to a different Claude host session")
    docker = Path(docker_command)
    if not docker.is_absolute() or docker.is_symlink() or not docker.is_file() or not os.access(docker, os.X_OK):
        raise LaunchError("docker command must be an existing absolute executable")
    if not _CONTAINER.fullmatch(container):
        raise LaunchError("invalid container name")
    container_root = PurePosixPath(container_config_root)
    if (not container_root.is_absolute() or str(container_root) == "/"
            or any(part in {".", ".."} for part in container_config_root.split("/"))
            or "//" in container_config_root):
        raise LaunchError("container config root must be a canonical absolute directory")
    container_path = str(container_root / (session + ".json"))
    return str(docker), [str(docker), "exec", "-i", container,
                         "kp-agent-memory", "--config", container_path, "serve"]


def prepare_session(*, selection_root: str | None = None, runner=subprocess.run,
                    **options) -> tuple[str, list[str], dict]:
    """Recover only a private, exact-session host selection and verify admission."""
    values = os.environ if options.get('environment') is None else options['environment']
    session = values.get('CLAUDE_CODE_HOST_SESSION_ID', '')
    if not isinstance(session, str) or not _SESSION.fullmatch(session):
        raise LaunchError('a valid CLAUDE_CODE_HOST_SESSION_ID is required before memory can start')
    root = _private_root(options['session_config_root'])
    config_path = root / (session + '.json')
    selection = None
    if selection_root is not None:
        selected_root = _private_root(selection_root)
        selected_path = selected_root / (session + '.selection.json')
        try:
            selection = _private_config_selection(selected_path, session)
        except LaunchError as error:
            if not config_path.exists():
                raise LaunchError('no trusted host selection for this session; have the operator select and admit a desk, then persist its selection') from error
            raise
    created = False
    created_identity = None
    temporary = None
    if not config_path.exists():
        if selection is None:
            raise LaunchError('no admitted session config; have the operator select and admit a desk for this host session')
        data = json.dumps(selection['config'], sort_keys=True, separators=(',', ':')).encode()
        with tempfile.NamedTemporaryFile(dir=root, prefix='.resume-', delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
        try:
            try:
                os.link(temporary, config_path)
                created = True
                info = config_path.stat()
                created_identity = (info.st_dev, info.st_ino)
            except FileExistsError:
                # Another same-session resume may have finished creating it.
                if _private_config(config_path) != selection['config']:
                    raise LaunchError('concurrent session config conflicts with host selection')
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        docker, argv = command_for_session(**options)
        config = _private_config(config_path)
        if selection is not None and config != selection['config']:
            raise LaunchError('host selection conflicts with this session config')
        base = [docker, 'exec', '-i', options['container'], 'kp-agent-desk',
                '--config', argv[-2]]
        if selection is not None:
            _invoke_desk(runner, base + ['admit', '--desk-id', selection['desk_id'],
                '--provider-id', selection['provider_id'], '--model-id', selection['model_id']])
        context = _invoke_desk(runner, base + ['context'])
        if (context.get('project', {}).get('tenant_id') is None or
                context.get('project', {}).get('repo_key') is None or
                context.get('desk', {}).get('role_id') is None):
            raise LaunchError('desk context receipt is incomplete')
        if selection is not None and any((context['project']['tenant_id'] != selection['tenant_id'],
                context['project']['repo_key'] != selection['repo_key'],
                context['desk']['role_id'] != selection['role'],
                context['desk']['binding_id'] != selection['binding_key'])):
            raise LaunchError('admitted desk context conflicts with host selection')
        receipt = {'status':'bound', 'provider_session_id':session,
                   'binding_key':context['desk']['binding_id'],
                   'tenant_id':context['project']['tenant_id'],
                   'repo_key':context['project']['repo_key'], 'role':context['desk']['role_id'],
                   'recovered':created}
        return docker, argv, receipt
    except Exception:
        if created and created_identity is not None:
            try:
                info = config_path.lstat()
                if (info.st_dev, info.st_ino) == created_identity:
                    config_path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        # Keep our hard-link witness alive through verification. VirtioFS may
        # remap the surviving inode after the temporary link is removed.
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _private_config_selection(path: Path, session: str) -> dict:
    selection = _read_private_json(path)
    expected = {'schema_version','config','desk_id','provider_id','model_id',
                'binding_key','tenant_id','role','repo_key'}
    if not isinstance(selection, dict) or set(selection) != expected or selection['schema_version'] != 'ops.desk-host-selection.v1':
        raise LaunchError('invalid trusted host selection')
    config = selection['config']
    if (not isinstance(config, dict) or set(config) != {'schema_version','state_root','catalog_path',
            'workspace_root','provider_instance','provider_session_id'} or
            config['schema_version'] != 'ops.desk-memory.local.v1' or
            config['provider_session_id'] != session):
        raise LaunchError('host selection belongs to a different session')
    for key in ('desk_id','provider_id','model_id','binding_key','tenant_id','role','repo_key'):
        if not isinstance(selection[key], str) or not selection[key] or len(selection[key]) > 512:
            raise LaunchError('host selection has invalid coordinates')
    coords = '|'.join(selection[key] for key in ('tenant_id','role','repo_key'))
    if ('|' in selection['tenant_id'] or '|' in selection['role'] or '|' in selection['repo_key'] or
            selection['binding_key'] != 'binding:' + hashlib.sha256(coords.encode()).hexdigest()):
        raise LaunchError('host selection binding key is inconsistent')
    return selection


def _read_private_json(path: Path) -> dict:
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > _MAX_CONFIG_BYTES:
                raise LaunchError('host selection must be an owned private regular file')
            raw = stream.read(_MAX_CONFIG_BYTES + 1)
            if len(raw) > _MAX_CONFIG_BYTES:
                raise LaunchError('trusted host selection exceeds 256 KiB')
            return json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LaunchError('trusted host selection is unavailable or invalid') from error


def _invoke_desk(runner, argv: list[str]) -> dict:
    try:
        result = runner(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30, check=False)
        if result.returncode:
            raise LaunchError('desk admission is unavailable or conflicts with this host selection')
        body = json.loads(result.stdout)
        if not isinstance(body, dict):
            raise ValueError('object receipt required')
        return body
    except LaunchError:
        raise
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        raise LaunchError('desk admission could not be verified; inspect the operator desk configuration') from error


def serve_setup_error(detail: str, *, session_config_root: str, reader=None, writer=None, retry_options=None, retry=None) -> int:
    """Stable-catalog MCP while setup is unavailable; retry exact admission per call.

    Discovery grants nothing. Only successful host preflight can forward an operation
    to the backend. This host boundary uses stdlib, not the MCP SDK or graph runtime.
    """
    reader = sys.stdin.buffer if reader is None else reader
    writer = sys.stdout if writer is None else writer
    session = os.environ.get('CLAUDE_CODE_HOST_SESSION_ID', '')
    valid_session = bool(_SESSION.fullmatch(session))
    if not valid_session:
        guidance = 'The trusted host must supply CLAUDE_CODE_HOST_SESSION_ID. Correct the host launch environment, then retry the memory tool.'
    elif 'no admitted session config' in detail or 'no trusted host selection' in detail:
        guidance = 'Have the operator configure and admit this exact host session through portable memory, then retry the memory tool. Board-memory claims are separate and do not configure this server.'
    else:
        guidance = 'Have the operator repair the named configuration or runtime fault and verify this session with kp-agent-memory doctor, then retry the memory tool. Do not reinitialize storage or infer admission from board-memory claims.'
    diagnostic = {'status':'not_ready', 'category':'portable_memory_setup_required',
        'observation':'startup_refusal', 'reconnect_required':False,
        'detail':detail, 'provider_session_id':session if valid_session else None,
        'expected_session_config':str(Path(session_config_root) / (session + '.json')) if valid_session else None,
        'guidance':guidance,
        'authority':'diagnostic only; no memory access or admission granted'}
    tool = {'name':'memory.connection_status',
        'description':'Explain why portable memory setup was refused. This diagnostic cannot read memory, admit a session, or change a desk. Retry after operator repair.',
        'inputSchema':{'type':'object','properties':{},'additionalProperties':False}}
    # Generated from the canonical backend schemas; shipped beside this stdlib host script.
    catalog = json.loads(Path(__file__).with_name('memory_tools.json').read_text())
    names = {entry['name'] for entry in catalog}
    initialized = False

    def send(message):
        writer.write(json.dumps(message, ensure_ascii=True) + '\n')
        writer.flush()

    while True:
        raw = reader.readline(65537)
        if not raw: return 0
        if len(raw) > 65536:
            while raw and not raw.endswith(b'\n'):
                raw = reader.readline(65537)
            send({'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Diagnostic request exceeds 64 KiB'}})
            continue
        try:
            request = json.loads(raw)
        except (ValueError, UnicodeError):
            send({'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Invalid JSON'}})
            continue
        if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or not isinstance(request.get('method'), str):
            send({'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Invalid request'}})
            continue
        if 'id' not in request: continue
        identity = request['id']
        if type(identity) not in (str, int):
            send({'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Invalid request identity'}})
            continue
        method, params = request['method'], request.get('params', {})
        if not isinstance(params, dict):
            send({'jsonrpc':'2.0','id':identity,'error':{'code':-32602,'message':'Object parameters required'}})
            continue
        if method == 'initialize':
            version = params.get('protocolVersion')
            supported = ('2024-11-05','2025-03-26','2025-06-18','2025-11-25')
            result = {'protocolVersion':version if version in supported else supported[-1],
                      'capabilities':{'tools':{}},
                      'serverInfo':{'name':'ops-desk-memory-setup','version':'1.0.0'},
                      'instructions':'Memory tools are discoverable before admission but refuse access. Each call rechecks exact-session setup. Tool presence grants no authority.'}
            initialized = True
        elif not initialized:
            send({'jsonrpc':'2.0','id':identity,'error':{'code':-32600,'message':'Initialize the diagnostic connection first'}})
            continue
        elif method == 'ping': result = {}
        elif method == 'tools/list': result = {'tools':catalog}
        elif method == 'tools/call':
            if params.get('name') == tool['name'] and params.get('arguments', {}) != {}:
                send({'jsonrpc':'2.0','id':identity,'error':{'code':-32602,'message':'Diagnostic takes no arguments and cannot select a session'}})
                continue
            name = params.get('name')
            value = diagnostic
            failed = name != tool['name']
            if name not in names:
                value = {'status':'error','category':'unknown_operation'}
                failed = True
            elif retry_options is not None:
                try:
                    command, parameters, receipt = (retry or prepare_session)(**retry_options)
                    arguments = params.get('arguments', {})
                    if not isinstance(arguments, dict):
                        raise LaunchError('tool arguments must be an object')
                    completed = subprocess.run(parameters[:-1] + ['call','--tool',name,'--arguments',json.dumps(arguments)],
                        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=45)
                    if len(completed.stdout) > 2_000_000:
                        raise LaunchError('memory response exceeds host bound')
                    value = json.loads(completed.stdout)
                    failed = completed.returncode != 0 or value.get('status') == 'error'
                except LaunchError as error:
                    value = {**diagnostic, 'observation':'current_preflight_refusal', 'detail':str(error)}
                    failed = name != tool['name']
                except (OSError, ValueError, subprocess.SubprocessError):
                    value = {'status':'not_ready', 'category':'memory_backend_unavailable',
                             'observation':'current_call_failure', 'authorization_changed':False,
                             'guidance':'Check the portable backend; no operation success is established.'}
                    failed = name != tool['name']
            result = {'content':[{'type':'text','text':json.dumps(value)}],
                      'structuredContent':value, 'isError':failed}
        else:
            send({'jsonrpc':'2.0','id':identity,'error':{'code':-32601,'message':'Method not found'}})
            continue
        send({'jsonrpc':'2.0','id':identity,'result':result})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-config-root", required=True)
    parser.add_argument("--selection-root", help="Private host-owned selection records for exact-session recovery")
    parser.add_argument("--docker-command", required=True)
    parser.add_argument("--container", default="ops-agent-tooling")
    parser.add_argument("--container-config-root", default="/config/sessions")
    args = parser.parse_args(argv)
    try:
        command, parameters, receipt = prepare_session(
            session_config_root=args.session_config_root,
            selection_root=args.selection_root,
            docker_command=args.docker_command,
            container=args.container,
            container_config_root=args.container_config_root)
        print('memory binding receipt: ' + json.dumps(receipt, sort_keys=True), file=sys.stderr)
        os.execv(command, parameters)
    except LaunchError as error:
        print(f"memory launcher refused: {error}", file=sys.stderr)
        return serve_setup_error(str(error), session_config_root=args.session_config_root, retry_options=vars(args))
    except OSError:
        print("memory launcher refused: Docker execution unavailable", file=sys.stderr)
        return serve_setup_error("Docker execution unavailable", session_config_root=args.session_config_root, retry_options=vars(args))


if __name__ == "__main__":
    raise SystemExit(main())
