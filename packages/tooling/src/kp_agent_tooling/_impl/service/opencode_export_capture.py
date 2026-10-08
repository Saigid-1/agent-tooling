"""OpenCode capture (order O2, R3): a bound session's visible turns, read through OpenCode's own export.

OpenCode keeps its sessions in its own database and writes no transcript a byte cursor could
follow. Capture therefore reads a whole-session snapshot through the harness itself, ``opencode
--pure export <session>``, run as a child process. Nothing here opens, reads or names OpenCode's
store: the export child is its only reader. The child runs in the launch workspace with external
plugins off (``--pure`` and ``OPENCODE_PURE``), the launch's disable flags, no desk memory server,
a timeout and an output bound; it never waits for input (an export without an id would open a
picker, so the id is always passed).

Before a session is bound, its snapshot proves it is a ROOT session of the launch workspace
(``info.id``, ``info.directory``, no ``info.parentID``); child sessions are out of scope.

Visible events follow the rule the OpenCode desk capture used before O2:

- messages up to the last COMPLETED assistant message (``time.completed`` set); a turn still
  running is left for the next capture;
- ``text`` parts in the message's role, except synthetic or ignored text;
- completed or errored ``tool`` parts, as role ``tool``;
- ``reasoning`` parts are excluded and counted, as is every other omitted part.

Event ids are OpenCode's part ids (``prt_...``), which are stable; text over 128 000 bytes is split
as Claude's adapter splits it.

The ledger keeps, per session, every published event id and the one page in flight. Its tables
live in the non-Claude capture ledger file the receipt names (``leaf.ROLLOUT_CAPTURE_DB``, the file
``launch_binding._ledgers`` assigns to every parser but Claude's; store filenames have their one
home in the leaf). A page is recorded as pending before it is sealed, so a replay after a crash
seals the same events under the same source reference; the episode store and the queue are both
idempotent on identical input. Re-capturing the same snapshot publishes nothing new, and a missed
turn end is recovered by the next one, because every capture reads the whole session. Pages follow
``RolloutCapture``: at most 100 events and 256 KiB of text each, at most 16 pages per capture; the
rest waits for the next capture.
"""
from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.claude_episode_capture import _bounded_events
from kp_agent_tooling._impl.service.launch_binding import LaunchRefused, RolloutCapture

SOURCE = 'opencode-export'
EXPORT_TIMEOUT_SECONDS = 120
EXPORT_BYTES = RolloutCapture.PREFIX_BYTES
STDERR_TAIL = 512
# The export child's environment over the hook's own (the launched OpenCode's): plugins off, no
# update, share or model-catalogue fetch, no Claude Code files, and no desk memory server.
EXPORT_ENVIRONMENT = {'OPENCODE_PURE': '1', 'OPENCODE_DISABLE_AUTOUPDATE': '1', 'OPENCODE_DISABLE_SHARE': '1',
                      'OPENCODE_DISABLE_MODELS_FETCH': '1', 'OPENCODE_DISABLE_CLAUDE_CODE': '1',
                      'OPENCODE_CONFIG_CONTENT': ''}
_PART_ID = re.compile(r'^prt_[A-Za-z0-9]{1,100}$')
_KIND = re.compile(r'^[a-z][a-z-]{0,31}$')
_CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')
_TABLES = (
    'CREATE TABLE IF NOT EXISTS opencode_cursor (session TEXT PRIMARY KEY, binding TEXT NOT NULL, '
    'pages INTEGER NOT NULL, pending TEXT)',
    'CREATE TABLE IF NOT EXISTS opencode_published (session TEXT NOT NULL, event_id TEXT NOT NULL, '
    'page INTEGER NOT NULL, PRIMARY KEY(session, event_id))',
    'CREATE TABLE IF NOT EXISTS opencode_receipts (session TEXT NOT NULL, page INTEGER NOT NULL, '
    'payload TEXT NOT NULL, PRIMARY KEY(session, page))',
)


class OpenCodeExportRefused(LaunchRefused):
    """The snapshot is not a root session of the launch workspace."""


class OpenCodeExportUnavailable(RuntimeError):
    """The export child failed, timed out, or exceeded its bound; nothing was read."""


class OpenCodeCaptureConflict(RuntimeError):
    """The ledger disagrees with the session or its binding; nothing was published."""


# --- the export child --------------------------------------------------------------------------

def _child_output(argv, *, cwd, env):
    """The child's stdout, bounded in bytes and time; raises with its stderr tail on failure."""
    out, err, size = [], b'', 0
    with subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as child:
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        selector.register(child.stderr, selectors.EVENT_READ)
        deadline = time.monotonic() + EXPORT_TIMEOUT_SECONDS
        try:
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise OpenCodeExportUnavailable('session export timed out')
                for key, _ in selector.select(left):
                    chunk = os.read(key.fileobj.fileno(), 1 << 20)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.fileobj is child.stdout:
                        size += len(chunk)
                        if size > EXPORT_BYTES:
                            raise OpenCodeExportUnavailable('session export exceeds its capture bound')
                        out.append(chunk)
                    else:
                        err = (err + chunk)[-STDERR_TAIL:]
            code = child.wait(timeout=max(deadline - time.monotonic(), 0.1))
        except BaseException:
            child.kill()
            raise
        finally:
            selector.close()
    if code != 0:
        tail = _CONTROL.sub('', err.decode('utf-8', 'replace')).strip()[-200:]
        raise OpenCodeExportUnavailable(f'session export exited {code}: {tail}')
    return b''.join(out)


def check_identity(snapshot, session, workspace):
    """The snapshot is ``session``, a root session whose directory is the launch workspace."""
    info = snapshot.get('info') if isinstance(snapshot, dict) else None
    if not isinstance(info, dict) or not isinstance(snapshot.get('messages'), list):
        raise OpenCodeExportRefused('session export lacks info and messages')
    if info.get('id') != session:
        raise OpenCodeExportRefused('session export names another session')
    directory = info.get('directory')
    if (not isinstance(directory, str) or not os.path.isabs(directory)
            or Path(directory).resolve() != Path(workspace).resolve()):
        raise OpenCodeExportRefused('the session belongs to another workspace')
    if info.get('parentID'):
        raise OpenCodeExportRefused('a child session is not captured; its root session is')
    return snapshot


def export_session(executable, session, workspace, *, environ=None):
    """``opencode --pure export <session>`` in the workspace: the parsed snapshot, identity checked."""
    env = dict(os.environ if environ is None else environ)
    env.update(EXPORT_ENVIRONMENT)
    try:
        raw = _child_output([executable, '--pure', 'export', session], cwd=workspace, env=env)
    except subprocess.TimeoutExpired:
        raise OpenCodeExportUnavailable('session export did not exit') from None
    except OSError as error:
        raise OpenCodeExportUnavailable(f'session export could not start: {error.strerror}') from None
    try:
        snapshot = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise OpenCodeExportUnavailable('session export is not JSON') from None
    return check_identity(snapshot, session, workspace)


# --- visible events ----------------------------------------------------------------------------

def _completed(message):
    info = message.get('info') if isinstance(message, dict) else None
    time_ = info.get('time') if isinstance(info, dict) else None
    return (isinstance(info, dict) and info.get('role') == 'assistant' and isinstance(time_, dict)
            and time_.get('completed') is not None)


def _part_event(part, role):
    """(event, None) for a visible part, or (None, the reason it is omitted)."""
    if not isinstance(part, dict) or not isinstance(part.get('id'), str) or not _PART_ID.fullmatch(part['id']):
        return None, 'malformed_part'
    kind = part.get('type')
    if kind == 'text':
        if part.get('synthetic') or part.get('ignored'):
            return None, 'synthetic_or_ignored_text'
        text = part.get('text')
        if not isinstance(text, str) or not text:
            return None, 'empty_text'
        return {'event_id': part['id'], 'role': role, 'text': text}, None
    if kind == 'tool':
        state = part.get('state') if isinstance(part.get('state'), dict) else {}
        status = state.get('status')
        if status not in ('completed', 'error'):
            return None, 'unfinished_tool'
        body = state.get('output' if status == 'completed' else 'error')
        name = part.get('tool') if isinstance(part.get('tool'), str) else 'tool'
        text = f'{name} ({status})' + (f'\n{body}' if isinstance(body, str) and body else '')
        return {'event_id': part['id'], 'role': 'tool', 'text': text}, None
    if kind == 'reasoning':
        return None, 'reasoning'
    return None, 'part_' + kind if isinstance(kind, str) and _KIND.fullmatch(kind) else 'malformed_part'


def visible_events(snapshot):
    """The snapshot's visible events, in session order, and the count of each omitted kind."""
    messages = snapshot['messages']
    finished = [index for index, message in enumerate(messages) if _completed(message)]
    events, omitted = [], Counter()
    for message in messages[:finished[-1] + 1 if finished else 0]:
        info = message.get('info') if isinstance(message, dict) else None
        role = info.get('role') if isinstance(info, dict) else None
        if role not in ('user', 'assistant') or not isinstance(message.get('parts'), list):
            omitted['malformed_message'] += 1
            continue
        if role == 'assistant' and not _completed(message):
            omitted['unfinished_assistant_message'] += 1
            continue
        for part in message['parts']:
            event, reason = _part_event(part, role)
            if event is None:
                omitted[reason] += 1
            else:
                events.append(event)
    ids = [event['event_id'] for event in events]
    if len(set(ids)) != len(ids):
        raise OpenCodeCaptureConflict('session export repeats a part id')
    return _bounded_events(events), dict(sorted(omitted.items()))


def _source_ref(session, page):
    return f"{SOURCE}:{session}:{page[0]['event_id']}:{page[-1]['event_id']}:{len(page)}"


# --- the ledger and publication ----------------------------------------------------------------

class OpenCodeExportCapture:
    """Idempotent capture of one bound OpenCode session's visible turns into its desk memory."""

    PAGE_BYTES = RolloutCapture.PAGE_BYTES
    PAGE_EVENTS = RolloutCapture.PAGE_EVENTS
    PAGES = RolloutCapture.PAGES

    def __init__(self, path, *, index=None, executable='opencode'):
        self.path = leaf.as_path(path)
        self.index = index
        self.executable = executable

    @property
    def _lock_path(self):
        return self.path.with_name(self.path.name + '.opencode.lock')

    def _db(self):
        if self.path.is_symlink() or not self.path.is_file():
            raise OpenCodeCaptureConflict('capture ledger unavailable; initialize explicitly')
        return leaf.sqlite_connect(self.path, mode='rw', resolve=True)

    def initialize(self):
        """This capture's tables in the existing ledger file; idempotent."""
        with closing(self._db()) as db, db:
            for statement in _TABLES:
                db.execute(statement)

    def capture(self, session, *, store, queue, workspace, reason='batch', snapshot=None, environ=None):
        binding = store._binding(session)  # live desk admission, before any source read
        if snapshot is None:
            snapshot = export_session(self.executable, session, workspace, environ=environ)
        events, omitted = visible_events(check_identity(snapshot, session, workspace))
        pages, waiting = [], []
        with leaf.private_lock(self._lock_path):
            published, pending = self._state(session, binding)
            if pending is not None:
                by_id = {event['event_id']: event for event in events}
                page = [by_id[event_id] for event_id in pending['event_ids'] if event_id in by_id]
                if len(page) != len(pending['event_ids']) or _source_ref(session, page) != pending['source_ref']:
                    raise OpenCodeCaptureConflict('the pending page is no longer in the session export')
                pages.append(self._publish(session, page, store=store, queue=queue, reason=reason,
                                           omitted=omitted, replay=True))
                published |= set(pending['event_ids'])
            waiting = [event for event in events if event['event_id'] not in published]
            while waiting and len(pages) < self.PAGES:
                page = self._take(waiting)
                waiting = waiting[len(page):]
                pages.append(self._publish(session, page, store=store, queue=queue, reason=reason,
                                           omitted=omitted, replay=False))
        if pages and self.index is not None:
            # T12b: indexing is a separate action. A host install drains once after the seals,
            # never waiting; inside Compose the indexer role drains and this opens nothing.
            from kp_agent_tooling._impl.service.episodic_search import drain_after_seal
            drain_after_seal(store, self.index)
        return {'status': 'captured' if pages else 'not_due', 'pages': pages,
                'episodes': [page['episode_id'] for page in pages], 'has_more': bool(waiting),
                'visible_events': len(events), 'omitted': omitted,
                'reasoning_excluded': omitted.get('reasoning', 0)}

    def _state(self, session, binding):
        """The session's published event ids and its pending page; the session's binding is fixed."""
        with closing(self._db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT binding, pending FROM opencode_cursor WHERE session=?', (session,)).fetchone()
            if row is None:
                db.execute('INSERT INTO opencode_cursor VALUES (?,?,0,NULL)', (session, binding))
                row = (binding, None)
            published = {value for (value,) in db.execute(
                'SELECT event_id FROM opencode_published WHERE session=?', (session,))}
        if row[0] != binding:
            raise OpenCodeCaptureConflict('session belongs to a different desk binding')
        return published, json.loads(row[1]) if row[1] else None

    def _take(self, waiting):
        page, size = [], 0
        for event in waiting:
            length = len(event['text'].encode('utf-8'))
            if page and (len(page) >= self.PAGE_EVENTS or size + length > self.PAGE_BYTES):
                break
            page.append(event)
            size += length
        return page

    def _publish(self, session, page, *, store, queue, reason, omitted, replay):
        source_ref = _source_ref(session, page)
        if not replay:
            with closing(self._db()) as db, db:
                db.execute('UPDATE opencode_cursor SET pending=? WHERE session=? AND pending IS NULL', (json.dumps(
                    {'source_ref': source_ref, 'event_ids': [event['event_id'] for event in page]}), session))
        episode = store.capture(session, source_ref=source_ref, events=page)
        job = queue.enqueue(session, episode_ids=[episode['episode_id']], reason=reason)
        receipt = {'state': 'captured', 'episode_id': episode['episode_id'], 'job_id': job['job_id'],
                   'source_ref': source_ref, 'event_count': len(page), 'replayed': replay,
                   'indexed': self.index is not None, 'omitted': omitted}
        with closing(self._db()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            number = db.execute('SELECT pages FROM opencode_cursor WHERE session=?', (session,)).fetchone()[0] + 1
            db.executemany('INSERT OR IGNORE INTO opencode_published VALUES (?,?,?)',
                           [(session, event['event_id'], number) for event in page])
            db.execute('UPDATE opencode_cursor SET pages=?, pending=NULL WHERE session=?', (number, session))
            db.execute('INSERT INTO opencode_receipts VALUES (?,?,?)', (session, number, json.dumps(receipt)))
        return receipt
