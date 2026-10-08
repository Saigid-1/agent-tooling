"""Operator-bound incremental capture of visible Claude JSONL events."""
from __future__ import annotations

import hashlib
import fcntl
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from kp_agent_tooling._impl import leaf


class ClaudeCaptureConflict(RuntimeError):
    pass


EMPTY = '00' * 32
MAPPED_ROW_BYTES = 1024 * 1024


def _bounded_events(events):
    result=[]
    for event in events:
        text=event['text']
        if len(text.encode('utf-8')) <= 128000:
            result.append(event)
            continue
        # Character coordinates remain exact; 32000 code points fit 128000 UTF-8 bytes.
        for start in range(0,len(text),32000):
            end=min(start+32000,len(text))
            result.append(dict(event,event_id=f"{event['event_id']}-chars-{start}-{end}",text=text[start:end]))
    return result


def _digest(previous, line):
    return leaf.sha256_hex(bytes.fromhex(previous) + line)


def _parts(line, offset, session=None, *, allow_sidechain=False):
    try:
        row = json.loads(line.decode('utf-8', 'strict'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ClaudeCaptureConflict(f'invalid JSON or UTF-8 at byte {offset}') from exc
    if not isinstance(row, dict):
        raise ClaudeCaptureConflict(f'non-object row at byte {offset}')
    if session is not None and row.get('sessionId') not in (None, session):
        raise ClaudeCaptureConflict(f'row session differs from selected session at byte {offset}')
    kind = row.get('type')
    if kind == 'summary':
        return [], ['native_summary']
    if kind not in ('user', 'assistant'):
        return [], ['control_or_metadata']
    if session is not None and row.get('sessionId') not in (None, session):
        raise ClaudeCaptureConflict(f'row session differs from selected session at byte {offset}')
    if ((row.get('isSidechain') is True and not allow_sidechain) or
            row.get('isMeta') is True or row.get('isCompactSummary') is True):
        return [], ['native_or_control_message']
    message = row.get('message')
    if not isinstance(message, dict) or message.get('role') != kind:
        return [], ['unsupported_message_shape']
    content = message.get('content')
    blocks = content if isinstance(content, list) else [content]
    events, omitted = [], []
    for i, block in enumerate(blocks):
        role, value = kind, None
        if isinstance(block, str):
            value = block
        elif isinstance(block, dict):
            if block.get('type') == 'text' and isinstance(block.get('text'), str):
                value = block['text']
            elif kind == 'user' and block.get('type') == 'tool_result':
                role = 'tool'
                result = block.get('content')
                if isinstance(result, str):
                    value = result
                elif isinstance(result, list) and result and all(
                        isinstance(item, dict) and item.get('type') == 'text'
                        and isinstance(item.get('text'), str) for item in result):
                    for j, item in enumerate(result):
                        if item['text']:
                            events.append({'event_id':f'byte-{offset}-part-{i}-text-{j}',
                                           'role':'tool','text':item['text']})
                    continue
        if value:
            label = 'snapshot-' if kind == 'assistant' else ''
            events.append({'event_id':f'{label}byte-{offset}-part-{i}', 'role':role, 'text':value})
        else:
            omitted.append(f'part-{i}:unsupported_or_empty')
    return _bounded_events(events), omitted


class ClaudeEpisodeCapture:
    def __init__(self, path, *, native_session_id=None, hook_transcript_path=None, index=None):
        self.path = leaf.as_path(path)
        self.native_session_id = native_session_id
        self.hook_transcript_path = hook_transcript_path
        self.index = index

    def bind(self, session, *, store, transcript_path):
        """Persist an operator-verified identity; hooks cannot create or change it."""
        binding = store._binding(session)
        path, stat = self._source(transcript_path)
        if not isinstance(self.native_session_id, str) or not self.native_session_id.strip():
            raise ClaudeCaptureConflict('explicit native session identity required')
        host = Path(self.hook_transcript_path or transcript_path)
        if not host.is_absolute():
            raise ClaudeCaptureConflict('absolute hook source required')
        record = (session, binding, self.native_session_id, str(path.resolve()),
                  str(host), stat.st_dev, stat.st_ino)
        with closing(self._db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS identities (session TEXT PRIMARY KEY, binding TEXT, native TEXT, path TEXT, host_path TEXT, dev INTEGER, ino INTEGER)')
            old = db.execute('SELECT * FROM identities WHERE session=?', (session,)).fetchone()
            cursor = db.execute('SELECT * FROM cursor WHERE session=?', (session,)).fetchone()
            if old is None and cursor is not None and self.native_session_id != session:
                raise ClaudeCaptureConflict('cannot remap an existing capture cursor')
            if old is not None and tuple(old) != record:
                raise ClaudeCaptureConflict('immutable capture identity differs')
            db.execute('INSERT OR IGNORE INTO identities VALUES (?,?,?,?,?,?,?)', record)
        return {'status': 'bound', 'session': session, 'native_session_id': self.native_session_id}

    def identity(self, session, *, store, transcript_path):
        binding = store._binding(session)
        path, stat = self._source(transcript_path)
        with closing(self._db()) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='identities'").fetchone()
            row = db.execute('SELECT * FROM identities WHERE session=?', (session,)).fetchone() if exists else None
        if row is None:
            if self.native_session_id is not None:
                raise ClaudeCaptureConflict('operator identity binding required')
            return session
        expected = (session, binding, self.native_session_id, str(path.resolve()),
                    str(Path(self.hook_transcript_path or transcript_path)), stat.st_dev, stat.st_ino)
        if tuple(row) != expected:
            raise ClaudeCaptureConflict('capture identity differs from operator binding')
        return row['native']

    def seed(self, session, *, store, transcript_path, offset, prefix_sha256, provenance):
        """Explicit operator checkpoint for already imported, verified history."""
        native = self.identity(session, store=store, transcript_path=transcript_path)
        if self.native_session_id is None:
            raise ClaudeCaptureConflict('mapped identity required for seed')
        binding = store._binding(session)
        path, stat = self._source(transcript_path)
        if type(offset) is not int or not 0 < offset <= min(stat.st_size, 64*1024*1024):
            raise ClaudeCaptureConflict('bounded seed boundary required')
        expected = {'session':session, 'native_session_id':native, 'binding':binding,
                    'transcript_path':str(path.resolve()), 'offset':offset,
                    'prefix_sha256':prefix_sha256, 'verified':True}
        if (not isinstance(provenance, dict) or any(provenance.get(k) != v for k,v in expected.items())
                or not isinstance(provenance.get('provenance'), str) or not provenance['provenance'].strip()):
            raise ClaudeCaptureConflict('exact operator-verified import receipt required')
        digest, raw_digest, end = EMPTY, hashlib.sha256(), 0
        with path.open('rb') as source:
            while end < offset:
                line = source.readline(min(MAPPED_ROW_BYTES+1, offset-end))
                if not line.endswith(b'\n') or len(line)>MAPPED_ROW_BYTES:
                    raise ClaudeCaptureConflict('seed must end at a bounded complete row')
                _parts(line, end, native)
                raw_digest.update(line)
                digest = _digest(digest,line)
                end += len(line)
        after = path.stat()
        if raw_digest.hexdigest() != prefix_sha256 or (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) != (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns):
            raise ClaudeCaptureConflict('seed prefix differs from verified receipt')
        receipt = {'state':'seeded', **expected, 'provenance':provenance['provenance']}
        with closing(self._db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM cursor WHERE session=?', (session,)).fetchone()
            if old is not None:
                if old['receipt'] == json.dumps(receipt):
                    return receipt
                raise ClaudeCaptureConflict('seed requires unused capture cursor')
            db.execute('INSERT INTO cursor VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (session,binding,str(path.resolve()),stat.st_dev,stat.st_ino,offset,digest,None,0,EMPTY,None,None,json.dumps(receipt)))
            db.execute('INSERT INTO receipts VALUES (?,?,?)', (session,offset,json.dumps(receipt)))
        return receipt

    def initialize(self):
        leaf.touch_new_private(self.path)
        leaf.touch_new_private(self.path.with_suffix(self.path.suffix+'.lock'))
        with closing(leaf.sqlite_connect(self.path, mode='rwc', resolve=True)) as db:
            db.execute('''CREATE TABLE cursor (session TEXT PRIMARY KEY, binding TEXT NOT NULL,
                path TEXT NOT NULL,
                dev INTEGER NOT NULL, ino INTEGER NOT NULL, offset INTEGER NOT NULL,
                digest TEXT NOT NULL, pending TEXT, verify_offset INTEGER NOT NULL,
                verify_digest TEXT NOT NULL, verify_size INTEGER,
                verify_mtime_ns INTEGER, receipt TEXT)''')
            db.execute('''CREATE TABLE receipts (session TEXT NOT NULL, end_offset INTEGER NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(session,end_offset))''')
            db.commit()

    def _db(self):
        if self.path.is_symlink() or not self.path.is_file():
            raise ClaudeCaptureConflict('capture ledger unavailable; initialize explicitly')
        db = leaf.sqlite_connect(self.path, mode='rw', resolve=True)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _source(path):
        path = Path(path)
        if path.is_symlink() or not path.is_file() or path.suffix != '.jsonl':
            raise ClaudeCaptureConflict('operator-selected regular JSONL source required')
        return path, path.stat()

    @staticmethod
    def _check(row, binding, path, stat):
        if row is None:
            return
        if row['binding'] != binding:
            raise ClaudeCaptureConflict('session belongs to a different desk binding')
        if row['path'] != str(path.resolve()) or (row['dev'],row['ino']) != (stat.st_dev,stat.st_ino):
            raise ClaudeCaptureConflict('session source replaced or rebound')
        if stat.st_size < row['offset']:
            raise ClaudeCaptureConflict('session source truncated')

    def status(self, session, *, store, transcript_path):
        binding = store._binding(session)
        self.identity(session, store=store, transcript_path=transcript_path)
        path, stat = self._source(transcript_path)
        with closing(self._db()) as db:
            row = db.execute('SELECT * FROM cursor WHERE session=?',(session,)).fetchone()
        self._check(row,binding,path,stat)
        offset = row['offset'] if row else 0
        return {'offset':offset, 'pending_source_bytes':stat.st_size-offset,
                'native_session_id':self.native_session_id or session,
                'binding':binding, 'index_required':self.index is not None,
                'publication_pending':bool(row and row['pending']),
                'has_pending_batch':bool(row and row['pending']),
                'last_receipt':json.loads(row['receipt']) if row and row['receipt'] else None,
                'verification_remaining_bytes':row['offset']-row['verify_offset'] if row else 0}

    def receipts(self, session, *, store, limit=100):
        binding = store._binding(session)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('bounded receipt page required')
        with closing(self._db()) as db:
            cursor = db.execute('SELECT binding FROM cursor WHERE session=?',(session,)).fetchone()
            if cursor is not None and cursor['binding'] != binding:
                raise ClaudeCaptureConflict('session belongs to a different desk binding')
            rows = db.execute('SELECT payload FROM receipts WHERE session=? ORDER BY end_offset DESC LIMIT ?',
                              (session,limit)).fetchall()
        return [json.loads(row['payload']) for row in rows]

    def capture(self, session, *, store, queue, transcript_path, reason='batch',
                max_bytes=262144, max_events=100):
        # SQLite transactions span only this ledger. Hold a process lock across
        # the separate store/queue effects so a second capturer cannot race it.
        lock_path = self.path.with_suffix(self.path.suffix+'.lock')
        if lock_path.is_symlink() or not lock_path.is_file():
            raise ClaudeCaptureConflict('capture lock unavailable')
        with leaf.open_binary(lock_path) as lock_file:
            deadline = time.monotonic()+2.0
            while True:
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ClaudeCaptureConflict('capture busy')
                    time.sleep(0.02)
            try:
                receipt = self._capture_locked(session,store=store,queue=queue,
                                               transcript_path=transcript_path,reason=reason,
                                               max_bytes=max_bytes,max_events=max_events)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
        if self.index is not None and receipt.get('state') == 'captured':
            # T12b: indexing is a separate action. A host install drains once after the seal,
            # never waiting; inside Compose the indexer role drains and this opens nothing.
            from kp_agent_tooling._impl.service.episodic_search import drain_after_seal
            drain_after_seal(store, self.index)
        return receipt

    def _capture_locked(self, session, *, store, queue, transcript_path, reason,
                        max_bytes, max_events):
        if reason not in ('batch','context_threshold','session_end','manual'):
            raise ValueError('invalid trigger reason')
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 1048576:
            raise ValueError('bounded source read required')
        if type(max_events) is not int or not 1 <= max_events <= 500:
            raise ValueError('bounded event count required')
        if self.native_session_id is not None:
            max_bytes = max(max_bytes, MAPPED_ROW_BYTES)
        binding = store._binding(session)  # live desk admission, before source read
        native = self.identity(session, store=store, transcript_path=transcript_path)
        path, stat = self._source(transcript_path)
        with closing(self._db()) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM cursor WHERE session=?',(session,)).fetchone()
            if row is None:
                db.execute('INSERT INTO cursor VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (session,binding,str(path.resolve()),stat.st_dev,stat.st_ino,0,EMPTY,None,0,EMPTY,None,None,None))
                db.commit()
                row = db.execute('SELECT * FROM cursor WHERE session=?',(session,)).fetchone()
            self._check(row,binding,path,stat)
            if row['offset'] > 64*1024*1024:
                raise ClaudeCaptureConflict('capture prefix exceeds verification bound; operator rotation required')
            pos, digest = (0, EMPTY) if self.native_session_id is not None else (row['verify_offset'], row['verify_digest'])
            if pos and (row['verify_size'],row['verify_mtime_ns']) != (stat.st_size,stat.st_mtime_ns):
                pos, digest = 0, EMPTY
            verify_start = pos
            with path.open('rb') as source:
                source.seek(pos)
                while pos < row['offset'] and (self.native_session_id is not None or pos-verify_start < max_bytes):
                    remaining = min(MAPPED_ROW_BYTES+1 if self.native_session_id is not None else 128001,row['offset']-pos, MAPPED_ROW_BYTES+1 if self.native_session_id is not None else max_bytes-(pos-verify_start))
                    line = source.readline(remaining)
                    if not line.endswith(b'\n'):
                        if self.native_session_id is not None:
                            raise ClaudeCaptureConflict('consumed prefix has oversized or incomplete row')
                        if pos == verify_start and remaining == max_bytes:
                            raise ClaudeCaptureConflict('source line exceeds capture page bound')
                        break
                    digest = _digest(digest,line)
                    pos += len(line)
            checked = path.stat()
            if (checked.st_dev,checked.st_ino,checked.st_size,checked.st_mtime_ns) != (
                    stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns):
                db.execute('UPDATE cursor SET verify_offset=0,verify_digest=?,verify_size=NULL,verify_mtime_ns=NULL WHERE session=?',
                           (EMPTY,session))
                db.commit()
                raise ClaudeCaptureConflict('source changed during prefix verification')
            if pos < row['offset']:
                db.execute('UPDATE cursor SET verify_offset=?,verify_digest=?,verify_size=?,verify_mtime_ns=? WHERE session=?',
                           (pos,digest,stat.st_size,stat.st_mtime_ns,session))
                db.commit()
                return {'state':'verifying','verified_bytes':pos,'remaining_bytes':row['offset']-pos}
            if digest != row['digest']:
                raise ClaudeCaptureConflict('consumed source prefix rewritten')
            db.execute('UPDATE cursor SET verify_offset=0,verify_digest=?,verify_size=NULL,verify_mtime_ns=NULL WHERE session=?',
                       (EMPTY,session))
            db.commit()
            if row['pending']:
                pending = json.loads(row['pending'])
                self._verify_pending(path,pending,native)
                return self._publish(db,session,pending,store,queue)
            start, end, digest = row['offset'],row['offset'],row['digest']
            events, omissions = [], []
            with path.open('rb') as source:
                source.seek(start)
                while end < stat.st_size and end-start < max_bytes and len(events) < max_events:
                    line_bound = MAPPED_ROW_BYTES if self.native_session_id is not None else 128000
                    limit = min(line_bound+1,max_bytes-(end-start),stat.st_size-end)
                    line = source.readline(limit)
                    if not line.endswith(b'\n'):
                        if len(line) >= line_bound+1:
                            raise ClaudeCaptureConflict(f'oversized JSONL line at byte {end}')
                        if end == start and limit == max_bytes:
                            raise ClaudeCaptureConflict('source line exceeds capture page bound')
                        break
                    if len(line) > line_bound:
                        raise ClaudeCaptureConflict(f'oversized JSONL line at byte {end}')
                    parsed, missing = _parts(line,end,native)
                    if len(events)+len(parsed) > max_events:
                        if not events:
                            raise ClaudeCaptureConflict('row exceeds event batch limit')
                        break
                    events.extend(parsed)
                    omissions.extend({'offset':end,'reason':item} for item in missing)
                    digest = _digest(digest,line)
                    end += len(line)
            after = path.stat()
            if (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns) != (
                    stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns):
                raise ClaudeCaptureConflict('source changed during capture')
            if end == start:
                return {'state':'idle','offset':start,'incomplete_tail':after.st_size>start}
            if not events:
                receipt = {'state':'omitted','offset':end,'omissions':omissions,'has_more':after.st_size>end}
                db.execute('UPDATE cursor SET offset=?,digest=?,receipt=? WHERE session=?',
                           (end,digest,json.dumps(receipt),session))
                db.execute('INSERT INTO receipts VALUES (?,?,?)',(session,end,json.dumps(receipt)))
                db.commit()
                return receipt
            pending = {'start':start,'end':end,'start_digest':row['digest'],'digest':digest,'events':events,
                       'omissions':omissions,'reason':reason,
                       'source_ref':f'claude-jsonl:{session}:{stat.st_dev}:{stat.st_ino}:{start}:{end}:{digest}'}
            db.execute('UPDATE cursor SET pending=? WHERE session=?',(json.dumps(pending,sort_keys=True),session))
            db.commit()
            return self._publish(db,session,pending,store,queue)

    @staticmethod
    def _verify_pending(path, pending, native):
        # Pending batches are at most one bounded capture page. Recheck the exact
        # rows before replaying side effects after a process crash.
        with path.open('rb') as source:
            source.seek(pending['start'])
            events = []
            offset = pending['start']
            digest = pending['start_digest']
            while offset < pending['end']:
                line = source.readline(min(MAPPED_ROW_BYTES+1,pending['end']-offset))
                if not line.endswith(b'\n'):
                    raise ClaudeCaptureConflict('pending source changed')
                parsed, _ = _parts(line,offset,native)
                events.extend(parsed)
                digest = _digest(digest,line)
                offset += len(line)
        if events != pending['events'] or digest != pending['digest']:
            raise ClaudeCaptureConflict('pending source rewritten')

    def _publish(self, db, session, pending, store, queue):
        episode = store.capture(session,source_ref=pending['source_ref'],events=pending['events'])
        job = queue.enqueue(session,episode_ids=[episode['episode_id']],reason=pending['reason'])
        # The seal's outbox row carries the indexing (T12b); `pending` is seal and replay state only.
        receipt = {'state':'captured','episode_id':episode['episode_id'],'job_id':job['job_id'],
                   'event_count':len(pending['events']),'offset':pending['end'],
                   'indexed':self.index is not None,
                   'omissions':pending['omissions']}
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT pending FROM cursor WHERE session=?',(session,)).fetchone()
        if not row or json.loads(row['pending']) != pending:
            raise ClaudeCaptureConflict('pending batch changed during publication')
        db.execute('UPDATE cursor SET offset=?,digest=?,pending=NULL,receipt=? WHERE session=?',
                   (pending['end'],pending['digest'],json.dumps(receipt),session))
        db.execute('INSERT OR IGNORE INTO receipts VALUES (?,?,?)',
                   (session,pending['end'],json.dumps(receipt)))
        db.commit()
        return receipt
