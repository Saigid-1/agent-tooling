"""T12b B3, W11: the ops reconcile script builds its isolated copy's index through the build function, never an
inline `rebuild()` (docs/work/orders/T12b-one-indexer-outbox.md, B3, "Callers request a reindex (r4 adds W11)").

Order: "W11 (`reconcile_desk_history.py`) builds its isolated copy's index through the same build function, never an
inline `rebuild()`, and opens it through the leaf (a marked path)." Falsifier: "W11 calling `rebuild()` (new in
r4)", and B3's "`DELETE FROM event_search` on any reindex".

The script runs as tests/test_memory_reconciliation.py runs it (its fixtures), with
`EpisodicSearchIndex.rebuild` replaced by a recorder that refuses. GREEN-IF the reconciliation completes
(`reconciled`), `rebuild()` is never called, no statement deletes from `event_search`, and the destination's index
holds every episode of the destination's store. The live source state is unchanged (as the existing test asserts).
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'tests'))

from scripts.reconcile_desk_history import reconcile  # noqa: E402
from test_imported_desk_runtime import setup  # noqa: E402
from test_legacy_desk_import import _node, _export  # noqa: E402

import t12b_seams as seams  # noqa: E402


def test_w11_reconcile_never_calls_rebuild_and_indexes_the_copy(tmp_path, monkeypatch):
    from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
    calls = []

    def refused(self, *args, **kwargs):
        calls.append(str(self.path))
        raise AssertionError('W11 called EpisodicSearchIndex.rebuild()')
    monkeypatch.setattr(EpisodicSearchIndex, 'rebuild', refused)
    config, rows = setup(tmp_path)
    source, catalog = tmp_path / 'state', tmp_path / 'catalog.json'
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.glob('*.sqlite3')}
    row = rows[0]
    text = 'Explicit juniper source memory.'
    attrs = {k: row[k] for k in ('tenant_id', 'role', 'repo_key')}
    note = _node('note:import', 'DeskNote', dict(attrs, text=text, content_digest=hashlib.sha256(text.encode()).hexdigest()))
    export = _export(tmp_path, [_node(row['binding_key'], 'Binding', row)], [note])
    target = tmp_path / 'rehearsal'
    with seams.traced() as trace:
        try:
            result = reconcile(source, catalog, export, target)
        except AssertionError as error:
            result = {'status': f'raised: {error}'}
    deletes = trace.statements(pattern=r'^\s*DELETE\s+FROM\s+(\w+\.)?"?event_search\b')
    assert not calls, f'W11 called rebuild() on {calls}'
    assert not deletes, f'W11 deleted from event_search: {[e.sql[:100] for e in deletes[:3]]}'
    assert result['status'] == 'reconciled', f'the reconciliation did not complete: {result.get("status")}'
    index = target / 'state' / seams.INDEX_NAME
    with seams.ro(target / 'state' / seams.STORE_NAME) as db:
        sealed = {r[0] for r in db.execute('SELECT id FROM episodes UNION SELECT id FROM source_episodes')}
    with seams.ro(index) as db:
        indexed = {r[0] for r in db.execute('SELECT episode_id FROM indexed_episodes')}
    assert sealed and sealed <= indexed, f'the copy\'s index lacks {len(sealed - indexed)} of {len(sealed)} episodes'
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.glob('*.sqlite3')}
