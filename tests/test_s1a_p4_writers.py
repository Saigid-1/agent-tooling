"""S1a P4: every store write the role makes (Verification's freeze point 1).

Order: docs/work/orders/S1a-scheduled-summarizer.md, P4 and the falsifier "Writers": "A
statement trace over one successful tick (the T12c instrument, `tests/t10_instruments.py`)
equals P4's table, and the capsule transaction holds exactly one row."

The trace keeps, per store file, the transaction boundaries and the statements that write
(INSERT, UPDATE, DELETE, REPLACE). The gateway ledger's per-connection schema guard
(`CREATE TABLE/INDEX IF NOT EXISTS`, a no-op on an existing ledger) is asserted separately:
it is the gateway's existing connect, unchanged here. Reads (the admission's `PRAGMA
quick_check` on `sessions.sqlite3`, census section 10) are CARRIED and only counted.
"""
from __future__ import annotations

import json
import os
import re

import pytest

from s1a_world import IMPL, World
from t10_instruments import recording

WRITE = re.compile(r'\s*(BEGIN|COMMIT|ROLLBACK|INSERT|UPDATE|DELETE|REPLACE)\b', re.I)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _shape(sql):
    """A statement's verb and table, with literals and whitespace folded."""
    text = ' '.join(sql.split())
    verb = text.split(' ', 1)[0].upper()
    if verb in ('BEGIN', 'COMMIT', 'ROLLBACK'):
        return text.upper().rstrip()
    match = re.match(r'(INSERT(?: OR IGNORE)? INTO|UPDATE|DELETE FROM|REPLACE INTO)\s+(\w+)', text, re.I)
    return f'{match.group(1).upper()} {match.group(2)}'


def _writes(recorder):
    found = {}
    for statement in recorder.top():
        if WRITE.match(statement.sql):
            found.setdefault(os.path.basename(statement.db), []).append(_shape(statement.sql))
    return found


# P4's table for one successful tick of one single-packet job, as built:
EXPECTED = {
    'queue.sqlite3': [
        # claim: expiry UPDATE + lease UPDATE (episodic_queue.claim)
        'BEGIN IMMEDIATE', 'UPDATE jobs', 'UPDATE jobs', 'COMMIT',
        # per packet, intent: INSERT attempts + UPDATE jobs (episodic_queue._intent)
        'BEGIN IMMEDIATE', 'INSERT INTO attempts', 'UPDATE jobs', 'COMMIT',
        # per packet, receipt: UPDATE attempts (episodic_queue._receipt)
        'BEGIN IMMEDIATE', 'UPDATE attempts', 'COMMIT',
        # finish: UPDATE jobs + UPDATE attempts (episodic_queue._finish)
        'BEGIN IMMEDIATE', 'UPDATE jobs', 'UPDATE attempts', 'COMMIT',
        # the claim that finds the queue idle and ends the desk's loop: the expiry UPDATE alone
        # (zero rows; census section 10's idle run), no lease
        'BEGIN IMMEDIATE', 'UPDATE jobs', 'COMMIT'],
    # per request: reserve (one IMMEDIATE) + finish (model_gateway.BudgetLedger)
    'ledger.sqlite3': ['BEGIN IMMEDIATE', 'INSERT INTO calls', 'COMMIT', 'UPDATE calls'],
    # per succeeded job: one transaction, one row (episodic_memory.consolidate)
    'episodes.sqlite3': ['BEGIN', 'INSERT OR IGNORE INTO capsules', 'COMMIT'],
}


def test_one_successful_tick_writes_exactly_p4s_table(world):
    job, _ = world.enqueue()
    world.role().tick()  # the first tick reads the listing and writes the profile receipt
    job, _ = world.enqueue(ref='second')
    capsules = world.capsules()
    with recording() as recorder:
        status = world.role().tick()
    assert status['status'] == 'ok' and world.job(IMPL, job)['state'] == 'succeeded', status
    writes = _writes(recorder)
    assert writes == EXPECTED, json.dumps({'got': writes, 'expected': EXPECTED}, indent=1)
    assert world.capsules() == capsules + 1
    schema = [s.sql for s in recorder.top() if os.path.basename(s.db) == 'ledger.sqlite3'
              and s.verb == 'CREATE']
    assert schema and all('IF NOT EXISTS' in sql for sql in schema), schema
    sessions = [s for s in recorder.top() if os.path.basename(s.db) == 'sessions.sqlite3']
    assert sessions and all(s.verb in ('SELECT', 'PRAGMA') for s in sessions), [s.sql for s in sessions]
    checks = sum(1 for s in sessions if 'quick_check' in s.sql)
    print(f'carried: PRAGMA quick_check on sessions.sqlite3 per successful tick = {checks}')


def test_the_capsule_transaction_holds_exactly_one_row(world):
    world.enqueue()
    with recording() as recorder:
        world.role().tick()
    statements = [s for s in recorder.top() if os.path.basename(s.db) == 'episodes.sqlite3']
    begins = [i for i, s in enumerate(statements) if s.sql.strip().upper().startswith('BEGIN')]
    assert len(begins) == 1, [s.sql for s in statements]
    body = []
    for statement in statements[begins[0] + 1:]:
        if statement.sql.strip().upper().startswith('COMMIT'):
            break
        body.append(statement)
    assert [_shape(s.sql) for s in body if WRITE.match(s.sql)] == ['INSERT OR IGNORE INTO capsules']


def test_role_state_writes_are_the_profile_receipt_and_its_status_file(world):
    world.enqueue()
    before = {path for path in world.root.rglob('*') if path.is_file()}
    world.role().tick()
    added = sorted(str(path.relative_to(world.root)) for path in world.root.rglob('*')
                   if path.is_file() and path not in before)
    assert added == ['state/summarizer/.model-gateway/ledger.sqlite3', 'state/summarizer/model-profile.json',
                     'state/summarizer/status.json'], added
    for name in ('model-profile.json', 'status.json'):
        assert (world.role_state / name).stat().st_mode & 0o077 == 0, f'{name} is readable by others'


def test_a_refusal_before_the_network_writes_intent_not_sent_and_release(world):
    job, _ = world.enqueue()
    world.role().tick()  # profile receipt
    job, _ = world.enqueue(ref='refused')
    world.write_gateway(budget={'max_calls_per_hour': 0})
    world.provider.calls.clear()
    with recording() as recorder:
        status = world.role().tick()
    assert status['status'] == 'refused' and world.provider.calls == []
    writes = _writes(recorder)
    assert writes['queue.sqlite3'] == [
        'BEGIN IMMEDIATE', 'UPDATE jobs', 'UPDATE jobs', 'COMMIT',
        'BEGIN IMMEDIATE', 'INSERT INTO attempts', 'UPDATE jobs', 'COMMIT',
        'BEGIN IMMEDIATE', 'UPDATE attempts', 'COMMIT',
        'BEGIN IMMEDIATE', 'UPDATE jobs', 'COMMIT'], writes
    assert writes.get('ledger.sqlite3') == ['BEGIN IMMEDIATE', 'ROLLBACK'], writes
    assert 'episodes.sqlite3' not in writes
    assert world.job(IMPL, job)['state'] == 'queued'
