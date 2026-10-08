"""Incremental projection: upsert_episodes appends new episodes without scanning the index.

The per-episode DELETE filters an UNINDEXED FTS5 column, so it scans every indexed event.
Capture calls upsert_episodes after every batch; the scan made each batch cost seconds on
a live-sized index (T9 meet, 2026-10-02). A new episode has nothing to delete and an
unchanged one needs no work; only a changed projection is replaced, exactly once.
"""
import sqlite3

from test_portable_desk_memory import episode_store
from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex


def _traced(index):
    statements = []
    original = index._connect

    def connect(*, writable=False):
        db = original(writable=writable)
        db.set_trace_callback(statements.append)
        return db

    index._connect = connect
    return statements


def _rows(index, episode_id):
    with sqlite3.connect(index.path) as db:
        return db.execute('SELECT count(*) FROM event_search WHERE episode_id=?', (episode_id,)).fetchone()[0]


def _deletes(statements):
    return [s for s in statements if s.lstrip().upper().startswith('DELETE FROM EVENT_SEARCH')]


def test_new_and_unchanged_episodes_never_scan_and_a_changed_one_is_replaced_once(tmp_path):
    store = episode_store(tmp_path)
    index = EpisodicSearchIndex(tmp_path / 'state' / 'episode-search.sqlite3', episode_store=store)
    first = store.capture('session-1', source_ref='visible:first', events=[
        {'event_id': 'a', 'role': 'user', 'text': 'Cedar room holds the recovery kit.'},
        {'event_id': 'b', 'role': 'assistant', 'text': 'Noted: Cedar room.'}])['episode_id']
    index.rebuild()
    second = store.capture('session-1', source_ref='visible:second', events=[
        {'event_id': 'c', 'role': 'tool', 'text': 'Maple room follows.'}])['episode_id']

    statements = _traced(index)
    assert index.upsert_episodes([second])['indexed_episodes'] == 1
    assert _deletes(statements) == []
    assert _rows(index, second) == 1
    found = index.search('session-2', query='Maple room')
    assert found['index_status'] == 'results'
    assert (found['covered_episodes'], found['total_episodes']) == (2, 2)

    statements.clear()
    index.upsert_episodes([first, second])
    assert _deletes(statements) == []
    assert not [s for s in statements if 'INSERT INTO event_search' in s]
    assert (_rows(index, first), _rows(index, second)) == (2, 1)

    with sqlite3.connect(index.path) as db:
        db.execute("UPDATE indexed_episodes SET payload_sha256='stale' WHERE episode_id=?", (first,))
    statements.clear()
    index.upsert_episodes([first])
    assert len(_deletes(statements)) == 1
    assert _rows(index, first) == 2
    with sqlite3.connect(index.path) as db:
        assert db.execute('SELECT payload_sha256 FROM indexed_episodes WHERE episode_id=?',
                          (first,)).fetchone()[0] != 'stale'
    assert len(index.search('session-2', query='Cedar room')['results']) == 2
