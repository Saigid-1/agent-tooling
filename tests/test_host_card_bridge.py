import json
from pathlib import Path
import pytest
from kp_agent_tooling._impl.service.desk_memory_runtime import initialize, admit, components
from kp_agent_tooling._impl.service.host_card_bridge import attach_card
from kp_agent_tooling._impl.service.session_sources import SessionSources
from kp_agent_tooling._impl.service.episodic_memory import EpisodeConflict, EpisodeUnavailable
from kp_agent_tooling._impl.service.desk_binding import DeskLaunchUnavailable
from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools

ROOT = Path(__file__).resolve().parents[1]


def setup(tmp_path, admitted=True, provider_instance='host-test'):
    tmp_path.chmod(0o700)
    for name in ('catalog.example.json','doctrine.md'):
        p=tmp_path/name;p.write_bytes((ROOT/'config/desk-context'/name).read_bytes());p.chmod(0o600)
    config=tmp_path/'session.json'
    config.write_text(json.dumps({'schema_version':'ops.desk-memory.local.v1','state_root':str(tmp_path),
        'catalog_path':str(tmp_path/'catalog.example.json'),'workspace_root':str(tmp_path),
        'provider_instance':provider_instance,'provider_session_id':'one'}));config.chmod(0o600)
    initialize(config)
    if admitted:admit(config,desk_id='implementation-desk',provider_id='fixture',model_id='fixture')
    event=tmp_path/'event.json';event.write_text(json.dumps({'schema_version':'ops.host-card-event.v1',
        'provider_instance':provider_instance,'host_session_id':'one','board_id':'board-a',
        'workspace_id':'workspace','card_id':'7','event_id':'launch-1',
        'recorded_at':'2026-09-23T12:00:00Z','asserted_by':'fixture:host'}));event.chmod(0o600)
    return config,event


def test_preview_replay_and_qualified_card(tmp_path):
    config,event=setup(tmp_path);_,registry,ledger,store=components(config)
    before=ledger.path.read_bytes()
    plan=attach_card(config,event)
    assert plan['status']=='planned'
    assert EpisodicMemoryTools(store, 'one').call('memory.list', {'kind':'episodes'})['entries']==[]
    result=attach_card(config,event,apply=True)
    assert result==attach_card(config,event,apply=True)
    assert ledger.path.read_bytes()==before
    metadata=SessionSources(store).metadata(result['source_session_id'])
    assert result['claim_filter'] in [{'predicate':x['predicate'],'object':x['object']} for x in metadata['claims']]
    changed=json.loads(event.read_text());changed['board_id']='board-b';event.write_text(json.dumps(changed))
    with pytest.raises(EpisodeConflict):attach_card(config,event,apply=True)
    changed['event_id']='launch-2';event.write_text(json.dumps(changed))
    other=attach_card(config,event,apply=True)
    assert result['card_key']!=other['card_key']


def test_conflicting_event_bytes_refuse(tmp_path):
    config,event=setup(tmp_path);attach_card(config,event,apply=True)
    changed=json.loads(event.read_text());changed['card_id']='8';event.write_text(json.dumps(changed))
    with pytest.raises(EpisodeConflict):attach_card(config,event,apply=True)


def test_wrong_session_and_unadmitted_refused(tmp_path):
    config,event=setup(tmp_path,admitted=False)
    with pytest.raises(DeskLaunchUnavailable):attach_card(config,event,apply=True)
    changed=json.loads(event.read_text());changed['host_session_id']='other';event.write_text(json.dumps(changed))
    with pytest.raises(ValueError,match='exact configured session'):attach_card(config,event,apply=True)
    event.chmod(0o644)
    with pytest.raises(ValueError,match='0600'):attach_card(config,event,apply=True)


def test_interrupted_claim_completes_on_retry(tmp_path, monkeypatch):
    config,event=setup(tmp_path)
    original=SessionSources.claim
    def fail(*args,**kwargs):raise RuntimeError('simulated interruption after capture')
    monkeypatch.setattr(SessionSources,'claim',fail)
    with pytest.raises(RuntimeError,match='simulated'):attach_card(config,event,apply=True)
    monkeypatch.setattr(SessionSources,'claim',original)
    result=attach_card(config,event,apply=True)
    assert result['status']=='attached'
    _,_,_,store=components(config)
    assert len(EpisodicMemoryTools(store,'one').call('memory.list',{'kind':'episodes'})['entries'])==1


def test_declared_native_runtime_tags_existing_import_without_owner_grant(tmp_path):
    config, event = setup(tmp_path)
    _, registry, ledger, store = components(config)
    tenant = ledger.resolve('one', registry).tenant_id
    sources = SessionSources(store)
    imported = sources.register(tenant_id=tenant, runtime='claude', native_id='one')
    imported_episode = sources.import_episode(
        session_id=imported, source_ref='native-jsonl:claude:one:0',
        events=[{'event_id': 'byte-0-part-0', 'role': 'user', 'text': 'historical text'}],
        provenance={'source_system': 'local:claude-jsonl', 'row_id': 'one:0',
                    'row_digest': 'a' * 64, 'evidence_event_map': {},
                    'import_actor': 'operator:test'})['episode_id']
    before = ledger.path.read_bytes()
    payload = json.loads(event.read_text())
    payload['native_runtime'] = 'claude'
    event.write_text(json.dumps(payload))
    receipt = attach_card(config, event, apply=True)
    assert receipt['imported_source_session_id'] == imported
    assert receipt['imported_claim_id']
    assert receipt['source_session_id'] != imported
    assert receipt['native_session_mapping'] == 'operator_asserted'
    assert attach_card(config, event, apply=True) == receipt
    selected, _ = sources.select('one', scope='topic', claims=[receipt['claim_filter']])
    assert imported_episode in selected
    metadata = sources.metadata(imported, tenant)
    assert metadata['desk_bindings'] == []
    assert not any(c['predicate'] == 'session.owner' for c in metadata['claims'])
    assert ledger.path.read_bytes() == before


def test_native_mapping_requires_existing_same_tenant_session_before_capture(tmp_path):
    config, event = setup(tmp_path)
    _, registry, ledger, store = components(config)
    payload = json.loads(event.read_text())
    payload['native_runtime'] = 'claude'
    event.write_text(json.dumps(payload))
    with pytest.raises(EpisodeUnavailable):
        attach_card(config, event, apply=True)
    assert EpisodicMemoryTools(store, 'one').call('memory.list', {'kind': 'episodes'})['entries'] == []
    SessionSources(store).register(tenant_id='another-tenant', runtime='claude', native_id='one')
    with pytest.raises(EpisodeUnavailable):
        attach_card(config, event, apply=True)
    assert EpisodicMemoryTools(store, 'one').call('memory.list', {'kind': 'episodes'})['entries'] == []
    assert ledger.resolve('one', registry)


def test_native_claim_retry_after_interrupted_second_claim(tmp_path, monkeypatch):
    config, event = setup(tmp_path)
    _, registry, ledger, store = components(config)
    tenant = ledger.resolve('one', registry).tenant_id
    imported = SessionSources(store).register(tenant_id=tenant, runtime='codex', native_id='one')
    payload = json.loads(event.read_text())
    payload['native_runtime'] = 'codex'
    event.write_text(json.dumps(payload))
    original = SessionSources.claim
    calls = 0
    def fail_second(self, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError('interrupted before imported claim')
        return original(self, **kwargs)
    monkeypatch.setattr(SessionSources, 'claim', fail_second)
    with pytest.raises(RuntimeError, match='interrupted'):
        attach_card(config, event, apply=True)
    monkeypatch.setattr(SessionSources, 'claim', original)
    receipt = attach_card(config, event, apply=True)
    assert receipt['imported_source_session_id'] == imported
    assert receipt['imported_claim_id']
    assert len(EpisodicMemoryTools(store, 'one').call('memory.list', {'kind': 'episodes'})['entries']) == 1


def test_native_mapping_cannot_convert_imported_identity_into_live_owner(tmp_path):
    config, event = setup(tmp_path, provider_instance='claude')
    _, registry, ledger, store = components(config)
    tenant = ledger.resolve('one', registry).tenant_id
    imported = SessionSources(store).register(tenant_id=tenant, runtime='claude', native_id='one')
    payload = json.loads(event.read_text())
    payload['native_runtime'] = 'claude'
    event.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match='distinct'):
        attach_card(config, event, apply=True)
    assert SessionSources(store).metadata(imported, tenant)['desk_bindings'] == []


def test_card_capture_refreshes_search_and_retry_repairs_projection(tmp_path, monkeypatch):
    """T12b R3 replacement (ruled exception, Verification, 2026-10-04; tests/t12b_ruled_exceptions.py), under the
    meet's W7 ruling: `attach_card` writes NO `reindex` outbox row and never rebuilds inline, and a failed drain is
    visible in the drainer's result, not silent. With the drainer failing (the order's `drain` raises): the card
    attaches (capture and claim intact, no exception), no `reindex` row is written and nothing rebuilds (a rebuild or
    a reindex call fails the test), the drainer's result (`drain_after_seal`'s return, tests/t12b_seams.py spy) reports
    the failure, the receipt shows the card not indexed, and the card's seal waits in the outbox. With the drainer
    back: replaying the event drains, the topic search over the card's claim finds it with full coverage, and a
    second replay returns the same receipt."""
    from kp_agent_tooling._impl.service import episodic_search
    from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
    from t12b_seams import drain_after_seal_spy, outbox_rows

    def refused(*args, **kwargs):
        raise AssertionError('a card event rebuilt the index inline')

    def failing(*args, **kwargs):
        raise RuntimeError('synthetic drain failure')
    monkeypatch.setattr(EpisodicSearchIndex, 'rebuild', refused)
    monkeypatch.setattr(episodic_search, 'reindex', refused)
    config,event=setup(tmp_path)
    _,_,_,store=components(config)
    index=store.path.with_name('episode-search.sqlite3')
    with monkeypatch.context() as outage, drain_after_seal_spy() as calls:
        outage.setattr(episodic_search, 'drain', failing)
        first=attach_card(config,event,apply=True)
    assert first['status']=='attached'
    results=[value for _, value in calls]
    assert results and all(isinstance(r,dict) and r.get('status')=='error' for r in results), (
        f'the failed drain was silent in the drainer\'s result: {results}')
    assert first['search_index']['indexed_episodes']==0, f'the receipt claims the card indexed: {first["search_index"]}'
    rows=outbox_rows(store.path)
    assert not [row for row in rows if row['reason']=='reindex'], 'a card event requested a reindex'
    assert [row for row in rows if row['reason']=='seal' and row['episode_id']==first['episode_id']], (
        'the card\'s seal does not wait in the outbox')
    receipt=attach_card(config,event,apply=True)
    assert not [row for row in outbox_rows(store.path) if row['reason']=='reindex']
    result=EpisodicSearchIndex(index,episode_store=store).search(
        'one',query='board',scope='topic',claims=[receipt['claim_filter']])
    assert result['covered_episodes']==result['total_episodes']==1
    assert result['results']
    assert receipt==attach_card(config,event,apply=True)