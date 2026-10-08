import copy
import subprocess
import pytest
from kp_agent_tooling._impl.source_citations import cite, validate
# S5 excluded: kp_ops.operation_navigation is OPS-only (not extracted). Its import is disabled so the
# source_citations tests can run; test_projection_exposes_missing_evidence_and_validates_citations is skipped.
# from kp_agent_tooling._impl.operation_navigation import project, validate_view

# The OPS repo fixture (lines 7-14) is verbatim in ops_repo_fixture.py.
from ops_repo_fixture import repo

def test_citation_ignores_worktree_and_detects_wrong_file_blob(repo):
    path,rev=repo;c=cite(path,rev,'client.js','function inspect')
    (path/'client.js').write_text('changed worktree')
    assert validate(path,c)['status']=='valid'
    c['blob_sha']=cite(path,rev,'server.py','def inspect')['blob_sha']
    assert 'blob_sha' in validate(path,c)['mismatches']

@pytest.mark.parametrize('field,value',[('start_line',2),('end_line',99),('excerpt','invented'),('excerpt_sha256','wrong'),('revision','HEAD')])
def test_tampered_citation_is_rejected(repo,field,value):
    path,rev=repo;c=cite(path,rev,'client.js','function inspect');c[field]=value
    assert validate(path,c)['status']=='invalid'

@pytest.mark.parametrize('source_path',['../client.js','/client.js','missing.js'])
def test_unsafe_or_missing_path_is_rejected(repo,source_path):
    path,rev=repo
    with pytest.raises(ValueError):cite(path,rev,source_path,'inspect')

def test_ambiguous_anchor_and_symlink_are_rejected(repo):
    path,rev=repo
    with pytest.raises(ValueError):cite(path,rev,'client.js','')
    (path/'link').symlink_to('client.js')
    (path/'repeat.js').write_text('same\nsame\n')
    subprocess.run(['git','add','.'],cwd=path,check=True);subprocess.run(['git','commit','-qm','new'],cwd=path,check=True)
    new=subprocess.check_output(['git','rev-parse','HEAD'],cwd=path).decode().strip()
    with pytest.raises(ValueError):cite(path,new,'repeat.js','same')
    with pytest.raises(ValueError):cite(path,new,'link','function')

@pytest.mark.skip(reason='S5 excluded: needs OPS-only kp_ops.operation_navigation (project, validate_view)')
def test_projection_exposes_missing_evidence_and_validates_citations(repo):
    path,rev=repo
    model={'schema':'ops.operation-navigation.v1','owner':'OPS','revision':rev,
           'references':[{'id':'client','path':'client.js','anchor':'function inspect'}],
           'operations':[{'id':'inspect','operation':'inspect','adapter':'client','service':'custody','reference_ids':['client']}]}
    v=project(path,model);assert validate_view(path,v)['status']=='valid'
    assert v['operations'][0]['runtime_reachability']=='not-assessed'
    assert v['operations'][0]['execution_evidence']==[]
    missing=copy.deepcopy(v);missing['citations']={}
    assert validate_view(path,missing)['status']=='invalid'
    model['references'][0]['anchor']='absent'
    unresolved=project(path,model)
    assert unresolved['operations'][0]['status']=='unverified'
    assert validate_view(path,unresolved)['status']=='invalid'

def test_qualified_python_symbol_disambiguates_same_method(repo):
    path,rev=repo
    (path/'classes.py').write_text('class A:\n def inspect(self):\n  return 1\nclass B:\n def inspect(self):\n  return 2\n')
    subprocess.run(['git','add','.'],cwd=path,check=True);subprocess.run(['git','commit','-qm','classes'],cwd=path,check=True)
    new=subprocess.check_output(['git','rev-parse','HEAD'],cwd=path).decode().strip()
    with pytest.raises(ValueError):cite(path,new,'classes.py','def inspect')
    c=cite(path,new,'classes.py','def inspect',symbol='B.inspect')
    assert c['start_line']==5 and 'return 2' in c['excerpt']
    assert validate(path,c)['status']=='valid'
