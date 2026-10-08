"""T10 P4b: integrity, the projection grants nothing.

Order: docs/work/orders/T10-read-path-projection.md (P4b). The projection is
discovered through ``sqlite_master`` only (``t10_tamper``): every table or
base-table column the base writer does not create.

* Editing a projection row (desk A's binding replaced by desk B's in every
  projected value) must not move A's episodes into B's reads.
* Editing the index's scope copy (the same replacement in every table of the
  index file) must not either.
* A tampered claim of a returned record's session refuses the read with the
  existing category (``memory_unavailable``).
* Behaviour change, stated by the order: a session whose sealed metadata is
  unavailable, or one of whose claims fails its ID check, now refuses only
  reads whose result or scope includes it (it used to refuse every read in
  the tenant).
"""
import pytest

import t10_corpus as corpus
import t10_tamper as tamper
import t10_world as w

A, B, C = corpus.A, corpus.B, corpus.C
ALPHA, BETA, GAMMA = corpus.ALPHA, corpus.BETA, corpus.GAMMA
UNAVAILABLE = {'status': 'error', 'category': 'memory_unavailable',
               'guidance': 'Verify this session is admitted and the record is available to its desk.'}


@pytest.fixture
def world(tmp_path):
    world = w.World(tmp_path / 'world', corpus.DESKS, corpus.SESSIONS)
    world.initialize()
    store, sources = world.store(), world.sources()
    ids = {}
    ids['legacy_a'] = store.capture(A, source_ref='p4b:legacy', events=w.events('Tamper cedar legacy secret.'))['episode_id']
    ids['S_A'] = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='p4b-a')
    sources.claim(**w.claim_args(ids['S_A'], ALPHA))
    ids['source_a'] = sources.import_episode(session_id=ids['S_A'], source_ref='p4b:a',
                                             events=w.events('Tamper cedar source secret.'),
                                             provenance=w.provenance('p4b-a'))['episode_id']
    ids['S_B'] = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='p4b-b')
    sources.claim(**w.claim_args(ids['S_B'], BETA))
    ids['source_b'] = sources.import_episode(session_id=ids['S_B'], source_ref='p4b:b',
                                             events=w.events('Tamper cedar belongs to beta.'),
                                             provenance=w.provenance('p4b-b'))['episode_id']
    ids['S_C'] = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='p4b-c')
    sources.claim(**w.claim_args(ids['S_C'], GAMMA))
    ids['source_c'] = sources.import_episode(session_id=ids['S_C'], source_ref='p4b:c',
                                             events=w.events('Gamma private ledger entry.'),
                                             provenance=w.provenance('p4b-c'))['episode_id']
    world.index().rebuild()
    world.ids = ids
    return world


def _alpha_ids(world):
    return {world.ids['legacy_a'], world.ids['source_a']}


def _beta_reads(world):
    """Everything desk B's session can see of desk A's records after a tamper."""
    tools = world.tools(B)
    seen = set()
    for name, arguments in (('memory.search', {'query': 'tamper cedar', 'limit': 20}),
                            ('memory.list', {'kind': 'episodes', 'limit': 50}),
                            ('memory.status', {'limit': 20})):
        result = w.call(tools, name, arguments)
        text = str(result)
        seen |= {i for i in _alpha_ids(world) if i in text}
    for episode in _alpha_ids(world):
        result = w.call(tools, 'memory.read_event', {'episode_id': episode, 'event_id': 'e0'})
        if 'error' not in result:
            seen.add(episode)
        result = w.call(tools, 'memory.episode_directory', {'episode_id': episode})
        if 'error' not in result:
            seen.add(episode)
    return seen


def _positive_control(world):
    tools_b = world.tools(B)
    found = tools_b.call('memory.search', {'query': 'tamper cedar', 'limit': 20})
    assert [r['episode_id'] for r in found['results']] == [world.ids['source_b']]
    found_a = world.tools(A).call('memory.search', {'query': 'tamper cedar', 'limit': 20})
    assert {r['episode_id'] for r in found_a['results']} == _alpha_ids(world)
    assert _beta_reads(world) == set(), 'desk B already sees desk A records before any tamper'


def test_p4b_projection_row_edit_moves_nothing_into_another_desk(world):
    _positive_control(world)
    columns = tamper.projection_columns(world.store_path)
    assert columns, 'no projection found in the source store (tables or columns beyond the base schema)'
    changed = tamper.replace_text(world.store_path, ALPHA, BETA, only=columns)
    assert changed > 0, f'no projected value names desk A; projection columns: {columns}'
    leaked = _beta_reads(world)
    assert not leaked, f'desk B got {len(leaked)} desk A record(s) after a projection edit'


def test_p4b_index_scope_copy_edit_moves_nothing_into_another_desk(world):
    _positive_control(world)
    changed = tamper.replace_text(world.index_path, ALPHA, BETA)
    assert changed > 0, 'positive control: the index holds no copy of desk A to edit'
    leaked = _beta_reads(world)
    assert not leaked, f'desk B got {len(leaked)} desk A record(s) after an index edit'


@pytest.mark.parametrize('kind', ['claim', 'session'])
def test_p4b_tampered_claim_refuses_reads_that_return_its_records(world, kind):
    tools = world.tools(A)
    episode = world.ids['source_a']
    assert episode in {r['episode_id'] for r in tools.call('memory.search', {'query': 'tamper cedar'})['results']}
    assert tools.call('memory.read_event', {'episode_id': episode, 'event_id': 'e0'})['text']
    changed = (tamper.tamper_claim if kind == 'claim' else tamper.tamper_session)(world.store_path, world.ids['S_A'])
    assert changed >= 1
    for name, arguments in (('memory.search', {'query': 'tamper cedar source'}),
                            ('memory.read_event', {'episode_id': episode, 'event_id': 'e0'}),
                            ('memory.episode_directory', {'episode_id': episode})):
        with pytest.raises(Exception) as refused:
            tools.call(name, arguments)
        assert w.failure(name, refused.value) == UNAVAILABLE, (name, refused.value)


@pytest.mark.parametrize('kind', ['claim', 'session'])
def test_p4b_behaviour_change_refusal_is_narrowed_to_reads_including_the_session(world, kind):
    tools_a, tools_c = world.tools(A), world.tools(C)
    before = tools_a.call('memory.search', {'query': 'tamper cedar source'})
    assert [r['episode_id'] for r in before['results']] == [world.ids['source_a']]
    assert world.ids['source_c'] in str(tools_c.call('memory.list', {'kind': 'episodes'}))
    changed = (tamper.tamper_claim if kind == 'claim' else tamper.tamper_session)(world.store_path, world.ids['S_C'])
    assert changed >= 1
    # Reads whose result and scope exclude the damaged session keep working.
    after = tools_a.call('memory.search', {'query': 'tamper cedar source'})
    assert [r['episode_id'] for r in after['results']] == [world.ids['source_a']]
    listed = tools_a.call('memory.list', {'kind': 'episodes'})
    assert {e['episode_id'] for e in listed['entries']} == _alpha_ids(world)
    assert tools_a.call('memory.read_event', {'episode_id': world.ids['source_a'], 'event_id': 'e0'})['text']
    # Reads whose result includes it still refuse with the existing category.
    for name, arguments in (('memory.search', {'query': 'gamma private ledger'}),
                            ('memory.read_event', {'episode_id': world.ids['source_c'], 'event_id': 'e0'})):
        with pytest.raises(Exception) as refused:
            tools_c.call(name, arguments)
        assert w.failure(name, refused.value) == UNAVAILABLE, (name, refused.value)
