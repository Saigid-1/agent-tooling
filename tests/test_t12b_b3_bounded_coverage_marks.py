"""T12b B3: the post-swap coverage marks are written in bounded store transactions (meet, for FEATURE's bounded-marks commit).

Background (FEATURE, measured on a copy of the live store): the single `mark_coverage` transaction after a reindex's
swap held the store's write lock for up to 367 s, while a sealer's `BEGIN IMMEDIATE` waits 5 s. The bounded-marks commit marks
coverage after the swap in store transactions of at most `batch` episodes; with the coverage token carried, only the
episodes not yet covered are marked. Verification accepts it with this property (its words), as amended by the meet
ruling of 2026-10-05 (MARK_BATCH): the bound is its own module constant `episodic_search.MARK_BATCH` (ruled 100, read
through tests/t12b_seams.py `mark_batch()`), not the drain's `batch`:

- every `mark_coverage` call carries at most MARK_BATCH episodes, and the call count is >= ceil(N / MARK_BATCH);
  superseded by the universal ruling (Verification, meet): no store transaction changes more than MARK_BATCH
  `episode_scope` rows, whatever the caller, and `mark_coverage` chunks every marking itself (the universal bound), so the bound is
  read per store transaction (class `Transactions`), not per call;
- when the token is new, the FIRST call alone resets; when the token is carried, NO call resets;
- with a carried token, only episodes with `covered = 0` are passed.
Edges pinned as their own cases:
- an empty store with a new token: exactly one call, with an empty dict, and it resets (an empty store must still
  reset: the store's coverage token becomes the new index's);
- a carried token with nothing uncovered: exactly one call, with an empty dict, no reset, and the store's coverage
  state is unchanged (a no-op `BEGIN IMMEDIATE`).

Soundness of reading the uncovered set OUTSIDE the marking transactions (stated here, as the meet asked). The set
`covered = 0` is read once, before the bounded marks; a writer that flipped `covered` 1 -> 0, or changed the store's
coverage token, between that read and the marks would leave an episode under-reported (never over-reported: each mark
sets `covered` from digest equality inside its own transaction). It is complete because the indexer (`drain`, and the
operator's `reindex`, both under the index lease) is the one coverage writer in the flows: seal, claim, link and import
write `covered` only as 0 for the rows they add, or carry it unchanged when they re-project (session_sources
`_reflect`), and they never call `mark_coverage`. Two writers exist outside the indexer, by reading the product at
the bounded-marks commit (a reading, not a measurement): `EpisodicSearchIndex.upsert_episodes`, which no product path calls (its
docstring says so; only tests call it), and the operator's `upgrade-sources` (`_establish_coverage`), which writes
`covered` through `_cover` in its own transactions, not through `mark_coverage`, and only while an operator runs it.
test_b3_the_indexer_is_the_only_mark_coverage_caller_in_the_flows measures the first half: every `mark_coverage` call
made by the sealing flows, `index-history`, `upgrade-sources` and the drains after them has the indexer on its stack.

Instrument: `SessionSources.mark_coverage` is wrapped for the duration of one drain; each call records the episodes
it carries, its `reset`, and whether the reindex's rename onto the index (the `os.rename` audit event, raised before
the rename runs) had already happened. The drain runs with only the reindex request ahead of the episodes it builds,
so every call in it is the post-swap marking (asserted). Seams: tests/t12b_seams.py (`request_reindex`, `drain`).
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest

import t10_corpus as corpus
import t10_world as w
import t12b_seams as seams


def _world(root):
    world = w.World(root, corpus.DESKS, corpus.SESSIONS)
    world.initialize()
    return world


def _seal(world, count, tag):
    store = world.store()
    return [store.import_operator_episode(
        tenant_id=w.TENANT_ONE, role='alpha', repo_key=w.REPO, source_ref=f't12b:marks:{tag}:{i}',
        events=w.imported_events(f'Juniper marks {tag} {i}.'),
        source_provenance=w.legacy_provenance(f't12b-marks-{tag}-{i}'))['episode_id'] for i in range(count)]


def _indexer_on_stack(frame):
    while frame is not None:
        if frame.f_code.co_name in ('drain', 'reindex') and frame.f_globals.get('__name__') == seams.DRAIN_MODULE:
            return True
        frame = frame.f_back
    return False


class Marks:
    """The `mark_coverage` calls of one drain, and whether the swap's rename had happened at each."""

    def __init__(self, world):
        self.world = world
        self.index = os.path.realpath(world.state / seams.INDEX_NAME)
        self.calls, self.renames = [], 0

    def on_audit(self, event, args):
        if event == 'os.rename':
            try:
                if os.path.realpath(os.fsdecode(os.fspath(args[1]))) == self.index:
                    self.renames += 1
            except (TypeError, ValueError):
                pass

    def run(self, monkeypatch, batch):
        from kp_agent_tooling._impl.service.session_sources import SessionSources
        real = SessionSources.mark_coverage
        marks = self

        def spy(self, digests, token, *, reset=False):
            marks.calls.append({'episodes': dict(digests), 'token': token, 'reset': reset,
                                'after_swap': marks.renames > 0, 'indexer': _indexer_on_stack(sys._getframe(1))})
            return real(self, digests, token, reset=reset)
        monkeypatch.setattr(SessionSources, 'mark_coverage', spy)
        with seams.audit_handler(self.on_audit):
            seams.drain(self.world.store(), batch=batch)
        monkeypatch.setattr(SessionSources, 'mark_coverage', real)
        return self

    def preconditions(self):
        problems = []
        if self.renames != 1:
            problems.append(f'{self.renames} renames onto the index (one build-and-swap expected)')
        if not self.calls:
            problems.append('no mark_coverage call at all')
        if any(not call['after_swap'] for call in self.calls):
            problems.append('a mark_coverage call before the swap in a drain holding only the reindex request')
        return problems


def _covered(world):
    with seams.ro(world.store_path) as db:
        return dict(db.execute('SELECT episode_id, covered FROM episode_scope'))


def _coverage_state(world):
    with seams.ro(world.store_path) as db:
        return (sorted(db.execute('SELECT episode_id, covered FROM episode_scope')),
                sorted(db.execute('SELECT scope, key, tenant, episodes, covered FROM scope_counts')),
                db.execute("SELECT value FROM scope_state WHERE key = 'coverage_token'").fetchone())


def _index_token(world):
    with seams.ro(world.state / seams.INDEX_NAME) as db:
        return db.execute('SELECT token FROM coverage_token').fetchone()[0]


def _bounded(txns, n):
    """The universal bound on a drain's store transactions: none changes more than MARK_BATCH episode_scope rows;
    positive control: at least `n` episode_scope row changes were seen (the n episodes marked)."""
    bound, source = seams.mark_batch()
    problems = []
    over = [txn['scope_rows'] for txn in txns if txn['scope_rows'] > bound]
    if over:
        problems.append(f'store transactions changed {over} episode_scope rows, MARK_BATCH {bound} ({source})')
    seen = sum(txn['scope_rows'] for txn in txns)
    if seen < n:
        problems.append(f'positive control: {seen} episode_scope row changes seen for {n} episodes marked')
    return problems


def _marks_and_transactions(world, monkeypatch, batch):
    """One drain, both instruments: the `mark_coverage` calls (class `Marks`) and the store transactions (class
    `Transactions`)."""
    recorder = Transactions()
    restore = recorder.install(monkeypatch)
    try:
        marks = Marks(world).run(monkeypatch, batch=batch)
    finally:
        restore()
    return marks, recorder.done


def _above_mark_batch():
    """N above MARK_BATCH where the bound matters (2 * MARK_BATCH + 50: 250 at the ruled 100), and the drain's
    batch, kept above N so a bound that followed `batch` would show as one call carrying all N."""
    bound, _ = seams.mark_batch()
    n = 2 * bound + 50
    return n, max(seams.DEFAULT_BATCH, n + 1)


def test_b3_new_token_marks_in_bounded_calls_and_only_the_first_resets(tmp_path, monkeypatch):
    """GREEN-IF a reindex whose token is new (the old index file is gone), over N = 2 * MARK_BATCH + 50 episodes (250
    at the ruled 100) with the drain's batch above N, marks coverage after the swap in store transactions of at most
    MARK_BATCH episode_scope rows (the universal ruling), the first `mark_coverage` call alone resets, together the
    calls carry every built episode, and afterwards every episode is covered under the new token."""
    n, batch = _above_mark_batch()
    world = _world(tmp_path / 'world')
    episodes = _seal(world, n, 'new')
    seams.drain(world.store())
    for name in (seams.INDEX_NAME, seams.INDEX_NAME + '-journal'):
        Path(world.state / name).unlink(missing_ok=True)
    seams.request_reindex(world.store())
    marks, txns = _marks_and_transactions(world, monkeypatch, batch)
    assert not marks.preconditions(), 'precondition: ' + '; '.join(marks.preconditions())
    problems = _bounded(txns, len(episodes))
    resets = [i for i, call in enumerate(marks.calls) if call['reset']]
    if resets != [0]:
        problems.append(f'calls that reset: {resets} (the first call alone expected)')
    carried = set().union(*(call['episodes'] for call in marks.calls))
    if carried != set(episodes):
        problems.append(f'the calls carried {len(carried)} episodes, {len(episodes)} built')
    if set(_covered(world).values()) != {1} or _coverage_state(world)[2] != (_index_token(world),):
        problems.append('after the marks not every episode is covered under the new token')
    assert not problems, 'post-swap coverage marks, new token:\n' + '\n'.join(problems)


def test_b3_carried_token_marks_only_the_uncovered_in_bounded_calls_and_never_resets(tmp_path, monkeypatch):
    """GREEN-IF a reindex whose token is carried, on a store holding K=4 covered episodes and M = 2 * MARK_BATCH + 50
    uncovered ones (250 at the ruled 100; sealed after the request, so still `covered = 0` when the build runs), with
    the drain's batch above M, marks after the swap in store transactions of at most MARK_BATCH episode_scope rows
    (the universal ruling), no `mark_coverage` call resetting, the calls carrying exactly the M uncovered episodes and
    none of the covered; afterwards all K + M are covered under the same token."""
    m, batch = _above_mark_batch()
    world = _world(tmp_path / 'world')
    covered = _seal(world, 4, 'covered')
    seams.drain(world.store())
    token = _index_token(world)
    seams.request_reindex(world.store())
    uncovered = _seal(world, m, 'uncovered')
    states = _covered(world)
    assert {states[e] for e in covered} == {1} and {states[e] for e in uncovered} == {0}, (
        f'precondition: the seeded store has 4 covered and {m} uncovered episodes')
    marks, txns = _marks_and_transactions(world, monkeypatch, batch)
    assert not marks.preconditions(), 'precondition: ' + '; '.join(marks.preconditions())
    assert _index_token(world) == token, 'precondition: the token was carried'
    problems = _bounded(txns, len(uncovered))
    resets = [i for i, call in enumerate(marks.calls) if call['reset']]
    if resets:
        problems.append(f'calls {resets} reset the coverage of a carried token')
    carried = set().union(*(call['episodes'] for call in marks.calls))
    if carried & set(covered):
        problems.append(f'{len(carried & set(covered))} already covered episodes were passed again')
    if carried - set(covered) != set(uncovered):
        problems.append(f'the calls carried {len(carried & set(uncovered))} of the {len(uncovered)} uncovered episodes')
    if set(_covered(world).values()) != {1}:
        problems.append('after the marks not every episode is covered')
    assert not problems, 'post-swap coverage marks, carried token:\n' + '\n'.join(problems)


def test_b3_edge_an_empty_store_with_a_new_token_makes_one_empty_call_that_resets(tmp_path, monkeypatch):
    """Edge (pinned): a reindex of a store with no episode, its token new, makes exactly one `mark_coverage` call,
    with an empty dict and reset, so the store's coverage token becomes the new index's (an empty store must still
    reset). GREEN-IF so."""
    world = _world(tmp_path / 'world')
    seams.request_reindex(world.store())
    marks = Marks(world).run(monkeypatch, batch=3)
    assert not marks.preconditions(), 'precondition: ' + '; '.join(marks.preconditions())
    shapes = [(len(call['episodes']), call['reset']) for call in marks.calls]
    assert shapes == [(0, True)], f'calls (episodes, reset): {shapes}; one empty call that resets expected'
    assert _coverage_state(world)[2] == (_index_token(world),), 'the empty store did not take the new token'


def test_b3_edge_a_carried_token_with_nothing_uncovered_makes_one_empty_noop_call(tmp_path, monkeypatch):
    """Edge (pinned): a reindex whose token is carried, on a store whose every episode is covered, makes exactly one
    `mark_coverage` call, with an empty dict and no reset: a no-op `BEGIN IMMEDIATE` (the store's episode coverage,
    aggregates and token are unchanged by the drain). GREEN-IF so."""
    world = _world(tmp_path / 'world')
    _seal(world, 5, 'all-covered')
    seams.drain(world.store())
    seams.request_reindex(world.store())
    before = _coverage_state(world)
    marks = Marks(world).run(monkeypatch, batch=2)
    assert not marks.preconditions(), 'precondition: ' + '; '.join(marks.preconditions())
    shapes = [(len(call['episodes']), call['reset']) for call in marks.calls]
    assert shapes == [(0, False)], f'calls (episodes, reset): {shapes}; one empty call without reset expected'
    assert _coverage_state(world) == before, 'the carried-token marking changed the store\'s coverage state'


def test_b3_the_indexer_is_the_only_mark_coverage_caller_in_the_flows(tmp_path, monkeypatch):
    """The soundness premise, measured over the flows (see the module docstring): every `mark_coverage` call made by
    the sealing flows (library seals, workspace capture `once`, a session import `advance`, a registry launch's Codex
    hook), `index-history`, `upgrade-sources` and the drains after them has the indexer (`drain` or `reindex` in
    episodic_search, both under the index lease) on its stack. GREEN-IF so, with at least one call observed."""
    import t12b_flows as flows
    from kp_agent_tooling._impl.service.session_sources import SessionSources
    real = SessionSources.mark_coverage
    calls = []

    def spy(self, digests, token, *, reset=False):
        calls.append(_indexer_on_stack(sys._getframe(1)))
        return real(self, digests, token, reset=reset)
    outside = {}
    for name in flows.SETUPS:
        flow = flows.SETUPS[name](tmp_path / name.replace(' ', '-'))
        start = len(calls)
        monkeypatch.setattr(SessionSources, 'mark_coverage', spy)
        try:
            flow.run()
            seams.drain(flow.store)
        finally:
            monkeypatch.setattr(SessionSources, 'mark_coverage', real)
        outside[name] = sum(1 for on in calls[start:] if not on)
    assert calls, 'positive control: no mark_coverage call was observed'
    assert not any(outside.values()), f'mark_coverage calls outside the indexer, per flow: {outside}'


# ------------------------------------------------------------------------------- the ruled value of MARK_BATCH

def test_b3_mark_batch_is_the_product_constant_at_the_ruled_value():
    """Meet ruling (Verification, 2026-10-05): "MARK_BATCH is the measured value; changing it re-opens the
    measurement." GREEN-IF `seams.mark_batch()` reads the product's own `episodic_search.MARK_BATCH` (source
    'product', not the seam's fallback) and it equals `seams.RULED_MARK_BATCH` (100)."""
    bound, source = seams.mark_batch()
    assert (bound, source) == (seams.RULED_MARK_BATCH, 'product'), (
        f'MARK_BATCH reads {bound} from {source}; the ruled, measured value is {seams.RULED_MARK_BATCH} from the '
        'product (changing it re-opens the measurement)')


# ------------------------------------------------------------- rows changed per store transaction (the reset)

_SCOPE_WRITE = re.compile(r'^\s*(UPDATE\s+(OR\s+\w+\s+)?(main\.)?episode_scope\b'
                          r'|(INSERT|REPLACE)(\s+OR\s+\w+)?\s+INTO\s+(main\.)?episode_scope\b'
                          r'|DELETE\s+FROM\s+(main\.)?episode_scope\b)', re.IGNORECASE)
_TOKEN_WRITE = re.compile(r'^\s*(UPDATE\s+(OR\s+\w+\s+)?(main\.)?scope_state\b'
                          r'|(INSERT|REPLACE)(\s+OR\s+\w+)?\s+INTO\s+(main\.)?scope_state\b'
                          r'|DELETE\s+FROM\s+(main\.)?scope_state\b)', re.IGNORECASE)


class Transactions:
    """Rows changed per store write transaction. Every store connection the product opens through
    `EpisodeStore._connect` is handed back wrapped: each `execute`/`executemany`/`executescript` reads the
    connection's `total_changes` before and after the statement runs, so a statement's delta is the rows it changed
    (exact for `episode_scope`, which carries no trigger: asserted by the test). A transaction ends when the
    connection is no longer `in_transaction` after a statement, or at `commit`/`rollback`/`with`-exit/`close`; a
    statement run outside a transaction is its own. Each transaction records the `episode_scope` rows its statements
    changed and whether it wrote `scope_state`'s `coverage_token`."""

    def __init__(self):
        self.done = []

    def install(self, monkeypatch):
        from kp_agent_tooling._impl.service.episodic_memory import EpisodeStore
        real = EpisodeStore._connect
        recorder = self

        def connect(store, *args, **kwargs):
            return _Counted(real(store, *args, **kwargs), recorder)
        monkeypatch.setattr(EpisodeStore, '_connect', connect)
        return lambda: monkeypatch.setattr(EpisodeStore, '_connect', real)


class _Counted:
    def __init__(self, db, recorder):
        self._db, self._recorder, self._open = db, recorder, None

    def _txn(self):
        if self._open is None:
            self._open = {'scope_rows': 0, 'token': False, 'statements': []}
        return self._open

    def _end(self, rolled_back=False):
        if self._open is not None and not rolled_back and self._open['statements']:
            self._recorder.done.append(self._open)
        self._open = None

    def _run(self, method, sql, *args):
        before = self._db.total_changes
        result = getattr(self._db, method)(sql, *args)
        changed = self._db.total_changes - before
        text = sql if isinstance(sql, str) else ''
        txn = self._txn()
        scope = bool(_SCOPE_WRITE.search(text)) or (method == 'executescript' and 'episode_scope' in text)
        if changed and scope:
            txn['scope_rows'] += changed
        if _TOKEN_WRITE.search(text) and ('coverage_token' in text or 'coverage_token' in repr(args)):
            txn['token'] = True
        if changed or scope or txn['token']:
            txn['statements'].append((' '.join(text.split())[:80], changed))
        if not self._db.in_transaction:
            self._end()
        return result

    def execute(self, sql, *args):
        return self._run('execute', sql, *args)

    def executemany(self, sql, *args):
        return self._run('executemany', sql, *args)

    def executescript(self, sql):
        return self._run('executescript', sql)

    def commit(self):
        self._db.commit()
        self._end()

    def rollback(self):
        self._db.rollback()
        self._end(rolled_back=True)

    def close(self):
        if self._db.in_transaction:
            self._open = None  # closing with a transaction open rolls it back
        self._end()
        self._db.close()

    def __enter__(self):
        self._db.__enter__()
        return self

    def __exit__(self, kind, value, traceback):
        result = self._db.__exit__(kind, value, traceback)
        self._end(rolled_back=kind is not None)
        return result

    def __getattr__(self, name):
        return getattr(self._db, name)


def test_b3_a_new_token_reindex_changes_at_most_mark_batch_scope_rows_per_store_transaction(tmp_path, monkeypatch):
    """Meet ruling (Verification): "during a new-token reindex, no single store transaction changes more than
    MARK_BATCH `episode_scope` rows, zeroing and marking alike, and the token-switch transaction changes none."
    Instrument: the store's own connections wrapped (class `Transactions`): rows changed per statement from the
    connection's `total_changes` around it, summed per store write transaction over the statements that write
    `episode_scope`. GREEN-IF, with N = 2 * MARK_BATCH + 50 covered episodes (250 at the ruled 100), the old index
    removed (a new token: the store's claimed token names no index), a reindex requested and drained (the drain's
    batch above N):
    - no store transaction changes more than MARK_BATCH `episode_scope` rows;
    - at least one transaction writes `scope_state`'s `coverage_token`, and every one that does changes no
      `episode_scope` row;
    - positive controls: the instrument saw at least N `episode_scope` row changes in all (every covered row is
      zeroed or re-marked), one swap happened, and afterwards every episode is covered under the index's new token."""
    n, batch = _above_mark_batch()
    bound, _ = seams.mark_batch()
    world = _world(tmp_path / 'world')
    episodes = _seal(world, n, 'reset')
    seams.drain(world.store())
    assert set(_covered(world).values()) == {1}, 'precondition: every episode is covered under the old token'
    old_token = _index_token(world)
    with seams.ro(world.store_path) as db:
        triggers = db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'episode_scope'"
                              ).fetchall()
    assert not triggers, f'precondition: no trigger on episode_scope (a delta would count its changes): {triggers}'
    for name in (seams.INDEX_NAME, seams.INDEX_NAME + '-journal', seams.INDEX_NAME + '-wal', seams.INDEX_NAME + '-shm'):
        Path(world.state / name).unlink(missing_ok=True)
    seams.request_reindex(world.store())
    recorder = Transactions()
    restore = recorder.install(monkeypatch)
    try:
        seams.drain(world.store(), batch=batch)
    finally:
        restore()
    txns = recorder.done
    seen = sum(txn['scope_rows'] for txn in txns)
    problems = []
    if seen < n:
        problems.append(f'positive control: the instrument saw {seen} episode_scope row changes for {n} covered rows')
    over = [(txn['scope_rows'], txn['statements'][:4]) for txn in txns if txn['scope_rows'] > bound]
    if over:
        problems.append(f'{len(over)} store transactions changed more than MARK_BATCH {bound} episode_scope rows: '
                        f'{over}')
    switches = [txn for txn in txns if txn['token']]
    if not switches:
        problems.append('no store transaction wrote scope_state coverage_token (no token switch observed)')
    mixed = [(txn['scope_rows'], txn['statements'][:4]) for txn in switches if txn['scope_rows']]
    if mixed:
        problems.append(f'token-switch transactions changed episode_scope rows: {mixed}')
    new_token = _index_token(world)
    if new_token == old_token:
        problems.append('positive control: no new-token swap happened')
    if set(_covered(world).values()) != {1} or _coverage_state(world)[2] != (new_token,):
        problems.append('after the reindex not every episode is covered under the new token')
    assert not problems, 'rows changed per store transaction in a new-token reindex:\n' + '\n'.join(problems)


def _transaction_problems(txns, bound, at_least):
    """The ruling's bound over recorded store transactions: none changes more than `bound` episode_scope rows; at
    least one writes coverage_token and every one that does changes no episode_scope row; positive control: at least
    `at_least` episode_scope row changes were seen in all."""
    problems = []
    seen = sum(txn['scope_rows'] for txn in txns)
    if seen < at_least:
        problems.append(f'positive control: the instrument saw {seen} episode_scope row changes, at least {at_least} '
                        'expected')
    over = [(txn['scope_rows'], txn['statements'][:4]) for txn in txns if txn['scope_rows'] > bound]
    if over:
        problems.append(f'{len(over)} store transactions changed more than MARK_BATCH {bound} episode_scope rows: '
                        f'{over}')
    switches = [txn for txn in txns if txn['token']]
    if not switches:
        problems.append('no store transaction wrote scope_state coverage_token (no token switch observed)')
    mixed = [(txn['scope_rows'], txn['statements'][:4]) for txn in switches if txn['scope_rows']]
    if mixed:
        problems.append(f'token-switch transactions changed episode_scope rows: {mixed}')
    return problems


def _store_token(world):
    with seams.ro(world.store_path) as db:
        row = db.execute("SELECT value FROM scope_state WHERE key = 'coverage_token'").fetchone()
    return row[0] if row else None


def _held(world):
    """{episode_id: payload_sha256} the store's index file holds."""
    with seams.ro(world.state / seams.INDEX_NAME) as db:
        return dict(db.execute('SELECT episode_id, payload_sha256 FROM indexed_episodes'))


def _covered_world(tmp_path, tag):
    """A store with N = 2 * MARK_BATCH + 50 episodes (250 at the ruled 100), all covered under a matching index."""
    n, _ = _above_mark_batch()
    world = _world(tmp_path / 'world')
    episodes = _seal(world, n, tag)
    seams.drain(world.store())
    assert set(_covered(world).values()) == {1} and _store_token(world) == _index_token(world), (
        'precondition: every episode is covered under the index\'s token')
    with seams.ro(world.store_path) as db:
        triggers = db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'episode_scope'"
                              ).fetchall()
    assert not triggers, f'precondition: no trigger on episode_scope (a delta would count its changes): {triggers}'
    return world, episodes


def _recorded(monkeypatch, action):
    recorder = Transactions()
    restore = recorder.install(monkeypatch)
    try:
        action()
    finally:
        restore()
    return recorder.done


def test_b3_a_drain_against_a_recreated_index_resets_coverage_in_bounded_store_transactions(tmp_path, monkeypatch):
    """Meet ruling (Verification): no store transaction changes more than MARK_BATCH `episode_scope` rows (universal:
    resets and marks alike). The path: `drain` against a recreated index,
    whose `mark_coverage` finds the store naming another token (the mismatch branch). Instrument as above (class
    `Transactions`). GREEN-IF, with N = 2 * MARK_BATCH + 50 episodes covered under a matching index, the index file
    deleted, M = MARK_BATCH + 50 more episodes sealed (150 at the ruled 100: more than one MARK_BATCH, so the new
    index's marks must be chunked too), then a drain:
    - no store transaction changes more than MARK_BATCH `episode_scope` rows;
    - at least one transaction writes `coverage_token`, and every one that does changes no `episode_scope` row;
    - afterwards the store's `coverage_token` equals the new index's token, no episode is recorded covered that the
      new index does not hold with its sealed digest (coverage never over-reported), and every one of the M episodes
      sealed after the index was recreated is recorded covered;
    - positive control: the instrument saw at least N `episode_scope` row changes (the N old marks cleared)."""
    n, _ = _above_mark_batch()
    bound, _ = seams.mark_batch()
    world, episodes = _covered_world(tmp_path, 'recreated')
    old_token = _index_token(world)
    for name in (seams.INDEX_NAME, seams.INDEX_NAME + '-journal', seams.INDEX_NAME + '-wal', seams.INDEX_NAME + '-shm'):
        Path(world.state / name).unlink(missing_ok=True)
    extra = _seal(world, bound + 50, 'recreated-extra')
    txns = _recorded(monkeypatch, lambda: seams.drain(world.store()))
    problems = _transaction_problems(txns, bound, n)
    new_token = _index_token(world)
    if new_token == old_token:
        problems.append('positive control: the recreated index carries the old token')
    if _store_token(world) != new_token:
        problems.append(f'afterwards the store names token {_store_token(world)!r}, the new index {new_token!r}')
    held = _held(world)
    with seams.ro(world.store_path) as db:
        covered = dict(db.execute('SELECT episode_id, payload_sha256 FROM episode_scope WHERE covered = 1'))
    over = [identity for identity, digest in covered.items() if held.get(identity) != digest]
    if over:
        problems.append(f'{len(over)} episodes recorded covered that the new index does not hold')
    missing = [identity for identity in extra if identity not in covered]
    if missing:
        problems.append(f'{len(missing)} of {len(extra)} episodes sealed after the index was recreated are not '
                        'recorded covered')
    assert not problems, 'a drain against a recreated index:\n' + '\n'.join(problems)


def _upgrade_sources(config):
    """`upgrade-sources` as an operator runs it: the CLI default `--batch-rows` (which governs only the projection
    backfill, meet ruling)."""
    import contextlib
    import io
    import json
    from kp_agent_tooling import desk_cli
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = desk_cli.main(['--config', str(config), 'upgrade-sources'])
    assert code == 0, f'upgrade-sources exit {code}: {out.getvalue()[:400]}'
    return json.loads(out.getvalue())


def test_b3_upgrade_sources_on_a_token_mismatch_resets_coverage_in_bounded_store_transactions(tmp_path, monkeypatch):
    """Meet ruling (Verification): no store transaction changes more than MARK_BATCH `episode_scope` rows (universal:
    resets and marks alike). The path: `upgrade-sources`
    (`_establish_coverage`) on a store whose `scope_state.coverage_token` names another index than the one beside it.
    Instrument as above (class `Transactions`), over every store transaction of the run, at the CLI default
    `--batch-rows` (the invariant is universal, meet ruling: no store transaction changes more than MARK_BATCH
    `episode_scope` rows, the operator's re-mark included). GREEN-IF, with N = 2 * MARK_BATCH + 50 episodes covered under a matching index and the store's token then
    replaced by another value (the index kept):
    - no store transaction changes more than MARK_BATCH `episode_scope` rows;
    - at least one transaction writes `coverage_token`, and every one that does changes no `episode_scope` row;
    - the end state the code defines (read at the bounded-reset fix, `_establish_coverage`): coverage recorded for another token is
      cleared, the store then names the index's token, and every episode is re-marked by digest equality with the
      index file, so afterwards the store's `coverage_token` equals the index's and every one of the N episodes (all
      held with their sealed digest) is recorded covered;
    - positive control: at least N `episode_scope` row changes were seen (the N stale marks cleared)."""
    n, _ = _above_mark_batch()
    bound, _ = seams.mark_batch()
    world, episodes = _covered_world(tmp_path, 'mismatch')
    token = _index_token(world)
    with closing(sqlite3.connect(world.store_path)) as db, db:
        db.execute("UPDATE scope_state SET value = ? WHERE key = 'coverage_token'", ('0' * 32,))
    assert _store_token(world) != token, 'precondition: the store names another token than its index'
    txns = _recorded(monkeypatch, lambda: _upgrade_sources(world.configs[corpus.A]))
    problems = _transaction_problems(txns, bound, n)
    if _store_token(world) != token or _index_token(world) != token:
        problems.append(f'afterwards the store names {_store_token(world)!r}, the index {_index_token(world)!r} '
                        f'({token!r} expected for both)')
    held = _held(world)
    with seams.ro(world.store_path) as db:
        scope = dict(db.execute('SELECT episode_id, covered FROM episode_scope'))
    not_covered = [e for e in episodes if scope.get(e) != 1]
    if not_covered or any(e not in held for e in episodes):
        problems.append(f'{len(not_covered)} of {n} episodes the index holds are not recorded covered afterwards')
    assert not problems, 'upgrade-sources on a token mismatch:\n' + '\n'.join(problems)


def test_b3_a_drain_catch_up_marks_coverage_in_bounded_store_transactions(tmp_path, monkeypatch):
    """Meet ruling (Verification): no store transaction changes more than MARK_BATCH `episode_scope` rows (universal).
    The path: the drain's catch-up, `mark_coverage` after each applied batch of DRAIN_BATCH outbox rows. GREEN-IF,
    with 2 * DRAIN_BATCH + 50 episodes sealed (1050 at DRAIN_BATCH 500) and their outbox rows waiting under a matching
    index, a drain at DRAIN_BATCH:
    - no store transaction changes more than MARK_BATCH `episode_scope` rows;
    - afterwards every sealed episode is recorded covered;
    - positive control: the instrument saw at least as many `episode_scope` row changes as episodes sealed."""
    bound, _ = seams.mark_batch()
    batch = seams.DEFAULT_BATCH
    n = 2 * batch + 50
    world = _world(tmp_path / 'world')
    _seal(world, 1, 'catch-up-first')
    seams.drain(world.store())  # a matching index, its token named by the store
    episodes = _seal(world, n, 'catch-up')
    assert seams.outbox_count(world.store_path) >= n, 'precondition: the seals\' outbox rows are waiting'
    txns = _recorded(monkeypatch, lambda: seams.drain(world.store(), batch=batch))
    seen = sum(txn['scope_rows'] for txn in txns)
    problems = []
    if seen < n:
        problems.append(f'positive control: the instrument saw {seen} episode_scope row changes for {n} seals')
    over = [(txn['scope_rows'], txn['statements'][:3]) for txn in txns if txn['scope_rows'] > bound]
    if over:
        problems.append(f'{len(over)} store transactions changed more than MARK_BATCH {bound} episode_scope rows: '
                        f'{[rows for rows, _ in over]} (first: {over[0][1]})')
    states = _covered(world)
    uncovered = [e for e in episodes if states.get(e) != 1]
    if uncovered:
        problems.append(f'{len(uncovered)} of {n} sealed episodes are not recorded covered after the catch-up')
    assert not problems, 'a drain catch-up:\n' + '\n'.join(problems)
