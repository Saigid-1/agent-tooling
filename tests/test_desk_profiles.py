import json
import uuid
from contextlib import closing
import pytest
from test_portable_desk_memory import episode_store
from kp_agent_tooling._impl.service.desk_profiles import DeskProfiles
from kp_agent_tooling._impl.service.session_sources import SessionSources
from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools


def setup(tmp_path):
    store=episode_store(tmp_path);r=DeskProfiles(store,'session-1');r.initialize()
    desk=r.save(dict(desk_id='desk:'+str(uuid.uuid4()),name='Engineer',description='Deploy across products.',role='Deployment Engineering',expected_version=0))
    ep=store.capture('session-1',source_ref='fixture',events=[dict(event_id='e',role='user',text='Cedar deployment observed.')])['episode_id']
    sources=SessionSources(store);selected,_=sources.select('session-1');sid=selected[ep]['source_session_id']
    context=dict(source_session_id=sid,desk_id=desk['desk_id'],repos=['ats','core'],adrs=['ats:ADR-1'],cards=['ats:card-2'],account_ref=None,provider='fixture',model=None,expected_version=0,source_ref='fixture:operator')
    index=EpisodicSearchIndex(store.path.with_name('episode-search.sqlite3'),episode_store=store);index.rebuild()
    return store,r,desk,ep,context


def test_independent_profile_joined_views_and_replay(tmp_path):
    store,r,desk,ep,context=setup(tmp_path)
    assert 'repo_key' not in desk
    assert r.save(dict(desk_id=desk['desk_id'],name=desk['name'],description=desk['description'],role=desk['role'],expected_version=0))==desk
    r.annotate(context);r.annotate(context)
    tools=EpisodicMemoryTools(store,'session-1')
    for view in ({'kind':'agent','value':desk['desk_id']},{'kind':'repo','value':'ats'},{'kind':'repo','value':'core'},{'kind':'global'}):
        result=tools.call('memory.search',{'query':'Cedar','view':view})
        assert result['total_episodes']==result['covered_episodes']==1
        assert result['results'][0]['episode_id']==ep
        call=result['results'][0]['next_call'];assert tools.call(call['name'],call['arguments'])['text'].startswith('Cedar')
    r.annotate({**context,'expected_version':1,'repos':['ads']})
    assert tools.call('memory.search',{'query':'Cedar','view':{'kind':'repo','value':'ats'}})['total_episodes']==0
    with closing(store._connect()) as db:assert db.execute('SELECT COUNT(*) FROM desk_session_contexts').fetchone()[0]==2
    assert store._binding('session-1')!=desk['desk_id'] # profile is not admission


def test_stale_edit_unknown_profile_and_corrupt_projection_refused(tmp_path):
    store,r,desk,ep,context=setup(tmp_path)
    r.annotate(context)
    with pytest.raises(ValueError,match='changed'):r.annotate({**context,'repos':['wrong']})
    with pytest.raises(ValueError,match='changed'):r.save(dict(desk_id=desk['desk_id'],name='Other',description=desk['description'],role=desk['role'],expected_version=0))
    with pytest.raises(Exception):r.source_ids({'kind':'agent','value':'desk:'+str(uuid.uuid4())})
    with closing(store._connect()) as db,db:db.execute("UPDATE desk_session_repos SET repo='forged' WHERE repo='ats'")
    with pytest.raises(ValueError,match='projection'):r.source_ids({'kind':'repo','value':'forged'})


def test_foreign_tenant_source_cannot_be_linked(tmp_path):
    store,r,desk,ep,context=setup(tmp_path)
    foreign=SessionSources(store).register(tenant_id='another-account',runtime='fixture',native_id='foreign')
    with pytest.raises(Exception):r.annotate({**context,'source_session_id':foreign})
    assert r.contexts()==[]
