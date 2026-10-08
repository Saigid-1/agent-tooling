"""Falsify attribution, immutable citation, tenant and admission boundaries."""
import json
import sqlite3
from dataclasses import replace
import pytest
from test_portable_desk_memory import episode_store
from kp_agent_tooling._impl.service.session_sources import SessionSources
from kp_agent_tooling._impl.service.episodic_memory import EpisodeUnavailable, EpisodeConflict
from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools
from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex


def fixture(tmp_path):
    store = episode_store(tmp_path)
    sources = SessionSources(store)
    tenant = store.sessions.resolve('session-1', store.registry).tenant_id
    session = sources.register(tenant_id=tenant, runtime='claude', native_id='historical-uuid')
    provenance = dict(source_system='local:claude-transcript', row_id='line:7', row_digest='a'*64,
                      evidence_event_map=[], import_actor='operator:test')
    receipt = sources.import_episode(session_id=session, source_ref='transcript:7', provenance=provenance,
        events=[dict(event_id='e', role='user', text='Cedar room is an abandoned proposal.')])
    index = EpisodicSearchIndex(store.path.with_name('episode-search.sqlite3'), episode_store=store)
    index.rebuild()
    return store, sources, session, receipt['episode_id'], index


def claim(sources, sid, binding, **overrides):
    args = dict(session_id=sid, predicate='session.owner', object={'kind':'desk','id':binding},
                asserted_by='operator:test', recorded_at='2026-09-23T12:00:00Z', evidence=['transcript:opening'])
    args.update(overrides)
    return sources.claim(**args)


def test_unresolved_topic_recovery_and_later_claim_do_not_rewrite_sources(tmp_path):
    store, sources, sid, episode, index = fixture(tmp_path)
    tools = EpisodicMemoryTools(store, 'session-1')
    before = sources.read(episode)
    assert 'binding_key' not in before
    assert index.search('session-1', query='Cedar room')['results'] == []
    found = tools.call('memory.search', {'query':'Cedar room', 'scope':'topic'})
    assert found['covered_episodes'] == found['total_episodes'] == 1
    row = found['results'][0]
    assert row['attribution_status'] == 'unresolved'
    assert row['binding_key'] is None
    assert tools.call(**dict(name=row['next_call']['name'], arguments=row['next_call']['arguments']))['text'] == row['quote']
    with pytest.raises(EpisodeUnavailable):
        store.read_event('session-1', episode_id=episode, event_id='e')
    owner = claim(sources, sid, store._binding('session-1'))
    # No text reindex is needed for attribution changes.
    assert index.search('session-1', query='Cedar room')['results'][0]['episode_id'] == episode
    assert tools.call('memory.list', {'kind':'episodes'})['entries'][0]['episode_id'] == episode
    assert sources.read(episode) == before
    contributor = claim(sources, sid, 'person:alice', predicate='session.contributor', object={'kind':'actor','id':'person:alice'})
    claim(sources, sid, 'unused', predicate='session.tag', object={'kind':'tag','id':'experiment'})
    for predicate, obj in [('session.contributor', {'kind':'actor','id':'person:alice'}),
                           ('session.tag', {'kind':'tag','id':'experiment'})]:
        assert index.search('session-1', query='Cedar', scope='topic', claims=[{'predicate':predicate,'object':obj}])['results']
    claim(sources, sid, store._binding('session-1'), supersedes=owner, retracted=True)
    assert index.search('session-1', query='Cedar')['results'] == []
    meta = tools.call('memory.session', {'session_id':sid})
    assert meta['attribution_status'] == 'unresolved'
    assert contributor in [r['claim_id'] for r in meta['active_claims']]
    assert len(meta['claims']) == 4
    assert sources.read(episode) == before


def test_conflicting_owners_remain_explicit_and_never_change_admission(tmp_path):
    store, sources, sid, episode, index = fixture(tmp_path)
    own, other = store._binding('session-1'), store._binding('session-3')
    claim(sources, sid, own)
    claim(sources, sid, other)
    row = index.search('session-1', query='Cedar')['results'][0]
    assert row['attribution_status'] == 'conflicting' and row['binding_key'] is None
    assert index.search('session-3', query='Cedar')['results']
    assert sources.metadata(sid)['desk_bindings'] == sorted([own,other])
    assert store._binding('session-1') == own
    with pytest.raises(EpisodeUnavailable):
        store.consolidate('session-3', episode_ids=[episode], items=[], unresolved_questions=[])


def test_cross_tenant_refusal_and_claim_supersession_integrity(tmp_path):
    store, sources, sid, episode, index = fixture(tmp_path)
    foreign = sources.register(tenant_id='foreign', runtime='codex', native_id='same')
    provenance = dict(source_system='local:codex-transcript', row_id='x', row_digest='b'*64,
                      evidence_event_map=[], import_actor='operator:test')
    hidden = sources.import_episode(session_id=foreign,source_ref='x',provenance=provenance,
                                    events=[dict(event_id='e',role='user',text='Cedar foreign secret')])['episode_id']
    index.rebuild()
    result = index.search('session-1',query='Cedar',scope='topic')
    assert [r['episode_id'] for r in result['results']] == [episode]
    with pytest.raises(EpisodeUnavailable):
        store.read_event('session-1',episode_id=hidden,event_id='e',scope='topic')
    with pytest.raises(EpisodeUnavailable):
        EpisodicMemoryTools(store,'session-1').call('memory.session',{'session_id':foreign})
    original = claim(sources,sid,store._binding('session-1'))
    with pytest.raises(ValueError): claim(sources,foreign,'x',supersedes=original)
    corrected = claim(sources,sid,store._binding('session-3'),supersedes=original)
    assert claim(sources,sid,store._binding('session-3'),supersedes=original) == corrected
    with pytest.raises(EpisodeConflict): claim(sources,sid,'x',supersedes=original)
    with sqlite3.connect(store.path) as db:
        db.execute('UPDATE session_claims SET payload=? WHERE id=?',(b'{}',corrected))
    with pytest.raises(EpisodeUnavailable): index.search('session-1',query='Cedar',scope='topic')


def test_legacy_link_and_owner_correction_preserve_original_citation(tmp_path):
    store, sources, sid, episode, index = fixture(tmp_path)
    legacy = store.capture('session-1',source_ref='legacy',events=[dict(event_id='old',role='user',text='Cedar old source')])['episode_id']
    with sqlite3.connect(store.path) as db:
        raw = db.execute('SELECT payload FROM episodes WHERE id=?',(legacy,)).fetchone()[0]
        legacy_sid = db.execute('SELECT session_id FROM session_episodes WHERE episode_id=?',(legacy,)).fetchone()[0]
    owner = sources.metadata(legacy_sid)['active_claims'][0]
    claim(sources,legacy_sid,store._binding('session-3'),supersedes=owner['claim_id'],recorded_at='2099-01-01T00:00:00Z')
    index.rebuild()
    assert not index.search('session-1',query='old source')['results']
    selected = index.search('session-3',query='old source')['results'][0]
    assert selected['episode_id'] == legacy
    assert store.read_event('session-3',episode_id=legacy,event_id='old')['text'] == 'Cedar old source'
    with sqlite3.connect(store.path) as db:
        assert db.execute('SELECT payload FROM episodes WHERE id=?',(legacy,)).fetchone()[0] == raw


def test_import_idempotence_missing_index_and_tampered_projection(tmp_path):
    store, sources, sid, episode, index = fixture(tmp_path)
    p = sources.read(episode)
    assert sources.import_episode(session_id=sid,source_ref=p['source_ref'],events=p['events'],provenance=p['source_provenance'])['status'] == 'already_present'
    with pytest.raises(EpisodeConflict):
        sources.import_episode(session_id=sid,source_ref=p['source_ref'],events=[dict(event_id='e',role='user',text='changed')],provenance=p['source_provenance'])
    with sqlite3.connect(index.path) as db: db.execute('UPDATE event_search SET text=?',('Cedar forged',))
    assert index.search('session-1',query='Cedar',scope='topic')['results'][0]['excerpt'] == p['events'][0]['text']
    with sqlite3.connect(store.path) as db: db.execute('UPDATE source_episodes SET payload=? WHERE id=?',(b'{}',episode))
    result = index.search('session-1',query='Cedar',scope='topic')
    assert result['index_status'] == 'incomplete_index' and result['results'] == []
    with pytest.raises(EpisodeUnavailable): index.rebuild()


def test_imported_citations_follow_resolved_desk_ownership_and_keep_bytes(tmp_path):
    store, sources, sid, episode, _ = fixture(tmp_path)
    before = sources.read(episode)
    text = before['events'][0]['text']
    arguments = dict(episode_ids=[episode], items=[dict(kind='observation', text=text,
        citations=[dict(episode_id=episode,event_id='e',start=0,end=len(text),quote=text)])],
        unresolved_questions=[])
    tools = EpisodicMemoryTools(store,'session-1')
    with pytest.raises(EpisodeUnavailable): tools.call('memory.propose', arguments)
    owner = claim(sources,sid,store._binding('session-1'))
    result = tools.call('memory.propose',arguments)
    assert sources.read(episode) == before
    assert EpisodicMemoryTools(store,'session-2').call('memory.resume',
        {'capsule_id':result['capsule_id']})['items']
    with pytest.raises(EpisodeUnavailable):
        EpisodicMemoryTools(store,'session-3').call('memory.propose',arguments)
    conflict = claim(sources,sid,store._binding('session-3'))
    with pytest.raises(EpisodeUnavailable): tools.call('memory.propose',arguments)
    claim(sources,sid,store._binding('session-3'),supersedes=conflict,retracted=True)
    bad = json.loads(json.dumps(arguments)); bad['items'][0]['citations'][0]['quote']='invented'
    with pytest.raises(EpisodeConflict): tools.call('memory.propose',bad)
    from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue
    queue=ConsolidationQueue(tmp_path/'state/imported-queue.sqlite3',store=store);queue.initialize()
    assert queue.enqueue('session-1',episode_ids=[episode])
    calls=[]
    def proposer(packet):
        calls.append(packet)
        return {'items':arguments['items'],'unresolved_questions':[]}
    assert store.consolidate_with('session-2',episode_ids=[episode],propose=proposer)['capsule_id'] == result['capsule_id']
    claim(sources,sid,store._binding('session-1'),supersedes=owner,retracted=True)
    with pytest.raises(EpisodeUnavailable): tools.call('memory.propose',arguments)
    assert sources.read(episode)==before
