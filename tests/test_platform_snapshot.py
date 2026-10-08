import subprocess
import pytest
from ops_repo_fixture import repo
from kp_agent_tooling._impl.platform_snapshot import inspect_platform


def model(rev):
    return dict(schema='ops.platform-request.v1', owner='OPS', profile='local-test',
        sources={'ops':dict(revision=rev, artifacts=[dict(path='client.js', role='dependency')]),
                 'core':dict(revision=rev, artifacts=[dict(path='server.py', role='configuration')])})


def test_two_repositories_get_stable_identity_without_deployment_claim(repo):
    root, rev = repo
    r = inspect_platform(model(rev), {'ops':root, 'core':root})
    assert r['status']=='ok'
    assert r['snapshot']['sources']['ops']['revision']==rev
    assert r['stages']['built']==r['stages']['deployed']==r['stages']['observed']=='not-assessed'
    assert r['snapshot_id']==inspect_platform(model(rev), {'core':root, 'ops':root})['snapshot_id']


def test_dirty_worktree_is_separate_from_committed_snapshot(repo):
    root,rev=repo;m=model(rev);before=inspect_platform(m,{'ops':root,'core':root})
    (root/'client.js').write_text('dirty')
    after=inspect_platform(m,{'ops':root,'core':root})
    assert after['status']=='review_required'
    assert before['snapshot_id']==after['snapshot_id']
    assert after['checkouts']['ops']['dirty'] is True


def test_unavailable_repository_never_produces_complete_snapshot(repo):
    root,rev=repo;r=inspect_platform(model(rev),{'ops':root})
    assert r['status']=='partial' and r['snapshot_id'] is None
    assert r['checkouts']['core']['status']=='unavailable'


def test_revision_alias_refused(repo):
    with pytest.raises(ValueError):inspect_platform(model('HEAD'),{})


def test_content_change_changes_snapshot(repo):
    root,rev=repo;first=inspect_platform(model(rev),{'ops':root,'core':root})
    (root/'server.py').write_text('def inspect():\n return changed()\n')
    subprocess.run(['git','add','.'],cwd=root,check=True);subprocess.run(['git','commit','-qm','change'],cwd=root,check=True)
    newer=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    assert inspect_platform(model(newer),{'ops':root,'core':root})['snapshot_id']!=first['snapshot_id']
    old=inspect_platform(model(rev),{'ops':root,'core':root})
    assert old['snapshot_id']==first['snapshot_id'] and old['status']=='review_required'


def test_unknown_fields_and_traversal_rejected(repo):
    m=model(repo[1]);m['deployed']=True
    with pytest.raises(ValueError):inspect_platform(m,{})
    m=model(repo[1]);m['sources']['ops']['artifacts'][0]['path']='../outside'
    with pytest.raises(ValueError):inspect_platform(m,{})
