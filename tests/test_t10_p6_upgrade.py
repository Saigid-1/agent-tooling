"""T10 P6: existing stores upgrade explicitly, in bounded steps.

Order: docs/work/orders/T10-read-path-projection.md (P6). The operator step
is ``kp-agent-desk --config <cfg> upgrade-sources`` (``desk_cli``), driven
in-process through ``desk_cli.main`` (traced) and once as a subprocess of
the interpreter under test.

An *older-writer store* is a store written through public writes and then
reduced to exactly the base writer's schema (``base_schema`` recorded with
the goldens): same sealed rows, same rowids, no projection. An *older-writer
row* is one the base writer's SQL statements would insert, written next to a
new-version store.

* A fresh or new-version store never reports ``projection_incomplete``.
* An older-writer store, and a new-version store with one older-writer row in
  any of episodes, source episodes (+ link), claims or links, refuses reads
  with ``projection_incomplete`` (with guidance), never by scanning, and
  ``memory.connection_status`` reports the same state.
* ``upgrade-sources`` brings it back: reads equal the fresh store's, the
  projection equals the fresh projection, the work is committed in more than
  one transaction, and a second run changes nothing (``iterdump``).
* The served path (``kp-agent-memory ... serve``, MCP stdio) reports the same
  state before the upgrade and answers after it (functional check only).
"""
import contextlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

import t10_corpus as corpus
import t10_tamper as tamper
import t10_world as w
import t12b_seams as seams
from t10_instruments import recording, strip_literals

A, B = corpus.A, corpus.B
ALPHA, BETA = corpus.ALPHA, corpus.BETA
READS = [(A, 'memory.search', {'query': 'cedar', 'limit': 20}),
         (A, 'memory.search', {'query': 'cedar', 'scope': 'topic', 'limit': 20}),
         (A, 'memory.list', {'kind': 'episodes', 'limit': 50}),
         (B, 'memory.list', {'kind': 'episodes', 'limit': 50}),
         (A, 'memory.status', {'limit': 5}),
         (B, 'memory.search', {'query': 'cedar', 'attribution': 'conflicting'})]
REFUSING = [('memory.search', {'query': 'cedar'}),
            ('memory.list', {'kind': 'episodes'}),
            ('memory.status', {}),
            ('memory.search', {'query': 'cedar', 'scope': 'topic'})]


def _populate(world):
    """A small new-version store written through public writes (no index operations)."""
    store, sources = world.store(), world.sources()
    ids = {}
    with w.frozen_clock():
        ids['L1'] = store.capture(A, source_ref='p6:l1', events=w.events('Alpha cedar capture.'))['episode_id']
        ids['S1'] = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='p6-s1')
        ids['E1'] = sources.import_episode(session_id=ids['S1'], source_ref='p6:e1', events=w.events('Cedar one.'),
                                           provenance=w.provenance('p6-e1', coordinates={
                                               'path': '/t10/native/p6.jsonl', 'start': 10, 'end': 20}))['episode_id']
        ids['L2'] = store.capture(B, source_ref='p6:l2', events=w.events('Beta cedar capture.'))['episode_id']
        ids['c1'] = sources.claim(**w.claim_args(ids['S1'], ALPHA))
        ids['E2'] = sources.import_episode(session_id=ids['S1'], source_ref='p6:e2', events=w.events('Cedar two.'),
                                           provenance=w.provenance('p6-e2', coordinates={
                                               'path': '/t10/native/p6.jsonl', 'start': 30, 'end': 40}))['episode_id']
        sources.claim(**w.claim_args(ids['S1'], BETA, recorded_at='2026-09-02T00:00:00Z', source_range={
            'native_id': 'p6-s1', 'source_file': '/t10/native/p6.jsonl', 'start_offset': 25}))
        ids['L3'] = store.import_operator_episode(
            tenant_id=w.TENANT_ONE, role='gamma', repo_key=w.REPO, source_ref='p6:l3',
            events=w.imported_events('Gamma cedar import.'), source_provenance=w.legacy_provenance('p6-l3'))['episode_id']
        ids['S2'] = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='p6-s2')
        ids['c2'] = sources.claim(**w.claim_args(ids['S2'], BETA))
        sources.link(episode_id=ids['L3'], session_id=ids['S2'])
        ids['c3'] = sources.claim(**w.claim_args(ids['S2'], ALPHA, supersedes=ids['c2'],
                                                 recorded_at='2026-09-03T00:00:00Z'))
        ids['S3'] = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='p6-s3')
        ids['E3'] = sources.import_episode(session_id=ids['S3'], source_ref='p6:e3', events=w.events('Unowned cedar.'),
                                           provenance=w.provenance('p6-e3'))['episode_id']
        ids['S4'] = sources.register(tenant_id=w.TENANT_TWO, runtime='claude', native_id='p6-s4')
        sources.import_episode(session_id=ids['S4'], source_ref='p6:e4', events=w.events('Foreign cedar.'),
                               provenance=w.provenance('p6-e4'))
        from kp_agent_tooling._impl.service.desk_profiles import DeskProfiles
        profiles = DeskProfiles(store, A)
        profiles.initialize()
        profiles.save(dict(desk_id=corpus.PROFILE_DESK, name='Planner', description='Plans cedar work.',
                           role='Coordinator', expected_version=0))
        profiles.annotate(dict(source_session_id=ids['S1'], desk_id=corpus.PROFILE_DESK, repos=['repo-x'], adrs=[],
                               cards=[], account_ref=None, provider='fixture', model=None, expected_version=0,
                               source_ref='fixture:t10'))
        store.consolidate(A, episode_ids=[ids['L1']], items=[{
            'kind': 'observation', 'text': 'Alpha', 'citations': [{'episode_id': ids['L1'], 'event_id': 'e0',
                                                                   'start': 0, 'end': 5, 'quote': 'Alpha'}]}],
            unresolved_questions=[], qualifier_scope='cited_events')
    return ids


def _world(path):
    world = w.World(path, corpus.DESKS, corpus.SESSIONS)
    with w.frozen_clock():
        world.initialize()
    return world


@pytest.fixture
def fresh(tmp_path):
    world = _world(tmp_path / 'fresh')
    world.ids = _populate(world)
    return world


def _older_writer_copy(fresh, root):
    """The same sealed rows as ``fresh`` in a store written by the base writer."""
    older = w.World(root, corpus.DESKS, corpus.SESSIONS)
    fresh.snapshot(root / 'snapshot')
    older.restore(root / 'snapshot')
    w.strip_to_older_writer(fresh.store_path, older.store_path, tamper.base_schema())
    older.ids = fresh.ids
    return older


def _store_token(world):
    with closing(sqlite3.connect(f'file:{world.store_path}?mode=ro', uri=True)) as db:
        row = db.execute("SELECT value FROM scope_state WHERE key = 'coverage_token'").fetchone()
    return row and row[0]


def _index_token(world):
    index = Path(world.store_path).with_name('episode-search.sqlite3')
    with closing(sqlite3.connect(f'file:{index}?mode=ro', uri=True)) as db:
        row = db.execute('SELECT token FROM coverage_token').fetchone()
    return row and row[0]


def _token_free(dump):
    """A projection dump with the coverage token's value set aside (T12b R4b)."""
    state = dump.get('scope_state')
    if not state:
        return dump
    rows = [[key, ('str', '<token>')] if key == ('str', 'coverage_token') else [key, value] for key, value in state['rows']]
    return {**dump, 'scope_state': {**state, 'rows': rows}}


def _upgrade(world, *, trace=False):
    from kp_agent_tooling import desk_cli
    output = io.StringIO()
    with w.frozen_clock(), contextlib.redirect_stdout(output):
        if trace:
            with recording() as recorder:
                recorder.phase = 'call'
                code = desk_cli.main(['--config', str(world.configs[A]), 'upgrade-sources'])
        else:
            recorder = None
            code = desk_cli.main(['--config', str(world.configs[A]), 'upgrade-sources'])
    text = output.getvalue()
    assert code == 0, f'upgrade-sources exit {code}: {text}'
    return json.loads(text), recorder


def _reads(world):
    outputs = []
    for session, name, arguments in READS:
        outputs.append(w.jsonable(w.call(world.tools(session), name, arguments)))
    return outputs


def _incomplete(value):
    return 'projection_incomplete' in json.dumps(value)


def _assert_refuses_incomplete(world):
    tools = world.tools(A)
    status = w.call(tools, 'memory.connection_status', {})
    assert _incomplete(status), f'connection_status does not report projection_incomplete: {status}'
    for name, arguments in REFUSING:
        with recording() as recorder:
            recorder.phase = 'call'
            result = w.call(tools, name, arguments)
        assert 'error' in result, f'{name} answered on an incomplete store: {str(result)[:300]}'
        assert result['error']['category'] == 'projection_incomplete', (name, result)
        assert isinstance(result['error'].get('guidance'), str) and result['error']['guidance'], result
        assert not recorder.scans(phases=('call',)), f'{name} scanned while refusing: {recorder.scans(("call",))[:3]}'


def test_p6_fresh_and_new_version_stores_are_never_incomplete(tmp_path):
    world = _world(tmp_path / 'new')
    tools = world.tools(A)
    assert tools.call('memory.connection_status', {})['status'] == 'ready'
    assert not _incomplete(tools.call('memory.connection_status', {}))
    world.ids = _populate(world)
    for output in [tools.call('memory.connection_status', {})] + _reads(world):
        assert 'error' not in output and not _incomplete(output), str(output)[:300]
    assert any(e['episode_id'] == world.ids['L1'] for e in tools.call('memory.list', {'kind': 'episodes'})['entries'])


def test_p6_older_writer_store_refuses_then_upgrades_to_the_fresh_projection(fresh, tmp_path):
    # T12b R4b (meet, after fix b): the older-writer copy is taken from the fresh store as sealed (no index), then
    # the fresh reference is what a running indexer leaves, drained. `upgrade-sources` on an index-less older store
    # requests the reindex and, on a host, drains it (fix b), so it is compared with a drained fresh store, never
    # with an undrained one that answers `index_unavailable`.
    older = _older_writer_copy(fresh, tmp_path / 'older')
    seams.drain(fresh.store())
    expected_reads = _reads(fresh)
    assert all('error' not in r for r in expected_reads), 'positive control: the fresh store answers'
    assert all(r.get('index_status') == 'results' for r in expected_reads if 'index_status' in r), (
        'positive control: the drained fresh store answers from its index')
    expected_projection = tamper.projection_dump(fresh.store_path)
    assert expected_projection, 'the fresh store has no projection (no tables or columns beyond the base schema)'
    assert tamper.projection_dump(older.store_path) == {}, 'fixture: the older-writer copy holds no projection'
    _assert_refuses_incomplete(older)
    report, _ = _upgrade(older)
    assert isinstance(report, dict) and report, report
    assert not _incomplete(older.tools(A).call('memory.connection_status', {}))
    assert _reads(older) == expected_reads
    # T12b R4b: each index has its own random coverage token, so the projections are compared with the token's
    # value set aside, and each store's token must be its own index's token (the marks describe that index).
    for world in (fresh, older):
        assert _store_token(world) == _index_token(world), 'the coverage marks do not describe this store\'s index'
    assert _token_free(tamper.projection_dump(older.store_path)) == _token_free(expected_projection), (
        'the backfill differs from a fresh projection')
    # T12b R4: the outbox is a store table; its schema (DDL, the four watched tables' triggers, user_version) is
    # identical to a fresh store's, and only its rows are excluded (tests/t12b_ruled_exceptions.py R4).
    fresh_store = tamper.store_table_schema(fresh.store_path)
    assert set(fresh_store['tables']) == {'index_outbox'} and len(fresh_store['triggers']) == 4, fresh_store
    assert fresh_store['user_version'] == 1, fresh_store
    assert tamper.store_table_schema(older.store_path) == fresh_store, 'the upgraded store\'s outbox schema differs'


def test_p6_second_backfill_changes_nothing(fresh, tmp_path):
    older = _older_writer_copy(fresh, tmp_path / 'older')
    with closing(sqlite3.connect(older.store_path)) as db:
        before = list(db.iterdump())
    _upgrade(older)
    with closing(sqlite3.connect(older.store_path)) as db:
        first = list(db.iterdump())
    assert first != before, 'positive control: the first upgrade-sources projected nothing'
    import kp_agent_tooling
    package_root = Path(kp_agent_tooling.__file__).resolve().parents[1]
    environment = dict(os.environ, PYTHONPATH=str(package_root))
    done = subprocess.run([sys.executable, '-m', 'kp_agent_tooling.desk_cli', '--config', str(older.configs[A]),
                           'upgrade-sources'], capture_output=True, text=True, env=environment, timeout=300)
    assert done.returncode == 0, done.stderr
    json.loads(done.stdout)
    with closing(sqlite3.connect(older.store_path)) as db:
        second = list(db.iterdump())
    assert second == first, 'a second upgrade-sources changed the store'


def _older_row(world, kind):
    """Insert one row exactly as the base writer's SQL would (no projection)."""
    ids = world.ids
    with closing(sqlite3.connect(world.store_path)) as db:
        if kind == 'episodes':  # base import_operator_episode
            payload = {'schema_version': 'ops.episode.v1', 'binding_key': ALPHA, 'provider_instance': 'operator-import',
                       'session': 'legacy-import', 'source_ref': 'older:episode',
                       'events': w.imported_events('Older writer cedar legacy.'),
                       'source_provenance': w.legacy_provenance('older-episode'),
                       'import_recorded_at': '2026-10-01T00:00:00+00:00',
                       'evidence_boundary': 'legacy graph assertion; original actor and observation time unverified'}
            identity = w.content_id('episode', payload)
            db.execute('INSERT INTO episodes(id,binding,session,source_ref,payload) VALUES (?,?,?,?,?)',
                       (identity, ALPHA, 'legacy-import', 'older:episode', w.canonical(payload)))
        elif kind == 'source_episodes':  # base import_episode: the source row and its link
            session = ids['S1']
            payload = dict(schema_version='ops.episode.v2', tenant_id=w.TENANT_ONE, source_session_id=session,
                           provider_instance='operator-transcript-import', session=session, source_ref='older:source',
                           events=w.events('Older writer cedar source.'), source_provenance=w.provenance('older-source'),
                           evidence_boundary='visible historical source; claims and truth not established by storage')
            identity = w.content_id('episode', payload)
            db.execute('INSERT INTO source_episodes(id,tenant,session_id,source_ref,payload) VALUES (?,?,?,?,?)',
                       (identity, w.TENANT_ONE, session, 'older:source', w.canonical(payload)))
            db.execute('INSERT INTO session_episodes(episode_id,session_id) VALUES (?,?)', (identity, session))
        elif kind == 'session_claims':  # base claim: alpha now owns S3 (unowned so far)
            claim = dict(schema_version='ops.session-claim.v1', session_id=ids['S3'], predicate='session.owner',
                         object={'kind': 'desk', 'id': ALPHA}, asserted_by='operator:older', recorded_at='2026-09-09T00:00:00Z',
                         evidence=['older'], valid_from=None, valid_until=None, supersedes=None, retracted=False)
            identity = w.content_id('session-claim', claim)
            db.execute('INSERT INTO session_claims(id,session_id,payload) VALUES (?,?,?)',
                       (identity, ids['S3'], w.canonical(claim)))
        elif kind == 'session_episodes':  # base link: an unlinked legacy row joins an alpha session
            db.execute('INSERT INTO session_episodes(episode_id,session_id) VALUES (?,?)', (ids['L_unlinked'], ids['S1']))
        else:
            raise ValueError(kind)
        db.commit()
        return identity if kind in ('episodes', 'source_episodes') else None


@pytest.mark.parametrize('kind', ['episodes', 'source_episodes', 'session_claims', 'session_episodes'])
def test_p6_older_writer_row_is_detected(fresh, kind):
    with w.frozen_clock():
        fresh.ids['L_unlinked'] = fresh.store().import_operator_episode(
            tenant_id=w.TENANT_ONE, role='beta', repo_key=w.REPO, source_ref='p6:unlinked',
            events=w.imported_events('Unlinked beta cedar.'), source_provenance=w.legacy_provenance('p6-unlinked'))['episode_id']
    tools = fresh.tools(A)
    assert not _incomplete(tools.call('memory.connection_status', {}))
    assert 'error' not in w.call(tools, 'memory.list', {'kind': 'episodes'}), 'positive control: reads answer'
    new = _older_row(fresh, kind)
    _assert_refuses_incomplete(fresh)
    _upgrade(fresh)
    listed = fresh.tools(A).call('memory.list', {'kind': 'episodes', 'limit': 50})
    assert not _incomplete(listed)
    if new is not None:
        assert new in {e['episode_id'] for e in listed['entries']}, 'the older-writer row is visible after upgrade'


_WRITE = re.compile(r'\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+'
                    r'(?:main\.)?["`\[]?(\w+)', re.I)


def _write_transactions(recorder, store_path, schema):
    """Transactions on the store that write projection tables or base-table columns.

    Per connection: BEGIN/SAVEPOINT opens, COMMIT/END/RELEASE closes a
    transaction; a write outside any transaction is its own transaction.
    Inserts into base tables are sealed rows, never projection.
    """
    base_tables = schema['tables']
    target = os.path.realpath(store_path)
    state = {}
    transactions = []
    for statement in recorder.top():
        if statement.db != target:
            continue
        open_, current = state.get(statement.conn, (False, []))
        verb = statement.verb
        if verb in ('BEGIN', 'SAVEPOINT'):
            state[statement.conn] = (True, current if open_ else [])
            continue
        if verb in ('COMMIT', 'END', 'RELEASE', 'ROLLBACK'):
            if open_ and current and verb != 'ROLLBACK':
                transactions.append(current)
            state[statement.conn] = (False, [])
            continue
        match = _WRITE.match(strip_literals(statement.sql))
        if match and (match.group(1) not in base_tables or verb == 'UPDATE'):
            if open_:
                current.append(statement.sql[:120])
                state[statement.conn] = (True, current)
            else:
                transactions.append([statement.sql[:120]])
    return transactions


def test_p6_backfill_is_committed_in_bounded_batches(tmp_path):
    world = _world(tmp_path / 'large')
    sources = world.sources()
    with w.frozen_clock():
        for s in range(300):
            session = sources.register(tenant_id=w.TENANT_ONE, runtime='codex', native_id=f'p6-batch-{s}')
            sources.claim(**w.claim_args(session, ALPHA if s % 2 else BETA))
            for j in range(4):
                sources.import_episode(session_id=session, source_ref=f'p6-batch:{s}:{j}',
                                       events=w.events(f'Batch note {s}-{j}.'), provenance=w.provenance(f'b-{s}-{j}'))
    world.ids = {}
    older = _older_writer_copy(world, tmp_path / 'older')
    with closing(sqlite3.connect(older.store_path)) as db:
        sealed = sum(db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
                     for t in ('episodes', 'source_episodes', 'session_claims', 'session_episodes'))
    assert sealed >= 2400, sealed
    _, recorder = _upgrade(older, trace=True)
    transactions = _write_transactions(recorder, older.store_path, tamper.base_schema())
    assert transactions, 'positive control: no projection write was traced'
    assert len(transactions) >= 2, (
        f'the backfill of {sealed} sealed rows wrote its projection in a single transaction')


def test_p6_stdio_server_reports_projection_incomplete(fresh, tmp_path):
    """Functional check through ``kp-agent-memory ... serve`` (MCP stdio), the served path."""
    import asyncio
    from datetime import timedelta
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    import kp_agent_tooling
    older = _older_writer_copy(fresh, tmp_path / 'older')
    package_root = Path(kp_agent_tooling.__file__).resolve().parents[1]
    program = ('import asyncio, sys\n'
               'from kp_agent_tooling.memory_cli import AdmissionAwareMemory, serve\n'
               'asyncio.run(serve(AdmissionAwareMemory(sys.argv[1])))\n')

    async def check(config):
        params = StdioServerParameters(command=sys.executable, args=['-c', program, str(config)],
                                       env={'PYTHONPATH': str(package_root)})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
                await session.initialize()
                status = await session.call_tool('memory.connection_status', {})
                found = await session.call_tool('memory.search', {'query': 'cedar'})
                return status, found

    status, found = asyncio.run(check(older.configs[A]))
    assert 'projection_incomplete' in json.dumps(status.structuredContent), status.structuredContent
    assert found.isError and found.structuredContent['category'] == 'projection_incomplete', found.structuredContent
    _upgrade(older)
    status, found = asyncio.run(check(older.configs[A]))
    assert not status.isError and status.structuredContent['status'] == 'ready', status.structuredContent
    assert not found.isError and found.structuredContent['total_episodes'] >= 1, found.structuredContent


def test_p6_backfill_honours_its_documented_batch_bound(tmp_path):
    """Meet (Coordinator): P6 'bounded batches, each its own transaction'. With the documented
    `upgrade-sources --batch-rows 100` (docs/MEMORY.md), a store holding 1,200 source episodes, 1,200
    links and 300 claims is projected in at least 1,200/100 = 12 transactions. A backfill that ignores
    its bound (one transaction per table) is red here; the 2-transaction check above cannot see it."""
    from kp_agent_tooling import desk_cli
    world = _world(tmp_path / 'large')
    sources = world.sources()
    with w.frozen_clock():
        for s in range(300):
            session = sources.register(tenant_id=w.TENANT_ONE, runtime='codex', native_id=f'p6-bound-{s}')
            sources.claim(**w.claim_args(session, ALPHA if s % 2 else BETA))
            for j in range(4):
                sources.import_episode(session_id=session, source_ref=f'p6-bound:{s}:{j}',
                                       events=w.events(f'Bound note {s}-{j}.'), provenance=w.provenance(f'c-{s}-{j}'))
    world.ids = {}
    older = _older_writer_copy(world, tmp_path / 'older')
    output = io.StringIO()
    with w.frozen_clock(), contextlib.redirect_stdout(output), recording() as recorder:
        recorder.phase = 'call'
        code = desk_cli.main(['--config', str(older.configs[A]), 'upgrade-sources', '--batch-rows', '100'])
    assert code == 0, output.getvalue()
    transactions = _write_transactions(recorder, older.store_path, tamper.base_schema())
    assert len(transactions) >= 12, (
        f'--batch-rows 100 over 1,200 source episodes wrote its projection in {len(transactions)} transactions')
