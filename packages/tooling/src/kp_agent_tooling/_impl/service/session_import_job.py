"""Resumable, byte-bounded import of one selected native Codex rollout.

The source is evidence, never a desk admission. Preview is read-only; apply
requires the exact preview token and an explicit consent bit. Each continuation
seals visible rows before advancing its durable cursor, then verifies a small
search projection. A crash can replay that batch without replacing evidence.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.leaf import sha256_hex as _sha
from kp_agent_tooling._impl.service.episodic_memory import EpisodeConflict, _id
from kp_agent_tooling._impl.service.episodic_search import drain_after_seal
from kp_agent_tooling._impl.service.native_history_import import (
    MAX_LINE_BYTES, _codex_events, _codex_identity,
)
from kp_agent_tooling._impl.service.session_sources import SessionSources


SCHEMA = 'ops.session-import.result.v1'
MAX_BATCH_BYTES = 8_000_000
MIN_BATCH_BYTES = 512_000
MAX_BATCH_ROWS = 2_000
MAX_BATCH_EVENTS = 2_000
MAX_SKIP_LINE_BYTES = 4_000_000
DEFAULT_BATCH_BYTES = 4_000_000
DEFAULT_BATCH_ROWS = 2_000
DEFAULT_BATCH_EVENTS = 1_000
_JOB_SCHEMA = '''
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, plan BLOB NOT NULL,
 cursor INTEGER NOT NULL, prefix_sha256 TEXT NOT NULL, inode INTEGER NOT NULL,
 device INTEGER NOT NULL, phase TEXT NOT NULL, follow_requested INTEGER NOT NULL,
 counts BLOB NOT NULL, errors BLOB NOT NULL, index_pending BLOB NOT NULL,
 lease_owner TEXT, lease_until REAL, updated_at TEXT NOT NULL
);
'''


class ImportJobError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _json(value):
    return leaf.canonical_json(value, ascii=True, allow_nan=True)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _source(path, native_id):
    if not isinstance(path, str):
        raise ImportJobError('invalid_source', 'Select an absolute regular native JSONL file.')
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.suffix != '.jsonl':
        raise ImportJobError('invalid_source', 'Select an absolute regular native JSONL file.')
    if _codex_identity(path) != native_id.lower():
        raise ImportJobError('native_identity_mismatch',
                             'Selected native session ID does not match the rollout filename.')
    return path


def _digest_prefix(path, limit):
    digest = hashlib.sha256()
    remaining = limit
    with path.open('rb') as stream:
        while remaining:
            chunk = stream.read(min(1_048_576, remaining))
            if not chunk:
                raise ImportJobError('source_truncated', 'Native source is shorter than its saved cursor.')
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _read_native_line(stream):
    """Consume one line with bounded memory, including large control rows."""
    parts = []
    length = 0
    while True:
        chunk = stream.readline(65_536)
        if not chunk:
            return (b''.join(parts) if parts else b''), length, False
        length += len(chunk)
        if length > MAX_SKIP_LINE_BYTES:
            raise ImportJobError('oversized_source_row',
                                 'A native row exceeds the bounded skip limit.')
        if length <= MAX_LINE_BYTES:
            parts.append(chunk)
        if chunk.endswith(b'\n'):
            return b''.join(parts), length, True


def _oversized_control_kind(prefix):
    """Recognize only native control envelopes from their bounded header."""
    head = prefix[:4096]
    before_payload = head.split(b'"payload"', 1)[0]
    top = re.search(rb'"type"\s*:\s*"([a-z_]+)"', before_payload)
    kind = top.group(1) if top else None
    if kind == b'compacted':
        return 'compacted'
    if kind == b'event_msg' and re.search(rb'"type"\s*:\s*"item_completed"', head):
        return 'item_completed'
    return None


def _visible_user(row, offset):
    if row.get('type') == 'event_msg' and isinstance(row.get('payload'), dict):
        if row['payload'].get('type') == 'user_message':
            events, _ = _codex_events(row, offset)
            return bool(events), 'event_msg'
    if row.get('type') == 'response_item' and isinstance(row.get('payload'), dict):
        payload = row['payload']
        if payload.get('type') == 'message' and payload.get('role') == 'user':
            events, _ = _codex_events(row, offset)
            return bool(events), 'response_item'
    return False, None


def _scan_native(path, native_id):
    """Stream metadata and latest native user boundary without retaining text."""
    before = path.stat()
    initial_sha = _digest_prefix(path, before.st_size)
    metadata = None
    anchor = None
    last_unknown_oversized = None
    offset = 0
    with path.open('rb') as stream:
        while offset < before.st_size:
            line, length, complete = _read_native_line(stream)
            if not complete or offset + length > before.st_size:
                break
            if length > MAX_LINE_BYTES:
                if offset == 0:
                    raise ImportJobError('native_metadata_unavailable',
                                         'The native metadata row exceeds the parse bound.')
                if _oversized_control_kind(line) is None:
                    last_unknown_oversized = offset
                offset += length
                continue
            try:
                row = json.loads(line)
            except (UnicodeError, json.JSONDecodeError):
                row = None
            if isinstance(row, dict):
                if offset == 0:
                    payload = row.get('payload')
                    if (row.get('type') != 'session_meta' or not isinstance(payload, dict)
                            or str(payload.get('id', '')).lower() != native_id
                            or not isinstance(payload.get('cwd'), str) or not payload['cwd']):
                        raise ImportJobError('native_metadata_unavailable',
                                             'Selected rollout lacks matching native session metadata.')
                    metadata = {'id': native_id, 'cwd': payload['cwd']}
                elif row.get('type') == 'session_meta':
                    payload = row.get('payload')
                    if (not isinstance(payload, dict) or
                            str(payload.get('id', '')).lower() != native_id or
                            payload.get('cwd') != metadata['cwd']):
                        raise ImportJobError('native_identity_changed',
                                             'Native session metadata changed within the selected file.')
                is_user, kind = _visible_user(row, offset)
                if is_user and metadata is not None:
                    anchor = {'native_session_id': native_id,
                              'user_row_offset': offset,
                              'user_row_sha256': _sha(line),
                              'native_row_kind': kind,
                              'timestamp': row.get('timestamp')}
            offset += length
    after = path.stat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or after.st_size < before.st_size:
        raise ImportJobError('source_changed', 'Native source rotated or truncated during preview; retry.')
    if _digest_prefix(path, before.st_size) != initial_sha:
        raise ImportJobError('source_changed', 'Native source prefix changed during preview; retry.')
    if metadata is None:
        raise ImportJobError('native_metadata_unavailable', 'Native session metadata is unavailable.')
    return metadata, anchor, before, _digest_prefix(path, offset), offset, last_unknown_oversized


def _decode_token(token):
    try:
        plan = json.loads(leaf.canonical_token_bytes(token, max_bytes=8192))
    except (AttributeError, UnicodeError, ValueError, json.JSONDecodeError):
        raise ImportJobError('invalid_plan_token', 'Preview token is invalid.') from None
    return plan


def _request(request):
    if not isinstance(request, dict) or request.get('schema_version') != 'ops.session-import.request.v1':
        raise ImportJobError('invalid_request', 'Session import request schema is invalid.')
    allowed = {'schema_version', 'runtime', 'source_file', 'native_session_id',
               'mode', 'follow', 'import_actor', 'selected_desk_id', 'max_batch_bytes',
               'max_batch_rows', 'max_batch_events'}
    if request.get('runtime') != 'codex':
        raise ImportJobError('unsupported_runtime',
                             'Streaming session jobs currently support native Codex JSONL only.')
    if set(request) - allowed:
        raise ImportJobError('invalid_request', 'Only explicit native Codex import is supported.')
    native_id = request.get('native_session_id')
    if not isinstance(native_id, str) or len(native_id) != 36:
        raise ImportJobError('invalid_request', 'Exact native session ID is required.')
    path = _source(request.get('source_file'), native_id)
    mode = request.get('mode')
    if mode not in ('full', 'current-turn-and-forward') or type(request.get('follow', False)) is not bool:
        raise ImportJobError('invalid_request', 'Choose an explicit import mode and follow option.')
    actor = request.get('import_actor')
    if not isinstance(actor, str) or not 1 <= len(actor) <= 512:
        raise ImportJobError('invalid_request', 'Bounded import actor is required.')
    desk = request.get('selected_desk_id')
    if not isinstance(desk, str) or not desk.startswith('binding:'):
        raise ImportJobError('invalid_request', 'Select an approved desk binding.')
    bounds = {}
    for key, default, minimum, maximum in (
        ('max_batch_bytes', DEFAULT_BATCH_BYTES, MIN_BATCH_BYTES, MAX_BATCH_BYTES),
        ('max_batch_rows', DEFAULT_BATCH_ROWS, 1, MAX_BATCH_ROWS),
        ('max_batch_events', DEFAULT_BATCH_EVENTS, 125, MAX_BATCH_EVENTS),
    ):
        value = request.get(key, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ImportJobError('invalid_request', f'{key} is outside its bounded range.')
        bounds[key] = value
    return {'runtime': 'codex', 'source_file': str(path),
            'native_session_id': native_id.lower(), 'mode': mode,
            'follow': request.get('follow', False), 'import_actor': actor,
            'selected_desk_id': desk, **bounds}


def _job_id(tenant_id, plan):
    key = {'tenant_id': tenant_id, 'native_session_id': plan['native_session_id'],
           'source_file': plan['source_file'], 'mode': plan['mode'],
           'start_offset': plan['start_offset']}
    if plan['mode'] == 'full' and not plan['follow']:
        key['reviewed_complete_end'] = plan['reviewed_complete_end']
        key['reviewed_prefix_sha256'] = plan['reviewed_prefix_sha256']
    return _id('session-import-job', key)


class SessionImportJobs:
    def __init__(self, store, tenant_id):
        self.store = store
        self.tenant_id = tenant_id
        self.path = leaf.store_path(store.path, leaf.SESSION_IMPORT_JOBS_DB, sibling=True)
        self.sources = SessionSources(store)
        with closing(store._connect()) as db:
            if not self.sources.available(db):
                raise ImportJobError('source_schema_unavailable',
                                     'Initialize the session source catalog before importing.')

    def _db(self, writable=False):
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ImportJobError('job_store_unavailable', 'Import job store is unavailable.')
        if not self.path.exists():
            if not writable:
                return None
            try:
                db = leaf.sqlite_create(self.path)
            except FileExistsError:
                db = None  # created concurrently: opened below like any existing job store
            if db is not None:
                try:
                    db.executescript(_JOB_SCHEMA)
                except Exception:
                    db.close()
                    self.path.unlink(missing_ok=True)
                    raise
                return db
        mode = 'rw' if writable else 'ro'
        return leaf.sqlite_connect(self.path, mode=mode, resolve=True, timeout=10)

    def preview(self, request):
        selected = _request(request)
        desk = self._desk(selected['selected_desk_id'])
        path = Path(selected['source_file'])
        metadata, anchor, stat, digest, complete_end, last_unknown_oversized = _scan_native(
            path, selected['native_session_id'])
        if selected['mode'] == 'current-turn-and-forward' and anchor is None:
            raise ImportJobError('anchor_unavailable',
                                 'No trustworthy native user-turn boundary was found.')
        if (selected['mode'] == 'current-turn-and-forward' and
                last_unknown_oversized is not None and
                last_unknown_oversized > anchor['user_row_offset']):
            raise ImportJobError('anchor_ambiguous',
                                 'A later unclassified large native row could contain a user boundary.')
        start = anchor['user_row_offset'] if selected['mode'] != 'full' else 0
        plan = {**selected, 'start_offset': start, 'current_turn_anchor': anchor if start else None,
                'reviewed_size': stat.st_size, 'reviewed_prefix_sha256': digest,
                'reviewed_complete_end': complete_end,
                'device': stat.st_dev, 'inode': stat.st_ino,
                'native_cwd': metadata['cwd']}
        token = leaf.canonical_token(plan, ascii=True, allow_nan=True)
        job_id = _job_id(self.tenant_id, plan)
        batch = self._batch(plan, start, max_end=stat.st_size)
        return {'schema_version': SCHEMA, 'status': 'preview', 'job_id': job_id,
                'plan_token': token, 'current_turn_anchor': plan['current_turn_anchor'],
                'coverage': self._coverage(plan, start, stat.st_size,
                                           batch['partial_trailing_bytes']),
                'counts': batch['counts'], 'next_batch_offset': batch['next_offset'],
                'attribution': {'selected_desk_id': desk.binding_key,
                                'desk_label': desk.desk_label,
                                'status': 'proposed_unasserted'},
                'authority': 'visible native evidence; no desk attribution or admission'}

    def _desk(self, binding_key):
        desk = next((item for item in self.store.registry.list_bindings()
                     if item.binding_key == binding_key and item.tenant_id == self.tenant_id), None)
        if desk is None:
            raise ImportJobError('unknown_desk', 'Choose a registered desk in this tenant.')
        return desk

    def desks(self):
        rows = [item for item in self.store.registry.list_bindings()
                if item.tenant_id == self.tenant_id]
        return {'schema_version': SCHEMA, 'status': 'ok', 'tenant_id': self.tenant_id,
                'desks': [{'binding_key': row.binding_key, 'desk_label': row.desk_label,
                           'role': row.role, 'repo_key': row.repo_key} for row in rows]}

    def _coverage(self, plan, cursor, source_size, partial=0):
        snapshot_end = plan['reviewed_complete_end'] if plan['mode'] == 'full' and not plan['follow'] else source_size
        return {'mode': plan['mode'], 'start_offset': plan['start_offset'],
                'next_offset': cursor, 'observed_size': source_size,
                'reviewed_complete_end': plan['reviewed_complete_end'],
                'reviewed_partial_trailing_bytes': plan['reviewed_size'] - plan['reviewed_complete_end'],
                'complete_at_snapshot': cursor == snapshot_end and partial == 0,
                'partial_trailing_bytes': partial,
                'pre_anchor_excluded_bytes': plan['start_offset'],
                'compaction_policy': 'visible user/assistant messages retained across compaction rows; control and reasoning omitted'}

    def _batch(self, plan, start, *, max_end=None):
        path = Path(plan['source_file'])
        size = path.stat().st_size if max_end is None else max_end
        counts = {'complete_rows': 0, 'visible_rows': 0, 'visible_events': 0,
                  'omitted_control_rows': 0, 'compaction_markers': 0,
                  'quarantined_rows': 0}
        staged = []
        rejections = []
        offset = start
        partial = 0
        with path.open('rb') as stream:
            stream.seek(start)
            while offset < size and counts['complete_rows'] < plan['max_batch_rows']:
                line_start = offset
                line, length, complete = _read_native_line(stream)
                if not length:
                    break
                if line_start + length > size or not complete:
                    partial = size - line_start
                    break
                # An oversized control row may exceed a caller's ordinary batch
                # budget. Consume one such bounded row so the cursor cannot stall.
                if (offset + length - start > plan['max_batch_bytes'] and
                        not (offset == start and length > MAX_LINE_BYTES)):
                    break
                if length > MAX_LINE_BYTES:
                    kind = _oversized_control_kind(line)
                    rejections.append({'offset': offset, 'reason':
                                       'oversized_control_row' if kind else 'oversized_unparsed_row'})
                    if length > plan['max_batch_bytes']:
                        counts['oversized_skip_budget_overrun_rows'] = (
                            counts.get('oversized_skip_budget_overrun_rows', 0) + 1)
                    counts['quarantined_rows'] += 1
                    if kind == 'compacted':
                        counts['compaction_markers'] += 1
                    offset += length
                    counts['complete_rows'] += 1
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError
                except (UnicodeError, json.JSONDecodeError, ValueError):
                    rejections.append({'offset': offset, 'reason': 'malformed_json_or_utf8'})
                    counts['quarantined_rows'] += 1
                    offset += length
                    counts['complete_rows'] += 1
                    continue
                if row.get('type') == 'session_meta':
                    payload = row.get('payload')
                    if (not isinstance(payload, dict) or
                            str(payload.get('id', '')).lower() != plan['native_session_id'] or
                            payload.get('cwd') != plan['native_cwd']):
                        raise ImportJobError('native_identity_changed',
                                             'Native session metadata changed within the selected file.')
                events, omissions = _codex_events(row, offset)
                if len(events) + counts['visible_events'] > plan['max_batch_events']:
                    break
                if events:
                    if len(events) > 125 or any(len(event['text'].encode()) > 128000 for event in events):
                        rejections.append({'offset': offset, 'reason': 'event_bound_exceeded'})
                        counts['quarantined_rows'] += 1
                    else:
                        staged.append((offset, line, row, events, omissions))
                        counts['visible_rows'] += 1
                        counts['visible_events'] += len(events)
                else:
                    counts['omitted_control_rows'] += 1
                    if row.get('type') in ('compacted', 'compaction') or (
                            row.get('type') == 'event_msg' and
                            isinstance(row.get('payload'), dict) and
                            'compact' in str(row['payload'].get('type', ''))):
                        counts['compaction_markers'] += 1
                offset += length
                counts['complete_rows'] += 1
        if offset == start and size > start and not partial:
            raise ImportJobError('batch_cannot_advance',
                                 'The next native row exceeds this batch bound.')
        return {'next_offset': offset, 'partial_trailing_bytes': partial,
                'counts': counts, 'staged': staged, 'rejections': rejections}

    def _load(self, db, job_id):
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM jobs WHERE id=? AND tenant=?',
                         (job_id, self.tenant_id)).fetchone()
        if row is None:
            raise ImportJobError('job_unavailable', 'Session import job is unavailable.')
        return {**dict(row), 'plan': json.loads(row['plan']),
                'counts': json.loads(row['counts']), 'errors': json.loads(row['errors']),
                'index_pending': json.loads(row['index_pending'])}

    def _owner_claim(self, row):
        plan = row['plan']
        identity = _id('source-session', {'schema_version': 'ops.source-session.v1',
            'tenant_id': self.tenant_id, 'runtime': 'codex',
            'native_id': plan['native_session_id']})
        expected_range = ({'native_id': plan['native_session_id'],
                           'source_file': plan['source_file'],
                           'start_offset': plan['start_offset']}
                          if plan['mode'] == 'current-turn-and-forward' else None)
        with closing(self.store._connect()) as db:
            present = db.execute('SELECT 1 FROM source_sessions WHERE id=?', (identity,)).fetchone()
        if not present:
            return None
        meta = self.sources.metadata(identity, self.tenant_id)
        return next((claim for claim in meta['active_claims']
                     if claim['predicate'] == 'session.owner' and
                     claim['object'] == {'kind': 'desk', 'id': plan['selected_desk_id']} and
                     claim.get('source_range') == expected_range), None)

    def _status(self, row):
        path = Path(row['plan']['source_file'])
        size = path.stat().st_size if path.exists() else None
        active = bool(row['lease_owner'] and row['lease_until'] > time.time())
        claim = self._owner_claim(row)
        return {'schema_version': SCHEMA, 'status': 'ok', 'job_id': row['id'],
                'phase': row['phase'], 'runtime': 'codex',
                'native_session_id': row['plan']['native_session_id'],
                'selected_desk_id': row['plan']['selected_desk_id'],
                'source_session_id': _id('source-session', {
                    'schema_version': 'ops.source-session.v1',
                    'tenant_id': self.tenant_id, 'runtime': 'codex',
                    'native_id': row['plan']['native_session_id']}),
                'coverage': self._coverage(row['plan'], row['cursor'], size,
                                           row['counts'].get('partial_trailing_bytes', 0)),
                'cursor': {'offset': row['cursor'],
                           'prefix_sha256': row['prefix_sha256']},
                'counts': row['counts'], 'errors': row['errors'][-20:],
                'index_pending_episodes': len(row['index_pending']),
                'follow_requested': bool(row['follow_requested']),
                'worker_active': active,
                'follower_heartbeat_at': row['updated_at'] if active else None,
                'lease_expires_at': row['lease_until'] if active else None,
                'attribution': {'selected_desk_id': row['plan']['selected_desk_id'],
                                'status': 'asserted' if claim else 'proposed_unasserted',
                                'claim_id': claim['claim_id'] if claim else None},
                'authority': 'historical user assertion if present; no desk admission'}

    def status(self, job_id):
        with closing(self._db()) as db:
            if db is None:
                raise ImportJobError('job_unavailable', 'Session import job is unavailable.')
            return self._status(self._load(db, job_id))

    def list_following(self):
        db = self._db()
        if db is None:
            return {'schema_version': SCHEMA, 'status': 'ok', 'jobs': []}
        with closing(db):
            ids = [row[0] for row in db.execute(
                "SELECT id FROM jobs WHERE tenant=? AND follow_requested=1 "
                "AND phase NOT IN ('error','paused') ORDER BY updated_at", (self.tenant_id,))]
            return {'schema_version': SCHEMA, 'status': 'ok',
                    'jobs': [self._status(self._load(db, identity)) for identity in ids]}

    def apply(self, plan_token, *, consent):
        if consent is not True:
            raise ImportJobError('consent_required', 'Apply requires explicit preview consent.')
        plan = _decode_token(plan_token)
        selected = {key: plan.get(key) for key in (
            'runtime', 'source_file', 'native_session_id', 'mode', 'follow',
            'import_actor', 'selected_desk_id', 'max_batch_bytes',
            'max_batch_rows', 'max_batch_events')}
        # Revalidate the entire selected shape, not just a token digest.
        if _request({'schema_version': 'ops.session-import.request.v1', **selected}) != selected:
            raise ImportJobError('invalid_plan_token', 'Preview selection changed.')
        self._desk(plan['selected_desk_id'])
        path = Path(plan['source_file'])
        stat = path.stat()
        if ((stat.st_dev, stat.st_ino) != (plan['device'], plan['inode']) or
                stat.st_size < plan['reviewed_size'] or
                _digest_prefix(path, plan['reviewed_complete_end']) != plan['reviewed_prefix_sha256']):
            raise ImportJobError('source_changed', 'Reviewed native prefix changed or rotated.')
        anchor = plan.get('current_turn_anchor')
        if plan['mode'] == 'current-turn-and-forward':
            if not isinstance(anchor, dict) or anchor.get('user_row_offset') != plan['start_offset']:
                raise ImportJobError('anchor_unavailable', 'Reviewed native turn anchor is invalid.')
            with path.open('rb') as stream:
                stream.seek(anchor['user_row_offset'])
                line, _, complete = _read_native_line(stream)
            if not complete or len(line) > MAX_LINE_BYTES or _sha(line) != anchor['user_row_sha256']:
                raise ImportJobError('anchor_changed', 'Reviewed native user turn changed.')
            try:
                row = json.loads(line)
            except (UnicodeError, json.JSONDecodeError):
                row = None
            if not isinstance(row, dict) or not _visible_user(row, anchor['user_row_offset'])[0]:
                raise ImportJobError('anchor_changed', 'Reviewed native user turn is unavailable.')
        job_id = _job_id(self.tenant_id, plan)
        prefix = _digest_prefix(path, plan['start_offset'])
        with closing(self._db(writable=True)) as db, db:
            prior = db.execute('SELECT plan FROM jobs WHERE id=?', (job_id,)).fetchone()
            if prior is not None:
                old = json.loads(prior[0])
                if any(old[key] != plan[key] for key in (
                        'source_file', 'native_session_id', 'mode', 'start_offset',
                        'selected_desk_id', 'import_actor', 'follow')):
                    raise ImportJobError('job_conflict', 'This source scope already has a different job.')
                if old['reviewed_size'] != plan['reviewed_size'] or old['reviewed_prefix_sha256'] != plan['reviewed_prefix_sha256']:
                    raise ImportJobError('new_snapshot_requires_new_job',
                                         'This scope already has a reviewed snapshot; continue or select a new scope.')
                return self._status(self._load(db, job_id))
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                job_id, self.tenant_id, _json(plan), plan['start_offset'], prefix,
                stat.st_ino, stat.st_dev, 'ready', int(plan['follow']),
                _json({}), _json([]), _json([]), None, None, _now()))
            return self._status(self._load(db, job_id))

    def _set_error(self, db, row, code, message, *, phase='error'):
        errors = (row['errors'] + [{'code': code, 'message': message,
                                    'at_offset': row['cursor']}])[-20:]
        db.execute('UPDATE jobs SET errors=?,phase=?,updated_at=? WHERE id=?',
                   (_json(errors), phase, _now(), row['id']))

    def advance(self, job_id, *, lease_owner=None):
        """Import at most one bounded batch. Its seals' outbox rows carry the indexing (T12b): a host
        install drains once after the batch, never waiting; inside Compose the indexer role drains."""
        with closing(self._db(writable=True)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = self._load(db, job_id)
            if row['phase'] == 'paused':
                raise ImportJobError('job_paused', 'This import job is paused.')
            if (row['lease_owner'] and row['lease_until'] > time.time()
                    and row['lease_owner'] != lease_owner):
                raise ImportJobError('worker_active', 'Another follower owns this import job.')
            if row['index_pending']:
                # A batch an earlier version left pending for its inline index: retired (T12b). Its
                # rows predate the outbox; upgrade-sources requests the reindex of such a store.
                db.execute('UPDATE jobs SET index_pending=?,phase=?,updated_at=? WHERE id=?',
                           (_json([]), 'ready' if row['phase'] == 'index_error' else row['phase'], _now(), job_id))
                return self._status(self._load(db, job_id))
            plan = row['plan']
            path = _source(plan['source_file'], plan['native_session_id'])
            stat = path.stat()
            if (stat.st_dev, stat.st_ino) != (row['device'], row['inode']):
                self._set_error(db, row, 'source_rotated', 'Selected native file was replaced.')
                db.commit()
                raise ImportJobError('source_rotated', 'Selected native file was replaced.')
            if stat.st_size < row['cursor'] or _digest_prefix(path, row['cursor']) != row['prefix_sha256']:
                self._set_error(db, row, 'source_prefix_changed',
                                'Selected native file was truncated or rewritten.')
                db.commit()
                raise ImportJobError('source_prefix_changed',
                                     'Selected native file was truncated or rewritten.')
            # The consent boundary includes reviewed bytes not yet captured.
            # Appends are allowed; rewriting the reviewed snapshot is not.
            reviewed_end = plan['reviewed_complete_end']
            if (stat.st_size < reviewed_end or
                    _digest_prefix(path, reviewed_end) != plan['reviewed_prefix_sha256']):
                self._set_error(db, row, 'reviewed_source_changed',
                                'Reviewed native source changed after apply; preview again.')
                db.commit()
                raise ImportJobError('reviewed_source_changed',
                                     'Reviewed native source changed after apply; preview again.')
            target_end = stat.st_size if plan['follow'] else plan['reviewed_complete_end']
            if row['cursor'] == target_end:
                phase = 'following' if row['follow_requested'] else 'complete'
                db.execute('UPDATE jobs SET phase=?,updated_at=? WHERE id=?',
                           (phase, _now(), job_id))
                return self._status(self._load(db, job_id))
            snapshot_sha = _digest_prefix(path, stat.st_size)
            batch = self._batch(plan, row['cursor'], max_end=target_end)
            after = path.stat()
            if ((after.st_dev, after.st_ino) != (stat.st_dev, stat.st_ino) or
                    after.st_size < stat.st_size or
                    _digest_prefix(path, stat.st_size) != snapshot_sha):
                raise ImportJobError('source_changed', 'Native source changed during this batch; retry.')
            session_id = _id('source-session', {
                'schema_version': 'ops.source-session.v1', 'tenant_id': self.tenant_id,
                'runtime': 'codex', 'native_id': plan['native_session_id']})
            ids = []
            imported = already = 0
            if batch['staged']:
                actual = self.sources.register(tenant_id=self.tenant_id, runtime='codex',
                                               native_id=plan['native_session_id'])
                if actual != session_id:
                    raise ImportJobError('source_identity_conflict', 'Source identity changed.')
            for offset, line, native_row, events, omissions in batch['staged']:
                source_ref = (f"native-jsonl:codex:{_sha(plan['native_session_id'].encode())}:"
                              f"{offset}")
                provenance = {
                    'source_system': 'local:codex-jsonl',
                    'row_id': f"{path}:{offset}:{offset + len(line)}",
                    'row_digest': _sha(line),
                    'source_artifact_sha256': snapshot_sha,
                    'source_coordinates': {'path': str(path), 'start': offset,
                                           'end': offset + len(line)},
                    'native_parent_id': None, 'native_child_id': None,
                    'legacy_host_session_id': plan['native_session_id'],
                    'evidence_event_map': [{'event_start': 0, 'event_count': len(events),
                                            'authored_at': native_row.get('timestamp', 'unknown'),
                                            'row_sha256': _sha(line)}],
                    'omissions': omissions, 'import_actor': plan['import_actor'],
                }
                # A prior sealed row may have been captured by another job or
                # before an append, so its snapshot hash/import actor can differ.
                # Verify immutable row evidence and reuse its original identity.
                with closing(self.store._connect()) as source_db:
                    prior = source_db.execute(
                        'SELECT id FROM source_episodes WHERE session_id=? AND source_ref=?',
                        (session_id, source_ref)).fetchone()
                if prior:
                    old = self.sources.read(prior[0])
                    old_prov = old.get('source_provenance', {})
                    coords = old_prov.get('source_coordinates', {})
                    if (old_prov.get('row_digest') != _sha(line) or
                            coords.get('start') != offset or
                            coords.get('end') != offset + len(line) or
                            old.get('events') != events):
                        raise EpisodeConflict('native source coordinate changed after import')
                    receipt = {'episode_id': prior[0], 'status': 'already_present'}
                else:
                    receipt = self.sources.import_episode(session_id=session_id,
                        source_ref=source_ref, events=events, provenance=provenance)
                ids.append(receipt['episode_id'])
                imported += receipt['status'] == 'imported'
                already += receipt['status'] == 'already_present'
            new_cursor = batch['next_offset']
            counts = row['counts']
            for name, amount in batch['counts'].items():
                counts[name] = counts.get(name, 0) + amount
            counts['imported'] = counts.get('imported', 0) + imported
            counts['already_present'] = counts.get('already_present', 0) + already
            counts['partial_trailing_bytes'] = batch['partial_trailing_bytes']
            phase = ('waiting_for_complete_row' if batch['partial_trailing_bytes'] else
                     'following' if row['follow_requested'] and new_cursor == target_end else
                     'complete' if new_cursor == target_end else 'ready')
            db.execute('UPDATE jobs SET cursor=?,prefix_sha256=?,counts=?,errors=?,index_pending=?,phase=?,updated_at=? WHERE id=?',
                       (new_cursor, _digest_prefix(path, new_cursor), _json(counts),
                        _json((row['errors'] + [
                            {'code': rejection['reason'], 'message': 'Native row omitted.',
                             'at_offset': rejection['offset']}
                            for rejection in batch['rejections']])[-20:]),
                        _json([]), phase, _now(), job_id))
            # The source writes above are already sealed, each with its outbox row.
        if ids:
            drain_after_seal(self.store)
        return self.status(job_id)

    def stop(self, job_id):
        with closing(self._db(writable=True)) as db, db:
            self._load(db, job_id)
            db.execute("UPDATE jobs SET follow_requested=0,phase='paused',updated_at=? WHERE id=?",
                       (_now(), job_id))
        return self.status(job_id)

    def resume(self, job_id):
        with closing(self._db(writable=True)) as db, db:
            row = self._load(db, job_id)
            if row['phase'] != 'paused':
                return self._status(row)
            db.execute('UPDATE jobs SET follow_requested=?,phase=?,updated_at=? WHERE id=?',
                       (int(row['plan']['follow']), 'ready', _now(), job_id))
        return self.status(job_id)

    def follow(self, job_id, *, interval_seconds=1):
        owner = uuid4().hex
        with closing(self._db(writable=True)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = self._load(db, job_id)
            if row['lease_owner'] and row['lease_until'] > time.time():
                raise ImportJobError('worker_active', 'Another follower owns this import job.')
            if row['phase'] in ('complete', 'paused', 'error'):
                return self._status(row)
            db.execute('UPDATE jobs SET lease_owner=?,lease_until=?,updated_at=? WHERE id=?',
                       (owner, time.time() + 120, _now(), job_id))
        try:
            while True:
                current = self.status(job_id)
                if current['phase'] in ('complete', 'paused', 'error') or (
                        current['phase'] == 'waiting_for_complete_row' and
                        not current['follow_requested']):
                    return current
                self.advance(job_id, lease_owner=owner)
                current = self.status(job_id)
                if current['phase'] in ('complete', 'paused', 'error') or (
                        current['phase'] == 'waiting_for_complete_row' and
                        not current['follow_requested']):
                    return current
                with closing(self._db(writable=True)) as db, db:
                    db.execute('UPDATE jobs SET lease_until=?,updated_at=? WHERE id=? AND lease_owner=?',
                               (time.time() + 120, _now(), job_id, owner))
                if current['phase'] in ('following', 'waiting_for_complete_row'):
                    time.sleep(interval_seconds)
        except Exception as error:
            code = error.code if isinstance(error, ImportJobError) else (
                'sealed_source_conflict' if isinstance(error, EpisodeConflict) else 'worker_failed')
            try:
                with closing(self._db(writable=True)) as db, db:
                    row = self._load(db, job_id)
                    if row['phase'] not in ('index_error', 'error', 'paused'):
                        self._set_error(db, row, code,
                                        'Follower stopped; inspect source integrity and job recovery.')
            except Exception:
                pass
            raise
        finally:
            with closing(self._db(writable=True)) as db, db:
                db.execute('UPDATE jobs SET lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=? AND lease_owner=?',
                           (_now(), job_id, owner))

    def assert_owner(self, *, job_id, selected_desk_id, asserted_by, recorded_at, evidence):
        row = None
        with closing(self._db()) as db:
            if db is None:
                raise ImportJobError('job_unavailable', 'Session import job is unavailable.')
            row = self._load(db, job_id)
        if selected_desk_id != row['plan']['selected_desk_id']:
            raise ImportJobError('desk_selection_changed', 'Owner desk differs from reviewed selection.')
        self._desk(selected_desk_id)
        if row['counts'].get('imported', 0) + row['counts'].get('already_present', 0) == 0:
            raise ImportJobError('no_visible_evidence', 'Import visible source evidence before asserting an owner.')
        plan = row['plan']
        session_id = _id('source-session', {'schema_version': 'ops.source-session.v1',
            'tenant_id': self.tenant_id, 'runtime': 'codex',
            'native_id': plan['native_session_id']})
        source_range = ({'native_id': plan['native_session_id'],
                         'source_file': plan['source_file'],
                         'start_offset': plan['start_offset']}
                        if plan['mode'] == 'current-turn-and-forward' else None)
        claim_id = self.sources.claim(
            session_id=session_id, predicate='session.owner',
            object={'kind': 'desk', 'id': selected_desk_id},
            asserted_by=asserted_by, recorded_at=recorded_at,
            evidence=evidence, source_range=source_range,
        )
        return {'schema_version': SCHEMA, 'status': 'asserted', 'job_id': job_id,
                'source_session_id': session_id, 'claim_id': claim_id,
                'attribution': {'selected_desk_id': selected_desk_id,
                                'status': 'asserted', 'claim_id': claim_id,
                                'source_range': source_range,
                                'authority': 'append-only user assertion; no admission grant'}}
