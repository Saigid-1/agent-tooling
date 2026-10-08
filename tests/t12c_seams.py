"""T12c seams and instruments: every name the T12c tests assume, in ONE place.

Order: docs/work/orders/T12c-per-desk-postings.md (frozen). The tests observe T12c through public reads
(`memory.search` through a fresh `EpisodicMemoryTools`), through the T12b indexer entry points
(`drain`, `drain_line`, `watch`, as tests/t12b_seams.py names them), through the T10 instruments
(statements and VM steps, tests/t10_instruments.py) and through the store's outbox rows. They never read
FEATURE's postings, never name a posting table or column, and never read the T12c index schema version.

The readings chosen here, the most conservative ones, written down ONLY here:

- "an index built before T12c" is a file in the base index schema, reproduced byte for byte by
  `BASE_INDEX_SCHEMA` below (the base `episodic_search._SCHEMA`, `metadata.version` 1, plus the T12b
  `coverage_token` and `outbox_watermark` tables), filled by `write_base_index`, a frozen standalone port
  of the base writer (`_verified` + `_post`, in the base build's order: the `episodes` rows, then the
  `source_episodes` rows, each by rowid). It carries the store's coverage token, so the store's marks
  describe it, and the outbox seq the store has issued as its watermark (a store drained by the base
  indexer). The committed T12b base-store fixtures (tests/fixtures/t12b, written at T12b's base) are
  also indexes built before T12c and are used as they are;
- a `reindex` row "written by the indexer" is an `index_outbox` row with reason `reindex` inserted while the
  indexer runs (`watch`, the role's loop, over the store's root). The instrument is a trigger on the test
  store's `index_outbox` that copies every inserted (seq, reason) into a log table (`OUTBOX_LOG`), so rows
  the drain retains are still counted;
- "the watermark's transaction" is the index transaction whose statements write `outbox_watermark` (the
  table the order names: `idx.outbox_watermark`); a transaction is delimited per connection by the traced
  BEGIN and COMMIT/ROLLBACK statements (tests/t12b_seams.py `traced`);
- "unapplied" rows are `desks_changed` rows above the index watermark: the tests make them by public writes
  with the indexer stopped (no drain); "applied rows still present before retention" are made by
  re-inserting, unchanged (same seq, reason, ids and detail), rows the drain has applied and retained.
  The index watermark is read with the T12b reader `EpisodicSearchIndex.watermark()`;
- C1's opt-in for N = 100,000 is the environment variable `LARGE_OPT_IN` = '1' (the default suite runs
  N = 10,000 only).
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import t10_world as w
import t12b_seams as t12b

OUTBOX = 'index_outbox'
WATERMARK_TABLE = 'outbox_watermark'
OUTBOX_LOG = 't12c_outbox_log'
LARGE_OPT_IN = 'T12C_C1_LARGE'
_REAL_CONNECT = sqlite3.connect

# The base index schema (episodic_search._SCHEMA, _TOKEN and _WATERMARK at the order's base), verbatim.
BASE_INDEX_SCHEMA = """
CREATE TABLE metadata (version INTEGER NOT NULL);
INSERT INTO metadata VALUES (1);
CREATE TABLE indexed_episodes (
    episode_id TEXT PRIMARY KEY, binding TEXT NOT NULL, payload_sha256 TEXT NOT NULL
);
CREATE VIRTUAL TABLE event_search USING fts5(
    binding UNINDEXED, episode_id UNINDEXED, event_id UNINDEXED, text,
    tokenize='unicode61'
);
"""
BASE_TOKEN = 'CREATE TABLE IF NOT EXISTS coverage_token (token TEXT NOT NULL)'
BASE_WATERMARK = ('CREATE TABLE IF NOT EXISTS outbox_watermark '
                  '(id INTEGER PRIMARY KEY CHECK (id = 0), seq INTEGER NOT NULL)')


def rw(path):
    """A read-write connection no instrument records (the real connect, a `file:` URI)."""
    return closing(_REAL_CONNECT(Path(path).resolve().as_uri() + '?mode=rw', uri=True))


# ------------------------------------------------------------------------------------- the outbox log

def install_outbox_log(store_path):
    """The instrument: a log of every `index_outbox` insert (seq, reason), kept when the drain retains the row.
    The store must already have its outbox (a writable open by the product installs it, T12b B1)."""
    with rw(store_path) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (OUTBOX,)).fetchone(), \
            'instrument: the store has no index_outbox yet'
        db.execute(f'CREATE TABLE IF NOT EXISTS {OUTBOX_LOG} (seq INTEGER, reason TEXT)')
        db.execute(f'CREATE TRIGGER IF NOT EXISTS {OUTBOX_LOG}_insert AFTER INSERT ON {OUTBOX} BEGIN'
                   f' INSERT INTO {OUTBOX_LOG} (seq, reason) VALUES (NEW.seq, NEW.reason); END')
        db.commit()


def logged(store_path, reason=None):
    with t12b.ro(store_path) as db:
        rows = db.execute(f'SELECT seq, reason FROM {OUTBOX_LOG} ORDER BY seq').fetchall()
    return [row for row in rows if reason is None or row[1] == reason]


def issued(store_path):
    """The highest outbox seq the store has issued (0 when none)."""
    with t12b.ro(store_path) as db:
        try:
            row = db.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (OUTBOX,)).fetchone()
        except sqlite3.OperationalError:
            return 0
    return row[0] if row else 0


def reinsert(store_path, rows):
    """Put applied outbox rows back, unchanged: rows at or below the watermark still present (before retention)."""
    with rw(store_path) as db:
        db.executemany(f'INSERT INTO {OUTBOX} (seq, reason, episode_id, session_id, detail) VALUES (?,?,?,?,?)',
                       [tuple(row[c] for c in t12b.OUTBOX_COLUMNS) for row in rows])
        db.commit()


def watermark(store):
    from kp_agent_tooling._impl.service.episodic_search import index_of
    return index_of(store).watermark()


# ------------------------------------------------------------------------ an index built before T12c

def _events(kind, identity, owner, session, raw):
    value = json.loads(raw)
    content = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=True).encode()
    assert 'episode:sha256:' + hashlib.sha256(content).hexdigest() == identity, f'fixture: {identity} is not sealed'
    return [(identity, event['event_id'], event['text']) for event in value['events']]


def write_base_index(store_path, index_path):
    """Write, at `index_path`, the index the base indexer leaves for this store (see the module docstring)."""
    index_path = Path(index_path)
    for name in (index_path, index_path.with_name(index_path.name + '-journal')):
        if name.exists():
            name.unlink()
    with t12b.ro(store_path) as source:
        token = source.execute("SELECT value FROM scope_state WHERE key = 'coverage_token'").fetchone()
        seq = source.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (OUTBOX,)).fetchone()
        rows = [(0, *row) for row in source.execute(
            'SELECT id, binding, NULL, payload FROM episodes ORDER BY rowid')]
        rows += [(1, *row) for row in source.execute(
            'SELECT id, tenant, session_id, payload FROM source_episodes ORDER BY rowid')]
    with closing(_REAL_CONNECT(index_path)) as db:
        db.executescript(BASE_INDEX_SCHEMA)
        db.execute('BEGIN')
        for kind, identity, owner, session, raw in rows:
            raw = bytes(raw) if isinstance(raw, memoryview) else raw
            raw_bytes = raw if isinstance(raw, bytes) else raw.encode()
            binding = owner if kind == 0 else ''
            db.execute('INSERT INTO indexed_episodes VALUES (?,?,?)',
                       (identity, binding, hashlib.sha256(raw_bytes).hexdigest()))
            db.executemany('INSERT INTO event_search(binding,episode_id,event_id,text) VALUES (?,?,?,?)',
                           [(binding, *event) for event in _events(kind, identity, owner, session, raw)])
        db.execute(BASE_TOKEN)
        if token is not None:
            db.execute('INSERT INTO coverage_token (token) VALUES (?)', (token[0],))
        db.execute(BASE_WATERMARK)
        db.execute('INSERT INTO outbox_watermark (id, seq) VALUES (0, ?)', (seq[0] if seq else 0,))
        db.commit()
    index_path.chmod(0o600)
    return index_path


def base_index_shape(index_path):
    """(metadata version, tables) of an index file, read without the product."""
    with t12b.ro(index_path) as db:
        version = db.execute('SELECT version FROM metadata').fetchone()
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return (version[0] if version else None), tables


# ------------------------------------------------------------------------------------- the indexer role

class Clock:
    """`watch`'s clock: no real sleep; records the pass boundaries in the captured output."""

    def __init__(self, out, steps=None):
        self.out, self.steps, self.sleeps, self.marks = out, steps or {}, [], []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.marks.append(len(self.out.getvalue()))
        step = self.steps.get(len(self.sleeps))
        if step is not None:
            step()

    def monotonic(self):
        return float(len(self.sleeps))

    time = monotonic


def run_indexer(root, *, max_passes, batch=t12b.DEFAULT_BATCH):
    """The indexer role (`watch`) over `root` for `max_passes` passes: (exit code, [[line, ...] per pass])."""
    watch = t12b.indexer_function(t12b.WATCH_NAME)
    out = io.StringIO()
    clock = Clock(out)
    with contextlib.redirect_stdout(out):
        code = watch(root, 2.0, max_passes=max_passes, batch=batch, clock=clock)
    text = out.getvalue()
    bounds = [0, *clock.marks, len(text)]
    passes = []
    for start, end in zip(bounds, bounds[1:]):
        lines = []
        for raw in text[start:end].splitlines():
            try:
                value = json.loads(raw)
            except ValueError:
                continue
            if isinstance(value, dict):
                lines.append(value)
        passes.append(lines)
    return code, passes


# --------------------------------------------------------------------------------------- search probes

def search(world, session, query, *, scope='desk', limit=20):
    """One `memory.search` through a fresh tools object: the output, or {'error': <public failure>}."""
    arguments = {'query': query, 'limit': limit}
    if scope != 'desk':
        arguments['scope'] = scope
    return w.call(world.tools(session), 'memory.search', arguments)


def ids(output):
    return [r['episode_id'] for r in output.get('results', [])]
