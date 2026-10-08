"""S1a falsifier "Writers": the statement trace of one successful tick equals P4's table.

Order: docs/work/orders/S1a-scheduled-summarizer.md, P4 (every store write the role makes) and the Writers
falsifier. Instrument: tests/t10_instruments.py (the T12c instrument: every top-level SQLite statement, by
database file, in order). Seams: tests/s1a_seams.py (store file names, the gateway ledger's place).

The world: one desk (`alpha`) approved and admitted, one queued job of one small episode (one packet), the
queue already created. One tick of the role runs under the recorder.

Reading of the table, stated once: a write is a DML statement (INSERT, UPDATE, DELETE, REPLACE); schema
statements that create nothing on an existing file (`CREATE ... IF NOT EXISTS`) are not compared. A
transaction is delimited per connection by the traced BEGIN and COMMIT/ROLLBACK; a write outside one is its
own (autocommit) transaction. Only transactions with at least one write are compared. "Claim" is the
`claim` transaction (expiry UPDATE + lease UPDATE); one more claim that finds the queue idle (its expiry
UPDATE alone) may close the tick, because the role "works jobs ... until the queue is idle".

GREEN-IF:
- `test_one_successful_tick_writes_exactly_p4s_table`: the job succeeded, and the write transactions are,
  per file:
    queue.sqlite3     [BEGIN IMMEDIATE: UPDATE jobs, UPDATE jobs] (claim),
                      [BEGIN IMMEDIATE: INSERT attempts, UPDATE jobs] (intent),
                      [BEGIN IMMEDIATE: UPDATE attempts] (receipt),
                      [BEGIN IMMEDIATE: UPDATE jobs, UPDATE attempts] (finish),
                      then at most one [BEGIN IMMEDIATE: UPDATE jobs] (the idle claim);
    episodes.sqlite3  [INSERT capsules] (the capsule);
    gateway ledger    [BEGIN IMMEDIATE: INSERT calls] (reserve), [UPDATE calls] (finish);
    sessions.sqlite3  none; any other SQLite file: none.
- `test_the_capsule_transaction_is_exactly_one_row`: the one write transaction on episodes.sqlite3 holds one
  statement, `INSERT OR IGNORE INTO capsules`, and the capsules table gains exactly one row.

Mutants that must be RED: the gateway recording the call in desk memory (an INSERT into episodes); the role
writing its own rows to sessions.sqlite3 (admitting itself); a second capsule write; a claim without the
fence's IMMEDIATE transaction; any extra write.
"""
from __future__ import annotations

import os
import re

from s1a_harness import assert_no_child_process, make_world, run_role  # noqa: F401 - fixture

_DML = re.compile(r'^\s*(INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM|REPLACE\s+INTO)'
                  r'\s+["`\[]?(\w+)', re.I)
_BEGIN = re.compile(r'^\s*BEGIN\b', re.I)
_END = re.compile(r'^\s*(COMMIT|END|ROLLBACK)\b', re.I)

QUEUE_TABLE = [
    ('claim', ['UPDATE jobs', 'UPDATE jobs']),
    ('intent', ['INSERT attempts', 'UPDATE jobs']),
    ('receipt', ['UPDATE attempts']),
    ('finish', ['UPDATE jobs', 'UPDATE attempts']),
]
IDLE_CLAIM = ['UPDATE jobs']
EPISODES_TABLE = [('capsule', ['INSERT capsules'])]
LEDGER_TABLE = [('reserve', ['INSERT calls']), ('finish', ['UPDATE calls'])]


def _write(sql):
    match = _DML.match(sql)
    if not match:
        return None
    verb = match.group(1).split()[0].upper()
    return f'{verb} {match.group(2)}'


def write_transactions(recorder, db):
    """[(begin statement or None, [write, ...], [sql, ...])] for one database file, in trace order."""
    target = os.path.realpath(db)
    open_by_conn, done = {}, []
    for statement in recorder.top(('call',)):
        if statement.db != target:
            continue
        conn, sql = statement.conn, statement.sql
        if _BEGIN.match(sql):
            open_by_conn[conn] = [sql.strip().upper(), [], []]
        elif _END.match(sql):
            current = open_by_conn.pop(conn, None)
            if current and current[1]:
                done.append(tuple(current))
        else:
            write = _write(sql)
            if write is None:
                continue
            if conn in open_by_conn:
                open_by_conn[conn][1].append(write)
                open_by_conn[conn][2].append(sql)
            else:
                done.append((None, [write], [sql]))
    return done


def _shape(transactions):
    return [(begin, writes) for begin, writes, _ in transactions]


def test_one_successful_tick_writes_exactly_p4s_table(make_world):
    world = make_world().build()
    run = run_role(world, record=True)
    assert_no_child_process(run)
    job = world.job('alpha')
    assert job['state'] == 'succeeded', f'the tick did not succeed: {job}\n{run.describe()}'
    recorder = run.recorder

    queue = _shape(write_transactions(recorder, world.queue_path))
    expected = [('BEGIN IMMEDIATE', writes) for _, writes in QUEUE_TABLE]
    tail = queue[len(expected):]
    assert queue[:len(expected)] == expected and (not tail or tail == [('BEGIN IMMEDIATE', IDLE_CLAIM)]), (
        f'queue.sqlite3 write transactions {queue}\nexpected {expected} then at most one idle claim')

    episodes = _shape(write_transactions(recorder, world.episodes_path))
    assert [w for _, w in episodes] == [w for _, w in EPISODES_TABLE], f'episodes.sqlite3 writes {episodes}'

    ledger = _shape(write_transactions(recorder, world.ledger_path))
    assert (len(ledger) == 2 and ledger[0] == ('BEGIN IMMEDIATE', LEDGER_TABLE[0][1])
            and ledger[1][1] == LEDGER_TABLE[1][1]), f'gateway ledger write transactions {ledger}'

    sessions = write_transactions(recorder, world.sessions_path)
    assert not sessions, f'the role wrote sessions.sqlite3: {_shape(sessions)}'

    known = {os.path.realpath(p) for p in (world.queue_path, world.episodes_path, world.ledger_path,
                                           world.sessions_path)}
    others = sorted({s.db for s in recorder.top(('call',)) if s.db not in known and s.db not in ('', ':memory:')
                     and _write(s.sql)})
    assert not others, f'the tick wrote other SQLite files: {others}'


def test_the_capsule_transaction_is_exactly_one_row(make_world):
    world = make_world().build()
    before = world.capsule_count()
    run = run_role(world, record=True)
    assert_no_child_process(run)
    assert world.job('alpha')['state'] == 'succeeded', run.describe()
    transactions = write_transactions(run.recorder, world.episodes_path)
    assert len(transactions) == 1, f'episodes.sqlite3 write transactions: {_shape(transactions)}'
    _, writes, sqls = transactions[0]
    assert writes == ['INSERT capsules'] and re.match(r'^\s*INSERT\s+OR\s+IGNORE\s+INTO\s+capsules\b', sqls[0], re.I), (
        f'the capsule transaction: {sqls}')
    assert world.capsule_count() == before + 1, f'capsules {before} -> {world.capsule_count()}'
