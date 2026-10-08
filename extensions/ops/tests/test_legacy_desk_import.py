"""Legacy import keeps source identity separate from the importing service."""
import hashlib
import json

from test_portable_desk_memory import episode_store
from kp_agent_tooling._impl.service.desk_identity import BindingRecord, binding_key
from kp_agent_tooling_ops._impl.service.legacy_desk_import import import_export, SCHEMA, SOURCE
from kp_agent_tooling._impl.service.episodic_provenance import origin
from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex


def _node(identity, kind, attributes):
    return {'id': identity, 'entity_type': kind, 'state': 'active',
            'attributes': attributes, 'created_at': '2025-01-01T00:00:00Z',
            'updated_at': '2025-01-02T00:00:00Z'}


def _export(tmp_path, bindings, records):
    path = tmp_path / 'legacy-private.json'
    path.write_text(json.dumps({'schema_version': SCHEMA, 'exported_at': '2026-09-22T00:00:00Z',
                                'source': SOURCE, 'bindings': bindings, 'records': records}))
    path.chmod(0o600)
    return path


def test_legacy_note_and_run_additive_and_idempotent(tmp_path):
    store = episode_store(tmp_path)
    registered = store.registry.list_bindings()
    source = registered[0]
    binding = _node(source.binding_key, 'Binding', {
        'binding_key': source.binding_key, 'tenant_id': source.tenant_id,
        'role': source.role, 'repo_key': source.repo_key})
    common = {'tenant_id': source.tenant_id, 'role': source.role, 'repo_key': source.repo_key}
    text = 'Deployment observation, attributed to a desk.'
    note = _node('note:one', 'DeskNote', {**common, 'text': text,
        'content_digest': hashlib.sha256(text.encode()).hexdigest(),
        'authored_at': '2025-01-01T01:00:00Z', 'shared': False,
        'trust_class': 'desk_authored', 'note_key': 'note:one'})
    begin, end = 'Started work.', 'Finished work.'
    run = _node('run:one', 'DeskRun', {**common, 'host_session_id': 'real-host-17',
        'begin_at': '2025-01-01T02:00:00Z', 'begin_summary': begin,
        'begin_content_digest': hashlib.sha256(begin.encode()).hexdigest(),
        'end_at': '2025-01-01T03:00:00Z', 'end_summary': end,
        'end_content_digest': hashlib.sha256(end.encode()).hexdigest(),
        'binding_provenance': 'claimed', 'status': 'retired'})
    excluded = _node('chunk:one', 'DeskDocumentChunk', {'text': 'maintained elsewhere'})
    malformed = _node('note:bad', 'DeskNote', {**common, 'text': 'Wrong digest',
        'content_digest': '0'*64})
    path = _export(tmp_path, [binding], [note, run, excluded, malformed])

    preflight = import_export(path, store, dry_run=True)
    assert preflight['ready'] == 2 and preflight['imported'] == 0
    assert preflight['excluded'] == {'DeskDocumentChunk': 1}
    assert preflight['quarantined_count'] == 1
    assert preflight['quarantined'][0]['source_id'] == 'note:bad'
    first = import_export(path, store)
    assert first['imported'] == 2 and first['excluded_total'] == 1
    second = import_export(path, store)
    assert second['already_present'] == 2 and second['imported'] == 0

    with store._connect() as db:
        rows = db.execute('SELECT id,payload FROM episodes WHERE binding=?',
                          (source.binding_key,)).fetchall()
    imported = [json.loads(payload) for _, payload in rows if b'"operator-import"' in payload]
    assert len(imported) == 2
    note_episode = next(row for row in imported if row['source_provenance']['entity_type'] == 'DeskNote')
    assert note_episode['session'] == 'legacy-import'
    assert note_episode['source_provenance']['source_actor'] == 'unknown'
    assert note_episode['source_provenance']['source_observed_at'] == 'unknown'
    assert note_episode['source_provenance']['authored_at'] == '2025-01-01T01:00:00Z'
    assert note['id'] in ''.join(event['text'] for event in note_episode['events'])
    run_episode = next(row for row in imported if row['source_provenance']['entity_type'] == 'DeskRun')
    assert run_episode['source_provenance']['legacy_host_session_id'] == 'real-host-17'
    assert run_episode['source_provenance']['legacy_binding_provenance'] == 'claimed'
    assert run_episode['session'] != 'real-host-17'
    assert run_episode['source_provenance']['authored_at'] == 'multiple'
    sections = run_episode['source_provenance']['evidence_event_map']
    assert [part['label'] for part in sections] == ['raw-row-json', 'begin-summary', 'end-summary']
    assert [part['authored_at'] for part in sections] == [
        'unknown', '2025-01-01T02:00:00Z', '2025-01-01T03:00:00Z']
    assert origin(run_episode, 'import-000')['source_authored_at'] == 'unknown'
    assert origin(run_episode, 'import-001')['source_authored_at'] == '2025-01-01T02:00:00Z'
    assert origin(run_episode, 'import-002')['source_authored_at'] == '2025-01-01T03:00:00Z'
    run_id = next(identity for identity, payload in rows
                  if json.loads(payload).get('source_provenance', {}).get('entity_type') == 'DeskRun')
    read_result = store.read_event('session-3', episode_id=run_id, event_id='import-002',
                              binding_key=source.binding_key)
    assert read_result['text'] == end and read_result['source_authored_at'] == '2025-01-01T03:00:00Z'
    index = EpisodicSearchIndex(tmp_path / 'search.sqlite3', episode_store=store)
    index.rebuild()
    hits = index.search('session-3', query='Finished work', binding_key=source.binding_key)['results']
    assert any(hit['source_authored_at'] == '2025-01-01T03:00:00Z'
               for hit in hits if hit['event_id'] == 'import-002')


def test_missing_or_unregistered_binding_is_quarantined(tmp_path):
    store = episode_store(tmp_path)
    attr = {'tenant_id': 'foreign', 'role': 'Other', 'repo_key': 'repo',
            'text': 'Claim.', 'content_digest': hashlib.sha256(b'Claim.').hexdigest()}
    receipt = import_export(_export(tmp_path, [], [_node('note:foreign', 'DeskNote', attr)]), store)
    assert receipt['imported'] == 0 and receipt['quarantined_count'] == 1
    assert 'binding absent' in receipt['quarantined'][0]['reason']


def test_exact_historical_binding_with_spaced_role_and_other_repo(tmp_path):
    store = episode_store(tmp_path)
    original = store.registry
    key = binding_key(tenant_id='workspace-demo', role='Deployment Engineering',
                      repo_key='another-repository')
    historical = BindingRecord(key, 'workspace-demo', 'Deployment Engineering',
        'another-repository', None, 'Deployment Engineering', 'operator-approved-export')
    class ExtendedRegistry:
        def list_bindings(self):
            return (*original.list_bindings(), historical)
        def resolve(self, *, tenant_id, role, repo_key):
            if (tenant_id, role, repo_key) == (historical.tenant_id,
                    historical.role, historical.repo_key):
                return historical
            return original.resolve(tenant_id=tenant_id, role=role, repo_key=repo_key)
    store.registry = ExtendedRegistry()
    binding = _node(key, 'Binding', {'binding_key': key,
        'tenant_id': historical.tenant_id, 'role': historical.role,
        'repo_key': historical.repo_key})
    text = 'Measured an isolated sandbox.'
    note = _node('note:other-repo', 'DeskNote', {
        'tenant_id': historical.tenant_id, 'role': historical.role,
        'repo_key': historical.repo_key, 'text': text,
        'content_digest': hashlib.sha256(text.encode()).hexdigest(),
        'authored_at': '2025-01-03T00:00:00Z'})
    result = import_export(_export(tmp_path, [binding], [note]), store)
    assert result['imported'] == 1 and result['quarantined_count'] == 0
    with store._connect() as db:
        imported = db.execute('SELECT binding,payload FROM episodes WHERE binding=?', (key,)).fetchone()
    assert imported is not None
    provenance = json.loads(imported[1])['source_provenance']
    assert provenance['evidence_event_map'][0]['label'] == 'raw-row-json'
    assert provenance['evidence_event_map'][1]['label'] == 'note-text'
