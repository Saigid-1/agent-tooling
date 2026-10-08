import hashlib
import json
import pytest
from kp_agent_tooling._impl.service.desk_memory_runtime import initialize, admit, components, from_local_config, ImportedDeskAuthority
from kp_agent_tooling._impl.service.desk_identity import binding_key
from kp_agent_tooling_ops._impl.service.legacy_desk_import import import_export
from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
from test_legacy_desk_import import _node, _export


def setup(tmp_path):
    catalog=tmp_path/'catalog.json'
    rows=[]
    for role,repo in [('Verification','ops'),('Deployment Engineering','core')]:
        key=binding_key(tenant_id='tenant',role=role,repo_key=repo)
        rows.append(dict(binding_key=key,tenant_id='tenant',role=role,repo_key=repo,
            desk_label=role,source='existing-registration',memory_write_allowed=False))
    catalog.write_text(json.dumps(dict(schema_version='ops.imported-desk-catalog.v1',
        approval_ref='explicit-history-migration',bindings=rows)));catalog.chmod(0o600)
    state=tmp_path/'state';state.mkdir(mode=0o700)
    config=tmp_path/'session.json';config.write_text(json.dumps(dict(schema_version='ops.desk-memory.local.v1',
        state_root=str(state),catalog_path=str(catalog),workspace_root=str(tmp_path),
        provider_instance='claude-local',provider_session_id='actual-verification-session')));config.chmod(0o600)
    initialize(config);admit(config,desk_id=rows[0]['binding_key'],provider_id='anthropic',model_id='opus-unspecified')
    return config, rows


def test_imported_catalog_preserves_coordinates_and_limits(tmp_path):
    config,rows=setup(tmp_path)
    tools=from_local_config(config)
    assert len(tools.call('memory.bindings',{})['bindings'])==2
    _,authority,_,store=components(config)
    assert authority.resolve(tenant_id='tenant',role='Deployment Engineering',repo_key='core').binding_key==rows[1]['binding_key']
    other=rows[1];text='Actual retained deployment evidence.'
    attributes={k:other[k] for k in ('tenant_id','role','repo_key')}
    note=_node('note:original','DeskNote',dict(attributes,text=text,content_digest=hashlib.sha256(text.encode()).hexdigest(),authored_at='2025-01-01T00:00:00Z'))
    binding=_node(other['binding_key'],'Binding',other)
    export=_export(tmp_path,[binding],[note])
    assert import_export(export,store)['imported']==1
    result=tools.call('memory.list',{'kind':'episodes','binding_key':other['binding_key']})
    source=result['entries'][0];stamp=source['read_attribution']
    assert stamp['source_sessions']==[] and stamp['sources'][0]['source_session']=='unknown'
    assert stamp['sources'][0]['capture_session']=='legacy-import'
    assert stamp['sources'][0]['source_authored_at']=='2025-01-01T00:00:00Z'
    assert stamp['source_observed_at']=='unknown'
    EpisodicSearchIndex(store.path.with_name('episode-search.sqlite3'),episode_store=store).rebuild()
    found=tools.call('memory.search',{'query':'retained deployment','binding_key':other['binding_key']})
    hit=found['results'][0]
    assert hit['source_session']=='unknown'
    assert tools.call(hit['next_call']['name'],hit['next_call']['arguments'])['text']==hit['quote']
    with pytest.raises(PermissionError):
        tools.call('memory.propose',{'episode_ids':[source['episode_id']],'items':[],'unresolved_questions':[]})


def test_catalog_conflicting_identity_is_refused(tmp_path):
    config,rows=setup(tmp_path)
    p=tmp_path/'catalog.json';d=json.loads(p.read_text());d['bindings'][1]['role']='Invented'
    p.write_text(json.dumps(d))
    with pytest.raises(ValueError,match='conflicting'):
        ImportedDeskAuthority(p).list_bindings()
