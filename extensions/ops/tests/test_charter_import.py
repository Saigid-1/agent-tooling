import copy,hashlib,uuid
from contextlib import closing
import pytest
from test_portable_desk_memory import episode_store
from kp_agent_tooling._impl.service.desk_profiles import DeskProfiles
from kp_agent_tooling_ops.charter_import_cli import reconcile


def fixture(tmp):
 s=episode_store(tmp);r=DeskProfiles(s,'session-1');r.initialize()
 m={'schema_version':'agent-tooling.charter-import.v1','tenant_id':r.tenant,'sources':[{'text':'Exact charter','sha256':hashlib.sha256(b'Exact charter').hexdigest()}], 'profiles':[{'profile':{'desk_id':'desk:'+str(uuid.uuid4()),'name':'Auditor','role':'Auditor','description':'Report only.','expected_version':0},'aliases':['legacy:auditor']} ]}
 return s,r,m


def test_preview_apply_replay_preserve_admission(tmp_path):
 s,r,m=fixture(tmp_path)
 before=s.sessions.resolve('session-1',s.registry)
 assert reconcile(r,m)['actions'][0]['action']=='create';assert r.list()==[]
 reconcile(r,m,apply=True)
 assert reconcile(r,m,apply=True)['actions'][0]['action']=='unchanged'
 assert s.sessions.resolve('session-1',s.registry)==before
 with closing(s._connect()) as db:
  assert db.execute('SELECT COUNT(*) FROM desk_profiles').fetchone()[0]==1
  assert db.execute('SELECT COUNT(*) FROM desk_charter_imports').fetchone()[0]==1
  assert db.execute('SELECT COUNT(*) FROM desk_charter_aliases').fetchone()[0]==1


def test_digest_tenant_alias_and_stale_profile_refused(tmp_path):
 _,r,m=fixture(tmp_path)
 bad=copy.deepcopy(m);bad['sources'][0]['text']='Changed'
 with pytest.raises(ValueError,match='digest'):reconcile(r,bad,apply=True)
 bad=copy.deepcopy(m);bad['tenant_id']='foreign'
 with pytest.raises(ValueError,match='tenant'):reconcile(r,bad,apply=True)
 reconcile(r,m,apply=True)
 bad=copy.deepcopy(m);bad['profiles'][0]['profile']['desk_id']='desk:'+str(uuid.uuid4())
 with pytest.raises(ValueError,match='alias'):reconcile(r,bad,apply=True)
 bad=copy.deepcopy(m);bad['profiles'][0]['profile']['description']='Changed'
 with pytest.raises(ValueError,match='changed'):reconcile(r,bad,apply=True)


def test_imported_alias_filters_evidence_without_extending_temporal_claim(tmp_path):
 _,r,m=fixture(tmp_path);reconcile(r,m,apply=True)
 base={'source_session_id':None,'binding_key':'legacy:auditor','attribution_status':'resolved','metadata':None,'storage_binding':'legacy:auditor'}
 bounded={**base,'storage_binding':None,'metadata':{'active_claims':[{'predicate':'session.owner','valid_from':'2026-09-01','valid_until':None}]}}
 conflict={**base,'attribution_status':'conflicting'}
 result,gaps=r.filter_episodes({'kind':'agent','value':m['profiles'][0]['profile']['desk_id']},{'authored':base,'bounded':bounded,'conflicting':conflict})
 assert set(result)=={'authored'};assert gaps==1
