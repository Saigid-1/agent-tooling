"""Launch binding: bind one harness launch to one registry desk, then verify and capture its hooks.

``prepare`` is an operator or launcher act. It selects the harness profile,
checks the desk, writes a private per-launch directory (receipt, memory config,
MCP config, hook settings) and returns the argv and environment additions that
inject them. A harness configured only through its environment (OpenCode, order
O2) receives the desk memory server and the hook command as environment additions
alone; its turn end comes from the board's plugin, so it binds board launches only. A ``mint`` harness gets its session id here and is bound at once
through the registry's ``bind``; a ``hook`` harness is bound by its first hook
event.

``hook`` runs inside the harness. The receipt fixes the desk, harness, target
and workspace; a payload can only present the session, ``cwd`` and transcript,
which must match the receipt or the bound session. Capture follows the desk's
``capture`` setting and is idempotent on replay. No path here lets a payload or
an MCP tool choose or change a desk or grant admission.

This is a single-owner deployment: the file owner is the authority. Receipts are
private files in the state root; they are not a boundary against processes that
run as that owner.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service import harness_profiles
from kp_agent_tooling._impl.service.desk_binding import _ID, DeskLaunchUnavailable
from kp_agent_tooling._impl.service.desk_memory_runtime import components, private_json
from kp_agent_tooling._impl.service.desk_registry import is_registry

RECEIPT_SCHEMA = 'agent-tooling.launch-receipt.v1'
SESSION_SCHEMA = 'agent-tooling.launch-session.v1'
REQUEST_FIELDS = ('harness', 'provider', 'model', 'desk_id', 'workspace', 'task_id', 'source', 'parent_session_id')
LAUNCH_SOURCES = ('board', 'host')
SERVER_NAME = 'kp_desk_memory'
# Lifecycle events that capture; any other configured event only binds or verifies.
CAPTURE_EVENTS = ('Stop', 'PreCompact', 'SessionEnd')
HOOK_TIMEOUT_SECONDS = 30
_RECEIPT_FIELDS = {'schema_version', 'launch_id', 'config', 'memory_config', 'harness', 'provider', 'model',
                   'desk_id', 'binding_key', 'workspace', 'task_id', 'source', 'parent_session_id', 'profile',
                   'native_session_id', 'transcript_root', 'capture_ledger', 'queue', 'telemetry', 'files',
                   'created_at', 'authority'}
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')


class LaunchRefused(ValueError):
    """A request or hook payload does not match the launch it names."""


# Create a new owner-only JSON file; never overwrite.
def write_private(path, value):
    return leaf.write_new_json(path, value)


# Exclusive write; an existing file must already hold exactly ``value``.
def _write_once(path, value):
    return leaf.write_new_json_once(path, value, read=private_json, conflict=lambda: LaunchRefused(
        'launch state already recorded with different values'))


def private_dir(path):
    return leaf.ensure_private_dir(path, message='private launch directory required (0700, owned by the running user)')


def _locked(path):
    return leaf.private_lock(path)


def claude_transcript(root, workspace, native):
    """Claude's project transcript layout: <root>/<cwd, non-alphanumerics as '-'>/<session>.jsonl."""
    return Path(root) / re.sub(r'[^a-zA-Z0-9]', '-', str(workspace)) / (native + '.jsonl')


def hook_command(module, *args):
    return shlex.join([sys.executable, '-m', module, *[str(a) for a in args]])


def hook_settings(events, command):
    """A Claude-format hook settings document running ``command`` on each event."""
    handler = {'type': 'command', 'command': command, 'timeout': HOOK_TIMEOUT_SECONDS}
    return {'hooks': {event: [{'hooks': [dict(handler)]}] for event in events}}


def memory_server(config_path):
    """The stdio desk memory MCP server for one session's memory configuration."""
    return {'command': sys.executable,
            'args': ['-m', 'kp_agent_tooling.memory_cli', '--config', str(config_path), 'serve'],
            'env': {'PYTHONPATH': os.environ.get('PYTHONPATH', '')}}


def local_command_server(server):
    """``server`` in OpenCode's local MCP shape: one argv, its environment, enabled."""
    return {'type': 'local', 'command': [server['command'], *server['args']], 'environment': dict(server['env']),
            'enabled': True}


# --- Codex-style configuration overrides -------------------------------------------------------

def toml_inline(value):
    """TOML inline value (the ``-c key=value`` override syntax)."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if _CONTROL.search(value.replace('\t', '').replace('\n', '')):
            raise ValueError('override text contains control characters')
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return '[' + ','.join(toml_inline(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{' + ','.join(f'{_toml_key(k)}={toml_inline(v)}' for k, v in value.items()) + '}'
    raise ValueError('unsupported override value')


def _toml_key(key):
    return key if re.fullmatch(r'[A-Za-z0-9_-]+', key) else json.dumps(key, ensure_ascii=False)


def codex_trust_entry(event, command, group_index, matcher=None):
    """Trust key and hash of one session-flag hook group (the scheme Kanban's Codex hooks use)."""
    return leaf.codex_trust_entry(leaf.codex_session_flags_source(), event, command, group_index,
                                  timeout=HOOK_TIMEOUT_SECONDS, matcher=matcher)


def codex_hook_overrides(events, command):
    """Standalone ``-c`` overrides installing ``command`` for each event, trusted for this session."""
    handler = {'type': 'command', 'command': command, 'timeout': HOOK_TIMEOUT_SECONDS}
    trust = dict(codex_trust_entry(event, command, 0) for event in events)
    args = ['-c', 'features.hooks=true',
            '-c', 'hooks.state=' + toml_inline({key: {'trusted_hash': value} for key, value in trust.items()})]
    for event in events:
        args += ['-c', f'hooks.{event}=' + toml_inline([{'hooks': [handler]}])]
    return args


# --- Requests, receipts and session state ------------------------------------------------------

def _text(value, name, bound=256):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > bound or _CONTROL.search(value):
        raise ValueError(f'{name} must contain 1..{bound} characters without surrounding space')
    return value


def _desk_id(value):
    try:
        canonical = 'desk:' + str(uuid.UUID(value.removeprefix('desk:'))) if isinstance(value, str) else None
    except ValueError:
        canonical = None
    if canonical is None or canonical != value or len(value) != 41:
        raise ValueError('canonical desk:<uuid> required')
    return value


def _request(request):
    if not isinstance(request, dict) or set(request) != set(REQUEST_FIELDS):
        raise ValueError('exact launch fields required: ' + ', '.join(REQUEST_FIELDS))
    record = {name: _text(request[name], name) for name in ('harness', 'provider', 'model', 'task_id')}
    record['desk_id'] = _desk_id(request['desk_id'])
    if request['source'] not in LAUNCH_SOURCES:
        raise ValueError('source must be board or host')
    record['source'] = request['source']
    parent = request['parent_session_id']
    if parent is not None and (not isinstance(parent, str) or not _ID.fullmatch(parent)):
        raise ValueError('parent_session_id must be null or a safe harness session ID')
    record['parent_session_id'] = parent
    workspace = request['workspace']
    if not isinstance(workspace, str) or not workspace or len(workspace) > 1024 or not os.path.isabs(workspace):
        raise ValueError('workspace must be an absolute path')
    resolved = Path(workspace).resolve()
    if not resolved.is_dir() or len(str(resolved)) > 1024 or _CONTROL.search(str(resolved)):
        raise ValueError('workspace must be an existing directory')
    record['workspace'] = str(resolved)
    return record


def _binding_request(receipt, native):
    return {'harness': receipt['harness'], 'provider': receipt['provider'], 'model': receipt['model'],
            'native_session_id': native, 'desk_id': receipt['desk_id'], 'source': receipt['source'],
            'workspace': receipt['workspace'], 'parent_session_id': receipt['parent_session_id']}


def _assert_state_ready(ledger):
    ledger.assert_ready()
    with closing(leaf.sqlite_connect(ledger.path, mode='ro', resolve=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='desk_contexts'").fetchone():
            raise DeskLaunchUnavailable('desk context table missing; initialize the registry state')


def _ledgers(profile, state):
    capture = profile['capture']
    if capture['mode'] == 'none':
        return {'capture_ledger': None, 'queue': None, 'telemetry': None}
    if capture['parser'] == 'claude-jsonl':
        return {'capture_ledger': leaf.store_file(state, leaf.CAPTURE_DB), 'queue': leaf.store_file(state, leaf.QUEUE_DB),
                'telemetry': leaf.store_file(state, leaf.TELEMETRY_DB)}
    # Every other parser (Codex's rollout, OpenCode's export) keeps its cursor in the one non-Claude
    # ledger file; OpenCode's capture owns its own tables there (opencode_export_capture).
    return {'capture_ledger': leaf.store_file(state, leaf.ROLLOUT_CAPTURE_DB), 'queue': leaf.store_file(state, leaf.QUEUE_DB),
            'telemetry': None}


def _initialize_ledgers(profile, paths, store):
    from kp_agent_tooling._impl.service.claude_episode_capture import ClaudeEpisodeCapture
    from kp_agent_tooling._impl.service.claude_memory_hook import HookTelemetry
    from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue
    makers = {'queue': lambda p: ConsolidationQueue(p, store=store), 'telemetry': HookTelemetry,
              'capture_ledger': ClaudeEpisodeCapture if profile['capture'].get('parser') == 'claude-jsonl'
              else RolloutCapture}
    for name, path in paths.items():
        if path is not None and not path.exists() and not path.is_symlink():
            makers[name](path).initialize()
    if profile['capture'].get('parser') == 'opencode-export':
        from kp_agent_tooling._impl.service.opencode_export_capture import OpenCodeExportCapture
        OpenCodeExportCapture(paths['capture_ledger']).initialize()  # its own tables; idempotent


def prepare(config_path, request):
    """Prepare one launch of ``request['harness']`` bound to ``request['desk_id']``."""
    from kp_agent_tooling._impl.service.session_bindings import bind, validate
    record = _request(request)
    config_path = Path(config_path)
    if not config_path.is_absolute():
        raise ValueError('registry configuration path must be absolute')
    config, registry, ledger, store = components(config_path)
    if not is_registry(registry):
        raise ValueError('launch binding requires an agent-tooling.desk-registry.v1 registry')
    profile = harness_profiles.select(harness_profiles.load(config['catalog_path']), record['harness'])
    if profile['hooks']['strategy'] == 'plugin_env' and record['source'] != 'board':
        # The plugin that runs the hook command, and the terminal exit that ends the session, are
        # the board's (order O2): a host launch would have no turn end at all.
        raise ValueError('this harness reports its turn end through the board; launch it from the board')
    context = registry.context(record['desk_id'])
    if context['desk']['desk_id'] != record['desk_id']:
        raise ValueError('select a desk registered in this registry')
    _assert_state_ready(ledger)
    mint = profile['session_id']['strategy'] == 'mint'
    native = str(uuid.uuid4()) if mint else None
    launch_id = str(uuid.uuid4())
    # Every binding coordinate is checked now, also for a session bound later by its first hook.
    validate(_binding_request({**record}, native or launch_id))
    state = leaf.mark_store(config['state_root'])
    launches = private_dir(leaf.launches_dir(state))
    paths = _ledgers(profile, state)
    with _locked(launches / '.lock'):
        _initialize_ledgers(profile, paths, store)
    run = launches / launch_id
    leaf.mkdir_private(run)
    try:
        return _write_launch(config_path, config, record, profile, context, native, launch_id, run, paths, bind)
    except BaseException:
        shutil.rmtree(run, ignore_errors=True)
        raise


def _write_launch(config_path, config, record, profile, context, native, launch_id, run, paths, bind):
    receipt_path = run / 'launch.json'
    memory_path = run / 'memory.json'
    mcp_path = run / 'mcp.json'
    settings_path = run / 'hooks.json'
    root = harness_profiles.transcript_root(profile)
    transcript = None
    if native is not None and profile['capture'].get('parser') == 'claude-jsonl':
        transcript = str(claude_transcript(root.resolve(), record['workspace'], native).resolve())
    # `files` lists only what exists when prepare returns. A hook-strategy session has no id yet,
    # so its memory configuration is deferred: the first hook event writes it at bind.
    files = {'receipt': str(receipt_path),
             **({'memory_config': str(memory_path)} if native is not None else {}),
             'mcp_config': str(mcp_path), 'hook_settings': str(settings_path)}
    deferred = {'memory_config': str(memory_path)} if native is None else {}
    receipt = {'schema_version': RECEIPT_SCHEMA, 'launch_id': launch_id, 'config': str(config_path),
               'memory_config': str(memory_path), **record, 'binding_key': context['desk']['binding_id'],
               'profile': profile, 'native_session_id': native,
               'transcript_root': str(root.resolve()) if root is not None else None,
               **{k: (str(v) if v is not None else None) for k, v in paths.items()},
               'files': files, **({'deferred_files': deferred} if deferred else {}),
               'created_at': datetime.now(timezone.utc).isoformat(),
               'authority': 'launcher receipt; fixes desk, harness, target and workspace; '
                            'hook payloads and MCP tools select nothing'}
    write_private(receipt_path, receipt)
    hook_argv = [sys.executable, '-m', 'kp_agent_tooling.launch_cli', '--receipt', str(receipt_path), 'hook']
    command = shlex.join(hook_argv)
    events = profile['hooks']['events']
    write_private(settings_path, hook_settings(events, command))
    server = memory_server(memory_path)
    write_private(mcp_path, {'mcpServers': {SERVER_NAME: server}})
    argv, env = [], {profile['receipt_env']: str(receipt_path)}
    if native is not None:
        write_private(memory_path, {**config, 'provider_session_id': native})
        bind(config_path, _binding_request(receipt, native))
        write_private(run / 'session.json', _session_record(native, transcript))
        argv += [profile['session_id']['flag'], native]
    mcp = profile['mcp']
    if mcp['strategy'] == 'config_file_flag':
        argv += [mcp['flag'], str(mcp_path)]
    elif mcp['strategy'] == 'config_content_env':
        # One configuration document, per launch, holding only this launch's server.
        env[mcp['env']] = json.dumps({mcp['key']: {SERVER_NAME: local_command_server(server)}})
    else:
        argv += ['-c', f"{mcp['key']}.{SERVER_NAME}=" + toml_inline(server)]
    hooks = profile['hooks']
    if hooks['strategy'] == 'settings_file_flag':
        argv += [hooks['flag'], str(settings_path)]
    elif hooks['strategy'] == 'plugin_env':
        env[hooks['env']] = json.dumps(hook_argv)
    else:
        argv += codex_hook_overrides(events, command)
    result = {'receipt_path': str(receipt_path), 'native_session_id': native, 'argv_additions': argv,
              'env_additions': env, 'files': files}
    if deferred:
        result['deferred_files'] = deferred
    return result


def _session_record(native, transcript):
    return {'schema_version': SESSION_SCHEMA, 'native_session_id': native, 'transcript': transcript}


def _read_session(run):
    path = run / 'session.json'
    if not path.exists() and not path.is_symlink():
        return None
    value = private_json(path)
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'native_session_id', 'transcript'}
            or value['schema_version'] != SESSION_SCHEMA or not _ID.fullmatch(str(value['native_session_id']))):
        raise LaunchRefused('launch session state is invalid')
    return value


def _receipt(path):
    path = Path(path)
    if not path.is_absolute():
        raise LaunchRefused('absolute launch receipt path required')
    receipt = private_json(leaf.mark_store(path))
    if (not isinstance(receipt, dict) or set(receipt) - {'deferred_files'} != _RECEIPT_FIELDS
            or receipt['schema_version'] != RECEIPT_SCHEMA
            or receipt.get('deferred_files', {'memory_config': receipt['memory_config']})
            != {'memory_config': receipt['memory_config']}
            or ('deferred_files' in receipt) != (receipt['native_session_id'] is None)):
        raise LaunchRefused('invalid launch receipt')
    if harness_profiles.validate_profile(receipt['profile']) != receipt['profile']:
        raise LaunchRefused('invalid launch receipt profile')
    return receipt


# --- Transcript identity -----------------------------------------------------------------------

def _codex_header(path):
    """The first rollout row, or None while the file does not exist or its first row is incomplete."""
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise LaunchRefused('hook transcript must be a regular file')
    with path.open('rb') as stream:
        line = stream.readline(65537)
    if not line.endswith(b'\n'):
        if len(line) > 65536:
            raise LaunchRefused('rollout header exceeds its read bound')
        return None
    try:
        return json.loads(line)
    except (UnicodeError, json.JSONDecodeError):
        raise LaunchRefused('rollout header is not JSON') from None


def codex_meta_matches(row, native, workspace):
    """A root (non-subagent) ``session_meta`` row for exactly this session and workspace."""
    payload = row.get('payload') if isinstance(row, dict) else None
    if row is None or not isinstance(payload, dict) or row.get('type') != 'session_meta':
        return False
    source = payload.get('source')
    return (str(payload.get('id', '')).lower() == native.lower() and isinstance(payload.get('cwd'), str)
            and os.path.isabs(payload['cwd']) and Path(payload['cwd']).resolve() == Path(workspace)
            and not (isinstance(source, dict) and 'subagent' in source))


def _transcript(receipt, native, value, pinned=None):
    """Check a payload transcript against the receipt's capture root, parser and session."""
    from kp_agent_tooling._impl.service.native_history_import import _codex_identity
    capture = receipt['profile']['capture']
    if capture['mode'] != 'transcript':
        return None
    if not isinstance(value, str) or not value or len(value) > 4096 or not os.path.isabs(value):
        raise LaunchRefused('hook transcript_path must be an absolute path')
    resolved = Path(value).resolve()
    if pinned is not None and str(resolved) != pinned:
        raise LaunchRefused('hook transcript differs from the bound session transcript')
    root = Path(receipt['transcript_root'])
    if capture['parser'] == 'claude-jsonl':
        if resolved != claude_transcript(root, receipt['workspace'], native).resolve():
            raise LaunchRefused('hook transcript is not this session transcript')
        return str(resolved)
    try:
        identity = _codex_identity(resolved)
    except ValueError:
        identity = None
    if not resolved.is_relative_to(root) or resolved.suffix != '.jsonl' or identity != native.lower():
        raise LaunchRefused('hook transcript is not this session rollout')
    header = _codex_header(resolved)
    if header is not None and not codex_meta_matches(header, native, receipt['workspace']):
        raise LaunchRefused('rollout header names another session or workspace')
    return str(resolved)


# --- Hook --------------------------------------------------------------------------------------

def hook(receipt_path, payload):
    """Verify one hook event against its launch receipt; bind on first event; capture on lifecycle events."""
    from kp_agent_tooling._impl.service.episodic_memory_tools import from_config
    from kp_agent_tooling._impl.service.session_bindings import bind
    receipt_path = Path(receipt_path)
    receipt = _receipt(receipt_path)
    receipt_path = leaf.mark_store(receipt_path)  # a launch receipt: a store path
    config, registry, _, _ = components(receipt['config'])
    run = receipt_path.parent
    if (receipt_path.name != 'launch.json' or run.name != receipt['launch_id']
            or run.parent.resolve() != leaf.launches_dir(config['state_root']).resolve()
            or receipt['memory_config'] != str(run / 'memory.json')):
        raise LaunchRefused('receipt is not a launch receipt of this state root')
    profile = receipt['profile']
    if not isinstance(payload, dict):
        raise LaunchRefused('hook payload must be a JSON object')
    event = payload.get('hook_event_name')
    if event not in profile['hooks']['events']:
        raise LaunchRefused('hook event is not configured for this launch')
    cwd = payload.get('cwd')
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or Path(cwd).resolve() != Path(receipt['workspace']):
        raise LaunchRefused('hook cwd differs from the launch workspace')
    native = payload.get(profile['session_id'].get('field', 'session_id'))
    if not isinstance(native, str) or not _ID.fullmatch(native):
        raise LaunchRefused('hook payload lacks a safe session id')
    bound, snapshot = False, None
    with _locked(run / '.lock'):
        session = _read_session(run)
        if session is None:
            if profile['session_id']['strategy'] != 'hook':
                raise LaunchRefused('launch session state missing')
            transcript = _transcript(receipt, native, payload.get('transcript_path'))
            # An export-captured session proves, before it is bound, that it is a root session
            # of this workspace (its export names it, its directory and no parent).
            snapshot = _export_snapshot(receipt, native)
            bind(receipt['config'], _binding_request(receipt, native))
            _write_once(leaf.mark_store(receipt['memory_config']), {**config, 'provider_session_id': native})
            _write_once(run / 'session.json', _session_record(native, transcript))
            bound = True
        elif native != session['native_session_id']:
            raise LaunchRefused('hook session differs from the bound session')
        else:
            transcript = _transcript(receipt, native, payload.get('transcript_path'), pinned=session['transcript'])
    adapter = from_config(leaf.mark_store(receipt['memory_config']))
    try:
        if adapter.session != native:
            raise LaunchRefused('memory configuration names another session')
        binding_key = adapter.store._binding(native)
        if binding_key != receipt['binding_key']:
            raise LaunchRefused('session is admitted to another desk')
        result = {'status': 'verified', 'event': event, 'native_session_id': native, 'desk_id': receipt['desk_id'],
                  'binding_key': binding_key, 'bound_by_this_event': bound, 'capture': {'status': 'not_due'}}
        if event in CAPTURE_EVENTS and (transcript is not None or profile['capture']['mode'] == 'export'):
            if not registry.allows_capture(binding_key):
                result['capture'] = {'status': 'disabled'}
            else:
                result['capture'] = _capture(receipt, payload, adapter, native, transcript, snapshot)
        return result
    finally:
        adapter.close()


def _export_snapshot(receipt, native):
    """An export-capture profile's identity-checked snapshot of ``native``; None for any other profile."""
    if receipt['profile']['capture']['mode'] != 'export':
        return None
    from kp_agent_tooling._impl.service.opencode_export_capture import export_session
    return export_session(receipt['profile']['executable'], native, receipt['workspace'])


_EXPORT_REASONS = {'SessionEnd': 'session_end', 'PreCompact': 'context_threshold'}


def _capture(receipt, payload, adapter, native, transcript, snapshot=None):
    from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue
    from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
    store = adapter.store
    index = EpisodicSearchIndex(leaf.store_path(store.path, leaf.SEARCH_INDEX_DB, sibling=True), episode_store=store)
    queue = ConsolidationQueue(leaf.mark_store(receipt['queue']), store=store)
    if receipt['profile']['capture']['parser'] == 'opencode-export':
        from kp_agent_tooling._impl.service.opencode_export_capture import OpenCodeExportCapture
        return OpenCodeExportCapture(leaf.mark_store(receipt['capture_ledger']), index=index,
                                     executable=receipt['profile']['executable']).capture(
            native, store=store, queue=queue, workspace=receipt['workspace'], snapshot=snapshot,
            reason=_EXPORT_REASONS.get(payload.get('hook_event_name'), 'batch'))
    if receipt['profile']['capture']['parser'] == 'claude-jsonl':
        from kp_agent_tooling._impl.service.claude_episode_capture import ClaudeEpisodeCapture
        from kp_agent_tooling._impl.service.claude_memory_hook import HookTelemetry, handle_hook
        capture = ClaudeEpisodeCapture(leaf.mark_store(receipt['capture_ledger']), native_session_id=native, index=index)
        # Identity is fixed by the launch receipt, never selected by the payload.
        capture.bind(native, store=store, transcript_path=Path(transcript))
        return handle_hook(payload, session=native, transcript_path=Path(transcript), store=store, queue=queue,
                           capture=capture, telemetry=HookTelemetry(leaf.mark_store(receipt['telemetry'])))
    return RolloutCapture(leaf.mark_store(receipt['capture_ledger']), index=index).capture(
        native, store=store, queue=queue, transcript_path=Path(transcript), workspace=receipt['workspace'],
        reason='session_end' if payload.get('hook_event_name') == 'SessionEnd' else 'batch')


# --- Codex rollout capture ---------------------------------------------------------------------

class RolloutCaptureConflict(RuntimeError):
    pass


_EMPTY_SHA = hashlib.sha256(b'').hexdigest()


class RolloutCapture:
    """Incremental capture of visible Codex rollout events into one bound session's desk memory.

    Visible events are those ``native_history_import._codex_events`` reads. The
    cursor, keyed by session, pins the source file (path, device, inode) and the
    SHA-256 of the consumed prefix; a rewritten or replaced prefix is refused. A
    page is recorded as pending before it is sealed, so a replay after a crash
    seals the same bytes under the same source reference instead of a duplicate.
    """

    PAGE_BYTES = 262144
    PAGE_EVENTS = 100
    LINE_BYTES = 1048576
    PREFIX_BYTES = 64 * 1024 * 1024
    PAGES = 16
    # _page's parameters: the session_meta predicate (_session_meta_matches), the exception it raises
    # and the refusal of a rollout whose first row is not this session's metadata.
    _CONFLICT = RolloutCaptureConflict
    _NOT_THIS_SESSION = 'rollout does not start with this session metadata'

    def __init__(self, path, *, index=None):
        self.path = leaf.as_path(path)
        self.index = index

    @property
    def _lock_path(self):
        return self.path.with_suffix(self.path.suffix + '.lock')

    def initialize(self):
        leaf.touch_new_private(self.path)
        leaf.touch_new_private(self._lock_path)
        with closing(leaf.sqlite_connect(self.path, mode='rwc', resolve=True)) as db, db:
            db.execute('CREATE TABLE cursor (session TEXT PRIMARY KEY, binding TEXT NOT NULL, path TEXT NOT NULL, '
                       'dev INTEGER NOT NULL, ino INTEGER NOT NULL, offset INTEGER NOT NULL, digest TEXT NOT NULL, '
                       'pending TEXT)')
            db.execute('CREATE TABLE receipts (session TEXT NOT NULL, end_offset INTEGER NOT NULL, '
                       'payload TEXT NOT NULL, PRIMARY KEY(session,end_offset))')

    def _db(self):
        if self.path.is_symlink() or not self.path.is_file():
            raise RolloutCaptureConflict('rollout capture ledger unavailable; initialize explicitly')
        db = leaf.sqlite_connect(self.path, mode='rw', resolve=True)
        db.row_factory = sqlite3.Row
        return db

    def capture(self, session, *, store, queue, transcript_path, workspace, reason='batch'):
        lock = self._lock_path
        if lock.is_symlink() or not lock.is_file():
            raise RolloutCaptureConflict('rollout capture lock unavailable')
        with leaf.open_binary(lock) as handle:
            deadline = time.monotonic() + 2.0
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RolloutCaptureConflict('rollout capture busy') from None
                    time.sleep(0.02)
            try:
                pages = []
                for _ in range(self.PAGES):
                    page = self._page(session, store=store, queue=queue, path=Path(transcript_path),
                                      workspace=workspace, reason=reason)
                    pages.append(page)
                    if page['state'] == 'idle' or not page.get('has_more'):
                        break
                captured = [p for p in pages if p['state'] == 'captured']
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        if captured and self.index is not None:
            # T12b: indexing is a separate action. A host install drains once after the seals,
            # never waiting; inside Compose the indexer role drains and this opens nothing.
            from kp_agent_tooling._impl.service.episodic_search import drain_after_seal
            drain_after_seal(store, self.index)
        return {'status': 'captured' if captured else 'not_due', 'pages': pages,
                'episodes': [p['episode_id'] for p in captured]}

    @staticmethod
    def _source(path):
        if path.is_symlink() or not path.is_file() or path.suffix != '.jsonl':
            raise RolloutCaptureConflict('operator-bound regular rollout file required')
        return path.stat()

    def _cursor(self, session, binding, path, stat):
        with closing(self._db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM cursor WHERE session=?', (session,)).fetchone()
            if row is None:
                db.execute('INSERT INTO cursor VALUES (?,?,?,?,?,?,?,NULL)',
                           (session, binding, str(path.resolve()), stat.st_dev, stat.st_ino, 0, _EMPTY_SHA))
                row = db.execute('SELECT * FROM cursor WHERE session=?', (session,)).fetchone()
        if row['binding'] != binding:
            raise RolloutCaptureConflict('session belongs to a different desk binding')
        if row['path'] != str(path.resolve()) or (row['dev'], row['ino']) != (stat.st_dev, stat.st_ino):
            raise RolloutCaptureConflict('session source replaced or rebound')
        if stat.st_size < row['offset']:
            raise RolloutCaptureConflict('session source truncated')
        if row['offset'] > self.PREFIX_BYTES:
            raise RolloutCaptureConflict('capture prefix exceeds verification bound; operator rotation required')
        return dict(row)

    def _read_line(self, stream):
        """One newline-terminated row (bounded copy) and its raw length; None at an incomplete tail."""
        digest_parts, kept, length = [], [], 0
        while True:
            chunk = stream.readline(65536)
            if not chunk:
                return None
            length += len(chunk)
            digest_parts.append(chunk)
            if length <= self.LINE_BYTES:
                kept.append(chunk)
            if length > self.PREFIX_BYTES:
                raise RolloutCaptureConflict('rollout row exceeds the capture bound')
            if chunk.endswith(b'\n'):
                return b''.join(kept) if length <= self.LINE_BYTES else None, digest_parts, length

    def _session_meta_matches(self, value, session, workspace):
        return codex_meta_matches(value, session, workspace)

    def _page(self, session, *, store, queue, path, workspace, reason):
        from kp_agent_tooling._impl.service.claude_episode_capture import _bounded_events
        from kp_agent_tooling._impl.service.native_history_import import _codex_events
        conflict = self._CONFLICT
        binding = store._binding(session)  # live desk admission, before any source read
        stat = self._source(path)
        row = self._cursor(session, binding, path, stat)
        pending = json.loads(row['pending']) if row['pending'] else None
        start = row['offset']
        stop_at = pending['end'] if pending else None
        digest = hashlib.sha256()
        events, omissions, end = [], [], start
        with path.open('rb') as stream:
            remaining = start
            while remaining:
                chunk = stream.read(min(1048576, remaining))
                if not chunk:
                    raise conflict('session source truncated')
                digest.update(chunk)
                remaining -= len(chunk)
            if digest.hexdigest() != row['digest']:
                raise conflict('consumed source prefix rewritten')
            while (end < stop_at) if stop_at is not None else (
                    end < stat.st_size and end - start < self.PAGE_BYTES and len(events) < self.PAGE_EVENTS):
                read = self._read_line(stream)
                if read is None:
                    if stop_at is not None:
                        raise conflict('pending source changed')
                    break
                line, parts, length = read
                parsed, missing = [], []
                if line is None and end == 0:
                    raise conflict(self._NOT_THIS_SESSION)
                if line is None:
                    missing = ['oversized_row']
                else:
                    try:
                        value = json.loads(line)
                    except (UnicodeError, json.JSONDecodeError):
                        value = None
                    if end == 0 and not self._session_meta_matches(value, session, workspace):
                        raise conflict(self._NOT_THIS_SESSION)
                    if isinstance(value, dict) and value.get('type') == 'session_meta':
                        if not self._session_meta_matches(value, session, workspace):
                            raise conflict('rollout session metadata changed')
                    elif isinstance(value, dict):
                        parsed, missing = _codex_events(value, end)
                        parsed = _bounded_events(parsed)
                    else:
                        missing = ['malformed_json_or_utf8']
                if stop_at is None and events and len(events) + len(parsed) > self.PAGE_EVENTS:
                    break
                if len(parsed) > 500:
                    raise conflict('row exceeds the event batch limit')
                for part in parts:
                    digest.update(part)
                events.extend(parsed)
                omissions.extend({'offset': end, 'reason': reason_} for reason_ in missing)
                end += length
        after = path.stat()
        if (after.st_dev, after.st_ino) != (stat.st_dev, stat.st_ino) or after.st_size < end:
            raise conflict('source replaced during capture')
        if end == start:
            return {'state': 'idle', 'offset': start, 'incomplete_tail': after.st_size > start}
        page = {'start': start, 'end': end, 'digest': digest.hexdigest(),
                'source_ref': f'codex-rollout:{session}:{stat.st_dev}:{stat.st_ino}:{start}:{end}:{digest.hexdigest()}'}
        if pending is not None and pending != page:
            raise conflict('pending source rewritten')
        has_more = after.st_size > end
        if not events:
            receipt = {'state': 'omitted', 'offset': end, 'omission_count': len(omissions), 'has_more': has_more}
            self._advance(session, row, page, receipt)
            return receipt
        if pending is None:
            with closing(self._db()) as db, db:
                db.execute('UPDATE cursor SET pending=? WHERE session=? AND offset=? AND pending IS NULL',
                           (json.dumps(page, sort_keys=True), session, start))
        episode = store.capture(session, source_ref=page['source_ref'], events=events)
        job = queue.enqueue(session, episode_ids=[episode['episode_id']], reason=reason)
        # The seal's outbox row carries the indexing (T12b); `pending` is seal and replay state only.
        receipt = {'state': 'captured', 'episode_id': episode['episode_id'], 'job_id': job['job_id'],
                   'event_count': len(events), 'offset': end, 'omission_count': len(omissions),
                   'indexed': self.index is not None, 'has_more': has_more}
        self._advance(session, row, page, receipt)
        return receipt

    def _advance(self, session, row, page, receipt):
        with closing(self._db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT offset,digest FROM cursor WHERE session=?', (session,)).fetchone()
            if tuple(current) != (row['offset'], row['digest']):
                raise RolloutCaptureConflict('capture cursor changed during publication')
            db.execute('UPDATE cursor SET offset=?,digest=?,pending=NULL WHERE session=?',
                       (page['end'], page['digest'], session))
            db.execute('INSERT OR IGNORE INTO receipts VALUES (?,?,?)', (session, page['end'], json.dumps(receipt)))

    def status(self, session):
        with closing(self._db()) as db:
            row = db.execute('SELECT offset,pending FROM cursor WHERE session=?', (session,)).fetchone()
        return {'offset': row['offset'] if row else 0, 'has_pending_batch': bool(row and row['pending'])}
