"""T12b B2: one indexer (docs/work/orders/T12b-one-indexer-outbox.md, B2, frozen r5), in-process falsifiers.

Falsifiers (the order's words) and the test for each:
- "a statement trace with `db == index_path` from any module other than the indexer":
  test_b2_only_the_indexer_writes_the_index (every flow that writes the index at base: the sealing flows, the
  operator's index-history and upgrade-sources);
- "two concurrent drains, both writing": test_b2_two_concurrent_drains_one_writes_one_skips (two processes);
- "a restart that re-applies below the watermark or skips above it":
  test_b2_a_restart_resumes_at_the_watermark (a drain process ended between two batches, then a second drain);
- "more outbox rows after N drained seals than the batch bound": test_b2_retention_is_bounded_by_the_batch;
- `index_lag` "is the outbox row count, reported ... `memory.connection_status` ..., as a number beside
  `status`": test_b2_connection_status_reports_index_lag.
The image falsifiers (per role inside the runtime, the tooling+board install, health during a catch-up) are in
tests/install/test_t12b_*_image.py; `verify`'s gap line is tests/install/test_t12b_b2_verify_names_no_indexer.py.

Seams (tests/t12b_seams.py): the product's `drain`, called positionally `(store, index, lease, batch)`; the indexer
is a frame of `drain` in episodic_search on the stack; the index family is the index file and any build file
beside it.

Readings (repeated in the report under AMBIGUITY): "from any module other than the indexer" counts WRITES
(INSERT/UPDATE/DELETE/REPLACE/CREATE/DROP/ALTER/BEGIN IMMEDIATE|EXCLUSIVE) on the index family; a read-only
statement outside the indexer (an operator step reading digests) is not counted. "Skip, never wait": the second
drain returns (exit 0, no error) within 3 s while the first holds the lease, having written nothing to the index.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest

import t10_corpus as corpus
import t10_world as w
import t12b_seams as seams
from t12b_seams import outbox_count, traced

HERE = Path(__file__).resolve().parent
WRITERS = ('library seals', 'workspace capture once', 'session import advance', 'codex launch hook', 'index-history',
           'upgrade-sources')
SKIP_SECONDS = 3.0


def _world(root: Path) -> w.World:
    world = w.World(root, corpus.DESKS, corpus.SESSIONS)
    world.initialize()
    return world


def _legacy_seals(world, count, tag='seal'):
    """`count` operator imports: one `seal` outbox row each, no claim or link row (nothing re-projects them)."""
    store = world.store()
    return [store.import_operator_episode(
        tenant_id=w.TENANT_ONE, role='alpha', repo_key=w.REPO, source_ref=f't12b:{tag}:{index}',
        events=w.imported_events(f'Juniper {tag} number {index} marker{index}x.'),
        source_provenance=w.legacy_provenance(f't12b-{tag}-{index}'))['episode_id'] for index in range(count)]


def _indexed(index_path) -> dict:
    if not Path(index_path).exists():
        return {}
    with seams.ro(index_path) as db:
        return dict(db.execute('SELECT episode_id, payload_sha256 FROM indexed_episodes'))


def _postings(index_path) -> dict:
    with seams.ro(index_path) as db:
        return dict(db.execute('SELECT episode_id, count(*) FROM event_search GROUP BY episode_id'))


def _process(config, out, *extra, wait=True, timeout=180):
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join([str(HERE), str(HERE.parent / 'packages' / 'tooling' / 'src'),
                                         env.get('PYTHONPATH', '')])
    argv = [sys.executable, str(HERE / 't12b_drain_process.py'), '--config', str(config), '--out', str(out), *extra]
    if not wait:
        return subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)


def _result(out) -> dict:
    return json.loads(Path(out).read_text()) if Path(out).exists() else {'status': 'no result file'}


# --------------------------------------------------------------------------------------------- the tests

@pytest.mark.parametrize('name', WRITERS)
def test_b2_only_the_indexer_writes_the_index(tmp_path, name):
    """GREEN-IF the flow writes nothing to the index (or a build file beside it) outside the indexer: every write
    statement on the index family has `drain` on the stack. Positive control (meet, 2026-10-04: a host install drains
    after its seal, so the flow's rows may already be applied when it returns): across the flow and a drain after
    it, the indexer (`drain`) wrote the index at least once, and every sealed episode is then indexed."""
    import t12b_flows as flows
    flow = flows.SETUPS[name](tmp_path / 'flow')
    with traced() as trace:
        flow.run()
    outside = [e for e in trace.family() if e.write and not e.drain]
    shown = '\n'.join(f'  {Path(e.db).name}: {e.sql[:140]}' for e in outside[:8])
    assert not outside, f'{name}: {len(outside)} statements wrote the index outside the indexer:\n{shown}'
    with traced() as drained:
        seams.drain(flow.store)
    by_drain = [e for e in trace.family() + drained.family() if e.write and e.drain]
    assert by_drain, f'positive control: no drain wrote the index during {name} or after it'
    assert not [e for e in drained.family() if e.write and not e.drain], 'a write outside drain after the flow'
    with seams.ro(flow.store_path) as db:
        sealed = {row[0] for row in db.execute('SELECT id FROM episodes UNION SELECT id FROM source_episodes')}
    missing = sorted(sealed - set(_indexed(flow.index_path)))
    assert not missing, f'after {name} and a drain, {len(missing)} of {len(sealed)} sealed episodes are not indexed'


def test_b2_two_concurrent_drains_one_writes_one_skips(tmp_path):
    """GREEN-IF, with drain A (its own process) holding the lease and stopped at its first index write, drain B (its
    own process) returns cleanly within 3 s having written nothing to the index; A then completes, and every sealed
    episode is indexed exactly once (one indexed_episodes row, its events posted once)."""
    world = _world(tmp_path / 'world')
    episodes = _legacy_seals(world, 6)
    config = next(iter(world.configs.values()))
    hold = tmp_path / 'hold'
    hold.mkdir()
    first = _process(config, tmp_path / 'a.json', '--hold', str(hold), wait=False)
    try:
        deadline = time.monotonic() + 60
        while not (hold / 'held').exists():
            if first.poll() is not None or time.monotonic() > deadline:
                pytest.fail(f'drain A never reached an index write: {_result(tmp_path / "a.json")}', pytrace=False)
            time.sleep(0.05)
        started = time.monotonic()
        try:
            second = _process(config, tmp_path / 'b.json', timeout=60)
        except subprocess.TimeoutExpired:
            pytest.fail('drain B waited (60 s) while drain A held the lease: a held lease must mean skip', pytrace=False)
        took = time.monotonic() - started
    finally:
        (hold / 'release').write_text('go')
        out, err = first.communicate(timeout=180)
    a, b = _result(tmp_path / 'a.json'), _result(tmp_path / 'b.json')
    b_writes = [s for s in b.get('statements', []) if s['db'] == 'index' and s['write']]
    a_writes = [s for s in a.get('statements', []) if s['db'] == 'index' and s['write']]
    problems = []
    if b_writes:
        problems.append(f'drain B wrote the index {len(b_writes)} times while A held the lease: {b_writes[:3]}')
    if second.returncode != 0 or b.get('status') != 'returned':
        problems.append(f'drain B did not return cleanly (exit {second.returncode}, {b.get("status")}): '
                        f'{b.get("error", second.stderr)[-1500:]}')
    if took > SKIP_SECONDS:
        problems.append(f'drain B took {took:.1f}s while the lease was held: it waited instead of skipping')
    if first.returncode != 0 or not a_writes:
        problems.append(f'drain A did not complete its writes (exit {first.returncode}, {a.get("status")}): '
                        f'{a.get("error", err)[-1500:]}')
    indexed = _indexed(seams.index_path_of(world.store()))
    postings = _postings(seams.index_path_of(world.store())) if indexed else {}
    if set(indexed) != set(episodes) or any(postings.get(e) != 1 for e in episodes):
        problems.append(f'after both drains: indexed {len(set(indexed) & set(episodes))}/{len(episodes)}, '
                        f'postings per episode {sorted(postings.get(e) for e in episodes)}')
    assert not problems, 'two concurrent drains:\n' + '\n'.join(problems)


def test_b2_a_restart_resumes_at_the_watermark(tmp_path):
    """GREEN-IF a drain (batch 2, its own process) ended right after its first committed batch, followed by a second
    drain, applies every sealed episode (none skipped above the watermark) and the second drain never touches an
    episode the first one applied (no statement names it, and a sentinel digest written on it survives)."""
    world = _world(tmp_path / 'world')
    episodes = _legacy_seals(world, 6, tag='restart')
    config = next(iter(world.configs.values()))
    index = seams.index_path_of(world.store())
    crashed = _process(config, tmp_path / 'a.json', '--batch', '2', '--crash-after-commit')
    first = _result(tmp_path / 'a.json')
    applied = set(_indexed(index))
    assert crashed.returncode in (0, 17), f'precondition: the first drain ran: {first.get("error", crashed.stderr)[-2000:]}'
    assert applied and applied < set(episodes), (
        f'precondition: the first drain committed one batch of 2 rows and stopped ({len(applied)} of {len(episodes)} '
        f'episodes applied; status {first.get("status")})')
    with closing(seams._REAL_CONNECT(str(index))) as db, db:
        db.executemany("UPDATE indexed_episodes SET payload_sha256='t12b-sentinel' WHERE episode_id=?",
                       [(e,) for e in applied])
    with traced() as trace:
        seams.drain(world.store(), batch=2)
    named = [e for e in trace.events if e.kind == 'sql' and any(identity in e.sql for identity in applied)]
    rest = set(episodes) - applied
    seen_rest = {identity for identity in rest if any(identity in e.sql for e in trace.events)}
    after = _indexed(index)
    problems = []
    if named:
        problems.append(f'the restart named {len(named)} times an episode applied below the watermark: '
                        f'{[e.sql[:120] for e in named[:3]]}')
    if any(after.get(identity) != 't12b-sentinel' for identity in applied):
        problems.append('the restart re-applied an episode below the watermark (its sentinel digest was replaced)')
    if not rest <= set(after):
        problems.append(f'the restart skipped {len(rest - set(after))} episodes above the watermark')
    if rest and not seen_rest:
        problems.append('instrument: the restart\'s statements name none of the episodes it applied (no expanded SQL)')
    assert not problems, 'restart at the watermark:\n' + '\n'.join(problems)


def test_b2_retention_is_bounded_by_the_batch(tmp_path):
    """GREEN-IF after N=7 seals drained with batch 2, at most 2 outbox rows remain, and so after a reindex request
    (the request seam, which only writes the row) followed by 5 more seals, drained with batch 2 (the build's watermark then covers rows queued
    after the request); and no committed store change during either drain removed more than 2 outbox rows at once
    (retention deletes at most `batch` rows per transaction)."""
    world = _world(tmp_path / 'world')
    store_path = world.store_path
    problems = []
    for phase in ('seals', 'seals and a reindex'):
        if phase != 'seals':
            seams.request_reindex(world.store())  # only the row: the drain below builds it
        _legacy_seals(world, 7 if phase == 'seals' else 5, tag='retention-' + phase.replace(' ', '-'))
        counts = []

        def observe(_trace):
            counts.append(outbox_count(store_path))
        start = outbox_count(store_path)
        assert start >= 5, f'precondition: the seals queued {start} outbox rows'
        with traced(observe):
            seams.drain(world.store(), batch=2)
        final = outbox_count(store_path)
        drops = [before - after for before, after in zip([start] + counts, counts) if before > after]
        if final > 2:
            problems.append(f'{phase}: {final} outbox rows remain after the drain (batch 2)')
        if drops and max(drops) > 2:
            problems.append(f'{phase}: one committed change removed {max(drops)} outbox rows (batch 2)')
        if start - final < start - 2:
            problems.append(f'{phase}: positive control: retention removed only {start - final} of {start} rows')
    assert not problems, 'retention:\n' + '\n'.join(problems)


def test_b2_connection_status_reports_index_lag(tmp_path):
    """GREEN-IF `memory.connection_status` reports `index_lag`, an integer equal to the store's outbox row count,
    beside `status` (still `ready`), before and after a drain."""
    world = _world(tmp_path / 'world')
    tools = world.tools(corpus.A)
    readings = []
    for step in ('fresh', 'sealed', 'drained'):
        if step == 'sealed':
            _legacy_seals(world, 3, tag='lag')
            world.store().capture(corpus.A, source_ref='t12b:lag', events=w.events('Juniper lag.'))
        if step == 'drained':
            seams.drain(world.store())
        status = tools.call('memory.connection_status', {})
        readings.append((step, status.get('status'), status.get(seams.INDEX_LAG), outbox_count(world.store_path)))
    problems = [f'{step}: status {state!r}, index_lag {lag!r}, outbox rows {rows}' for step, state, lag, rows in readings
                if state != 'ready' or type(lag) is not int or lag != rows]
    assert readings[1][3] >= 4, f'precondition: the seals queued rows: {readings}'
    assert not problems, 'memory.connection_status index_lag:\n' + '\n'.join(problems)
