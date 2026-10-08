"""T10 P4: projections are written when facts change, and stay honest.

Order: docs/work/orders/T10-read-path-projection.md (P4). Publicly observable,
with no operator step, through long-lived tools objects (so nothing a call
caches can mask a change):

* a read immediately after a public seal or claim reflects it;
* an owner reassignment from desk A to B, before any index refresh: B finds
  the reassigned text, A does not; coverage follows the scope; the
  reassignment opens no index connection;
* a binding added to or removed from the catalog changes the next read;
* seal, claim and link succeed while the index file is absent (and never
  open it), and do not wait on a locked index.
"""
import sqlite3
import time

import pytest

import t10_corpus as corpus
import t10_oracle
import t10_world as w
from t10_instruments import recording

A, B, C = corpus.A, corpus.B, corpus.C
ALPHA, BETA, DELTA = corpus.ALPHA, corpus.BETA, corpus.DELTA
UNAVAILABLE = {'status': 'error', 'category': 'memory_unavailable',
               'guidance': 'Verify this session is admitted and the record is available to its desk.'}


@pytest.fixture
def world(tmp_path):
    world = w.World(tmp_path / 'world', corpus.DESKS, corpus.SESSIONS)
    world.initialize()
    return world


def _ids(output):
    return [r['episode_id'] for r in output['results']]


def _list_ids(tools, **arguments):
    return [e['episode_id'] for e in tools.call('memory.list', {'kind': 'episodes', 'limit': 50, **arguments})['entries']]


def _oracle_list(world, session, **filters):
    selected, _ = t10_oracle.select_for(world, session, **filters)
    return list(selected)[::-1]


def test_p4_read_immediately_after_seal_reflects_it(world):
    world.index().rebuild()
    tools = world.tools(A)
    assert tools.call('memory.search', {'query': 'juniper'})['total_episodes'] == 0
    episode = world.store().capture(A, source_ref='p4:seal', events=w.events('Juniper sealed capture.'))['episode_id']
    assert _list_ids(tools) == [episode]
    assert tools.call('memory.read_event', {'episode_id': episode, 'event_id': 'e0'})['text'] == 'Juniper sealed capture.'
    found = tools.call('memory.search', {'query': 'juniper'})
    assert (found['total_episodes'], found['covered_episodes'], found['index_status']) == (1, 0, 'incomplete_index')
    sources = world.sources()
    session = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='p4-seal')
    sources.claim(**w.claim_args(session, ALPHA))
    imported = sources.import_episode(session_id=session, source_ref='p4:import', events=w.events('Juniper import.'),
                                      provenance=w.provenance('p4-import'))['episode_id']
    assert _list_ids(tools) == [imported, episode] == _oracle_list(world, A)
    assert tools.call('memory.episode_directory', {'episode_id': imported})['total'] == 1
    assert tools.call('memory.search', {'query': 'juniper'})['total_episodes'] == 2


def test_p4_read_immediately_after_claim_reflects_it(world):
    sources = world.sources()
    session = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='p4-claim')
    episode = sources.import_episode(session_id=session, source_ref='p4:claim',
                                     events=w.events('Juniper awaiting an owner.'),
                                     provenance=w.provenance('p4-claim'))['episode_id']
    world.index().rebuild()
    tools_a, tools_b = world.tools(A), world.tools(B)
    before = tools_a.call('memory.search', {'query': 'juniper'})
    assert _ids(before) == [] and before['search_scope']['excluded_unresolved_episodes'] == 1
    assert _ids(tools_a.call('memory.search', {'query': 'juniper', 'scope': 'topic'})) == [episode]
    sources.claim(**w.claim_args(session, ALPHA))
    after = tools_a.call('memory.search', {'query': 'juniper'})
    assert _ids(after) == [episode] and after['results'][0]['attribution_status'] == 'resolved'
    assert after['search_scope']['excluded_unresolved_episodes'] == 0
    assert _list_ids(tools_a) == [episode] == _oracle_list(world, A)
    assert _ids(tools_b.call('memory.search', {'query': 'juniper'})) == []


def test_p4_owner_reassignment_before_index_refresh(world):
    sources = world.sources()
    session = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='p4-reassign')
    first = sources.claim(**w.claim_args(session, ALPHA))
    episode = sources.import_episode(session_id=session, source_ref='p4:reassign',
                                     events=w.events('Juniper reassigned decision text.'),
                                     provenance=w.provenance('p4-reassign'))['episode_id']
    world.index().upsert_episodes([episode])
    tools_a, tools_b = world.tools(A), world.tools(B)
    assert _ids(tools_a.call('memory.search', {'query': 'juniper reassigned'})) == [episode]
    assert _ids(tools_b.call('memory.search', {'query': 'juniper reassigned'})) == []
    with recording() as recorder:
        sources.claim(**w.claim_args(session, BETA, supersedes=first, recorded_at='2026-09-02T00:00:00Z'))
    assert recorder.connected(world.store_path) >= 1, 'positive control: the claim is audited'
    assert recorder.connected(world.index_path) == 0, 'the reassignment opened the index'
    found_b = tools_b.call('memory.search', {'query': 'juniper reassigned'})
    assert _ids(found_b) == [episode] and found_b['results'][0]['quote'] == 'Juniper reassigned'
    assert found_b['results'][0]['binding_key'] == BETA
    found_a = tools_a.call('memory.search', {'query': 'juniper reassigned'})
    assert _ids(found_a) == []
    for tools, session_name, found in ((tools_a, A, found_a), (tools_b, B, found_b)):
        expected = len(t10_oracle.select_for(world, session_name)[0])
        assert found['total_episodes'] == expected == found['covered_episodes'], (session_name, found)
    assert tools_b.call('memory.read_event', {'episode_id': episode, 'event_id': 'e0'})['binding_key'] == BETA
    with pytest.raises(Exception) as refused:
        tools_a.call('memory.read_event', {'episode_id': episode, 'event_id': 'e0'})
    assert w.failure('memory.read_event', refused.value) == UNAVAILABLE
    assert _list_ids(tools_b) == [episode] == _oracle_list(world, B)
    assert _list_ids(tools_a) == [] == _oracle_list(world, A)


def test_p4_catalog_binding_change_takes_effect_on_next_read(world):
    store = world.store()
    legacy = store.import_operator_episode(
        tenant_id=w.TENANT_ONE, role='delta', repo_key=w.REPO, source_ref='p4:delta',
        events=w.imported_events('Juniper delta legacy note.'), source_provenance=w.legacy_provenance('p4-delta'))
    episode = legacy['episode_id']
    world.index().rebuild()
    tools = world.tools(A)
    topic = {'query': 'juniper', 'scope': 'topic'}
    assert _ids(tools.call('memory.search', topic)) == [episode]
    total = tools.call('memory.search', topic)['total_episodes']
    bindings = tools.call('memory.bindings', {})['total']
    assert _list_ids(tools, binding_key=DELTA) == [episode]
    world.write_catalog([d for d in corpus.DESKS if d[1] != 'delta'])
    removed = tools.call('memory.search', topic)
    assert _ids(removed) == [] and removed['total_episodes'] == total - 1
    assert tools.call('memory.bindings', {})['total'] == bindings - 1
    with pytest.raises(Exception) as unknown:
        tools.call('memory.list', {'kind': 'episodes', 'binding_key': DELTA})
    assert w.failure('memory.list', unknown.value)['category'] == 'unknown_binding'
    world.write_catalog(corpus.DESKS)
    restored = tools.call('memory.search', topic)
    assert _ids(restored) == [episode] and restored['total_episodes'] == total
    assert tools.call('memory.bindings', {})['total'] == bindings
    assert _list_ids(tools, binding_key=DELTA) == [episode]
    world.write_catalog([d for d in corpus.DESKS if d[1] != 'alpha'])
    with pytest.raises(Exception) as withdrawn:
        tools.call('memory.list', {'kind': 'episodes'})
    assert w.failure('memory.list', withdrawn.value)['category'] == 'memory_unavailable'
    world.write_catalog(corpus.DESKS)
    assert tools.call('memory.connection_status', {})['status'] == 'ready'


def test_p4_seal_claim_and_link_never_open_the_index(world):
    assert not world.index_path.exists()
    store, sources = world.store(), world.sources()
    with recording() as recorder:
        captured = store.capture(A, source_ref='p4:absent', events=w.events('Juniper without an index.'))['episode_id']
        session = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='p4-absent')
        imported = sources.import_episode(session_id=session, source_ref='p4:absent-import',
                                          events=w.events('Juniper imported without an index.'),
                                          provenance=w.provenance('p4-absent'))['episode_id']
        sources.claim(**w.claim_args(session, ALPHA))
        legacy = store.import_operator_episode(
            tenant_id=w.TENANT_ONE, role='alpha', repo_key=w.REPO, source_ref='p4:absent-legacy',
            events=w.imported_events('Juniper legacy without an index.'),
            source_provenance=w.legacy_provenance('p4-absent'))['episode_id']
        sources.link(episode_id=legacy, session_id=session)
    assert recorder.connected(world.store_path) >= 5, 'positive control: the writes are audited'
    assert recorder.connected(world.index_path) == 0, 'a seal, claim or link opened the index'
    assert not world.index_path.exists()
    tools = world.tools(A)
    assert set(_list_ids(tools)) == {captured, imported, legacy}
    assert _list_ids(tools) == _oracle_list(world, A)
    found = tools.call('memory.search', {'query': 'juniper'})
    assert found['index_status'] == 'index_unavailable' and found['total_episodes'] == 3


def test_p4_seal_and_claim_do_not_wait_on_a_locked_index(world):
    world.index().rebuild()
    store, sources = world.store(), world.sources()
    lock = sqlite3.connect(world.index_path, timeout=0)
    try:
        lock.execute('BEGIN EXCLUSIVE')
        started = time.monotonic()
        captured = store.capture(A, source_ref='p4:locked', events=w.events('Juniper while locked.'))['episode_id']
        session = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='p4-locked')
        sources.claim(**w.claim_args(session, ALPHA))
        elapsed = time.monotonic() - started
    finally:
        lock.rollback()
        lock.close()
    assert elapsed < 2.0, f'seal and claim waited {elapsed:.1f}s on the locked index'
    assert _list_ids(world.tools(A)) == [captured]
