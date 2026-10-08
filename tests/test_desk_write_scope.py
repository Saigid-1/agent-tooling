import json
import pytest
from kp_agent_tooling._impl.service.desk_identity import binding_key
from kp_agent_tooling._impl.service.desk_memory_runtime import initialize,admit,components
from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools
from kp_agent_tooling._impl.service.desk_write_scope import record_scope


def setup(tmp_path):
    state=tmp_path/'state';state.mkdir(mode=0o700)
    rows=[]
    for tenant,role,repo in [('one','Deployment Engineering','home'),('one','Deployment Engineering','app'),('one','Deployment Engineering','lib'),('one','Coordinator','app'),('two','Deployment Engineering','app')]:
        rows.append(dict(binding_key=binding_key(tenant_id=tenant,role=role,repo_key=repo),tenant_id=tenant,role=role,repo_key=repo,desk_label=role,source='fixture:operator',memory_write_allowed=True))
    catalog=tmp_path/'catalog.json';catalog.write_text(json.dumps(dict(schema_version='ops.imported-desk-catalog.v1',approval_ref='fixture:roster',bindings=rows)));catalog.chmod(0o600)
    cfg=tmp_path/'config.json';cfg.write_text(json.dumps(dict(schema_version='ops.desk-memory.local.v1',state_root=str(state),catalog_path=str(catalog),workspace_root=str(tmp_path),provider_instance='fixture',provider_session_id='session')));cfg.chmod(0o600)
    initialize(cfg);admit(cfg,desk_id=rows[0]['binding_key'],provider_id='fixture',model_id='none')
    store=components(cfg)[3];tools=EpisodicMemoryTools(store,'session')
    text='Retain exact source.';ep=store.capture('session',source_ref='fixture:source',events=[dict(event_id='e',role='user',text=text)])['episode_id']
    args=dict(episode_ids=[ep],items=[dict(kind='observation',text=text,citations=[dict(episode_id=ep,event_id='e',start=0,end=len(text),quote=text)])],unresolved_questions=[])
    return store,tools,rows,args,catalog,cfg


def test_scoped_targets_preserve_home_and_require_operator_grant(tmp_path):
    store,tools,rows,args,catalog,cfg=setup(tmp_path)
    ats=rows[1]['binding_key'];core=rows[2]['binding_key'];home=rows[0]['binding_key']
    with pytest.raises(PermissionError):tools.call('memory.propose',dict(args,target_binding_key=ats))
    record_scope(store,'session',enabled=True,approval_ref='fixture:human-all-repos')
    assert {r['binding_key'] for r in tools.call('memory.bindings',{})['bindings'] if r['writable']}=={home,ats,core}
    for target in (ats,core):
        result=tools.call('memory.propose',dict(args,target_binding_key=target))
        assert result['handoff']['binding_key']==target
        assert result['handoff']['write_attribution']['author_binding_key']==home
        assert tools.call('memory.resume',{'capsule_id':result['capsule_id'],'binding_key':target})['items']
        assert tools.call('memory.propose',dict(args,target_binding_key=target))['capsule_id']==result['capsule_id']
    assert tools.call('memory.propose',args)['handoff']['binding_key']==home
    assert store._binding('session')==home
    for target in (rows[3]['binding_key'],rows[4]['binding_key']):
        with pytest.raises(PermissionError):tools.call('memory.propose',dict(args,target_binding_key=target))
    record_scope(store,'session',enabled=False,approval_ref='fixture:revoke')
    with pytest.raises(PermissionError):tools.call('memory.propose',dict(args,target_binding_key=ats))


def test_catalog_write_revocation_is_live_and_scope_is_exact_session(tmp_path):
    store,tools,rows,args,catalog,cfg=setup(tmp_path)
    record_scope(store,'session',enabled=True,approval_ref='fixture:approval')
    other=tmp_path/'other.json';doc=json.loads(cfg.read_text());doc['provider_session_id']='other';other.write_text(json.dumps(doc));other.chmod(0o600)
    admit(other,desk_id=rows[0]['binding_key'],provider_id='fixture',model_id='none')
    with pytest.raises(PermissionError):EpisodicMemoryTools(components(other)[3],'other').call('memory.propose',dict(args,target_binding_key=rows[1]['binding_key']))
    c=json.loads(catalog.read_text());c['bindings'][1]['memory_write_allowed']=False;catalog.write_text(json.dumps(c))
    with pytest.raises(PermissionError):tools.call('memory.propose',dict(args,target_binding_key=rows[1]['binding_key']))


def test_combined_repo_sources_keep_original_attribution(tmp_path):
    store,tools,rows,args,catalog,cfg=setup(tmp_path)
    record_scope(store,'session',enabled=True,approval_ref='fixture:cross-repo')
    other=tmp_path/'core.json';doc=json.loads(cfg.read_text());doc['provider_session_id']='core-session';other.write_text(json.dumps(doc));other.chmod(0o600)
    admit(other,desk_id=rows[2]['binding_key'],provider_id='fixture',model_id='none')
    text='Core source.';core=components(other)[3];ep=core.capture('core-session',source_ref='fixture:core',events=[dict(event_id='core',role='user',text=text)])['episode_id']
    args['episode_ids'].append(ep);args['items'].append(dict(kind='observation',text=text,citations=[dict(episode_id=ep,event_id='core',start=0,end=len(text),quote=text)]))
    result=tools.call('memory.propose',dict(args,target_binding_key=rows[1]['binding_key']))
    resumed=tools.call('memory.resume',dict(capsule_id=result['capsule_id'],binding_key=rows[1]['binding_key']))
    assert len(resumed['items'])==2
    assert result['handoff']['items'][1]['attribution'][0]['source_session']=='core-session'
