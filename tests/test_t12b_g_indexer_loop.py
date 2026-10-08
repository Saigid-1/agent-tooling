"""T12b meet G1–G4 (Verification, read of the round-1 meet head; product with the bounded marks): the indexer role's loop, health,
reindex refusal, the index-ahead-of-outbox refusal and the per-drain `index_lag`, in-process on the product's own loop.

Order: docs/work/orders/T12b-one-indexer-outbox.md.
- G1, B2: "the `indexer` role's health (r5): unhealthy when the watermark has not advanced across 5 consecutive
  non-skipped drains while outbox rows exist. A drain that skipped on a held lease does not count. Steady inflow with a
  moving watermark is healthy"; and the role "follows T12a's loop rules: backoff, `--max-passes`, restart" and "A lease
  skip is a clean pass. It is not an error, and it does not advance the backoff."
- G2, B3: "Otherwise the reindex refuses and keeps the old file ... The refusal names the episodes."
- G3, B2/B3: an index that has applied outbox rows its store never issued (a store restored from a snapshot, the newer
  index kept) is reported (`index_ahead_of_outbox`), applies nothing, and a requested reindex resolves it.
- G4, B2: "`index_lag` (T4). It is the outbox row count, reported in three places: ... the `indexer` role's per-drain
  JSON line".

The loop is FEATURE's `episodic_search.watch(root, interval, max_passes=..., batch=..., clock=...)` (seam:
tests/t12b_seams.py WATCH_NAME, HEALTH_NAME, STALL_COUNT, HEALTH_FILE), driven with a fake clock whose `sleep` records
each wait and runs the test's step between passes; its per-drain JSON lines are read from stdout. A store's drain fails
on the same rows by an index outage: a directory at the index path, with outbox rows waiting. A lease is held by a
child process through the product's non-blocking lock primitive (`leaf.nonblocking_lock`), as another drainer would.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

import t10_corpus as corpus
import t10_world as w
import t12b_seams as seams

HERE = Path(__file__).resolve().parent
INTERVAL = 2.0


def _world(root):
    world = w.World(root, corpus.DESKS, corpus.SESSIONS)
    world.initialize()
    return world


def _seal(world, count, tag):
    store = world.store()
    return [store.import_operator_episode(
        tenant_id=w.TENANT_ONE, role='alpha', repo_key=w.REPO, source_ref=f't12b:g:{tag}:{i}',
        events=w.imported_events(f'Juniper loop {tag} {i}.'),
        source_provenance=w.legacy_provenance(f't12b-g-{tag}-{i}'))['episode_id'] for i in range(count)]


class Clock:
    """A fake clock: `sleep` records the wait, the stdout written so far (a pass boundary), and runs `steps[n]`
    after the n-th pass (1-based)."""

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


def _run_watch(root, *, max_passes, steps=None, batch=500):
    watch = seams.indexer_function(seams.WATCH_NAME)
    out = io.StringIO()
    clock = Clock(out, steps)
    with contextlib.redirect_stdout(out):
        code = watch(root, INTERVAL, max_passes=max_passes, batch=batch, clock=clock)
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
    return code, passes, clock


def _line(lines, store_dir):
    found = [line for line in lines if str(line.get('store', '')).startswith(os.path.realpath(store_dir))
             or str(line.get('store', '')).startswith(str(store_dir))]
    return found[0] if len(found) == 1 else None


class LeaseHolder:
    """Another drainer: a child process holding `<state_root>/index.lock` through the product's lock primitive."""

    SCRIPT = ('import sys\nfrom pathlib import Path\nfrom kp_agent_tooling._impl import leaf\n'
              'with leaf.nonblocking_lock(Path(sys.argv[1])) as held:\n'
              '    print(held, flush=True)\n    sys.stdin.read()\n')

    def __init__(self, lock_path):
        env = dict(os.environ)
        env['PYTHONPATH'] = os.pathsep.join([str(HERE.parent / 'packages' / 'tooling' / 'src'), env.get('PYTHONPATH', '')])
        self.process = subprocess.Popen([sys.executable, '-c', self.SCRIPT, str(lock_path)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, text=True, env=env)
        self.held = self.process.stdout.readline().strip() == 'True'

    def release(self):
        self.process.stdin.close()
        self.process.wait(30)


def _health_file(root):
    path = Path(root) / seams.HEALTH_FILE
    return json.loads(path.read_text()) if path.is_file() else None


# ------------------------------------------------------------------------------------------------------------ G1

def test_g1_indexer_health_turns_unhealthy_after_5_stalled_drains_ignores_lease_skips_and_recovers(tmp_path):
    """GREEN-IF, with the indexer watching a root that holds store A (3 seals waiting, its index path a directory: every
    drain fails on the same rows) and a healthy store B, over 8 passes of `watch`, A's lease held by another process
    during passes 5 and 7:
    - passes 1-4: A's per-drain line reads `error` (a non-skipped drain), `healthy`, its stall count 1..4;
    - pass 5 (lease held, A one stalled drain short of the rule): A's line reads `skipped` with the count left at exactly
      4 and `healthy`, and `<root>/indexer-health.json` still reads `healthy` (a skip does not count);
    - pass 6: A's line reads `error`, `unhealthy`, count 5 (5 consecutive non-skipped drains, the watermark not
      advancing, outbox rows waiting); `<root>/indexer-health.json` then reads `unhealthy` and `health(root)` returns
      exit 1 with a record naming A's store;
    - pass 7 (lease held again): A's line reads `skipped` with the count left at exactly 5 and `unhealthy`;
    - pass 8, A's index back: A's rows are applied (`drained`, the watermark advanced, A's outbox empty) and the line
      reads `healthy` with the count reset to 0; `health(root)` then exits 0;
    - B is drained in pass 1 and stays healthy throughout."""
    root = tmp_path / 'root'
    a, b = _world(root / 'a'), _world(root / 'b')
    _seal(a, 3, 'stalled')
    _seal(b, 2, 'healthy')
    index_a = a.state / seams.INDEX_NAME
    index_a.mkdir()  # the outage: every drain of A fails on the same rows
    health = seams.indexer_function(seams.HEALTH_NAME)
    seen, holders = {}, []

    def hold():
        holders.append(LeaseHolder(seams.lease_path_of(a.store())))

    def after_pass_5():
        seen['file_5'] = _health_file(root)
        holders[0].release()

    def after_pass_6():
        seen['file_6'] = _health_file(root)
        seen['health_6'] = health(root)
        hold()

    def after_pass_7():
        holders[1].release()
        index_a.rmdir()  # the index is back (absent: the next drain creates it)
    try:
        code, passes, clock = _run_watch(root, max_passes=8,
                                         steps={4: hold, 5: after_pass_5, 6: after_pass_6, 7: after_pass_7})
    finally:
        for holder in holders:
            if holder.process.poll() is None:
                holder.release()
    assert len(holders) == 2 and all(holder.held for holder in holders), \
        'precondition: the other process held A\'s lease before passes 5 and 7'
    assert len(passes) == 8, f'the loop ran {len(passes)} passes, max_passes 8'
    lines_a = [_line(p, a.state) for p in passes]
    lines_b = [_line(p, b.state) for p in passes]
    assert all(lines_a) and all(lines_b), f'precondition: one line per store per pass: {passes}'
    expected = {1: ('error', 'healthy', 1), 2: ('error', 'healthy', 2), 3: ('error', 'healthy', 3),
                4: ('error', 'healthy', 4), 5: ('skipped', 'healthy', 4), 6: ('error', 'unhealthy', 5),
                7: ('skipped', 'unhealthy', 5)}
    problems = []
    for number, (status, state, count) in expected.items():
        line = lines_a[number - 1]
        found = (line.get('status'), line.get('health'), line.get(seams.STALL_COUNT))
        if found != (status, state, count):
            problems.append(f'pass {number}: A reads (status, health, {seams.STALL_COUNT}) {found}, '
                            f'expected {(status, state, count)}')
    if (seen.get('file_5') or {}).get('status') != 'healthy':
        problems.append(f'after the lease-skip pass 5 {seams.HEALTH_FILE} reads {seen.get("file_5")} (healthy expected)')
    if (seen.get('file_6') or {}).get('status') != 'unhealthy':
        problems.append(f'after pass 6 {seams.HEALTH_FILE} is not written unhealthy: {seen.get("file_6")}')
    exit_code, health_record = seen.get('health_6', (None, None))
    named = json.dumps(health_record)
    if exit_code != 1 or (str(a.state.resolve()) not in named and str(a.state) not in named):
        problems.append(f'after pass 6 health(root) returned {exit_code}, {health_record} (exit 1 naming A expected)')
    eight = lines_a[7]
    if (eight.get('status') != 'drained' or eight.get('advanced') is not True or eight.get('health') != 'healthy'
            or eight.get(seams.STALL_COUNT) != 0):
        problems.append(f'pass 8 (index back): A reads {eight.get("status")}, advanced {eight.get("advanced")}, '
                        f'{eight.get("health")}, {seams.STALL_COUNT}={eight.get(seams.STALL_COUNT)} '
                        '(drained, advanced, healthy, count 0 expected)')
    if seams.outbox_count(a.store_path) != 0:
        problems.append('pass 8 did not apply A\'s rows')
    if health(root)[0] != 0:
        problems.append(f'after pass 8 health(root) is {health(root)}')
    if lines_b[0].get('status') != 'drained' or any(line.get('health') != 'healthy' for line in lines_b):
        problems.append(f'B was not drained in pass 1 or read unhealthy: {[(l.get("status"), l.get("health")) for l in lines_b]}')
    assert not problems, 'the indexer health rule:\n' + '\n'.join(problems)

def _health_of(root):
    """`health(root)` as (exit code, status), or (None, the exception) when it raises."""
    try:
        code, record = seams.indexer_function(seams.HEALTH_NAME)(root)
    except Exception as error:  # noqa: BLE001 - a raising health check is the reading
        return None, f'raised {error!r}'
    return code, (record or {}).get('status')


def _dead_pid():
    """The pid of a child process that has exited and been reaped."""
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    child.wait(30)
    return child.pid


HAS_PROC = Path('/proc/self/stat').exists()


def test_g1_health_reads_absent_then_healthy_after_the_first_write_and_unreadable_for_garbage(tmp_path):
    """The health record's states (meet ruling). GREEN-IF `health(root)` returns:
    - exit 1, `absent`, when no `<root>/indexer-health.json` exists;
    - exit 0, `healthy`, after the indexer's first write (one pass of `watch` over a healthy store);
    - exit 1, `unreadable`, when the file holds garbage bytes (it neither raises nor reads healthy)."""
    root = tmp_path / 'root'
    world = _world(root / 'w')
    _seal(world, 1, 'states')
    path = root / seams.HEALTH_FILE
    problems = []
    found = _health_of(root)
    if found != (1, 'absent'):
        problems.append(f'no record: health(root) reads {found}, (1, absent) expected')
    _run_watch(root, max_passes=1)
    found = _health_of(root)
    if not path.is_file() or found != (0, 'healthy'):
        problems.append(f'after the first write: file {path.is_file()}, health(root) reads {found}, (0, healthy) expected')
    path.write_bytes(b'\x00\xff{not json')
    found = _health_of(root)
    if found != (1, 'unreadable'):
        problems.append(f'garbage bytes: health(root) reads {found}, (1, unreadable) expected')
    assert not problems, 'the health record states:\n' + '\n'.join(problems)


@pytest.mark.skipif(not HAS_PROC, reason='the staleness rule reads /proc (Linux, the indexer role\'s image); this '
                                         'host has no /proc, so a dead writer is not measurable here')
def test_g1_health_reads_stale_for_a_record_whose_writer_is_not_running(tmp_path):
    """The health record's states (meet ruling). GREEN-IF a healthy record rewritten to name a writer pid that is not
    running (a child that has exited and been reaped) reads exit 1, `stale`; the same record naming this live process
    reads exit 0, `healthy` (positive control). The record is the one the indexer wrote; only its writer's pid is
    changed (FEATURE's `writer.pid`; a record with no writer field gains one)."""
    root = tmp_path / 'root'
    world = _world(root / 'w')
    _seal(world, 1, 'stale')
    _run_watch(root, max_passes=1)
    path = root / seams.HEALTH_FILE
    record = json.loads(path.read_text())
    live = _health_of(root)
    writer = record.get('writer') if isinstance(record.get('writer'), dict) else {}
    record['writer'] = dict(writer, pid=_dead_pid())
    path.write_text(json.dumps(record, sort_keys=True) + '\n')
    dead = _health_of(root)
    assert live == (0, 'healthy') and dead == (1, 'stale'), (
        f'health(root): live writer {live} ((0, healthy) expected), dead writer {dead} ((1, stale) expected)')


def test_g1_indexer_loop_backs_off_after_failed_passes_not_after_a_skip_and_ends_at_max_passes(tmp_path):
    """T12a's loop rules for the indexer. GREEN-IF, over 7 passes (A failing 5 times, then skipped under a held
    lease, then applying; B healthy): the loop survives the failing store (all 7 passes run, B drained), ends at
    `max_passes` with no sleep after the last pass (6 sleeps) and exit 0 (the last pass did not fail); each sleep after
    a failed pass is at least the interval and they back off (non-decreasing, the last of them longer than the interval,
    and never more than 10 intervals: T12a's cap); the sleep after the lease-skip pass is exactly the interval (a skip is
    a clean pass that does not advance the backoff)."""
    root = tmp_path / 'root'
    a, b = _world(root / 'a'), _world(root / 'b')
    _seal(a, 3, 'stalled')
    _seal(b, 2, 'healthy')
    index_a = a.state / seams.INDEX_NAME
    index_a.mkdir()
    holder = {}

    def after_pass_6():
        holder['lease'].release()
        index_a.rmdir()
    steps = {5: lambda: holder.setdefault('lease', LeaseHolder(seams.lease_path_of(a.store()))), 6: after_pass_6}
    try:
        code, passes, clock = _run_watch(root, max_passes=7, steps=steps)
    finally:
        if 'lease' in holder and holder['lease'].process.poll() is None:
            holder['lease'].release()
    # The lease is taken after pass 5: a loop that ended earlier never took it (reported below as too few passes).
    assert 'lease' not in holder or holder['lease'].held, 'precondition: the other process held A\'s lease'
    problems = []
    if len(passes) != 7 or len(clock.sleeps) != 6:
        problems.append(f'{len(passes)} passes and {len(clock.sleeps)} sleeps (7 passes, 6 sleeps expected)')
    if code != 0:
        problems.append(f'watch returned {code} after a clean last pass')
    failed = clock.sleeps[:5]
    if (len(failed) != 5 or any(s < INTERVAL for s in failed) or failed != sorted(failed) or failed[-1] <= INTERVAL
            or any(s > 10 * INTERVAL for s in failed)):
        problems.append(f'sleeps after the 5 failed passes {failed} do not back off (interval {INTERVAL})')
    if len(clock.sleeps) > 5 and clock.sleeps[5] != INTERVAL:
        problems.append(f'the sleep after the lease-skip pass is {clock.sleeps[5]}, not the interval {INTERVAL}')
    if not passes or (_line(passes[0], b.state) or {}).get('status') != 'drained':
        problems.append('the healthy store was not drained beside the failing one')
    assert not problems, 'the indexer loop rules:\n' + '\n'.join(problems)


# ------------------------------------------------------------------------------------------------------------ G2

def _tamper_one_covered(world):
    with closing(sqlite3.connect(world.store_path)) as db, db:
        episode = db.execute('SELECT episode_id FROM episode_scope WHERE covered = 1 ORDER BY episode_id LIMIT 1'
                             ).fetchone()[0]
        db.execute("UPDATE episode_scope SET payload_sha256 = ? WHERE episode_id = ?", ('0' * 64, episode))
    return episode


def _token(index):
    with seams.ro(index) as db:
        return db.execute('SELECT token FROM coverage_token').fetchone()[0]


@pytest.mark.parametrize('tampered', [True, False], ids=['tampered', 'positive-control'])
def test_g2_a_reindex_whose_build_does_not_hold_the_claimed_coverage_is_refused_naming_the_episode(tmp_path, tampered):
    """GREEN-IF, on a complete store (3 episodes drained, token carried) whose coverage marks claim, for one covered
    episode, a digest the build will not hold (`episode_scope.payload_sha256` tampered), a requested reindex run by the
    indexer is reported as `reindex_refused` in its per-drain line naming that episode; the index file keeps its inode
    and its token; no build file is left beside it; and a search still answers (results returned). Positive control
    (not tampered): the same reindex is applied (`drained`, its reindex reporting the token carried), and the index is
    a new file (new inode) holding the old token."""
    world = _world(tmp_path / 'root' / 'w')
    _seal(world, 3, 'refused')
    seams.drain(world.store())
    index = world.state / seams.INDEX_NAME
    inode, token = os.stat(index).st_ino, _token(index)
    episode = _tamper_one_covered(world) if tampered else None
    seams.request_reindex(world.store())
    code, passes, _ = _run_watch(tmp_path / 'root', max_passes=1)
    line = _line(passes[0], world.state)
    assert line, f'precondition: one per-drain line for the store: {passes}'
    # The build file and its journals: index-family names that are not the index file or the index's own journals.
    leftovers = [p.name for p in world.state.iterdir()
                 if seams.index_family(p.name) and not p.name.startswith(seams.INDEX_NAME)]
    if tampered:
        problems = []
        if line.get('category') != 'reindex_refused' or episode not in json.dumps(line):
            problems.append(f'the refusal is not reported as reindex_refused naming {episode}: {line}')
        if os.stat(index).st_ino != inode or _token(index) != token:
            problems.append('the old index file was not kept (inode or token changed)')
        if leftovers:
            problems.append(f'build files left beside the index: {leftovers}')
        found = world.tools(corpus.A).call('memory.search', {'query': 'juniper loop'})
        if found.get('index_status') == 'index_unavailable' or not found.get('results'):
            problems.append(f'search does not answer after the refusal: {found}')
        assert not problems, 'the reindex refusal:\n' + '\n'.join(problems)
    else:
        assert line.get('status') == 'drained' and (line.get('reindex') or {}).get('token') == 'carried', (
            f'the un-tampered reindex was not applied with the token carried: {line}')
        assert os.stat(index).st_ino != inode and _token(index) == token, 'positive control: no swap with a carried token'
        assert not leftovers, f'build files left beside the index: {leftovers}'


# ------------------------------------------------------------------------------------------------------------ G3

def test_g3_an_index_ahead_of_its_restored_store_is_reported_applies_nothing_and_a_reindex_resolves_it(tmp_path):
    """GREEN-IF, with a store restored from a snapshot taken before its last 3 seals while the newer index (which applied
    those seals' outbox rows) is kept: the indexer's next drain reports `index_ahead_of_outbox` and applies nothing (the
    index's postings, event count, watermark row and inode unchanged); then, a reindex requested, the next drain
    applies it (no error), the index holds exactly the restored store's episodes, and the drain after that is clean."""
    root = tmp_path / 'root'
    world = _world(root / 'w')
    kept = _seal(world, 2, 'kept')
    seams.drain(world.store())
    snapshot = tmp_path / 'snapshot.sqlite3'
    with closing(sqlite3.connect(world.store_path)) as live, closing(sqlite3.connect(snapshot)) as out:
        live.backup(out)
    _seal(world, 3, 'lost')
    seams.drain(world.store())
    index = world.state / seams.INDEX_NAME

    def index_state():
        with seams.ro(index) as db:
            return (sorted(db.execute('SELECT episode_id, payload_sha256 FROM indexed_episodes')),
                    db.execute('SELECT count(*) FROM event_search').fetchone()[0],
                    db.execute('SELECT seq FROM outbox_watermark').fetchall(), os.stat(index).st_ino)
    with closing(sqlite3.connect(snapshot)) as saved, closing(sqlite3.connect(world.store_path)) as live:
        saved.backup(live)  # the store restored; the newer index kept
    before = index_state()
    code, passes, _ = _run_watch(root, max_passes=1)
    line = _line(passes[0], world.state)
    problems = []
    if not line or line.get('category') != 'index_ahead_of_outbox':
        problems.append(f'the drain did not report index_ahead_of_outbox: {line}')
    if index_state() != before:
        problems.append('the drain changed the index of another store history')
    seams.request_reindex(world.store())
    code, passes, _ = _run_watch(root, max_passes=1)
    line = _line(passes[0], world.state)
    if not line or line.get('status') == 'error':
        problems.append(f'the requested reindex did not resolve it: {line}')
    else:
        held = {episode for episode, _ in index_state()[0]}
        if held != set(kept):
            problems.append(f'after the reindex the index holds {len(held)} episodes, the restored store {len(kept)}')
        code, passes, _ = _run_watch(root, max_passes=1)
        after = _line(passes[0], world.state) or {}
        if after.get('status') == 'error':
            problems.append(f'the drain after the reindex still fails: {after}')
    assert not problems, 'an index ahead of its store:\n' + '\n'.join(problems)


# ------------------------------------------------------------------------------------------------------------ G4

def test_g4_the_per_drain_line_index_lag_is_the_outbox_row_count_before_and_after_an_applying_pass(tmp_path):
    """B2: `index_lag` "is the outbox row count, reported in ... the `indexer` role's per-drain JSON line". GREEN-IF,
    with a store holding 3 seals waiting: pass 1, the store's lease held by another process, is a skip whose line's
    `index_lag` equals the store's `index_outbox` row count read (read-only) right after the pass, 3; pass 2 applies
    the rows (`drained`, 3 rows applied) and its line's `index_lag` equals the row count read right after it (the rows
    left above the watermark: 0). The line and the table are read, not a product function, so the reading does not
    depend on which function computes the count."""
    root = tmp_path / 'root'
    world = _world(root / 'w')
    _seal(world, 3, 'lag')
    counts, holders = {}, [LeaseHolder(seams.lease_path_of(world.store()))]

    def after_pass_1():
        counts[1] = seams.outbox_count(world.store_path)
        holders[0].release()
    try:
        code, passes, _ = _run_watch(root, max_passes=2, steps={1: after_pass_1})
    finally:
        if holders[0].process.poll() is None:
            holders[0].release()
    counts[2] = seams.outbox_count(world.store_path)
    assert holders[0].held, 'precondition: the other process held the store\'s lease before pass 1'
    assert counts[1] == 3, f'precondition: 3 outbox rows waiting after the skipped pass, found {counts[1]}'
    one, two = (_line(p, world.state) or {} for p in passes)
    problems = []
    if one.get('status') != 'skipped' or one.get(seams.INDEX_LAG) != counts[1]:
        problems.append(f'pass 1 (lease held): status {one.get("status")}, {seams.INDEX_LAG} {one.get(seams.INDEX_LAG)}; '
                        f'the outbox holds {counts[1]} rows')
    if two.get('status') != 'drained' or two.get('applied_rows') != 3 or two.get(seams.INDEX_LAG) != counts[2]:
        problems.append(f'pass 2 (applying): status {two.get("status")}, applied {two.get("applied_rows")}, '
                        f'{seams.INDEX_LAG} {two.get(seams.INDEX_LAG)}; the outbox holds {counts[2]} rows')
    assert not problems, 'index_lag on the per-drain line is the outbox row count:\n' + '\n'.join(problems)
