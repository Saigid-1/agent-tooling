"""T11a carried items (docs/work/orders/T11a-leaf-consolidation.md, "Carried items" and
"Carried from the T10h meet").

- T10 V3: "A record whose sealed row disagrees with the index is dropped"
  (episodic_search.py:300-305). After indexing, one sealed episode's payload is re-serialised
  as equivalent JSON: the same content address (the record still verifies), a different raw
  sha256. GREEN-IF: ``memory.search`` drops that record and ``covered_episodes`` falls by 1.
- T10h M-V1 guard: ``SessionSources.upgrade(coverage_index=...)`` on a store whose projection
  is already complete establishes coverage. GREEN-IF: the report's ``coverage.index`` is
  ``'present'`` and ``memory.search`` then covers every episode.

Both pass at base; they pin behaviour the T11a consolidation must not move.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing

import t10_tamper
import t10_world
from test_portable_desk_memory import episode_store
from kp_agent_tooling._impl.service.episodic_memory import _id
from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools
from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
from kp_agent_tooling._impl.service.session_sources import SessionSources

SESSION = 'session-1'
QUERY = {'query': 'coverage marker', 'limit': 10}


def _store_with_episodes(tmp_path, count=3):
    store = episode_store(tmp_path)
    ids = [store.capture(SESSION, source_ref=f'transcript:{i}',
                         events=[{'event_id': '1', 'role': 'user', 'text': f'coverage marker {i} é'}])['episode_id']
           for i in range(count)]
    index_path = store.path.with_name('episode-search.sqlite3')
    EpisodicSearchIndex(index_path, episode_store=store).rebuild()
    return store, ids, index_path


def _search(store):
    return EpisodicMemoryTools(store, SESSION).call('memory.search', dict(QUERY))


def test_v3_a_sealed_row_that_disagrees_with_the_index_is_dropped(tmp_path):
    store, ids, _ = _store_with_episodes(tmp_path)
    before = _search(store)
    assert {r['episode_id'] for r in before['results']} == set(ids), before
    assert before['covered_episodes'] == before['total_episodes'] == len(ids), before
    with closing(sqlite3.connect(store.path)) as db:
        raw = db.execute('SELECT payload FROM episodes WHERE id=?', (ids[0],)).fetchone()[0]
        equivalent = json.dumps(json.loads(raw), indent=1).encode()
        # Same content address (the sealed record still verifies), different raw bytes.
        assert equivalent != raw and _id('episode', json.loads(equivalent)) == ids[0]
        db.execute('UPDATE episodes SET payload=? WHERE id=?', (equivalent, ids[0]))
        db.commit()
    after = _search(store)
    assert ids[0] not in {r['episode_id'] for r in after['results']}, (
        'a record whose sealed row disagrees with the index was returned')
    assert {r['episode_id'] for r in after['results']} == set(ids[1:]), after
    assert after['covered_episodes'] == before['covered_episodes'] - 1, (before, after)
    assert after['total_episodes'] == before['total_episodes'], after


def test_mv1_upgrade_with_a_coverage_index_covers_a_complete_store(tmp_path):
    store, ids, index_path = _store_with_episodes(tmp_path)
    # The store as the writer before T10 left it (no projection), beside an existing index.
    older = tmp_path / 'older-writer.sqlite3'
    t10_world.strip_to_older_writer(store.path, older, t10_tamper.base_schema())
    os.replace(older, store.path)
    first = SessionSources(store).upgrade()
    assert first['status'] == 'complete' and 'coverage' not in first, first
    uncovered = _search(store)
    assert uncovered['covered_episodes'] < uncovered['total_episodes'] == len(ids), (
        f'fixture: coverage must not be established before the coverage step: {uncovered}')
    second = SessionSources(store).upgrade(coverage_index=index_path)
    assert second.get('already_complete') is True, second  # the complete path
    assert second.get('coverage', {}).get('index') == 'present', second
    covered = _search(store)
    assert covered['covered_episodes'] == covered['total_episodes'] == len(ids), covered
    assert {r['episode_id'] for r in covered['results']} == set(ids), covered
