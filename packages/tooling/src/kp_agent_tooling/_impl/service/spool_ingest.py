"""Spool ingestion: apply T3's hook semantics to the events the host hook appended.

``kp-agent-launch ingest-spool --root <spool>`` runs where the runtime state lives:
locally, or in the ``capture`` role (no network) with the spool mounted. Each
``<spool>/<id>/`` holds one host record and ``events.jsonl``:

- ``launch.json`` (``kp-agent-host launch``): every event is verified against the
  T3 receipt it names, with ``launch_binding.hook`` (bind on first event,
  capture, refusal). An event for another session is a delegation candidate: it
  binds as a child of the launch's session only when the child's own transcript
  proves the parentage (``child_evidence``); otherwise it is refused.
- ``policy.json`` (``kp-agent-host install-hooks``): the first event of a session
  started in the policy's project, with its transcript in place, records an
  operator binding (``source: "operator"``) to the policy's desk and a receipt for
  that session; that and later events then follow ``launch_binding.hook``. A
  revoked policy binds nothing new.

A durable cursor per spool file (offset and SHA-256 of the consumed prefix) lives
in ``<state_root>/spool-ingest.sqlite3`` beside the registry state, so the spool
itself can be mounted read-only. An event is applied before its cursor advances,
and T3 binding and capture are idempotent, so a restart never captures twice.
A runtime fault is retried on later passes (at most ``ATTEMPTS`` times); a
refusal is recorded and the cursor moves on.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service import harness_profiles, host_adapter
from kp_agent_tooling._impl.service import launch_binding as lb
from kp_agent_tooling._impl.service.desk_binding import _ID
from kp_agent_tooling._impl.service.desk_memory_runtime import components, private_json

LEDGER = leaf.SPOOL_INGEST_DB
CHILD_SCHEMA = 'agent-tooling.launch-child.v1'
LINE_BYTES = 1048576
SPOOL_BYTES = 64 * 1024 * 1024
EVENTS_PER_PASS = 256
ATTEMPTS = 8
HEADER_BYTES = 65536
# Policy sessions get a deterministic launch id, so a replayed first event finds its own receipt.
POLICY_NAMESPACE = uuid.UUID('6b1d3c52-9f0e-4c7a-a7a4-6c2a1e0f4b19')
_ENVELOPE = {'schema_version', 'launch_id', 'kind', 'receipt', 'received_at', 'payload_bytes', 'payload_reduced',
             'payload'}


def _now():
    return datetime.now(timezone.utc).isoformat()


# --- Cursor ledger ---------------------------------------------------------------------------------

class Cursors:
    """Per spool file: consumed offset, SHA-256 of the consumed prefix, retry count, and results."""

    def __init__(self, state_root):
        self.path = leaf.store_file(state_root, LEDGER)
        self.lock = leaf.store_file(state_root, LEDGER + '.lock')

    def _db(self):
        if self.path.is_symlink():
            raise RuntimeError('spool cursor ledger must not be a symlink')
        if not self.path.exists():
            try:
                leaf.create_new_empty(self.path)
            except FileExistsError:
                pass
        db = leaf.sqlite_connect(self.path, mode='rwc', resolve=True)
        db.execute('CREATE TABLE IF NOT EXISTS cursors (launch_id TEXT PRIMARY KEY, kind TEXT NOT NULL, '
                   'offset INTEGER NOT NULL, digest TEXT NOT NULL, attempts INTEGER NOT NULL, '
                   'updated_at TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS results (launch_id TEXT NOT NULL, offset INTEGER NOT NULL, '
                   'end_offset INTEGER NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL, '
                   'recorded_at TEXT NOT NULL, PRIMARY KEY (launch_id, offset))')
        return db

    def locked(self):
        return leaf.private_lock(self.lock, value=self)

    def get(self, launch_id):
        with closing(self._db()) as db:
            row = db.execute('SELECT offset,digest,attempts FROM cursors WHERE launch_id=?', (launch_id,)).fetchone()
        return tuple(row) if row else (0, hashlib.sha256().hexdigest(), 0)

    def advance(self, launch_id, kind, start, end, digest, status, result):
        with closing(self._db()) as db, db:
            db.execute('INSERT INTO cursors VALUES (?,?,?,?,0,?) ON CONFLICT(launch_id) DO UPDATE SET '
                       'offset=excluded.offset, digest=excluded.digest, attempts=0, updated_at=excluded.updated_at',
                       (launch_id, kind, end, digest, _now()))
            db.execute('INSERT OR REPLACE INTO results VALUES (?,?,?,?,?,?)',
                       (launch_id, start, end, status, json.dumps(result, sort_keys=True), _now()))

    def attempt(self, launch_id, kind, offset, digest, attempts):
        with closing(self._db()) as db, db:
            db.execute('INSERT INTO cursors VALUES (?,?,?,?,?,?) ON CONFLICT(launch_id) DO UPDATE SET '
                       'attempts=excluded.attempts, updated_at=excluded.updated_at',
                       (launch_id, kind, offset, digest, attempts, _now()))

    def results(self, launch_id):
        with closing(self._db()) as db:
            rows = db.execute('SELECT offset,end_offset,status,result FROM results WHERE launch_id=? ORDER BY offset',
                              (launch_id,)).fetchall()
        return [{'offset': a, 'end_offset': b, 'status': c, 'result': json.loads(d)} for a, b, c, d in rows]


# --- Delegation evidence ---------------------------------------------------------------------------

def _first_row(path):
    """(raw bytes, parsed row) of a transcript's first complete row, or None while it is not written."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise lb.LaunchRefused('hook transcript must be a regular file')
    with path.open('rb') as stream:
        line = stream.readline(HEADER_BYTES + 1)
    if not line.endswith(b'\n'):
        if len(line) > HEADER_BYTES:
            raise lb.LaunchRefused('rollout header exceeds its read bound')
        return None
    try:
        return line, json.loads(line)
    except (UnicodeError, json.JSONDecodeError):
        raise lb.LaunchRefused('rollout header is not JSON') from None


def child_meta_matches(row, child, parent, workspace):
    """True when a Codex ``session_meta`` row is a child thread of ``parent`` in ``workspace``.

    Evidence read from the child's own rollout header:
    - ``payload.id`` is the child and ``payload.cwd`` is the launch workspace;
    - ``payload.source.subagent.thread_spawn`` is present (how Kanban recognises a
      descendant thread, apps/kanban/src/commands/hook-events/codex-hook-events.ts
      ``isCodexDescendantSession``) and its ``parent_thread_id`` is the launch's
      session (the field the OPS delegation adapter verifies);
    - no other parent claim disagrees: top-level ``parent_thread_id`` and an
      inherited ``session_id``, which ``native_history_import`` reads as the native
      parent, are absent or name the same parent.
    """
    payload = row.get('payload') if isinstance(row, dict) else None
    if not isinstance(payload, dict) or row.get('type') != 'session_meta':
        return False
    if str(payload.get('id', '')).lower() != child.lower():
        return False
    cwd = payload.get('cwd')
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or Path(cwd).resolve() != Path(workspace):
        return False
    source = payload.get('source')
    subagent = source.get('subagent') if isinstance(source, dict) else None
    spawn = subagent.get('thread_spawn') if isinstance(subagent, dict) else None
    if not isinstance(spawn, dict):
        return False
    stated = spawn.get('parent_thread_id')
    if not isinstance(stated, str) or stated.lower() != parent.lower():
        return False
    depth = spawn.get('depth')
    if depth is not None and (type(depth) is not int or depth < 1):
        return False
    for name in ('parent_thread_id', 'session_id'):
        claim = payload.get(name)
        if claim is not None and (not isinstance(claim, str) or claim.lower() != parent.lower()):
            return False
    return True


def child_evidence(transcript, child, parent, workspace):
    """The parentage evidence for a child rollout, or a refusal; never a guess."""
    first = _first_row(transcript)
    if first is None:
        raise lb.LaunchRefused('the child rollout has no complete session_meta row yet; parentage is not proven')
    raw, row = first
    if not child_meta_matches(row, child, parent, workspace):
        raise lb.LaunchRefused('the child rollout does not prove a thread_spawn by the launch session')
    spawn = row['payload']['source']['subagent']['thread_spawn']
    return {'basis': 'codex session_meta source.subagent.thread_spawn.parent_thread_id',
            'parent_thread_id': spawn['parent_thread_id'], 'depth': spawn.get('depth'),
            'agent_path': spawn.get('agent_path') if isinstance(spawn.get('agent_path'), str) else None,
            'header_sha256': hashlib.sha256(raw).hexdigest()}


class ChildRolloutCapture(lb.RolloutCapture):
    """``RolloutCapture`` for a verified child thread: its rollout opens with a child ``session_meta``.

    It pages with ``RolloutCapture._page``; only the session_meta predicate and the first-row
    refusal differ.
    """

    _NOT_THIS_SESSION = 'rollout does not start with this child session metadata'

    def __init__(self, path, *, parent, index=None):
        super().__init__(path, index=index)
        self.parent = parent

    def _session_meta_matches(self, value, session, workspace):
        return child_meta_matches(value, session, self.parent, workspace)


# --- Launch events ---------------------------------------------------------------------------------

def _host_receipt(record):
    receipt_path = Path(record['receipt'])
    receipt = lb._receipt(receipt_path)
    if receipt['source'] != 'host' or receipt['launch_id'] != record['launch_id']:
        raise lb.LaunchRefused('the spool launch record does not name this host launch receipt')
    return leaf.mark_store(receipt_path), receipt


def _check_cwd(payload, workspace):
    cwd = payload.get('cwd')
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or Path(cwd).resolve() != Path(workspace):
        raise lb.LaunchRefused('hook cwd differs from the launch workspace')


def _launch_event(record, payload):
    receipt_path, receipt = _host_receipt(record)
    if not isinstance(payload, dict):
        raise lb.LaunchRefused('hook payload must be a JSON object')
    native = payload.get(receipt['profile']['session_id'].get('field', 'session_id'))
    session = lb._read_session(receipt_path.parent)
    if session is not None and isinstance(native, str) and native != session['native_session_id']:
        return _child_event(receipt_path, receipt, session, payload, native)
    return lb.hook(receipt_path, payload)


def _child_transcript(receipt, child, value):
    from kp_agent_tooling._impl.service.native_history_import import _codex_identity
    if not isinstance(value, str) or not value or len(value) > 4096 or not os.path.isabs(value):
        raise lb.LaunchRefused('hook transcript_path must be an absolute path')
    resolved = Path(value).resolve()
    try:
        identity = _codex_identity(resolved)
    except ValueError:
        identity = None
    if (not resolved.is_relative_to(Path(receipt['transcript_root'])) or resolved.suffix != '.jsonl'
            or identity != child.lower()):
        raise lb.LaunchRefused('hook transcript is not this child rollout')
    return str(resolved)


def _read_child(path):
    if not path.exists() and not path.is_symlink():
        return None
    value = private_json(path)
    if (not isinstance(value, dict) or value.get('schema_version') != CHILD_SCHEMA
            or set(value) != {'schema_version', 'native_session_id', 'parent_session_id', 'transcript', 'evidence'}):
        raise lb.LaunchRefused('child session state is invalid')
    return value


def _child_event(receipt_path, receipt, session, payload, child):
    """Bind and capture a delegated child session only with transcript evidence of its parentage."""
    from kp_agent_tooling._impl.service.episodic_memory_tools import from_config
    from kp_agent_tooling._impl.service.session_bindings import bind
    profile = receipt['profile']
    capture = profile['capture']
    if capture['mode'] != 'transcript' or capture['parser'] != 'codex-rollout':
        # Claude subagents run inside the parent session: their hooks carry the parent's id and
        # their rows are sidechains of the parent transcript. A new id is not a subagent.
        raise lb.LaunchRefused('hook session differs from the bound session; this harness has no '
                               'separately transcribed child sessions')
    if not _ID.fullmatch(child):
        raise lb.LaunchRefused('hook payload lacks a safe session id')
    event = payload.get('hook_event_name')
    if event not in profile['hooks']['events']:
        raise lb.LaunchRefused('hook event is not configured for this launch')
    _check_cwd(payload, receipt['workspace'])
    config, registry, _, _ = components(receipt['config'])
    run = receipt_path.parent
    if run.parent.resolve() != leaf.launches_dir(config['state_root']).resolve():
        raise lb.LaunchRefused('receipt is not a launch receipt of this state root')
    parent = session['native_session_id']
    transcript = _child_transcript(receipt, child, payload.get('transcript_path'))
    evidence = child_evidence(transcript, child, parent, receipt['workspace'])
    children = lb.private_dir(run / 'children')
    record_path, memory_path = children / f'{child}.json', children / f'{child}.memory.json'
    bound = False
    with lb._locked(run / '.lock'):
        existing = _read_child(record_path)
        if existing is None:
            bind(receipt['config'], {'harness': receipt['harness'], 'provider': receipt['provider'],
                                     'model': receipt['model'], 'native_session_id': child,
                                     'desk_id': receipt['desk_id'], 'source': receipt['source'],
                                     'workspace': receipt['workspace'], 'parent_session_id': parent})
            lb._write_once(memory_path, {**config, 'provider_session_id': child})
            lb._write_once(record_path, {'schema_version': CHILD_SCHEMA, 'native_session_id': child,
                                         'parent_session_id': parent, 'transcript': transcript, 'evidence': evidence})
            bound = True
        elif existing['transcript'] != transcript or existing['parent_session_id'] != parent:
            raise lb.LaunchRefused('hook transcript differs from the bound child transcript')
    adapter = from_config(memory_path)
    try:
        if adapter.session != child:
            raise lb.LaunchRefused('memory configuration names another session')
        binding_key = adapter.store._binding(child)
        if binding_key != receipt['binding_key']:
            raise lb.LaunchRefused('session is admitted to another desk')
        result = {'status': 'verified', 'event': event, 'native_session_id': child, 'parent_session_id': parent,
                  'desk_id': receipt['desk_id'], 'binding_key': binding_key, 'bound_by_this_event': bound,
                  'evidence': evidence, 'capture': {'status': 'not_due'}}
        if event in lb.CAPTURE_EVENTS:
            if not registry.allows_capture(binding_key):
                result['capture'] = {'status': 'disabled'}
            else:
                result['capture'] = _capture_child(receipt, payload, adapter, child, parent, transcript)
        return result
    finally:
        adapter.close()


def _capture_child(receipt, payload, adapter, child, parent, transcript):
    from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue
    from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
    store = adapter.store
    index = EpisodicSearchIndex(leaf.store_path(store.path, leaf.SEARCH_INDEX_DB, sibling=True), episode_store=store)
    queue = ConsolidationQueue(leaf.mark_store(receipt['queue']), store=store)
    return ChildRolloutCapture(leaf.mark_store(receipt['capture_ledger']), parent=parent, index=index).capture(
        child, store=store, queue=queue, transcript_path=Path(transcript), workspace=receipt['workspace'],
        reason='session_end' if payload.get('hook_event_name') == 'SessionEnd' else 'batch')


# --- Policy events ---------------------------------------------------------------------------------

def _policy_profile(record):
    profile = harness_profiles.validate_profile(record.get('profile'))
    if (profile != record['profile'] or record.get('events') != profile['hooks']['events']
            or record.get('source') != 'operator'):
        raise lb.LaunchRefused('the policy record is invalid')
    return profile


def _policy_transcript(record, profile, native, value):
    from kp_agent_tooling._impl.service.native_history_import import _codex_identity
    if profile['capture']['mode'] != 'transcript':
        return None  # as T3: a profile without transcript capture pins no transcript
    if not isinstance(value, str) or not value or len(value) > 4096 or not os.path.isabs(value):
        raise lb.LaunchRefused('hook transcript_path must be an absolute path')
    resolved = Path(value).resolve()
    root = Path(record['transcript_root'])
    if profile['capture'].get('parser') == 'claude-jsonl':
        if resolved != lb.claude_transcript(root, record['project'], native).resolve():
            raise lb.LaunchRefused('hook transcript is not this session transcript')
    else:
        try:
            identity = _codex_identity(resolved)
        except ValueError:
            identity = None
        if not resolved.is_relative_to(root) or resolved.suffix != '.jsonl' or identity != native.lower():
            raise lb.LaunchRefused('hook transcript is not this session rollout')
        first = _first_row(resolved)
        if first is None or not lb.codex_meta_matches(first[1], native, record['project']):
            raise lb.LaunchRefused('the rollout does not open with a root session_meta for this session and project')
    if resolved.is_symlink() or not resolved.is_file():
        raise lb.LaunchRefused('the session transcript does not exist yet; nothing is bound')
    return str(resolved)


def _policy_receipt(record, profile, config_path, config, context, run, native, paths):
    files = {'receipt': str(run / 'launch.json'), 'memory_config': str(run / 'memory.json')}
    return {'schema_version': lb.RECEIPT_SCHEMA, 'launch_id': run.name, 'config': str(config_path),
            'memory_config': files['memory_config'], 'harness': record['harness'],
            'provider': host_adapter.DEFAULT_TARGET, 'model': host_adapter.DEFAULT_TARGET,
            'desk_id': record['desk_id'], 'workspace': record['project'], 'task_id': 'host-policy',
            'source': 'operator', 'parent_session_id': None, 'binding_key': context['desk']['binding_id'],
            'profile': profile, 'native_session_id': native, 'transcript_root': record['transcript_root'],
            **{k: (str(v) if v is not None else None) for k, v in paths.items()}, 'files': files,
            'created_at': _now(),
            'authority': f"operator project policy {record['launch_id']} (kp-agent-host install-hooks); "
                         'fixes desk, harness and workspace; hook payloads select nothing'}


def _policy_event(record, payload):
    from kp_agent_tooling._impl.service.session_bindings import bind
    profile = _policy_profile(record)
    if not isinstance(payload, dict):
        raise lb.LaunchRefused('hook payload must be a JSON object')
    if payload.get('hook_event_name') not in profile['hooks']['events']:
        raise lb.LaunchRefused('hook event is not configured for this policy')
    cwd = payload.get('cwd')
    if not isinstance(cwd, str) or not os.path.isabs(cwd) or Path(cwd).resolve() != Path(record['project']):
        raise lb.LaunchRefused('hook cwd is not the policy project')
    native = payload.get(profile['session_id'].get('field', 'session_id'))
    if not isinstance(native, str) or not _ID.fullmatch(native):
        raise lb.LaunchRefused('hook payload lacks a safe session id')
    config_path = record['config']
    config, registry, ledger, store = components(config_path)
    state = leaf.mark_store(config['state_root'])
    launches = lb.private_dir(leaf.launches_dir(state))
    run = launches / str(uuid.uuid5(POLICY_NAMESPACE, f"{record['launch_id']}:{native}"))
    receipt_path = run / 'launch.json'
    if not (receipt_path.is_file() and (run / 'session.json').is_file()):
        if record['status'] != 'active':
            raise lb.LaunchRefused('the policy is revoked; no new session is bound')
        transcript = _policy_transcript(record, profile, native, payload.get('transcript_path'))
        context = registry.context(record['desk_id'])
        if context['desk']['desk_id'] != record['desk_id']:
            raise lb.LaunchRefused('the policy desk is not registered in this registry')
        lb._assert_state_ready(ledger)
        paths = lb._ledgers(profile, state)
        with lb._locked(launches / '.lock'):
            lb._initialize_ledgers(profile, paths, store)
        lb.private_dir(run)
        with lb._locked(run / '.lock'):
            if not receipt_path.exists() and not receipt_path.is_symlink():
                lb.write_private(receipt_path, _policy_receipt(record, profile, config_path, config, context, run,
                                                               native, paths))
            receipt = lb._receipt(receipt_path)
            if (receipt['native_session_id'] != native or receipt['desk_id'] != record['desk_id']
                    or receipt['source'] != 'operator' or receipt['workspace'] != record['project']):
                raise lb.LaunchRefused('policy session receipt differs from the policy')
            bind(config_path, lb._binding_request(receipt, native))
            lb._write_once(run / 'memory.json', {**config, 'provider_session_id': native})
            lb._write_once(run / 'session.json', lb._session_record(native, transcript))
        result = lb.hook(receipt_path, payload)
        return {**result, 'policy_id': record['launch_id'], 'bound_by_this_event': True, 'source': 'operator'}
    return {**lb.hook(receipt_path, payload), 'policy_id': record['launch_id'], 'source': 'operator'}


# --- Passes ----------------------------------------------------------------------------------------

def _envelope(event, kind, record, launch_id):
    if (not isinstance(event, dict) or set(event) != _ENVELOPE or event['schema_version'] != host_adapter.EVENT_SCHEMA
            or event['launch_id'] != launch_id or event['kind'] != kind
            or event['receipt'] != host_adapter.receipt_reference(kind, record)
            or not isinstance(event['payload'], dict)):
        raise lb.LaunchRefused('spool event does not belong to this spool record')
    return event['payload']


def _summary(result):
    keys = ('event', 'native_session_id', 'parent_session_id', 'desk_id', 'bound_by_this_event', 'policy_id',
            'source')
    summary = {'result': result.get('status'), **{key: result[key] for key in keys if key in result}}
    capture = result.get('capture')
    if isinstance(capture, dict):
        summary['capture'] = {key: capture[key] for key in ('status', 'episodes') if key in capture}
        if 'batches' in capture:
            summary['capture']['episodes'] = [b['episode_id'] for b in capture['batches'] if b.get('episode_id')]
    if 'evidence' in result:
        summary['evidence'] = result['evidence']['basis']
    return summary


def _apply(kind, record, launch_id, line):
    """('applied' | 'refused' | 'retry', detail) for one spool line."""
    try:
        payload = _envelope(json.loads(line), kind, record, launch_id)
        result = _launch_event(record, payload) if kind == 'launch' else _policy_event(record, payload)
        return 'applied', _summary(result)
    except ValueError as error:  # LaunchRefused, desk conflicts, JSON: permanent
        return 'refused', {'category': type(error).__name__, 'message': str(error)[:300]}
    except (RuntimeError, OSError, sqlite3.Error) as error:  # runtime faults: retried
        return 'retry', {'category': type(error).__name__,
                         'message': str(error)[:300] if isinstance(error, RuntimeError) else 'runtime fault'}
    except Exception as error:  # never wedge the spool on an unexpected payload shape
        return 'refused', {'category': type(error).__name__, 'message': 'unexpected payload shape'}


def _state_root(kind, record):
    if kind == 'launch':
        _, receipt = _host_receipt(record)
        config_path = receipt['config']
    else:
        _policy_profile(record)
        config_path = record['config']
    config, _, _, _ = components(config_path)
    return leaf.mark_store(config['state_root'])


# Spool files this process consumed to the end, by (size, mtime, inode): a watcher skips
# them without opening receipts or registry state until they change. Not durable.
_CONSUMED = {}


def _ingest_spool(root, name):
    directory = host_adapter.spool_dir(root, name)
    events = directory / host_adapter.EVENTS
    try:
        info = events.lstat()
    except FileNotFoundError:
        info = None
    marker = None if info is None else (info.st_size, info.st_mtime_ns, info.st_ino)
    if info is not None and (info.st_size == 0 or _CONSUMED.get(name) == marker):
        return {'launch_id': name, 'status': 'idle', 'events': []}
    kind, record = host_adapter.read_record(directory)
    report = {'launch_id': name, 'kind': kind, 'status': 'idle', 'events': []}
    if info is None:
        return report
    if events.is_symlink() or not events.is_file():
        raise lb.LaunchRefused('spool events must be a regular file')
    cursors = Cursors(_state_root(kind, record))
    report = _consume(cursors, events, kind, record, name, report)
    if report.get('consumed_to_end'):
        _CONSUMED[name] = marker
    report.pop('consumed_to_end', None)
    return report


def _consume(cursors, events, kind, record, name, report):
    with cursors.locked():
        offset, digest, attempts = cursors.get(name)
        size = events.stat().st_size
        if size == offset:
            return {**report, 'consumed_to_end': True}
        if size < offset:
            raise lb.LaunchRefused('spool file truncated below its cursor')
        if size > SPOOL_BYTES:
            raise lb.LaunchRefused('spool file exceeds its bound; rotate the launch')
        hasher = hashlib.sha256()
        with events.open('rb') as stream:
            remaining = offset
            while remaining:
                chunk = stream.read(min(1048576, remaining))
                if not chunk:
                    raise lb.LaunchRefused('spool file truncated below its cursor')
                hasher.update(chunk)
                remaining -= len(chunk)
            if hasher.hexdigest() != digest:
                raise lb.LaunchRefused('consumed spool prefix rewritten')
            report['status'] = 'ingested'
            while len(report['events']) < EVENTS_PER_PASS:
                line = stream.readline(LINE_BYTES + 1)
                if not line.endswith(b'\n'):
                    if len(line) <= LINE_BYTES:
                        break  # incomplete tail: the hook is still writing
                    rest = stream.readline(SPOOL_BYTES)
                    if not rest.endswith(b'\n'):
                        break
                    line += rest
                    outcome = ('refused', {'category': 'LaunchRefused', 'message': 'spool line exceeds its bound'})
                else:
                    outcome = _apply(kind, record, name, line)
                start, end = offset, offset + len(line)
                if outcome[0] == 'retry':
                    attempts += 1
                    if attempts < ATTEMPTS:
                        cursors.attempt(name, kind, offset, hasher.hexdigest(), attempts)
                        report['events'].append({'offset': start, 'status': 'retry', 'attempt': attempts,
                                                 **outcome[1]})
                        break
                    outcome = ('failed', {**outcome[1], 'attempts': attempts})
                hasher.update(line)
                offset, attempts = end, 0
                cursors.advance(name, kind, start, end, hasher.hexdigest(), outcome[0], outcome[1])
                report['events'].append({'offset': start, 'status': outcome[0], **outcome[1]})
    report['consumed_to_end'] = offset == size
    return report


def ingest(root):
    """One pass over every spool directory under ``root``."""
    root = Path(root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise lb.LaunchRefused('--root must be an existing absolute spool directory')
    reports, counts = [], {}
    with os.scandir(root) as entries:
        names = sorted(entry.name for entry in entries
                       if host_adapter._UUID.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False))
    for name in names[:host_adapter.MAX_SPOOL_ENTRIES]:
        try:
            report = _ingest_spool(root, name)
        except ValueError as error:
            report = {'launch_id': name, 'status': 'error', 'category': type(error).__name__,
                      'message': str(error)[:300]}
        except (RuntimeError, OSError, sqlite3.Error) as error:
            report = {'launch_id': name, 'status': 'error', 'category': type(error).__name__,
                      'message': 'spool unavailable; retried on the next pass'}
        for event in report.get('events', []):
            counts[event['status']] = counts.get(event['status'], 0) + 1
        if report['status'] != 'idle':
            reports.append(report)
    errors = sum(1 for report in reports if report['status'] == 'error')
    return {'status': 'error' if errors else 'ok', 'root': str(root), 'spools': reports, 'counts': counts}


def watch(root, *, interval=2.0, command=None, out=None):
    """Ingest every ``interval`` seconds until signalled; with ``command``, supervise it as well.

    The capture role runs ``kp-agent-launch ingest-spool --watch -- <its own command>``:
    the command runs as a child, signals are forwarded to it, and the watcher exits
    with the child's status when it ends.

    That rule is unchanged by T12a. The child (``kp-agent-workspace-capture ... watch``)
    no longer exits on a pass error; it ends only on a signal, at ``--max-passes``, on a
    startup refusal (an invalid interval), or when it is killed (an OOM kill). Whichever
    it is, the watcher stops ingesting and exits with that status, never respawning the
    child itself: the role's restart policy (``unless-stopped``) starts both again, and a
    deliberate stop leaves both stopped. A pass of the watcher's own ingestion that
    raises is reported and retried on the next pass, as before.
    """
    out = out or sys.stdout
    state = {'stop': False}
    child = subprocess.Popen(command) if command else None

    def handler(signum, frame):
        state['stop'] = True
        if child is not None and child.poll() is None:
            child.send_signal(signum)

    previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        while not state['stop']:
            try:
                summary = ingest(root)
                if summary['spools']:
                    print(json.dumps(summary, sort_keys=True), file=out, flush=True)
            except Exception as error:
                print(json.dumps({'status': 'error', 'category': type(error).__name__}), file=out, flush=True)
            deadline = time.monotonic() + interval
            while not state['stop'] and time.monotonic() < deadline:
                if child is not None and child.poll() is not None:
                    state['stop'] = True
                    break
                time.sleep(0.2)
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    if child is None:
        return 0
    code = child.returncode
    return 128 - code if code < 0 else code
