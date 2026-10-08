"""T12b B3 (meet ruling, Verification): the mismatch-reset-retry loop in `SessionSources.mark_coverage` is capped.

Background: mutant M18 (the token switch in `_reset_coverage` made a no-op) made `mark_coverage`'s loop (the store
names another token -> reset -> re-read -> ...) spin forever; the suite hung. FEATURE caps it: after 3 resets the
loop raises `EpisodeUnavailable` with category `coverage_token_unstable`.

M18's effect is applied in-process: `SessionSources._reset_coverage` is replaced by a spy that counts each reset and
zeroes the store's coverage marks (one statement) but never writes `scope_state`'s `coverage_token`, so the switch
never lands. Before the cap exists the call never returns, so every call here runs under a deadline (`signal.alarm`,
raising a BaseException so no product `except Exception` turns it into a result); a deadline is reported as the
failure it is.
"""
from __future__ import annotations

import json
import signal
import sqlite3
from contextlib import closing, contextmanager

import pytest

import t12b_seams as seams
from test_t12b_g_indexer_loop import _line, _run_watch, _seal, _world

DEADLINE_SECONDS = 10
CAP = 3
CATEGORY = 'coverage_token_unstable'


class Deadline(BaseException):
    """The call did not end within DEADLINE_SECONDS."""


@contextmanager
def deadline(seconds=DEADLINE_SECONDS):
    def expire(signum, frame):
        raise Deadline(f'no result within {seconds} s')
    previous = signal.signal(signal.SIGALRM, expire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@contextmanager
def switch_never_lands(monkeypatch):
    """M18's effect: each `_reset_coverage(token)` zeroes the marks but never names `token`. Yields the reset count."""
    from kp_agent_tooling._impl.service.session_sources import SessionSources
    real = SessionSources._reset_coverage
    resets = {'count': 0, 'tokens': []}

    def reset(self, token):
        resets['count'] += 1
        resets['tokens'].append(token)
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE episode_scope SET covered = 0 WHERE covered = 1')
    monkeypatch.setattr(SessionSources, '_reset_coverage', reset)
    try:
        yield resets
    finally:
        monkeypatch.setattr(SessionSources, '_reset_coverage', real)


def _store_token(world):
    with seams.ro(world.store_path) as db:
        row = db.execute("SELECT value FROM scope_state WHERE key = 'coverage_token'").fetchone()
    return row[0] if row else None


def _index_state(world):
    with seams.ro(world.state / seams.INDEX_NAME) as db:
        return (db.execute('SELECT token FROM coverage_token').fetchone()[0],
                db.execute('SELECT seq FROM outbox_watermark').fetchall())


def _drained_world(tmp_path):
    world = _world(tmp_path / 'root' / 'w')
    _seal(world, 3, 'unstable')
    seams.drain(world.store())
    assert _store_token(world) == _index_state(world)[0], 'precondition: the store names its index\'s token'
    return world


def test_b3_mark_coverage_on_a_token_that_never_lands_raises_coverage_token_unstable_within_3_resets(tmp_path,
                                                                                                    monkeypatch):
    """GREEN-IF, with the token switch never landing (M18's effect, in-process), a `mark_coverage` call for a token the
    store does not name raises, within DEADLINE_SECONDS, `EpisodeUnavailable` whose `category` is
    `coverage_token_unstable`, after at least 1 and at most 3 resets (counted by the spy), every one of them for the
    call's token; and the store still names its own token (nothing switched)."""
    from kp_agent_tooling._impl.service.episodic_memory import EpisodeUnavailable
    from kp_agent_tooling._impl.service.session_sources import SessionSources
    world = _drained_world(tmp_path)
    store, before = world.store(), _store_token(world)
    other = 'f' * 32
    with seams.ro(world.store_path) as db:
        digests = dict(db.execute('SELECT episode_id, payload_sha256 FROM episode_scope'))
    raised = outcome = None
    with switch_never_lands(monkeypatch) as resets:
        try:
            with deadline():
                outcome = SessionSources(store).mark_coverage(digests, other)
        except Deadline as expired:
            pytest.fail(f'mark_coverage did not end: {expired} after {resets["count"]} resets (no cap)', pytrace=False)
        except EpisodeUnavailable as error:
            raised = error
    problems = []
    if raised is None:
        problems.append(f'mark_coverage returned {outcome!r} instead of raising')
    elif getattr(raised, 'category', None) != CATEGORY:
        problems.append(f'it raised {type(raised).__name__} with category {getattr(raised, "category", None)!r}: {raised}')
    if not 1 <= resets['count'] <= CAP or set(resets['tokens']) != {other}:
        problems.append(f'{resets["count"]} resets for tokens {set(resets["tokens"])} (1..{CAP} for {other!r} expected)')
    if _store_token(world) != before:
        problems.append('the store\'s token changed although the switch never lands')
    assert not problems, 'the capped mismatch-reset-retry loop:\n' + '\n'.join(problems)


def _unstable_drain(tmp_path, monkeypatch):
    """A drained store whose `coverage_token` is then replaced (its index kept) and one more episode sealed; one pass
    of the indexer (`watch`, max_passes 1) with the token switch never landing, under the deadline. Returns (the
    store's per-drain line, the index's (token, watermark rows) before and after, the reset count)."""
    world = _drained_world(tmp_path)
    with closing(sqlite3.connect(world.store_path)) as db, db:
        db.execute("UPDATE scope_state SET value = ? WHERE key = 'coverage_token'", ('0' * 32,))
    _seal(world, 1, 'unstable-more')
    before = _index_state(world)
    assert _store_token(world) != before[0] and seams.outbox_count(world.store_path) >= 1, (
        'precondition: the store names another token than its index, and a seal is waiting')
    with switch_never_lands(monkeypatch) as resets:
        try:
            with deadline():
                code, passes, _ = _run_watch(tmp_path / 'root', max_passes=1)
        except Deadline as expired:
            pytest.fail(f'the drain did not end: {expired} after {resets["count"]} resets (no cap)', pytrace=False)
    return _line(passes[0], world.state) or {}, before, _index_state(world), resets['count']


def test_b3_a_drain_on_a_store_whose_token_never_lands_reports_coverage_token_unstable(tmp_path, monkeypatch):
    """GREEN-IF, with the token switch never landing (M18's effect) on a store whose token was replaced and a seal
    waiting, one indexer pass ends within DEADLINE_SECONDS and its per-drain line for the store reads `status: error`
    and `category` `coverage_token_unstable`, after at most 3 resets."""
    line, _, _, resets = _unstable_drain(tmp_path, monkeypatch)
    problems = []
    if (line.get('status'), line.get('category')) != ('error', CATEGORY):
        problems.append(f'the per-drain line reads {json.dumps({k: line.get(k) for k in ("status", "category", "message")})}'
                        f' (error, {CATEGORY} expected)')
    if resets > CAP:
        problems.append(f'{resets} resets ran (at most {CAP} expected)')
    assert not problems, 'a drain on a store whose token never lands:\n' + '\n'.join(problems)


def test_b3_a_failing_drain_reports_advanced_as_the_index_watermark_moved(tmp_path, monkeypatch):
    """Meet ruling: "On the error path `advanced` is the watermark's real movement (`index.watermark()` after the pass
    vs before), never a default false." GREEN-IF, in the failing pass above (the batch's postings and watermark
    committed to the index before the marks raised), the index's `outbox_watermark` row moved and the line's
    `advanced` is true, equal to (before != after)."""
    line, before, after, _ = _unstable_drain(tmp_path, monkeypatch)
    moved = after[1] != before[1]
    assert moved and line.get('advanced') is True, (
        f'the index watermark went {before[1]} -> {after[1]} (moved {moved}); the line reads '
        f'{line.get("status")}, {line.get("category")}, advanced {line.get("advanced")!r} (true expected)')


# ------------------------------------------------------------------- the sticky signal for lost coverage marks

def _lost_row(world):
    with seams.ro(world.store_path) as db:
        row = db.execute('SELECT value FROM scope_state WHERE key = ?', (seams.LOST_MARKS_KEY,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return {'unparsed': row[0]}


def _lost_on(line):
    return line.get(seams.LINE_COVERAGE_FIELD) == seams.LINE_COVERAGE_LOST


def _reading(line):
    return {k: line.get(k) for k in ('status', 'category', seams.LINE_COVERAGE_FIELD, 'health', 'stalled_drains')}


def _names_store(record, world):
    text = json.dumps(record)
    return str(world.state) in text or str(world.state.resolve()) in text


def _health(root):
    return seams.indexer_function(seams.HEALTH_NAME)(root)


@contextmanager
def _m18_until(monkeypatch, holder):
    """M18's effect while `holder['on']` is true; `holder['on'] = False` restores the real reset mid-run."""
    from kp_agent_tooling._impl.service.session_sources import SessionSources
    real = SessionSources._reset_coverage
    holder.setdefault('on', True)
    holder.setdefault('resets', 0)

    def reset(self, token):
        if not holder['on']:
            return real(self, token)
        holder['resets'] += 1
        with closing(self.store._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE episode_scope SET covered = 0 WHERE covered = 1')
    monkeypatch.setattr(SessionSources, '_reset_coverage', reset)
    try:
        yield holder
    finally:
        monkeypatch.setattr(SessionSources, '_reset_coverage', real)


def _mismatched_world(tmp_path):
    world = _drained_world(tmp_path)
    with closing(sqlite3.connect(world.store_path)) as db, db:
        db.execute("UPDATE scope_state SET value = ? WHERE key = 'coverage_token'", ('0' * 32,))
    return world


def _watch_within_deadline(root, **kwargs):
    try:
        with deadline():
            return _run_watch(root, **kwargs)
    except Deadline as expired:
        pytest.fail(f'the indexer did not end: {expired}', pytrace=False)


def _watermark(world):
    with seams.ro(world.state / seams.INDEX_NAME) as db:
        return db.execute('SELECT seq FROM outbox_watermark').fetchone()[0]


def _held_seq(line):
    detail = line.get(seams.LINE_COVERAGE_DETAIL)
    return detail.get('seq') if isinstance(detail, dict) else None


def test_b3_scenario_a_lost_marks_stay_reported_on_every_later_idle_pass(tmp_path, monkeypatch):
    """Scenario A (meet ruling). GREEN-IF, on a store with one seal waiting whose marks raise once (M18's effect for
    pass 1 only, then the real reset restored), over 6 passes of one `watch`:
    - after pass 1 the store holds the `coverage_marks_lost` row, JSON with `category` `coverage_token_unstable`,
      `message`, `seq` and `at`;
    - pass 1 is an error; passes 2-6 are idle drains with `advanced: false`;
    - EVERY line, passes 1-6, carries `coverage: lost` and reads `unhealthy`;
    - after the run `health(root)` exits 1 with a record naming the store."""
    world = _mismatched_world(tmp_path)
    _seal(world, 1, 'scenario-a')
    holder, seen = {}, {}

    def after_pass_1():
        holder['on'] = False
        seen['row'] = _lost_row(world)
    with _m18_until(monkeypatch, holder):
        code, passes, _ = _watch_within_deadline(tmp_path / 'root', max_passes=6, steps={1: after_pass_1})
    lines = [_line(p, world.state) or {} for p in passes]
    problems = []
    row = seen.get('row')
    if not row or row.get('category') != CATEGORY or not all(key in row for key in ('message', 'seq', 'at')):
        problems.append(f'after the failing pass the store\'s {seams.LOST_MARKS_KEY} row is {row}')
    if len(lines) != 6 or lines[0].get('status') != 'error':
        problems.append(f'precondition: 6 passes, pass 1 failing: {[l.get("status") for l in lines]}')
    for number, line in enumerate(lines, start=1):
        if not _lost_on(line) or line.get('health') != 'unhealthy':
            problems.append(f'pass {number}: {_reading(line)} (coverage lost, unhealthy expected)')
        if number > 1 and (line.get('status') != 'idle' or line.get('advanced') is not False):
            problems.append(f'pass {number}: status {line.get("status")}, advanced {line.get("advanced")!r} '
                            '(an idle drain, advanced false expected)')
    code, record = _health(tmp_path / 'root')
    if code != 1 or not _names_store(record, world):
        problems.append(f'health(root) after the run: {code}, {record} (exit 1 naming the store expected)')
    assert not problems, 'lost coverage marks, idle passes after:\n' + '\n'.join(problems)


def test_b3_scenario_b_a_failing_drain_per_new_seal_stalls_to_unhealthy_with_coverage_lost(tmp_path, monkeypatch):
    """Scenario B (meet ruling). GREEN-IF, with M18's effect throughout and a new seal before each of 5 passes (each
    drain applies its seal's batch to the index, then its marks raise), EVERY line reads `status: error`, category
    `coverage_token_unstable`, `advanced: true`, `stalled_drains` 0 (the B2 rule on a moving watermark),
    `coverage: lost` whose seq is that pass's batch (the index watermark the pass reached), and `unhealthy`."""
    world = _mismatched_world(tmp_path)
    _seal(world, 1, 'scenario-b-0')
    reached = {}

    def step(n):
        reached[n] = _watermark(world)
        _seal(world, 1, f'scenario-b-{n}')
    steps = {n: (lambda n=n: step(n)) for n in range(1, 5)}
    with _m18_until(monkeypatch, {}):
        code, passes, _ = _watch_within_deadline(tmp_path / 'root', max_passes=5, steps=steps)
    reached[5] = _watermark(world)
    lines = [_line(p, world.state) or {} for p in passes]
    problems = []
    if len(lines) != 5:
        problems.append(f'{len(lines)} passes (5 expected)')
    for number, line in enumerate(lines, start=1):
        reading = (line.get('status'), line.get('category'), line.get('advanced'), line.get('stalled_drains'),
                   _lost_on(line), _held_seq(line), line.get('health'))
        expected = ('error', CATEGORY, True, 0, True, reached.get(number), 'unhealthy')
        if reading != expected:
            problems.append(f'pass {number}: (status, category, advanced, stalled, lost, seq, health) {reading}, '
                            f'expected {expected}')
    assert not problems, 'a failing drain per new seal:\n' + '\n'.join(problems)


def test_b3_lost_marks_survive_a_restart_and_a_new_seal_and_only_a_full_re_mark_clears_them(tmp_path, monkeypatch):
    """Restart (meet ruling: only a FULL re-mark clears the record; a drain's incremental chunk never does). GREEN-IF,
    after one failing pass (M18's effect, then the real reset restored):
    - a second `watch` over the same root, with fresh loop state (a new call: no stall counts, no written record),
      reads `coverage: lost` and `unhealthy` on its FIRST pass, and `health(root)` then exits 1;
    - a NEW SEAL, drained by the next pass (its marks now succeed), LEAVES the record: the row is still present, the
      line still reads `coverage: lost` and `unhealthy`, and `health(root)` still exits 1;
    - then a requested reindex, drained (its post-swap marks complete: a full re-mark), clears it: the row is gone,
      the line carries no `coverage: lost` and reads `healthy`, and `health(root)` exits 0."""
    world = _mismatched_world(tmp_path)
    _seal(world, 1, 'restart')
    with _m18_until(monkeypatch, {}):
        _watch_within_deadline(tmp_path / 'root', max_passes=1)
    assert _lost_row(world), 'precondition: the failing pass wrote the lost-marks row'
    _, passes, _ = _watch_within_deadline(tmp_path / 'root', max_passes=1)
    first = _line(passes[0], world.state) or {}
    after_restart = _health(tmp_path / 'root')
    _seal(world, 1, 'restart-new-seal')
    _, passes, _ = _watch_within_deadline(tmp_path / 'root', max_passes=1)
    incremental = _line(passes[0], world.state) or {}
    after_seal = (_lost_row(world), _health(tmp_path / 'root'))
    seams.request_reindex(world.store())
    _, passes, _ = _watch_within_deadline(tmp_path / 'root', max_passes=1)
    cleared = _line(passes[0], world.state) or {}
    problems = []
    if not _lost_on(first) or first.get('health') != 'unhealthy':
        problems.append(f'the restarted indexer\'s first pass: {_reading(first)} (coverage lost, unhealthy expected)')
    if after_restart[0] != 1:
        problems.append(f'health(root) after the restart: {after_restart}')
    if incremental.get('status') != 'drained' or incremental.get('applied_rows') != 1:
        problems.append(f'precondition: the new seal\'s drain applied it: {_reading(incremental)}')
    if after_seal[0] is None or not _lost_on(incremental) or incremental.get('health') != 'unhealthy' \
            or after_seal[1][0] != 1:
        problems.append(f'the new seal\'s drain (an incremental chunk) cleared the record: row {after_seal[0]}, line '
                        f'{_reading(incremental)}, health(root) {after_seal[1][0]} (record kept, lost, unhealthy, '
                        'exit 1 expected)')
    if cleared.get('status') == 'error' or _lost_on(cleared) or cleared.get('health') != 'healthy':
        problems.append(f'after the reindex: {_reading(cleared)} (no error, no coverage lost, healthy expected)')
    if _lost_row(world) is not None:
        problems.append(f'the {seams.LOST_MARKS_KEY} row is still present after the reindex: {_lost_row(world)}')
    if _health(tmp_path / 'root')[0] != 0:
        problems.append(f'health(root) after the reindex: {_health(tmp_path / "root")}')
    assert not problems, 'lost coverage marks across a restart:\n' + '\n'.join(problems)


@contextmanager
def _record_write_fails_once(monkeypatch):
    """The lost-marks record's write fails once (its first call returns a write error and writes nothing), then
    writes as the product does. Patched at `episodic_search._record_marks_lost`, the one write of the row (read at
    FEATURE's first lost-marks commit); no directory or file is put in the store's path."""
    from kp_agent_tooling._impl.service import episodic_search
    real = episodic_search._record_marks_lost
    calls = {'count': 0}

    def write(store, value):
        calls['count'] += 1
        if calls['count'] == 1:
            return sqlite3.OperationalError('database is locked (the test failed this write once)')
        return real(store, value)
    monkeypatch.setattr(episodic_search, '_record_marks_lost', write)
    try:
        yield calls
    finally:
        monkeypatch.setattr(episodic_search, '_record_marks_lost', real)


def test_b3_an_unwritten_lost_marks_record_is_reported_and_written_at_the_next_pass(tmp_path, monkeypatch):
    """An unwritten record (meet ruling). GREEN-IF, with the marks raising in pass 1 (M18's effect, then the real reset
    restored) and the record's write failing that once:
    - pass 1's error line carries `coverage_record` with `status: 'unwritten'`, and the store holds no row after it;
    - pass 2's start writes the record: after pass 2 the row is present (category `coverage_token_unstable`) and pass
      2's line carries `coverage: lost` and reads `unhealthy`."""
    world = _mismatched_world(tmp_path)
    _seal(world, 1, 'unwritten')
    holder, seen = {}, {}

    def after_pass_1():
        holder['on'] = False
        seen['row'] = _lost_row(world)
    with _m18_until(monkeypatch, holder), _record_write_fails_once(monkeypatch) as calls:
        code, passes, _ = _watch_within_deadline(tmp_path / 'root', max_passes=2, steps={1: after_pass_1})
    lines = [_line(p, world.state) or {} for p in passes]
    one, two = (lines + [{}, {}])[:2]
    record = one.get(seams.LINE_COVERAGE_RECORD)
    problems = []
    if one.get('status') != 'error' or not isinstance(record, dict) or record.get('status') != 'unwritten':
        problems.append(f'pass 1: status {one.get("status")}, {seams.LINE_COVERAGE_RECORD} {record!r} '
                        "(error with status 'unwritten' expected)")
    if seen.get('row') is not None:
        problems.append(f'precondition: the failed write left a row after pass 1: {seen.get("row")}')
    row = _lost_row(world)
    if not row or row.get('category') != CATEGORY:
        problems.append(f'after pass 2 the store\'s {seams.LOST_MARKS_KEY} row is {row} (written at the pass start)')
    if not _lost_on(two) or two.get('health') != 'unhealthy':
        problems.append(f'pass 2: {_reading(two)} (coverage lost, unhealthy expected)')
    if calls['count'] < 2:
        problems.append(f'the record write was attempted {calls["count"]} times (a retry at pass 2 expected)')
    assert not problems, 'an unwritten lost-marks record:\n' + '\n'.join(problems)
