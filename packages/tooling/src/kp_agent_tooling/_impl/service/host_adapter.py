"""Host adapter (``agent-tooling.host-adapter.v1``): bind host-run Claude and Codex CLIs to desks.

The adapter config names the runtime that holds the desk state and receipts::

    {"schema_version": "agent-tooling.host-adapter.v1",
     "runtime": {"mode": "local" | "docker", "container": "<docker mode only>",
                 "config_path": "<registry operator config, as the runtime sees it>"},
     "spool_root": "<host directory the hooks append to>",
     "transcript_roots": ["<native transcript roots, host paths>"]}

``launch`` prepares a T3 launch with ``source: "host"`` (in-process for ``local``,
``docker exec -i <container> kp-agent-launch ... prepare`` for ``docker``), rewrites
the memory MCP server and the hooks for the host, and returns the argv and
environment to ``exec``. ``hook`` only appends the payload to
``<spool_root>/<launch_id>/events.jsonl``; it has no network, Docker or memory
store access, and nothing in it selects a desk. Spool ingestion
(``spool_ingest``) applies T3's hook semantics inside the runtime.

``install-hooks`` records an operator policy in the spool and merges the policy's
hook into the project's native configuration; ``uninstall-hooks`` revokes the
policy and restores the original file byte for byte. ``bind`` is an explicit
operator bind (``source: "operator"``).

Only the standard library is imported at module level, so the hook starts fast.
This is a single-owner deployment: the file owner is the authority.
"""
from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.leaf import sha256_hex as _sha

CONFIG_SCHEMA = 'agent-tooling.host-adapter.v1'
LAUNCH_SCHEMA = 'agent-tooling.host-launch.v1'
POLICY_SCHEMA = 'agent-tooling.host-policy.v1'
EVENT_SCHEMA = 'agent-tooling.host-spool-event.v1'
MODES = ('local', 'docker')
POLICY_HARNESSES = {'claude': ('.claude', 'settings.local.json'), 'codex': ('.codex', 'config.toml')}
# Codex runs a hook for at most its configured timeout; Kanban configures 5 s
# (apps/kanban/src/terminal/codex-hook-config.ts, CODEX_HOOK_TIMEOUT_SECONDS). The
# spool hook finishes well inside it, so both harnesses get the same value.
HOOK_TIMEOUT_SECONDS = 5
DEFAULT_TARGET = 'harness-default'
SERVER_NAME = 'kp_desk_memory'
EVENTS = 'events.jsonl'
LAUNCH_RECORD = 'launch.json'
POLICY_RECORD = 'policy.json'
# A payload up to the T3 hook bound is spooled verbatim; a larger one (a long
# prompt, say) is reduced to its identity fields. Anything larger than READ_BYTES
# is refused.
PAYLOAD_BYTES = 65536
READ_BYTES = 4 * 1024 * 1024
FIELD_CHARS = 4096
IDENTITY_FIELDS = ('hook_event_name', 'session_id', 'transcript_path', 'cwd', 'trigger', 'reason', 'source',
                   'stop_hook_active', 'turn_id', 'model', 'permission_mode', 'agent_id', 'agent_transcript_path')
LOCK_SECONDS = 0.5
RECORD_BYTES = 262144
MAX_SPOOL_ENTRIES = 10000
_UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
_CONTAINER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')


class HostRefused(ValueError):
    """The request, configuration or payload is refused; nothing was changed."""

    def __init__(self, message, category=None):
        super().__init__(message)
        self.category = category


class RuntimeUnavailable(RuntimeError):
    """The runtime (Docker or the local state) did not answer."""


def _now():
    return datetime.now(timezone.utc).isoformat()


# --- Files ---------------------------------------------------------------------------------------

# An existing ``kind`` ('file' or 'dir') owned by this user, not a symlink, not writable by others.
def _owned(path, kind):
    return leaf.owned_entry(path, kind, refuse=HostRefused)


def _private_dir(path):
    return leaf.owned_private_dir(path, refuse=HostRefused)


def _read_json(path, bound=RECORD_BYTES):
    return leaf.read_owned_json(path, bound, refuse=HostRefused)


# A new private (0600) JSON file; never overwrite.
def _write_new(path, value):
    return leaf.write_new_private(path, leaf.json_writer(value, newline=True, indent=1, sort_keys=True), text=True,
                                  fsync=True)


# Atomically replace ``path`` with ``data`` (bytes) at ``mode``.
def _replace(path, data, mode):
    return leaf.replace_file(Path(path), data, sibling_tag='kp-agent-host', temp_mode=mode, chmod_mode=mode,
                             cleanup='on-error')


# --- Adapter configuration ------------------------------------------------------------------------

def _absolute(value, name):
    if (not isinstance(value, str) or not value or len(value) > 4096 or _CONTROL.search(value)
            or not PurePosixPath(value).is_absolute()):
        raise HostRefused(f'{name} must be an absolute path')
    return value


def config_path(value=None):
    """The adapter config path: ``--config``, else ``KP_AGENT_HOST_CONFIG``."""
    value = value or os.environ.get('KP_AGENT_HOST_CONFIG')
    if not value:
        raise HostRefused('adapter config required: --config <path> or KP_AGENT_HOST_CONFIG')
    path = Path(value)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def load_config(path):
    """One exact ``agent-tooling.host-adapter.v1`` document; unknown fields are refused."""
    path = config_path(path)
    try:
        value = _read_json(path, 65536)
    except FileNotFoundError:
        raise HostRefused('adapter config not found') from None
    except (UnicodeError, json.JSONDecodeError):
        raise HostRefused('adapter config is not JSON') from None
    if (not isinstance(value, dict) or not {'runtime', 'spool_root', 'transcript_roots'} <= set(value)
            <= {'schema_version', 'runtime', 'spool_root', 'transcript_roots'}):
        raise HostRefused('adapter config requires exactly: runtime, spool_root, transcript_roots '
                          '(schema_version optional)')
    if value.get('schema_version', CONFIG_SCHEMA) != CONFIG_SCHEMA:
        raise HostRefused('unsupported adapter config schema')
    runtime = value['runtime']
    if not isinstance(runtime, dict) or not {'mode', 'config_path'} <= set(runtime) <= {'mode', 'config_path',
                                                                                         'container'}:
        raise HostRefused('runtime requires mode and config_path (container in docker mode)')
    if runtime['mode'] not in MODES:
        raise HostRefused('runtime mode must be local or docker')
    container = runtime.get('container')
    if runtime['mode'] == 'docker':
        if not isinstance(container, str) or not _CONTAINER.fullmatch(container):
            raise HostRefused('docker mode requires a container name')
    elif container is not None:
        raise HostRefused('local mode takes no container')
    roots = value['transcript_roots']
    if not isinstance(roots, list) or len(roots) > 64:
        raise HostRefused('transcript_roots must be a list of at most 64 absolute paths')
    return {'path': str(path), 'mode': runtime['mode'], 'container': container,
            'runtime_config': _absolute(runtime['config_path'], 'runtime config_path'),
            'spool_root': Path(_absolute(value['spool_root'], 'spool_root')),
            'transcript_roots': [Path(_absolute(root, 'transcript root')) for root in roots]}


def spool_root(config, *, create=False):
    root = config['spool_root']
    if create and not os.path.lexists(root):
        try:
            leaf.mkdir_private(root)
        except FileNotFoundError:
            raise HostRefused('spool_root parent does not exist') from None
    try:
        _owned(root, 'dir')
    except FileNotFoundError:
        raise HostRefused('spool_root does not exist') from None
    return root


def _launch_id(value):
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise HostRefused('launch id must be a canonical lowercase UUID')
    return value


def spool_dir(root, launch_id):
    directory = Path(root) / _launch_id(launch_id)
    try:
        _private_dir(directory)
    except FileNotFoundError:
        raise HostRefused('no launch or policy with this id in the spool') from None
    return directory


def read_record(directory):
    """(kind, record) of one spool directory: exactly one of launch.json or policy.json."""
    directory = Path(directory)
    present = [name for name in (LAUNCH_RECORD, POLICY_RECORD) if os.path.lexists(directory / name)]
    if len(present) != 1:
        raise HostRefused('spool directory must hold exactly one launch or policy record')
    record = _read_json(directory / present[0])
    kind = 'launch' if present[0] == LAUNCH_RECORD else 'policy'
    schema = LAUNCH_SCHEMA if kind == 'launch' else POLICY_SCHEMA
    if (not isinstance(record, dict) or record.get('schema_version') != schema
            or record.get('launch_id') != directory.name or record.get('kind') != kind):
        raise HostRefused('spool record does not name this directory')
    if kind == 'launch':
        _absolute(record.get('receipt'), 'receipt reference')
    elif record.get('status') not in ('active', 'revoked'):
        raise HostRefused('policy status must be active or revoked')
    return kind, record


def receipt_reference(kind, record):
    return record['receipt'] if kind == 'launch' else 'policy:' + record['launch_id']


# --- Hook (stdlib only; no network, Docker or memory store) --------------------------------------------

def _identity(payload):
    reduced = {}
    for name in IDENTITY_FIELDS:
        value = payload.get(name)
        if isinstance(value, bool) or value is None or isinstance(value, int):
            if name in payload:
                reduced[name] = value
        elif isinstance(value, str) and len(value) <= FIELD_CHARS:
            reduced[name] = value
    return reduced


def append_event(config_file, launch_id, raw):
    """Append one hook payload to its launch's spool file (append + fsync)."""
    config = load_config(config_file)
    directory = spool_dir(spool_root(config), launch_id)
    kind, record = read_record(directory)
    if kind == 'policy' and record['status'] != 'active':
        return {'status': 'not_applicable', 'reason': 'policy revoked', 'launch_id': launch_id}
    if len(raw) > READ_BYTES:
        raise HostRefused('hook payload exceeds its bound')
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise HostRefused('hook payload is not JSON') from None
    if not isinstance(payload, dict):
        raise HostRefused('hook payload must be a JSON object')
    reduced = len(raw) > PAYLOAD_BYTES
    event = {'schema_version': EVENT_SCHEMA, 'launch_id': launch_id, 'kind': kind,
             'receipt': receipt_reference(kind, record), 'received_at': _now(), 'payload_bytes': len(raw),
             'payload_reduced': reduced, 'payload': _identity(payload) if reduced else payload}
    line = (json.dumps(event, separators=(',', ':'), ensure_ascii=False) + '\n').encode()
    if len(line) > 4 * PAYLOAD_BYTES:  # JSON escapes can grow a bounded payload
        event.update(payload_reduced=True, payload=_identity(payload))
        line = (json.dumps(event, separators=(',', ':'), ensure_ascii=False) + '\n').encode()
    descriptor = leaf.open_fd(directory / EVENTS, access='w', append=True, create=True, nofollow=True, cloexec=True)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise HostRefused('spool file must be a regular file owned by the running user')
        deadline = time.monotonic() + LOCK_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeUnavailable('spool file busy') from None
                time.sleep(0.01)
        leaf.write_all(descriptor, line, fsync=True)
    finally:
        os.close(descriptor)
    return {'status': 'spooled', 'launch_id': launch_id, 'kind': kind, 'bytes': len(line),
            'payload_reduced': event['payload_reduced']}


# --- Runtime access --------------------------------------------------------------------------------

class Runtime:
    """The runtime that holds desk state: this process (local) or a container (docker)."""

    def __init__(self, config):
        self.mode, self.container = config['mode'], config['container']
        self.config_path = config['runtime_config']

    def prepare(self, request):
        if self.mode == 'local':
            from kp_agent_tooling._impl.service.harness_profiles import HarnessUnavailable
            from kp_agent_tooling._impl.service.launch_binding import prepare
            try:
                return prepare(self.config_path, request)
            except HarnessUnavailable as error:
                raise HostRefused(str(error), 'harness_unavailable') from None
        code, value = self._exec(['kp-agent-launch', '--config', self.config_path, 'prepare'], request)
        if code == 3:
            raise HostRefused(_message(value), 'harness_unavailable')
        if code != 0:
            raise HostRefused(_message(value), _category(value))
        return value

    def call(self, request):
        """One runtime operation (``runtime_call``), the same code in both modes."""
        if self.mode == 'local':
            return runtime_call(request)
        code, value = self._exec(['kp-agent-host', 'runtime'], request)
        if code != 0:
            raise HostRefused(_message(value), _category(value))
        return value

    def memory_server(self, memory_config):
        """The stdio desk memory MCP server the host CLI starts for this session."""
        if self.mode == 'local':
            from kp_agent_tooling._impl.service.launch_binding import memory_server
            return memory_server(memory_config)
        # MCP is stdio: -i, never -t.
        return {'command': 'docker', 'args': ['exec', '-i', self.container, 'kp-agent-memory', '--config',
                                              str(memory_config), 'serve']}

    def _exec(self, command, request, timeout=120):
        docker = shutil.which('docker')
        if docker is None:
            raise RuntimeUnavailable('docker CLI not found on PATH')
        try:
            done = subprocess.run([docker, 'exec', '-i', self.container, *command],
                                  input=json.dumps(request).encode(), capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeUnavailable(f'docker exec failed: {type(error).__name__}') from None
        try:
            value = json.loads(done.stdout)
        except (UnicodeError, json.JSONDecodeError):
            raise RuntimeUnavailable(f'runtime command {command[0]} exited {done.returncode} without a JSON '
                                     'answer; check that the container is running') from None
        return done.returncode, value


def _message(value):
    return value.get('message', 'runtime refused') if isinstance(value, dict) else 'runtime refused'


def _category(value):
    return value.get('category') if isinstance(value, dict) else None


def runtime_call(request):
    """Runtime-side operations behind ``kp-agent-host runtime``; run in-process for ``local``.

    ``harness-profile``: the enabled profile for a harness, as the runtime resolves it,
    after checking the desk; no side effect. ``bind``: an operator session binding.
    """
    from kp_agent_tooling._impl.service import harness_profiles
    from kp_agent_tooling._impl.service.desk_memory_runtime import components
    from kp_agent_tooling._impl.service.desk_registry import is_registry
    if not isinstance(request, dict):
        raise HostRefused('runtime request must be a JSON object')
    op = request.get('op')
    if op in ('harness-profile', 'bind'):
        fields = {'op', 'config', 'harness', 'desk_id'} | ({'request'} if op == 'bind' else set())
        if set(request) != fields:
            raise HostRefused('runtime request fields: ' + ', '.join(sorted(fields)))
        config_file = _absolute(request['config'], 'config')
        config, registry, _, _ = components(config_file)
        if not is_registry(registry):
            raise HostRefused('the host adapter requires an agent-tooling.desk-registry.v1 registry')
        profile = harness_profiles.select(harness_profiles.load(config['catalog_path']), request['harness'])
        context = registry.context(request['desk_id'])
        if context['desk']['desk_id'] != request['desk_id']:
            raise HostRefused('select a desk registered in this registry')
        if op == 'bind':
            from kp_agent_tooling._impl.service.session_bindings import bind
            body = request['request']
            if (not isinstance(body, dict) or body.get('source') != 'operator' or body.get('harness') != request['harness']
                    or body.get('desk_id') != request['desk_id']):
                raise HostRefused('operator bind request must name this harness and desk with source operator')
            return bind(config_file, body)
        root = harness_profiles.transcript_root(profile)
        return {'profile': profile, 'desk_id': request['desk_id'], 'binding_key': context['desk']['binding_id'],
                'transcript_root': str(root.resolve()) if root is not None else None}
    raise HostRefused('unknown runtime operation')


# --- Shared checks ---------------------------------------------------------------------------------

def _desk_id(value):
    try:
        canonical = 'desk:' + str(uuid.UUID(value.removeprefix('desk:'))) if isinstance(value, str) else None
    except ValueError:
        canonical = None
    if canonical is None or canonical != value:
        raise HostRefused('canonical desk:<uuid> required')
    return value


def _check_roots(config, transcript_root, profile):
    """A transcript-capturing profile's root must lie under one of the adapter's transcript roots."""
    if profile['capture']['mode'] != 'transcript':
        return
    root = Path(transcript_root)
    allowed = [Path(os.path.realpath(path)) for path in config['transcript_roots']]
    if not any(root == base or base in root.parents for base in allowed):
        raise HostRefused('the harness transcript root, as the runtime resolves it, is not one of the adapter '
                          'transcript_roots; configure an absolute host root in the harness profile')


def host_command():
    """The argv prefix that runs this adapter: the console script next to the interpreter."""
    beside = Path(sys.executable).parent / 'kp-agent-host'
    if beside.is_file() and os.access(beside, os.X_OK):
        return [str(beside)]
    found = shutil.which('kp-agent-host')
    return [found] if found else [sys.executable, '-m', 'kp_agent_tooling.host_cli']


def hook_command(config_file, launch_id):
    return shlex.join([*host_command(), 'hook', '--launch', launch_id, '--config', str(config_file)])


def _handler(command):
    return {'type': 'command', 'command': command, 'timeout': HOOK_TIMEOUT_SECONDS}


def _claude_settings(events, command):
    return {'hooks': {event: [{'hooks': [_handler(command)]}] for event in events}}


def codex_trust_entry(source, event, command, group_index):
    """Codex hook trust key and hash (the scheme of Kanban's codex-hook-config.ts) at this timeout."""
    return leaf.codex_trust_entry(source, event, command, group_index, timeout=HOOK_TIMEOUT_SECONDS)


def codex_hook_overrides(events, command):
    from kp_agent_tooling._impl.service.launch_binding import toml_inline
    trust = dict(codex_trust_entry(leaf.codex_session_flags_source(), event, command, 0) for event in events)
    args = ['-c', 'features.hooks=true',
            '-c', 'hooks.state=' + toml_inline({key: {'trusted_hash': value} for key, value in trust.items()})]
    for event in events:
        args += ['-c', f'hooks.{event}=' + toml_inline([{'hooks': [_handler(command)]}])]
    return args


def _override_key(args, index):
    arg = args[index]
    if arg in ('-c', '--config') and index + 1 < len(args):
        return args[index + 1].partition('=')[0].strip()
    if arg.startswith('--config='):
        return arg[len('--config='):].partition('=')[0].strip()
    if arg.startswith('-c') and len(arg) > 2 and not arg.startswith('--'):
        return arg[2:].partition('=')[0].strip()
    return None


def _check_user_args(profile, args):
    """Refuse user arguments that would replace an injection; the arguments themselves are never edited."""
    flags = []
    if profile['session_id']['strategy'] == 'mint':
        flags.append(profile['session_id']['flag'])
    if profile['hooks']['strategy'] == 'settings_file_flag':
        flags.append(profile['hooks']['flag'])
    for arg in args:
        if any(arg == flag or arg.startswith(flag + '=') for flag in flags):
            raise HostRefused(f'{arg.partition("=")[0]} is set by the host adapter; remove it from the CLI arguments')
    exact, prefixes = set(), []
    if profile['hooks']['strategy'] == 'config_override':
        exact |= {'hooks', 'features', 'features.hooks'}
        prefixes.append('hooks.')
    if profile['mcp']['strategy'] == 'config_override':
        server = f"{profile['mcp']['key']}.{SERVER_NAME}"
        exact |= {profile['mcp']['key'], server}
        prefixes.append(server + '.')
    for index in range(len(args)):
        key = _override_key(args, index)
        if key is not None and (key in exact or any(key.startswith(prefix) for prefix in prefixes)):
            raise HostRefused(f'the override {key} is set by the host adapter; remove it from the CLI arguments')


def _executable(value, environ):
    if os.path.isabs(value):
        if not (os.path.isfile(value) and os.access(value, os.X_OK)):
            raise HostRefused('the harness profile executable is not an executable file')
        return value
    found = shutil.which(value, path=environ.get('PATH', os.defpath))
    if found is None:
        raise HostRefused(f'the harness executable {value!r} is not on PATH')
    return found


# --- launch ----------------------------------------------------------------------------------------

def plan_launch(config_file, harness, desk_id, *, provider=None, model=None, user_args=(), cwd=None, environ=None):
    """Prepare one host launch; return (executable, argv, env, summary). Nothing is executed."""
    from kp_agent_tooling._impl.service import harness_profiles
    from kp_agent_tooling._impl.service.launch_binding import toml_inline
    config = load_config(config_file)
    environ = dict(os.environ if environ is None else environ)
    user_args = [str(arg) for arg in user_args]
    root = spool_root(config, create=True)
    workspace = str(Path(cwd or os.getcwd()).resolve())
    request = {'harness': harness, 'provider': provider or DEFAULT_TARGET, 'model': model or DEFAULT_TARGET,
               'desk_id': _desk_id(desk_id), 'workspace': workspace, 'task_id': 'host',
               'source': 'host', 'parent_session_id': None}
    runtime = Runtime(config)
    # Every refusal happens before prepare: a mint-strategy prepare binds the session at once.
    info = runtime.call({'op': 'harness-profile', 'config': config['runtime_config'], 'harness': harness,
                         'desk_id': request['desk_id']})
    profile = harness_profiles.validate_profile(info['profile'])
    _check_roots(config, info['transcript_root'], profile)
    _check_user_args(profile, user_args)
    executable = _executable(profile['executable'], environ)
    prepared = runtime.prepare(request)
    receipt = PurePosixPath(prepared['receipt_path'])
    memory_config = {**prepared.get('deferred_files', {}), **prepared['files']}.get('memory_config')
    if (receipt.name != 'launch.json' or not _UUID.fullmatch(receipt.parent.name)
            or memory_config != str(receipt.parent / 'memory.json')
            or (prepared['native_session_id'] is None) != (profile['session_id']['strategy'] == 'hook')):
        raise HostRefused('the prepared launch does not have the T3 launch layout')
    launch_id = receipt.parent.name
    directory = root / launch_id
    leaf.mkdir_private(directory)
    command = hook_command(config['path'], launch_id)
    server = runtime.memory_server(memory_config)
    injected = []
    if profile['session_id']['strategy'] == 'mint':
        injected += [profile['session_id']['flag'], prepared['native_session_id']]
    files = {}
    if profile['mcp']['strategy'] == 'config_file_flag':
        files['mcp_config'] = directory / 'mcp.json'
        _write_new(files['mcp_config'], {'mcpServers': {SERVER_NAME: server}})
        injected += [profile['mcp']['flag'], str(files['mcp_config'])]
    else:
        injected += ['-c', f"{profile['mcp']['key']}.{SERVER_NAME}=" + toml_inline(server)]
    events = profile['hooks']['events']
    if profile['hooks']['strategy'] == 'settings_file_flag':
        files['hook_settings'] = directory / 'hooks.json'
        _write_new(files['hook_settings'], _claude_settings(events, command))
        injected += [profile['hooks']['flag'], str(files['hook_settings'])]
    else:
        injected += codex_hook_overrides(events, command)
    leaf.create_new_empty(directory / EVENTS)
    # The record is written last: the hook accepts events only for a complete launch directory.
    _write_new(directory / LAUNCH_RECORD, {
        'schema_version': LAUNCH_SCHEMA, 'launch_id': launch_id, 'kind': 'launch', 'harness': harness,
        'desk_id': desk_id, 'receipt': prepared['receipt_path'], 'runtime_mode': config['mode'],
        'native_session_id': prepared['native_session_id'], 'workspace': workspace, 'created_at': _now(),
        'files': {name: str(path) for name, path in files.items()}})
    env = dict(environ)
    env.update({str(k): str(v) for k, v in prepared['env_additions'].items()})
    argv = [profile['executable'], *injected, *user_args]
    summary = {'launch_id': launch_id, 'native_session_id': prepared['native_session_id'],
               'receipt_path': prepared['receipt_path'], 'spool': str(directory), 'executable': executable}
    return executable, argv, env, summary


# --- bind ------------------------------------------------------------------------------------------

def operator_bind(config_file, harness, native_session_id, desk_id, *, provider=None, model=None, workspace=None):
    config = load_config(config_file)
    request = {'harness': harness, 'provider': provider or DEFAULT_TARGET, 'model': model or DEFAULT_TARGET,
               'native_session_id': native_session_id, 'desk_id': _desk_id(desk_id), 'source': 'operator',
               'workspace': str(Path(workspace or os.getcwd()).resolve()), 'parent_session_id': None}
    return Runtime(config).call({'op': 'bind', 'config': config['runtime_config'], 'harness': harness,
                                 'desk_id': desk_id, 'request': request})


# --- install-hooks / uninstall-hooks -----------------------------------------------------------------

def _policies(root, project, harness):
    found = []
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            if index >= MAX_SPOOL_ENTRIES:
                raise HostRefused('spool holds too many entries to search')
            if not _UUID.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
                continue
            if not os.path.lexists(Path(entry.path) / POLICY_RECORD):
                continue
            kind, record = read_record(entry.path)
            if record['status'] == 'active' and record['project'] == project and record['harness'] == harness:
                found.append((Path(entry.path), record))
    return found


def _target(project, harness):
    if harness not in POLICY_HARNESSES:
        raise HostRefused('install-hooks supports the claude and codex harnesses')
    directory, name = POLICY_HARNESSES[harness]
    return project / directory, project / directory / name


def _current(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(info.st_mode):
        raise HostRefused(f'{path} must be a regular file')
    return Path(path).read_bytes(), stat.S_IMODE(info.st_mode)


def merge_claude(original, events, command):
    """Append one hook group per event to a Claude settings document; other settings are kept."""
    try:
        value = {} if original is None else json.loads(original.decode('utf-8'))
    except (UnicodeError, json.JSONDecodeError):
        raise HostRefused('the Claude settings file is not JSON; nothing was changed') from None
    if not isinstance(value, dict) or not isinstance(value.get('hooks', {}), dict):
        raise HostRefused('the Claude settings file must be an object whose hooks is an object')
    hooks = value.setdefault('hooks', {})
    for event in events:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise HostRefused(f'hooks.{event} in the Claude settings file must be a list')
        groups.append({'hooks': [_handler(command)]})
    return (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode()


_FEATURES_HEADER = re.compile(r'^[ \t]*\[[ \t]*features[ \t]*\][ \t]*(?:#[^\n]*)?$', re.M)


def merge_codex(original, events, command, target, policy_id):
    """Add the policy hook to a Codex TOML configuration as a delimited block; refuse what cannot merge."""
    import tomllib
    from kp_agent_tooling._impl.service.launch_binding import toml_inline
    try:
        text = '' if original is None else original.decode('utf-8')
        parsed = tomllib.loads(text)
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise HostRefused('the Codex configuration is not valid TOML; nothing was changed') from None
    lines = []
    features = parsed.get('features')
    if features is None:
        lines += ['[features]', 'hooks = true']
    elif not isinstance(features, dict) or ('hooks' in features and features['hooks'] is not True):
        raise HostRefused('features.hooks is set to a non-true value in the Codex configuration; '
                          'enable it or remove it first')
    elif features.get('hooks') is not True:
        headers = list(_FEATURES_HEADER.finditer(text))
        if len(headers) != 1:
            raise HostRefused('cannot add features.hooks to this Codex configuration; add it, then retry')
        at = headers[0].end()
        text = text[:at] + '\nhooks = true' + text[at:]
    hooks = parsed.get('hooks', {})
    if not isinstance(hooks, dict):
        raise HostRefused('hooks in the Codex configuration must be a table')
    group = {'hooks': [_handler(command)]}
    trust = {}
    for event in events:
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            raise HostRefused(f'hooks.{event} in the Codex configuration must be an array of tables')
        key, digest = codex_trust_entry(str(target), event, command, len(existing))
        trust[key] = digest
        lines += [f'[[hooks.{event}]]', 'hooks = [' + toml_inline(group['hooks'][0]) + ']']
    for key, digest in trust.items():
        lines += [f'[hooks.state.{json.dumps(key)}]', f'trusted_hash = {json.dumps(digest)}']
    block = (f'# kp-agent-host policy {policy_id}: begin (managed by kp-agent-host install-hooks)\n'
             + '\n'.join(lines) + f'\n# kp-agent-host policy {policy_id}: end\n')
    merged = text + ('\n' if text and not text.endswith('\n') else '') + block
    try:
        check = tomllib.loads(merged)
    except tomllib.TOMLDecodeError:
        raise HostRefused('the policy hooks cannot be merged into this Codex configuration (an inline hooks '
                          'table?); nothing was changed') from None
    # Everything the user had is unchanged; only the policy's keys were added.
    def plain(value):
        return json.loads(json.dumps(value, default=str, sort_keys=True))
    stripped = plain(check)
    if parsed.get('features') is None:
        stripped.pop('features')
    elif features.get('hooks') is not True:
        stripped['features'].pop('hooks')
    for event in events:
        if stripped['hooks'][event][-1] != group:
            raise HostRefused('merged Codex hooks differ from the policy hooks')
        stripped['hooks'][event].pop()
        if not stripped['hooks'][event] and event not in hooks:
            stripped['hooks'].pop(event)
    for key, digest in trust.items():
        if stripped['hooks'].get('state', {}).pop(key, None) != {'trusted_hash': digest}:
            raise HostRefused('merged Codex trust state differs from the policy hooks')
    if 'state' in stripped['hooks'] and not stripped['hooks']['state'] and 'state' not in hooks:
        stripped['hooks'].pop('state')
    if not stripped['hooks'] and 'hooks' not in parsed:
        stripped.pop('hooks')
    if check.get('features', {}).get('hooks') is not True or stripped != plain(parsed):
        raise HostRefused('merging would change existing Codex settings; nothing was changed')
    return merged.encode()


def install_hooks(config_file, project, harness, desk_id):
    from kp_agent_tooling._impl.service import harness_profiles
    config = load_config(config_file)
    root = spool_root(config, create=True)
    project = Path(project).resolve()
    if not project.is_dir():
        raise HostRefused('--project must be an existing directory')
    directory, target = _target(project, harness)
    desk_id = _desk_id(desk_id)
    existing = _policies(root, str(project), harness)
    if existing:
        path, record = existing[0]
        if record['desk_id'] != desk_id:
            raise HostRefused('this project already has a policy for another desk; uninstall it first')
        current, _ = _current(target)
        return {'status': 'unchanged', 'policy_id': record['launch_id'], 'desk_id': desk_id, 'harness': harness,
                'project': str(project), 'target': str(target),
                'file_matches_install': current is not None and _sha(current) == record['installed_sha256']}
    info = Runtime(config).call({'op': 'harness-profile', 'config': config['runtime_config'], 'harness': harness,
                                 'desk_id': desk_id})
    profile = harness_profiles.validate_profile(info['profile'])
    _check_roots(config, info['transcript_root'], profile)
    policy_id = str(uuid.uuid4())
    command = hook_command(config['path'], policy_id)
    events = profile['hooks']['events']
    original, mode = _current(target)
    if original is None and os.path.lexists(directory) and not os.path.isdir(directory):
        raise HostRefused(f'{directory} is not a directory')
    merged = (merge_claude(original, events, command) if harness == 'claude'
              else merge_codex(original, events, command, target, policy_id))
    created_dir = not os.path.lexists(directory)
    spool = root / policy_id
    leaf.mkdir_private(spool)
    record = {'schema_version': POLICY_SCHEMA, 'launch_id': policy_id, 'kind': 'policy', 'status': 'active',
              'source': 'operator', 'harness': harness, 'desk_id': desk_id, 'project': str(project),
              'config': config['runtime_config'], 'profile': profile, 'transcript_root': info['transcript_root'],
              'events': events, 'target': str(target), 'created_dir': created_dir,
              'original_b64': None if original is None else base64.b64encode(original).decode(),
              'original_sha256': None if original is None else _sha(original), 'original_mode': mode,
              'installed_sha256': _sha(merged), 'hook_command': command, 'created_at': _now()}
    try:
        leaf.create_new_empty(spool / EVENTS)
        _write_new(spool / POLICY_RECORD, record)
        if created_dir:
            directory.mkdir()
        _replace(target, merged, mode if mode is not None else 0o600)
    except BaseException:
        if created_dir and os.path.isdir(directory) and not any(directory.iterdir()):
            directory.rmdir()
        shutil.rmtree(spool, ignore_errors=True)
        raise
    return {'status': 'installed', 'policy_id': policy_id, 'desk_id': desk_id, 'harness': harness,
            'project': str(project), 'target': str(target), 'events': events, 'source': 'operator'}


def uninstall_hooks(config_file, project, harness, desk_id=None):
    config = load_config(config_file)
    root = spool_root(config)
    project = Path(project).resolve()
    directory, target = _target(project, harness)
    existing = _policies(root, str(project), harness)
    if not existing:
        return {'status': 'not_installed', 'project': str(project), 'harness': harness}
    path, record = existing[0]
    if desk_id is not None and record['desk_id'] != desk_id:
        raise HostRefused('the installed policy names another desk')
    # Revocation first: no new binding after this point, whatever happens to the file.
    revoked = {**record, 'status': 'revoked', 'revoked_at': _now()}
    _replace(path / POLICY_RECORD, (json.dumps(revoked, indent=1, sort_keys=True) + '\n').encode(), 0o600)
    current, _ = _current(target)
    result = {'policy_id': record['launch_id'], 'project': str(project), 'harness': harness,
              'target': str(target), 'policy': 'revoked'}
    if current is None or _sha(current) != record['installed_sha256']:
        return {**result, 'status': 'revoked_not_restored', 'restored': False,
                'message': 'the file changed since install-hooks wrote it; it was left as it is'}
    if record['original_b64'] is None:
        os.unlink(target)
        if record['created_dir'] and os.path.isdir(directory) and not any(directory.iterdir()):
            directory.rmdir()
    else:
        original = base64.b64decode(record['original_b64'])
        _replace(target, original, record['original_mode'])
        if _sha(target.read_bytes()) != record['original_sha256']:
            raise RuntimeUnavailable('restore did not reproduce the original bytes')
    return {**result, 'status': 'uninstalled', 'restored': True}
