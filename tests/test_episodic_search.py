import sqlite3

import pytest

from test_portable_desk_memory import episode_store
from kp_agent_tooling._impl.service.episodic_memory import EpisodeUnavailable
from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex


def test_literal_search_uses_verified_source_and_reports_coverage(tmp_path):
    store = episode_store(tmp_path)
    index = EpisodicSearchIndex(tmp_path / 'state' / 'episode-search.sqlite3',
                                episode_store=store)
    first = store.capture('session-1', source_ref='visible:first', events=[
        {'event_id': 'a', 'role': 'user',
         'text': 'We decided Cedar room at 04:17 UTC for recovery.'}])['episode_id']
    absent = index.search('session-2', query='Cedar room')
    assert absent['index_status'] == 'index_unavailable'
    assert absent['total_episodes'] == 1 and absent['covered_episodes'] == 0
    assert absent['match_mode'] == 'literal_phrase'
    assert absent['absence_verdict'] == 'not-established'
    assert not index.path.exists()

    index.rebuild()
    found = index.search('session-2', query='Cedar room')
    assert found['index_status'] == 'results'
    assert found['match_mode'] == 'literal_phrase'
    assert found['absence_verdict'] == 'not-established'
    assert not found['candidate_limit_reached']
    assert not found['result_limit_reached']
    assert (found['covered_episodes'], found['total_episodes']) == (1, 1)
    row = found['results'][0]
    assert row['episode_id'] == first and row['event_id'] == 'a'
    assert row['quote'] == 'Cedar room'
    assert row['source_ref'] == 'visible:first'
    assert store.read_event('session-2', episode_id=first, event_id='a',
                            start=row['start'], length=row['end'] - row['start'])['text'] == row['quote']
    assert index.search('session-2', query='Maple room')['index_status'] == 'zero_results'

    store.capture('session-1', source_ref='visible:second', events=[
        {'event_id': 'b', 'role': 'tool', 'text': 'Maple room follows.'}])
    incomplete = index.search('session-2', query='Maple room')
    assert incomplete['index_status'] == 'incomplete_index'
    assert (incomplete['covered_episodes'], incomplete['total_episodes']) == (1, 2)
    index.rebuild()
    assert index.search('session-2', query='Maple room')['index_status'] == 'results'


def test_scope_and_injected_fts_syntax(tmp_path):
    store = episode_store(tmp_path)
    index = EpisodicSearchIndex(tmp_path / 'search.sqlite3', episode_store=store)
    source_binding = store._binding('session-1')
    store.capture('session-1', source_ref='visible:secret', events=[
        {'event_id': 'a', 'role': 'user', 'text': 'Cedar private marker.'}])
    index.rebuild()
    assert index.search('session-3', query='Cedar')['results'] == []
    selected = index.search('session-3', query='Cedar', binding_key=source_binding)
    assert selected['results'][0]['quote'] == 'Cedar'
    assert index.search('session-2', query='Cedar" OR private')['results'] == []
    with pytest.raises((ValueError, RuntimeError)):
        index.search('missing', query='Cedar')
    for bad in ('', '*', 'x' * 257):
        with pytest.raises(ValueError):
            index.search('session-2', query=bad)
    with pytest.raises(ValueError):
        index.search('session-2', query='Cedar', limit=21)


def test_projection_cannot_launder_a_quote_or_tampered_source(tmp_path):
    store = episode_store(tmp_path)
    index = EpisodicSearchIndex(tmp_path / 'search.sqlite3', episode_store=store)
    episode = store.capture('session-1', source_ref='visible:one', events=[
        {'event_id': 'a', 'role': 'user', 'text': 'Cedar is the original.'}])['episode_id']
    index.rebuild()
    with sqlite3.connect(index.path) as db:
        db.execute('UPDATE event_search SET text=?', ('Cedar forged statement.',))
    result = index.search('session-2', query='Cedar')
    assert result['results'][0]['quote'] == 'Cedar'
    assert 'forged' not in str(result)
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE episodes SET payload=? WHERE id=?', (b'{}', episode))
    result = index.search('session-2', query='Cedar')
    assert result['index_status'] == 'incomplete_index'
    assert not result['results']
    with pytest.raises(EpisodeUnavailable):
        index.rebuild()


def test_imported_source_session_and_bounded_candidate_disclosure(tmp_path):
    store = episode_store(tmp_path)
    binding = store.registry.list_bindings()[0]
    index = EpisodicSearchIndex(tmp_path / 'search.sqlite3', episode_store=store)
    provenance = {
        'source_system': 'postgres:org_ops.kp_nodes', 'row_id': 'run:1',
        'row_digest': 'a' * 64, 'entity_type': 'DeskRun', 'source_actor': 'unknown',
        'source_observed_at': 'unknown', 'authored_at': 'unknown',
        'legacy_host_session_id': 'original-host-7',
        'legacy_binding_provenance': 'claimed', 'evidence_event_map': [],
    }
    store.import_operator_episode(
        tenant_id=binding.tenant_id, role=binding.role, repo_key=binding.repo_key,
        source_ref='legacy:run:1', source_provenance=provenance,
        events=[{'event_id': f'import-{i:03d}', 'role': 'tool',
                 'text': f'cats cat marker {i}'} for i in range(12)])
    index.rebuild()
    result = index.search('session-1', query='cat', limit=1)
    assert result['candidate_limit'] == 10
    assert result['candidate_limit_reached']
    assert result['result_limit_reached']
    assert result['result_limit'] == 1
    assert result['absence_verdict'] == 'not-established'
    row = result['results'][0]
    assert row['source_session'] == 'original-host-7'
    assert row['capture_session'] == 'legacy-import'
    assert row['quote'] == 'cat'
    assert row['start'] == len('cats ')
