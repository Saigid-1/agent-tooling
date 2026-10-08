"""T12b B1: the outbox, as schema (docs/work/orders/T12b-one-indexer-outbox.md, B1, frozen r5).

Falsifiers (the order's words) and the test for each:
- "a path inserting a watched row with no outbox row in the same commit":
  test_b1_each_of_the_eight_inserts_appends_one_outbox_row_in_its_own_commit;
- "an outbox row whose insert rolled back": test_b1_a_rolled_back_insert_leaves_no_outbox_row;
- "a claim or link producing more than one row": test_b1_a_claim_or_a_link_appends_exactly_one_row;
- "for each seal path, a fresh store opened through that path alone lacking the outbox or its triggers":
  test_b1_a_fresh_store_opened_through_one_seal_path_alone_has_the_outbox_and_its_triggers (and, for "a
  watched table created afterwards gets its trigger in the same schema step that creates it",
  test_b1_a_watched_table_created_later_gets_its_trigger_in_the_same_step);
- "existing stores (r5): a store created at BASE (a T10 corpus store, and one shaped like the live
  T10h-upgraded store), sealed through each of the eight statements by the T12b code, not having exactly one
  outbox row per insert": test_b1_a_store_created_at_base_gets_one_row_per_insert (fixtures generated at base:
  tests/fixtures/t12b/generate_base_stores.py);
- "capture, claim or link opening the index (t10_instruments.connected(index_path))":
  test_b1_capture_claim_and_link_never_open_the_index (capture flows that index inline at base).

The eight statements (census C8): episodic_memory.py:167 (episodes, capture), :176 (session_episodes, capture's
link), :236 (episodes, operator import); session_sources.py:434 (session_claims, capture's admission claim),
:563 (session_claims, claim), :623 (session_episodes, link), :655 (source_episodes, import), :658
(session_episodes, import's link). They are reached through five public operations (OPERATIONS).

Instrument for "in the same commit" (tests/t12b_seams.py `same_commit_observer`): at every statement start and
every connect, on every connection, a separate read-only connection counts the COMMITTED watched rows and
outbox rows. Committed state changes only at a commit, and every commit is followed by a statement start, a
connect or the end of the call, so a watched row committed in a transaction other than its outbox row's is
observed as a difference. Readings (repeated in the report under AMBIGUITY): a seal row's `session_id` is not
asserted (the order gives only `episode_id`); `detail` is asserted NULL or JSON only (`_reflect` may set it in
the same transaction, for T12c).
"""
from __future__ import annotations

import gzip
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import t10_corpus as corpus
import t10_tamper as tamper
import t10_world as w
import t12b_seams as seams
from t12b_seams import OUTBOX, WATCHED, committed_counts, outbox_rows, same_commit_observer, traced

A = corpus.A
ALPHA, BETA = corpus.ALPHA, corpus.BETA
ANY = object()
FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 't12b'
BASE_STORES = ('t10_corpus', 't10h_upgraded')


# ------------------------------------------------------------------------------------------------ worlds

def _world(root: Path, *, initialize=True) -> w.World:
    world = w.World(root, corpus.DESKS, corpus.SESSIONS)
    if initialize:
        world.initialize()
    return world


def base_world(root: Path, name: str) -> tuple[w.World, dict]:
    """A world whose state root holds a store created at base (the committed fixture), unchanged."""
    world = _world(root, initialize=False)
    manifest = json.loads((FIXTURES / 'manifest.json').read_text())
    for file in ('episodes.sqlite3', 'sessions.sqlite3', 'episode-search.sqlite3'):
        data = gzip.decompress((FIXTURES / name / (file + '.gz')).read_bytes())
        import hashlib
        assert hashlib.sha256(data).hexdigest() == manifest['files'][name][file], f'fixture {name}/{file} changed'
        (world.state / file).write_bytes(data)
        (world.state / file).chmod(0o600)
    ids = json.loads((FIXTURES / name / 'ids.json').read_text())
    return world, ids


def _linked_session(world, episode_id):
    with seams.ro(world.store_path) as db:
        row = db.execute('SELECT session_id FROM session_episodes WHERE episode_id=?', (episode_id,)).fetchone()
    return row[0] if row else None


# -------------------------------------------------------------------------------------------- operations
# Each operation performs one or more of the eight statements and returns the outbox rows it must append,
# in order: (reason, episode_id, session_id), ANY where the order names no value.

def op_capture(world, ids, tag):
    """episodic_memory.py:167 + session_sources.py:434 + episodic_memory.py:176 (a session's first capture)."""
    session = ids.get('capture_session', A)
    episode = world.store().capture(session, source_ref=f't12b:{tag}:capture',
                                    events=w.events(f'Juniper outbox capture {tag}.'))['episode_id']
    ids['captured'] = episode
    source = _linked_session(world, episode)
    assert source, 'precondition: the capture was linked to its source session (catalog present)'
    return [('seal', episode, ANY), ('desks_changed', None, source), ('desks_changed', episode, source)]


def op_legacy(world, ids, tag):
    """episodic_memory.py:236 (the operator import)."""
    episode = world.store().import_operator_episode(
        tenant_id=w.TENANT_ONE, role='alpha', repo_key=w.REPO, source_ref=f't12b:{tag}:legacy',
        events=w.imported_events(f'Juniper legacy {tag}.'),
        source_provenance=w.legacy_provenance(f't12b-{tag}-legacy'))['episode_id']
    ids['legacy'] = episode
    return [('seal', episode, ANY)]


def op_import(world, ids, tag):
    """session_sources.py:655 + :658 (import_episode and its link). The session is registered first (not watched)."""
    sources = world.sources()
    if 'session' not in ids:
        ids['session'] = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id=f't12b-{tag}')
    episode = sources.import_episode(session_id=ids['session'], source_ref=f't12b:{tag}:import',
                                     events=w.events(f'Juniper imported {tag}.'),
                                     provenance=w.provenance(f't12b-{tag}-import'))['episode_id']
    ids['imported'] = episode
    return [('seal', episode, ANY), ('desks_changed', episode, ids['session'])]


def op_claim(world, ids, tag):
    """session_sources.py:563."""
    world.sources().claim(**w.claim_args(ids['session'], ALPHA, recorded_at='2026-09-01T00:00:00Z'))
    return [('desks_changed', None, ids['session'])]


def op_link(world, ids, tag):
    """session_sources.py:623 (the legacy episode linked to the imported session)."""
    world.sources().link(episode_id=ids['legacy'], session_id=ids['session'])
    return [('desks_changed', ids['legacy'], ids['session'])]


OPERATIONS = (('capture', op_capture), ('legacy', op_legacy), ('import', op_import), ('claim', op_claim),
              ('link', op_link))
STATEMENTS = {'capture': ('episodic_memory.py:167', 'session_sources.py:434', 'episodic_memory.py:176'),
              'legacy': ('episodic_memory.py:236',), 'import': ('session_sources.py:655', 'session_sources.py:658'),
              'claim': ('session_sources.py:563',), 'link': ('session_sources.py:623',)}


def _shape(row):
    return (row['reason'], row['episode_id'], row['session_id'])


def _matches(expected, row):
    return all(e is ANY or e == r for e, r in zip(expected, _shape(row)))


def _detail_ok(row):
    if row['detail'] is None:
        return True
    try:
        json.loads(row['detail'])
        return True
    except (TypeError, ValueError):
        return False


def run_operations(world, ids, tag, operations=OPERATIONS):
    """Run each operation under the same-commit observer; return [(name, expected, appended rows)] and the trace."""
    results = []
    problems = []
    for name, operation in operations:
        before = committed_counts(world.store_path)
        seen = {row['seq'] for row in outbox_rows(world.store_path)}
        with traced(same_commit_observer(world.store_path, before)) as trace:
            expected = operation(world, ids, tag)
        appended = [row for row in outbox_rows(world.store_path) if row['seq'] not in seen]
        after = committed_counts(world.store_path)
        inserted = sum(after[t] - before[t] for t in WATCHED)
        if trace.violations:
            problems.append(f'{name} ({", ".join(STATEMENTS[name])}): ' + '; '.join(trace.violations[:3]))
        if inserted != len(expected):
            problems.append(f'{name}: precondition: {inserted} watched rows inserted, the operation names {len(expected)}')
        if len(appended) != len(expected) or not all(_matches(e, r) for e, r in zip(expected, appended)):
            problems.append(f'{name} ({", ".join(STATEMENTS[name])}): appended {[_shape(r) for r in appended]}, '
                            f'expected {[tuple("*" if v is ANY else v for v in e) for e in expected]}')
        bad = [r for r in appended if not _detail_ok(r) or r['reason'] not in seams.REASONS]
        if bad:
            problems.append(f'{name}: outbox rows with an unknown reason or non-JSON detail: {bad}')
        if trace.observations < 2:
            problems.append(f'{name}: instrument: the observer ran {trace.observations} times')
        results.append((name, expected, appended))
    return results, problems


def _outbox_schema_problems(path) -> list[str]:
    with seams.ro(path) as db:
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (OUTBOX,)).fetchone()
        if row is None:
            return [f'the store has no {OUTBOX} table']
        columns = [r[1] for r in db.execute(f'PRAGMA table_info({OUTBOX})')]
        problems = []
        if columns != list(seams.OUTBOX_COLUMNS):
            problems.append(f'{OUTBOX} columns {columns}, the order names {list(seams.OUTBOX_COLUMNS)}')
        if 'AUTOINCREMENT' not in (row[0] or '').upper():
            problems.append(f'{OUTBOX}.seq is not AUTOINCREMENT: {row[0]}')
        return problems


_DUMMY = {
    'episodes': ('INSERT INTO episodes (id, binding, session, source_ref, payload) VALUES (?,?,?,?,?)',
                 ('t12b-probe-episode', 'binding:probe', 'probe', 'probe:ref', b'{}')),
    'source_episodes': ('INSERT INTO source_episodes (id, tenant, session_id, source_ref, payload) VALUES (?,?,?,?,?)',
                        ('t12b-probe-source', 'tenant-probe', 'session-probe', 'probe:ref', b'{}')),
    'session_claims': ('INSERT INTO session_claims (id, session_id, payload) VALUES (?,?,?)',
                       ('t12b-probe-claim', 'session-probe', b'{}')),
    'session_episodes': ('INSERT INTO session_episodes (episode_id, session_id) VALUES (?,?)',
                         ('t12b-probe-link', 'session-probe')),
}


def trigger_problems(path) -> list[str]:
    """Each watched table that exists, written by plain SQL (an older writer), appends exactly one outbox row in
    the same transaction. Probed in a transaction that is rolled back, so the store is unchanged."""
    problems = _outbox_schema_problems(path)
    if problems and f'the store has no {OUTBOX} table' in problems:
        return problems
    with closing(seams._REAL_CONNECT(str(path), isolation_level=None)) as db:
        present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in WATCHED:
            if table not in present:
                continue
            db.execute('BEGIN IMMEDIATE')
            try:
                before = db.execute(f'SELECT count(*) FROM {OUTBOX}').fetchone()[0]
                db.execute(*_DUMMY[table])
                after = db.execute(f'SELECT count(*) FROM {OUTBOX}').fetchone()[0]
                if after - before != 1:
                    problems.append(f'a plain INSERT into {table} appended {after - before} outbox rows, not 1')
            finally:
                db.execute('ROLLBACK')
    return problems


# --------------------------------------------------------------------------------------------- the tests

def test_b1_each_of_the_eight_inserts_appends_one_outbox_row_in_its_own_commit(tmp_path):
    """GREEN-IF each of the eight inserting statements, reached through its public operation on a fresh store,
    appends exactly one `index_outbox` row per inserted row, with the order's reason, episode_id and session_id,
    and the committed store never holds a watched row without its outbox row (or the reverse) at any statement."""
    world = _world(tmp_path / 'world')
    results, problems = run_operations(world, {}, 'fresh')
    assert sum(len(expected) for _, expected, _ in results) == 8, 'precondition: the operations cover eight inserts'
    assert not problems, 'outbox rows do not follow the eight inserts:\n' + '\n'.join(problems)


def test_b1_a_rolled_back_insert_leaves_no_outbox_row(tmp_path):
    """GREEN-IF, for each operation, a transaction that inserts its watched rows and then rolls back (a probe trigger
    aborts the projection update that follows the inserts) leaves neither the rows nor any outbox row committed,
    and the same operation without the probe appends its outbox rows (so the outbox is live)."""
    world = _world(tmp_path / 'world')
    ids = {}
    probe = ("CREATE TRIGGER t12b_rollback_probe BEFORE UPDATE OF projected ON scope_marks"
             " BEGIN SELECT RAISE(ABORT, 't12b rollback probe'); END")
    problems = []
    for name, operation in OPERATIONS:
        with closing(seams._REAL_CONNECT(str(world.store_path))) as db, db:
            db.execute(probe)
        before = committed_counts(world.store_path)
        with traced(same_commit_observer(world.store_path, before)) as trace:
            with pytest.raises(Exception) as refused:
                operation(world, dict(ids), 'rollback')
        after = committed_counts(world.store_path)
        with closing(seams._REAL_CONNECT(str(world.store_path))) as db, db:
            db.execute('DROP TRIGGER t12b_rollback_probe')
        if 't12b rollback probe' not in str(refused.value) and 'rollback probe' not in repr(refused.value.__cause__):
            problems.append(f'{name}: precondition: the probe did not abort the transaction ({refused.value!r})')
        if after != before:
            problems.append(f'{name}: a rolled-back transaction changed the committed store: {before} -> {after}')
        problems += [f'{name}: {v}' for v in trace.violations[:3]]
        _, done = run_operations(world, ids, 'rollback', ((name, operation),))
        problems += [f'{name} (positive control, no probe): {p}' for p in done]
    assert not problems, 'rolled-back inserts and the outbox:\n' + '\n'.join(problems)


def test_b1_a_claim_or_a_link_appends_exactly_one_row(tmp_path):
    """GREEN-IF a claim on a session holding three episodes, a superseding claim that re-projects them, and a link
    each append exactly one outbox row (desks_changed: the claim with the session and no episode; the link with
    both)."""
    world = _world(tmp_path / 'world')
    sources = world.sources()
    session = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='t12b-claims')
    for index in range(3):
        sources.import_episode(session_id=session, source_ref=f't12b:claims:{index}',
                               events=w.events(f'Juniper claimed {index}.'), provenance=w.provenance(f't12b-claims-{index}'))
    legacy = world.store().import_operator_episode(
        tenant_id=w.TENANT_ONE, role='alpha', repo_key=w.REPO, source_ref='t12b:claims:legacy',
        events=w.imported_events('Juniper legacy to link.'), source_provenance=w.legacy_provenance('t12b-claims'))
    problems = []

    def appended_by(call):
        seen = {row['seq'] for row in outbox_rows(world.store_path)}
        result = call()
        return result, [row for row in outbox_rows(world.store_path) if row['seq'] not in seen]

    first, rows = appended_by(lambda: sources.claim(**w.claim_args(session, ALPHA)))
    if [_shape(r) for r in rows] != [('desks_changed', None, session)]:
        problems.append(f'a claim appended {[_shape(r) for r in rows]}')
    _, rows = appended_by(lambda: sources.claim(**w.claim_args(session, BETA, supersedes=first,
                                                                recorded_at='2026-09-02T00:00:00Z')))
    if [_shape(r) for r in rows] != [('desks_changed', None, session)]:
        problems.append(f'a superseding claim (re-projecting 3 episodes) appended {[_shape(r) for r in rows]}')
    _, rows = appended_by(lambda: sources.link(episode_id=legacy['episode_id'], session_id=session))
    if [_shape(r) for r in rows] != [('desks_changed', legacy['episode_id'], session)]:
        problems.append(f'a link appended {[_shape(r) for r in rows]}')
    assert not problems, 'claims and links must append exactly one outbox row each:\n' + '\n'.join(problems)


def _older_writer_world(root: Path, *, tables=None) -> tuple[w.World, dict]:
    """A world whose store was written by the base WRITER schema (T10's base: no projection, no outbox): public
    writes through a fresh world, then reduced to that schema (t10_world.strip_to_older_writer). With `tables`, only
    those tables are kept (an older store created before the catalog)."""
    fresh = _world(root / 'fresh')
    ids = {}
    store, sources = fresh.store(), fresh.sources()
    with w.frozen_clock():
        ids['captured_before'] = store.capture(corpus.A2, source_ref='t12b:older:capture',
                                               events=w.events('Older juniper.'))['episode_id']
        ids['legacy'] = store.import_operator_episode(
            tenant_id=w.TENANT_ONE, role='alpha', repo_key=w.REPO, source_ref='t12b:older:legacy',
            events=w.imported_events('Older legacy juniper.'), source_provenance=w.legacy_provenance('t12b-older'))[
            'episode_id']
        ids['session'] = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='t12b-older')
    schema = tamper.base_schema()
    if tables is not None:
        schema = {'tables': {k: v for k, v in schema['tables'].items() if k in tables},
                  'indexes': {k: v for k, v in schema['indexes'].items() if v['table'] in tables}}
    older = _world(root / 'older', initialize=False)
    fresh.snapshot(root / 'snapshot')
    older.restore(root / 'snapshot')
    w.strip_to_older_writer(fresh.store_path, older.store_path, schema)
    if (older.state / w.INDEX_NAME).exists():
        (older.state / w.INDEX_NAME).unlink()
    assert OUTBOX not in seams.tables(older.store_path), 'precondition: the older-writer store has no outbox'
    return older, ids


SEAL_PATHS = {
    'capture': lambda world, ids: world.store().capture(A, source_ref='t12b:path:capture',
                                                          events=w.events('Path capture.')),
    'legacy import': lambda world, ids: op_legacy(world, ids, 'path'),
    'register': lambda world, ids: world.sources().register(tenant_id=w.TENANT_ONE, runtime='claude',
                                                             native_id='t12b-path-register'),
    'claim': lambda world, ids: world.sources().claim(**w.claim_args(ids['session'], ALPHA)),
    'import': lambda world, ids: world.sources().import_episode(
        session_id=ids['session'], source_ref='t12b:path:import', events=w.events('Path import.'),
        provenance=w.provenance('t12b-path-import')),
    'link': lambda world, ids: world.sources().link(episode_id=ids['legacy'], session_id=ids['session']),
}


@pytest.mark.parametrize('path', sorted(SEAL_PATHS))
def test_b1_a_fresh_store_opened_through_one_seal_path_alone_has_the_outbox_and_its_triggers(tmp_path, path):
    """GREEN-IF a store no T12b code has opened (the base writer's schema, no outbox), opened by this one seal path
    alone, then has `index_outbox(seq AUTOINCREMENT, reason, episode_id, session_id, detail)`, and a plain INSERT
    into each watched table appends exactly one outbox row; and the path's own inserts each got their row in their
    own commit."""
    world, ids = _older_writer_world(tmp_path)
    before = committed_counts(world.store_path)
    with traced(same_commit_observer(world.store_path, before)) as trace:
        SEAL_PATHS[path](world, ids)
    problems = trigger_problems(world.store_path) + list(trace.violations[:3])
    assert not problems, f'after {path!r} alone on a store without an outbox:\n' + '\n'.join(problems)


def test_b1_a_watched_table_created_later_gets_its_trigger_in_the_same_step(tmp_path):
    """GREEN-IF a store holding only `episodes` and `capsules` (before the catalog), sealed by a capture, has the
    outbox and the `episodes` trigger; and the catalog step that creates `session_claims`, `session_episodes` and
    `source_episodes` (SessionSources.ensure_catalog, the catalog schema) leaves each of them with a working trigger."""
    world, _ = _older_writer_world(tmp_path, tables=('episodes', 'capsules'))
    assert seams.tables(world.store_path) >= {'episodes'} and 'session_claims' not in seams.tables(world.store_path)
    world.store().capture(A, source_ref='t12b:later:capture', events=w.events('Later capture.'))
    problems = [f'after a capture: {p}' for p in trigger_problems(world.store_path)]
    world.sources().ensure_catalog()
    created = seams.tables(world.store_path) & set(WATCHED)
    assert created == set(WATCHED), f'precondition: ensure_catalog created the catalog tables: {created}'
    problems += [f'after ensure_catalog: {p}' for p in trigger_problems(world.store_path)]
    assert not problems, 'a watched table created after the outbox has no working trigger:\n' + '\n'.join(problems)


@pytest.mark.parametrize('name', BASE_STORES)
def test_b1_a_store_created_at_base_gets_one_row_per_insert(tmp_path, name):
    """GREEN-IF a store created at base (fixture), first opened for writing by the T12b code through a write that
    inserts no watched row (`register`), gets an EMPTY outbox (nothing sealed at base is re-queued), and then each of
    the eight statements appends exactly one outbox row per insert, in its own commit. The capture is by
    `sess-gamma`, which never captured in either fixture, so its first capture inserts all three of :167, :434 and
    :176."""
    world, _ = base_world(tmp_path / name, name)
    assert OUTBOX not in seams.tables(world.store_path), 'precondition: the base store has no outbox'
    with seams.ro(world.store_path) as db:
        admitted = db.execute('SELECT count(*) FROM episodes WHERE session=?', (corpus.C,)).fetchone()[0]
    assert admitted == 0, 'precondition: sess-gamma never captured in the fixture'
    before = committed_counts(world.store_path)
    ids = {'capture_session': corpus.C}
    with traced(same_commit_observer(world.store_path, before)) as trace:
        ids['session'] = world.sources().register(tenant_id=w.TENANT_ONE, runtime='claude', native_id=f't12b-{name}')
    problems = list(trace.violations[:3]) + _outbox_schema_problems(world.store_path)
    if OUTBOX in seams.tables(world.store_path) and outbox_rows(world.store_path):
        problems.append(f'the first writable open re-queued {len(outbox_rows(world.store_path))} rows sealed at base')
    results, done = run_operations(world, ids, name)
    problems += done
    assert sum(len(expected) for _, expected, _ in results) == 8, 'precondition: the operations cover eight inserts'
    problems += trigger_problems(world.store_path)
    assert not problems, f'{name} (created at base, sealed by this code):\n' + '\n'.join(problems)


INDEX_FLOWS = ('library seals', 'workspace capture once', 'session import advance', 'codex launch hook')


@pytest.mark.parametrize('name', INDEX_FLOWS)
def test_b1_capture_claim_and_link_never_open_the_index(tmp_path, name):
    """GREEN-IF the flow (capture, claim, link and import through the library; and the capture flows that index
    inline at base: workspace capture `once`, a session-import job's `advance`, a registry launch's Codex Stop hook)
    connects to the existing index file zero times outside the indexer (`drain` on the stack: a host CLI's
    drain-after-seal is the indexer). The count is the order's instrument, t10_instruments
    `recording().connected(index_path)`; the indexer attribution is tests/t12b_seams.py `traced`."""
    from t10_instruments import recording
    import t12b_flows as flows
    flow = flows.SETUPS[name](tmp_path / 'flow')
    assert flow.index_path.exists(), 'precondition: the index exists before the flow'
    with recording() as recorder, traced() as trace:
        recorder.phase = 'call'
        flow.run()
    assert recorder.connected(flow.store_path, phases=('call',)) >= 1, 'positive control: the flow opened the store'
    total = recorder.connected(flow.index_path, phases=('call',))
    outside = [e for e in trace.connects(flow.index_path) if not e.drain]
    assert not outside, (f'{name}: connected to the index {total} times, {len(outside)} of them outside the indexer '
                         '(capture, claim or link opened the index)')


# ------------------------------------------------------------------------- the installed check is one read

def _writable_open(store, monkeypatch):
    """One writable `EpisodeStore._connect()`, traced: (the statements the returned connection executed before
    `_connect` returned, whether it is `in_transaction` then, how many connections `_connect` opened).

    Instrument: `sqlite3.connect` is wrapped for the call (`leaf.sqlite_connect` calls it and runs no statement of
    its own), and sqlite3's trace callback (`set_trace_callback`) is installed on each connection as it is
    returned, before any statement runs on it. (The `sqlite3.connect/handle` audit event fires before the
    connection is initialised, so the callback cannot be installed there.)"""
    real = sqlite3.connect
    opened = []

    def connect(*args, **kwargs):
        db = real(*args, **kwargs)
        statements = []
        db.set_trace_callback(statements.append)
        opened.append((db, statements))
        return db
    monkeypatch.setattr(sqlite3, 'connect', connect)
    try:
        db = store._connect()
    finally:
        monkeypatch.setattr(sqlite3, 'connect', real)
    try:
        statements = next(list(found) for conn, found in opened if conn is db)
        return [' '.join(sql.split()) for sql in statements], db.in_transaction, len(opened)
    finally:
        db.close()


def _user_version(path):
    with seams.ro(path) as db:
        return db.execute('PRAGMA user_version').fetchone()[0]


def _only_the_read(label, found):
    statements, in_transaction, connections = found
    problems = []
    if [sql.upper() for sql in statements] != ['PRAGMA USER_VERSION']:
        problems.append(f'{label}: the open executed {statements} (exactly ["PRAGMA user_version"] expected)')
    if in_transaction:
        problems.append(f'{label}: the returned connection is in a transaction')
    if connections != 1:
        problems.append(f'{label}: _connect opened {connections} connections (one expected)')
    return problems


@pytest.mark.parametrize('name', ('fresh',) + BASE_STORES)
def test_b1_once_installed_a_writable_open_executes_only_the_one_read(tmp_path, monkeypatch, name):
    """Amendment 1, B1 (ii) and `ensure_outbox`'s docstring: "the check is exactly one read statement ... Every later
    open shows only the read." GREEN-IF, on a store whose outbox is installed (`user_version` >= 1), a writable
    `EpisodeStore._connect()` executes exactly one statement before returning, `PRAGMA user_version` (no `BEGIN`, no
    write, no DDL), and the connection is not `in_transaction`:
    - `fresh`: a store created by this code (`World.initialize`), its first writable open done, then its second open;
    - each base-created store (the committed fixtures, `user_version` 0, no outbox): its first writable open installs
      the outbox (positive control: that open's trace shows `BEGIN IMMEDIATE` and the outbox's DDL, and the store then
      has the outbox at `user_version` >= 1), and its second open shows only the read."""
    if name == 'fresh':
        world = _world(tmp_path / name)
        store = world.store()
        store._connect().close()  # the first writable open (installs, if initialize did not)
    else:
        world, _ = base_world(tmp_path / name, name)
        assert _user_version(world.store_path) == 0 and OUTBOX not in seams.tables(world.store_path), (
            'precondition: the base store has no outbox, user_version 0')
        store = world.store()
        first, _, _ = _writable_open(store, monkeypatch)
        upper = [sql.upper() for sql in first]
        assert 'BEGIN IMMEDIATE' in upper and any(sql.startswith('CREATE TABLE') and OUTBOX.upper() in sql
                                                  for sql in upper), (
            f'positive control: the first open of a base store installs the outbox in a traced transaction: {first}')
    assert _user_version(world.store_path) >= 1 and OUTBOX in seams.tables(world.store_path), (
        'precondition: the outbox is installed (user_version >= 1)')
    problems = _only_the_read(f'{name}: the second writable open', _writable_open(store, monkeypatch))
    assert not problems, 'the installed outbox check:\n' + '\n'.join(problems)
