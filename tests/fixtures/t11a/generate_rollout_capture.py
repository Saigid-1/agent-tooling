"""P7 golden capture generator (docs/work/orders/T11a-leaf-consolidation.md, P7, L6).

Generated at base by:

    python tests/fixtures/t11a/generate_rollout_capture.py

which rewrites tests/fixtures/t11a/rollout_capture.json; tests/test_t11a_p7_rollout_capture.py
recomputes it with ``build()`` and compares byte for byte.

Both classes are driven as the product drives them (launch_binding.py:497,
spool_ingest.py:413): ``initialize()`` once, then ``capture(session, store=, queue=,
transcript_path=, workspace=)``, with a recording store and queue. Each scenario records,
step by step, the capture result or the exception (type and message), the cursor row and
the receipts. Fixtures: a complete page, a partial trailing line, a prefix change, paging,
omissions, and every RolloutCaptureConflict raise in ``_page`` (the census's 7 that differ
only in the exception spelling, launch_binding.py:640/644/650/667/676/684/690, plus the
2 whose message differs, :655/:664). Two raises need the source to change between the
cursor check and the read; an audit hook does that at the transcript's ``open``.

Normalised: the scratch root, the source's st_dev/st_ino in source references, and the
recording store's and queue's ids (numbered by first appearance).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # tests/

from t11a_golden import golden, normalise_paths, scratch, write_golden  # noqa: E402

GOLDEN = 'rollout_capture.json'
PARENT = '019a0000-0000-7000-8000-00000000aaaa'
SESSION = '019a0000-0000-7000-8000-00000000bbbb'
WORKSPACE = '/t11a-workspace/répo'
SOURCE_REF = re.compile(r'(codex-rollout:[^:"]+:)\d+:\d+:')


class Store:
    """Records what the capture seals; ``fail`` simulates a crash after the pending mark."""

    def __init__(self):
        self.episodes, self.fail = [], 0

    def _binding(self, session):
        return 'desk:binding-é'

    def capture(self, session, *, source_ref, events):
        if self.fail:
            self.fail -= 1
            raise RuntimeError('simulated store outage after the pending mark')
        self.episodes.append({'session': session, 'source_ref': source_ref, 'events': events})
        return {'episode_id': f'episode-{len(self.episodes)}'}


class Queue:
    def __init__(self):
        self.jobs = []

    def enqueue(self, session, *, episode_ids, reason):
        self.jobs.append({'session': session, 'episode_ids': episode_ids, 'reason': reason})
        return {'job_id': f'job-{len(self.jobs)}'}


class OpenHook:
    """One-shot action at the next audited ``open`` of one path. The hook is installed once per
    process (audit hooks cannot be removed); the armed action lives on ``sys`` so every load of
    this module shares it."""

    @staticmethod
    def arm(path, action):
        sys._t11a_open_action = (os.fspath(path), action)


def _open_hook(event, args):
    armed = getattr(sys, '_t11a_open_action', None)
    if event != 'open' or armed is None or not args or isinstance(args[0], int):
        return
    path, action = armed
    if os.fspath(args[0]) != path:
        return
    sys._t11a_open_action = None
    action()


if not getattr(sys, '_t11a_open_hook', False):
    sys.addaudithook(_open_hook)
    sys._t11a_open_hook = True


def meta(kind, workspace, session=SESSION, **extra):
    payload = {'id': session, 'cwd': str(workspace), **extra}
    if kind == 'child':
        payload['source'] = {'subagent': {'thread_spawn': {'parent_thread_id': PARENT, 'depth': 1}}}
    return {'type': 'session_meta', 'payload': payload}


def said(text, role='user'):
    kind = 'user_message' if role == 'user' else 'agent_message'
    return {'type': 'event_msg', 'payload': {'type': kind, 'message': text}}


def message(*texts, role='assistant'):
    return {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
                                                 'content': [{'type': 'output_text', 'text': t} for t in texts]}}


def line(row):
    return json.dumps(row, ensure_ascii=False).encode() + b'\n'


class World:
    def __init__(self, root, kind):
        from kp_agent_tooling._impl.service.launch_binding import RolloutCapture
        from kp_agent_tooling._impl.service.spool_ingest import ChildRolloutCapture
        self.root, self.kind = root, kind
        # A fixed workspace (the predicates compare resolved paths; it need not exist), so
        # byte offsets do not depend on where the scratch directory is.
        self.workspace = Path(WORKSPACE).resolve()
        self.transcript = root / 'rollout-2026.jsonl'
        self.ledger = root / 'rollout-capture.sqlite3'
        self.capture = (RolloutCapture(self.ledger) if kind == 'root'
                        else ChildRolloutCapture(self.ledger, parent=PARENT))
        self.capture.initialize()
        self.store, self.queue, self.steps = Store(), Queue(), []

    def write(self, data):
        self.transcript.write_bytes(data)

    def append(self, data):
        with self.transcript.open('ab') as out:
            out.write(data)

    def run(self, label):
        stat = self.transcript.stat() if self.transcript.exists() else None
        try:
            result = {'result': self.capture.capture(SESSION, store=self.store, queue=self.queue,
                                                     transcript_path=self.transcript, workspace=self.workspace)}
        except Exception as error:
            result = {'raised': type(error).__name__, 'message': str(error)}
        result['cursor'], result['receipts'] = self.ledger_rows()
        self.steps.append({'step': label, **self.normalise(result, stat)})

    def ledger_rows(self):
        with closing(sqlite3.connect(self.ledger.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            cursor = [dict(r) for r in db.execute('SELECT * FROM cursor ORDER BY session')]
            receipts = [dict(r) for r in db.execute('SELECT * FROM receipts ORDER BY session, end_offset')]
        return cursor, receipts

    def normalise(self, value, stat=None):
        """Scratch root -> <root>; the source's device and inode -> <dev>/<ino>."""
        text = normalise_paths(json.dumps(value, sort_keys=True, ensure_ascii=False), self.root)
        text = SOURCE_REF.sub(r'\1<dev>:<ino>:', text)
        text = re.sub(r'"(dev|ino)": \d+', r'"\1": "<\1>"', text)
        return json.loads(text)

    def finish(self):
        return {'steps': self.steps, 'sealed': self.normalise(self.store.episodes),
                'jobs': self.normalise(self.queue.jobs)}


def complete_page(w):
    w.write(line(meta(w.kind, w.workspace)) + line(said('héllo ☃')) + line(message('réponse', 'deux')) +
            line({'type': 'turn_context', 'payload': {'cwd': str(w.workspace)}}))
    w.run('complete page')
    w.run('nothing new')
    w.append(line(said('later')))
    w.run('appended row')


def partial_trailing_line(w):
    w.write(line(meta(w.kind, w.workspace)) + line(said('first')) + b'{"type":"event_msg","payl')
    w.run('page stops before the partial line')
    w.append(b'oad":{"type":"agent_message","message":"completed"}}\n')
    w.run('the completed line is captured')


def prefix_change(w):
    w.write(line(meta(w.kind, w.workspace)) + line(said('aaaa')))
    w.run('first page')
    data = w.transcript.read_bytes().replace(b'aaaa', b'bbbb')
    with w.transcript.open('r+b') as out:  # same file, same length, consumed bytes changed
        out.write(data)
    w.append(line(said('next')))
    w.run('consumed source prefix rewritten (launch_binding.py:644)')


def paging(w):
    rows = b''.join(line(said(f'event {i} é')) for i in range(230))
    w.write(line(meta(w.kind, w.workspace)) + rows)
    w.run('three pages in one capture')


def omissions(w):
    big = said('x' * (1048576 + 10))
    w.write(line(meta(w.kind, w.workspace)) + line({'type': 'turn_context', 'payload': {}}) +
            b'not json\n' + line(big) + line(message('kept', role='user')) + line(['not', 'an', 'object']))
    w.run('omitted and kept rows')


def truncated_during_read(w):
    w.write(line(meta(w.kind, w.workspace)) + line(said('one')))
    w.run('first page')
    w.append(line(said('two')))
    consumed = w.ledger_rows()[0][0]['offset']
    OpenHook.arm(w.transcript, lambda: os.truncate(w.transcript, consumed - 3))
    w.run('session source truncated (launch_binding.py:640)')


def pending_changed(w):
    w.write(line(meta(w.kind, w.workspace)) + line(said('one')) + line(said('two')))
    w.store.fail = 1
    w.run('store outage after the pending mark')
    size = w.transcript.stat().st_size
    os.truncate(w.transcript, size - 1)
    w.run('pending source changed (launch_binding.py:650)')


def oversized_first_row(w):
    w.write(line(meta(w.kind, w.workspace, padding='p' * (1048576 + 10))))
    w.run('oversized first row (launch_binding.py:655)')


def foreign_first_row(w):
    w.write(line(meta(w.kind, w.workspace, session='019a0000-0000-7000-8000-00000000ffff')) + line(said('x')))
    w.run('first row is another session (launch_binding.py:664)')
    w.write(line(said('no metadata')) + line(said('x')))
    w.run('first row is not session metadata (launch_binding.py:664)')


def metadata_changed(w):
    w.write(line(meta(w.kind, w.workspace)) + line(said('x')) +
            line(meta(w.kind, w.workspace / 'elsewhere')))
    w.run('later session_meta disagrees (launch_binding.py:667)')


def batch_limit(w):
    w.write(line(meta(w.kind, w.workspace)) + line(message(*[f'part {i}' for i in range(501)])))
    w.run('row exceeds the event batch limit (launch_binding.py:676)')


def replaced_during_read(w):
    w.write(line(meta(w.kind, w.workspace)) + line(said('one')))
    replacement = w.root / 'replacement.jsonl'
    replacement.write_bytes(w.transcript.read_bytes())
    OpenHook.arm(w.transcript, lambda: os.replace(replacement, w.transcript))
    w.run('source replaced during capture (launch_binding.py:684)')


def pending_rewritten(w):
    w.write(line(meta(w.kind, w.workspace)) + line(said('one')) + line(said('two')))
    w.store.fail = 1
    w.run('store outage after the pending mark')
    data = w.transcript.read_bytes().replace(b'"two"', b'"owt"')
    with w.transcript.open('r+b') as out:
        out.write(data)
    w.run('pending source rewritten (launch_binding.py:690)')
    w.run('still refused')


SCENARIOS = (complete_page, partial_trailing_line, prefix_change, paging, omissions, truncated_during_read,
             pending_changed, oversized_first_row, foreign_first_row, metadata_changed, batch_limit,
             replaced_during_read, pending_rewritten)


def build():
    out = {}
    for kind, label in (('root', 'RolloutCapture'), ('child', 'ChildRolloutCapture')):
        out[label] = {}
        for scenario in SCENARIOS:
            with scratch(f't11a-p7-{kind}-') as root:
                world = World(root, kind)
                scenario(world)
                out[label][scenario.__name__] = world.finish()
    return golden({'captures': out})


def main():
    text = build()
    write_golden(text, GOLDEN)
    print(f'wrote tests/fixtures/t11a/{GOLDEN} ({len(text)} bytes)')


if __name__ == '__main__':
    main()
