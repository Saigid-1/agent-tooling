import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import pytest
from kp_agent_tooling._impl.navigation_workspace import active, workspace, read_source
from kp_agent_tooling._impl.service.agent_tooling import AgentTooling


def checked_status(value, **overrides):
    payload = {
        'status': 'current',
        'checked_at': '2026-09-21T21:31:59Z',
        'source_revisions': {
            key: {field: row[field] for field in ('revision', 'default_ref') if field in row}
            for key, row in value['repos'].items()
        },
        'profile_sha256': __import__('hashlib').sha256(json.dumps(value).encode()).hexdigest(),
    }
    payload.update(overrides)
    return payload


def test_refresh_status_is_qualified_at_response_time(project, tmp_path):
    """GREEN-IF: current is qualified by checked time and becomes stale at the configured boundary."""
    root, profile, cfg, value = project
    status = tmp_path / 'refresh.json'
    value['check_status'] = str(status)
    profile.write_text(json.dumps(value))
    cfg['navigation_refresh_max_age_seconds'] = 30
    status.write_text(json.dumps(checked_status(value)))
    at = datetime(2026, 9, 21, 21, 32, 29, tzinfo=timezone.utc)
    fresh = active(cfg, now=at)['refresh_status']
    assert fresh['status'] == 'current_as_of_check'
    assert fresh['age_seconds'] == 30
    stale = active(cfg, now=datetime(2026, 9, 21, 21, 32, 30, tzinfo=timezone.utc))['refresh_status']
    assert stale['status'] == 'unverified_stale'
    assert stale['age_seconds'] == 31


@pytest.mark.parametrize('payload', [
    {'status': 'current', 'checked_at': None},
    {'status': 'current', 'checked_at': 'not-a-date'},
    {'status': 'current', 'checked_at': '2026-09-21T21:31:59'},
    {'status': 'current', 'checked_at': '2026-09-21T21:33:00Z'},
])
def test_invalid_current_observations_are_never_reported_current(project, tmp_path, payload):
    """GREEN-IF: absent, malformed, naive, and future checks are explicitly unverified."""
    root, profile, cfg, value = project
    status = tmp_path / 'refresh.json'
    value['check_status'] = str(status); profile.write_text(json.dumps(value))
    status.write_text(json.dumps({**checked_status(value), **payload}))
    observed = active(cfg, now=datetime(2026, 9, 21, 21, 32, 0, tzinfo=timezone.utc))['refresh_status']
    assert observed['status'].startswith('unverified_')


def test_failed_refresh_state_and_profile_identity_are_preserved(project, tmp_path):
    """GREEN-IF: freshness decoration preserves failure details and immutable profile identity."""
    root, profile, cfg, value = project
    status = tmp_path / 'refresh.json'
    status.write_text(json.dumps({'status': 'failed', 'checked_at': '2026-09-21T21:31:59Z', 'reason': 'offline'}))
    value['check_status'] = str(status); profile.write_text(json.dumps(value))
    first = active(cfg, now=datetime(2026, 9, 21, 21, 32, tzinfo=timezone.utc))
    second = active(cfg, now=datetime(2026, 9, 21, 21, 33, tzinfo=timezone.utc))
    assert first['refresh_status']['status'] == 'failed'
    assert first['refresh_status']['reason'] == 'offline'
    assert first['profile_sha256'] == second['profile_sha256']


def test_tooling_identity_recomputes_age_without_refresh_lookup(project, tmp_path, monkeypatch):
    """GREEN-IF: two public identity calls recompute age locally while preserving snapshot identity."""
    root, profile, cfg, value = project
    status = tmp_path / 'refresh.json'
    value['check_status'] = str(status); profile.write_text(json.dumps(value))
    status.write_text(json.dumps(checked_status(value)))
    cfg['navigation_refresh_max_age_seconds'] = 30
    config = tmp_path / 'tooling.json'; config.write_text(json.dumps(cfg))
    adapter = AgentTooling(config)
    import kp_agent_tooling._impl.navigation_workspace as navigation_workspace
    class Clock(datetime):
        current = datetime(2026, 9, 21, 21, 32, 29, tzinfo=timezone.utc)
        @classmethod
        def now(cls, tz=None): return cls.current
    monkeypatch.setattr(navigation_workspace, 'datetime', Clock)
    first = adapter.call('tooling.identity', {})['active_navigation_profile']
    Clock.current = datetime(2026, 9, 21, 21, 32, 30, tzinfo=timezone.utc)
    second = adapter.call('tooling.identity', {})['active_navigation_profile']
    assert first['refresh_status']['status'] == 'current_as_of_check'
    assert second['refresh_status']['status'] == 'unverified_stale'
    assert first['profile_sha256'] == second['profile_sha256']


def test_recent_check_for_different_source_identity_is_unverified_without_mutation(project, tmp_path):
    """GREEN-IF: a recent writer-shaped receipt for an older profile cannot attest the active profile."""
    root, profile, cfg, value = project
    status = tmp_path / 'refresh.json'
    value['check_status'] = str(status); profile.write_text(json.dumps(value))
    old = checked_status(value)
    old['source_revisions']['repo']['revision'] = 'f' * 40
    status.write_text(json.dumps(old))
    profile_before = profile.read_bytes(); status_before = status.read_bytes()

    observed = active(cfg, now=datetime(2026, 9, 21, 21, 32, 0, tzinfo=timezone.utc))

    assert observed['refresh_status']['status'] == 'unverified_source_identity'
    assert observed['refresh_status']['age_seconds'] == 1
    assert observed['refresh_status']['source_revisions'] == old['source_revisions']
    assert observed['refresh_status']['expected_source_revisions']['repo']['revision'] == value['repos']['repo']['revision']
    assert profile.read_bytes() == profile_before
    assert status.read_bytes() == status_before


def test_recent_check_for_different_profile_hash_is_unverified_even_when_sources_match(project, tmp_path):
    """GREEN-IF: equal source tips do not let a receipt attest different profile configuration."""
    root, profile, cfg, value = project
    status = tmp_path / 'refresh.json'
    value['check_status'] = str(status); profile.write_text(json.dumps(value))
    receipt = checked_status(value)
    value['request_sha256'] = 'new-build-inputs-with-same-source-tips'
    profile.write_text(json.dumps(value)); status.write_text(json.dumps(receipt))
    observed = active(cfg, now=datetime(2026, 9, 21, 21, 32, 0, tzinfo=timezone.utc))
    assert observed['refresh_status']['status'] == 'unverified_profile_identity'
    assert observed['refresh_status']['profile_sha256'] == receipt['profile_sha256']
    assert observed['refresh_status']['expected_profile_sha256'] == observed['profile_sha256']


def test_published_success_is_bound_to_the_profile_it_published(project, tmp_path):
    """GREEN-IF: a published-success receipt cannot describe a different active publication."""
    root, profile, cfg, value = project
    status = tmp_path / 'refresh.json'
    value['check_status'] = str(status); profile.write_text(json.dumps(value))
    receipt = checked_status(value, status='published')
    receipt['profile_sha256'] = 'f' * 64
    status.write_text(json.dumps(receipt))
    observed = active(cfg, now=datetime(2026, 9, 21, 21, 32, 0, tzinfo=timezone.utc))
    assert observed['refresh_status']['status'] == 'unverified_profile_identity'


def test_current_receipt_requires_complete_writer_shaped_source_identity(project, tmp_path):
    """GREEN-IF: missing repositories or writer identity fields fail closed while failures stay explicit."""
    root, profile, cfg, value = project
    status = tmp_path / 'refresh.json'; value['check_status'] = str(status); profile.write_text(json.dumps(value))
    now = datetime(2026, 9, 21, 21, 32, 0, tzinfo=timezone.utc)
    for source_revisions in ({}, {'repo': None},
                             {'repo': {'revision': value['repos']['repo']['revision']}}):
        status.write_text(json.dumps(checked_status(value, source_revisions=source_revisions)))
        assert active(cfg, now=now)['refresh_status']['status'] == 'unverified_source_identity'
    status.write_text(json.dumps({
        'status': 'refresh_failed', 'checked_at': '2026-09-21T21:31:59Z',
        'error_type': 'Offline', 'source_revisions': {},
    }))
    assert active(cfg, now=now)['refresh_status']['status'] == 'refresh_failed'


@pytest.mark.parametrize('threshold', [0, -1, float('inf'), True, '30'])
def test_refresh_age_threshold_requires_a_finite_positive_number(project, threshold):
    """GREEN-IF: invalid configured freshness thresholds fail closed."""
    root, profile, cfg, value = project
    cfg['navigation_refresh_max_age_seconds'] = threshold
    with pytest.raises(ValueError, match='refresh age threshold'):
        active(cfg)


def git(path,*args):
    return subprocess.check_output(['git','-C',str(path),*args],text=True).strip()


@pytest.fixture
def project(tmp_path):
    root=tmp_path/'repo';root.mkdir()
    git(root,'init','-q','-b','main');git(root,'config','user.name','Test');git(root,'config','user.email','test@example.invalid')
    (root/'a.py').write_text('VALUE = 1\n');git(root,'add','a.py');git(root,'commit','-qm','base')
    sha=git(root,'rev-parse','HEAD')
    profile=tmp_path/'profile.json'
    value={'schema_version':'ops.navigation-profile.v1','profile':'dev-current','published_at':'fixture',
           'repos':{'repo':{'path':str(root),'revision':sha,'default_ref':'refs/heads/main'}},
           'workspace_repositories':{'repo':[str(root)]}}
    profile.write_text(json.dumps(value))
    cfg={'schema_version':'ops.agent-tooling.v1','repos':{'repo':{'path':str(root),'revision':sha}},'navigation_profile':str(profile)}
    return root,profile,cfg,value


def test_workspace_overlap_and_exact_branch_read(project,tmp_path):
    root,profile,cfg,value=project
    left=tmp_path/'left';right=tmp_path/'right'
    git(root,'worktree','add','-qb','left',str(left));git(root,'worktree','add','-qb','right',str(right))
    (left/'a.py').write_text('VALUE = 2\n');git(left,'add','a.py');git(left,'commit','-qm','in flight')
    target=git(left,'rev-parse','HEAD');(right/'a.py').write_text('VALUE = 3\n')
    report=workspace(cfg,'repo')
    assert len(report['worktrees'])==3
    assert len(report['overlaps'])==1
    assert report['overlaps'][0]['paths']==['a.py']
    assert report['overlaps'][0]['verdict']=='potential_file_overlap'
    current=read_source(cfg,'repo',target,'a.py');assert current['excerpt']=='VALUE = 2'
    baseline=read_source(cfg,'repo',value['repos']['repo']['revision'],'a.py');assert baseline['excerpt']=='VALUE = 1'
    assert read_source(cfg,'repo','f'*40,'a.py')['status']=='unavailable'
    limited=workspace(cfg,'repo',limit=1);assert limited['omitted_worktrees']==2


def test_profile_is_read_each_call_but_evidence_config_stays_pinned(project,tmp_path):
    root,profile,cfg,value=project
    config=tmp_path/'tooling.json';config.write_text(json.dumps(cfg));a=AgentTooling(config)
    original=cfg['repos']['repo']['revision']
    (root/'a.py').write_text('VALUE = 2\n');git(root,'add','a.py');git(root,'commit','-qm','new default')
    new=git(root,'rev-parse','HEAD');value['repos']['repo']['revision']=new;profile.write_text(json.dumps(value))
    identity=a.call('tooling.identity',{})
    assert identity['active_navigation_source_revisions']['repo']==new
    assert identity['configured_navigation_source_revisions']['repo']==original
    assert a.config['repos']['repo']['revision']==original
    result=a.call('navigation.source',{'repo_key':'repo','target_revision':new,'path':'a.py'})
    assert result['source_revision']==new and result['excerpt']=='VALUE = 2'
    assert result['selected_snapshot_revision']==new
    assert result['selected_snapshot']['scope']=='explicit-requested-revision'
    assert result['active_navigation_revision']['revision']==new
    assert result['active_navigation_revision']['status']=='observed'
    assert result['default_revision']==new
    assert result['default_revision_role']=='deprecated alias for selected_snapshot_revision'


def test_source_without_published_profile_does_not_call_configured_pin_active(project):
    root,profile,cfg,value=project
    cfg.pop('navigation_profile')
    revision=cfg['repos']['repo']['revision']
    result=read_source(cfg,'repo',revision,'a.py')
    assert result['selected_snapshot_revision']==revision
    assert result['selected_snapshot']['scope']=='explicit-requested-revision'
    assert result['active_navigation_revision']=={
        'status':'not_observed','reason':'navigation_profile_not_configured',
        'observed_at':result['active_navigation_revision']['observed_at'],
        'scope':'live-navigation-profile-observation'}


def test_invalid_profile_never_silently_uses_historical_pins(project):
    root,profile,cfg,value=project
    value['repos']['other']=dict(value['repos']['repo']);profile.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='membership'):active(cfg)


def test_handoff_refuses_receipt_from_superseded_profile(project,tmp_path,monkeypatch):
    root,profile,cfg,value=project
    config=tmp_path/'tooling.json';config.write_text(json.dumps(cfg));a=AgentTooling(config)
    # This test isolates the handoff's context binding, independent of evidence validation.
    from kp_agent_tooling_ops._impl.verification_finding import finding_digest
    artifact={'schema_version':'ops.verification-finding.v5','claim':'fixture'}
    digest=finding_digest(artifact)
    a.validation_receipts[digest]={'status':'valid','validation_profile_sha256':active(cfg)['profile_sha256']}
    args={'final_text':json.dumps(artifact),'expected_digests':[digest]}
    assert a.call('verification.handoff',args)['status']=='valid'
    value['published_at']='new publication';profile.write_text(json.dumps(value))
    failed=a.call('verification.handoff',args)
    assert failed['errors'][0]['code']=='validation_context_changed'
    # Explicit retained-evidence validation is independent of dev-current refresh.
    a.validation_receipts[digest]={'status':'valid','validation_profile_sha256':None}
    assert a.call('verification.handoff',args)['status']=='valid'


def test_focused_workspace_does_not_claim_unrelated_changes(project):
    root,profile,cfg,value=project
    (root/'a.py').write_text('VALUE = 3\n')
    assert len(workspace(cfg,'repo',path='a.py')['worktrees'])==1
    result=workspace(cfg,'repo',path='another.py')
    assert result['worktrees']==[] and result['filtered_worktrees']==1
    with pytest.raises(ValueError):workspace(cfg,'repo',path='../outside')


def test_catalog_cannot_drift_from_published_profile(project,tmp_path):
    import hashlib
    root,profile,cfg,value=project
    cat=tmp_path/'catalog.json';cat.write_text(json.dumps({'repositories':{'repo':{'path':str(root),'ref':'f'*40}},'platforms':{}}))
    value.update(knowledge_config=str(cat),knowledge_config_sha256=hashlib.sha256(cat.read_bytes()).hexdigest());profile.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='source differs'):active(cfg)
    cat.write_text('{}')
    with pytest.raises(ValueError,match='identity mismatch'):active(cfg)


def test_retained_evidence_membership_is_independent(project,tmp_path,monkeypatch):
    root,profile,cfg,value=project
    original=dict(cfg['repos'])
    cfg['retained_evidence_repos']=original
    cfg['repos']=dict(original,additional=dict(original['repo']))
    value['repos']=dict(cfg['repos']);profile.write_text(json.dumps(value))
    config=tmp_path/'tooling.json';config.write_text(json.dumps(cfg));adapter=AgentTooling(config)
    seen=[]
    def validate(config,finding,**kwargs):
        seen.append(config['repos'])
        return {'status':'valid','finding_sha256':'a'*64,'errors':[]}
    monkeypatch.setattr('kp_agent_tooling_ops._impl.verification_finding.validate_finding',validate)
    adapter.call('verification.finding',{'mode':'validate','finding':{},'source_profile':'retained-evidence'})
    adapter.call('verification.finding',{'mode':'validate','finding':{}})
    assert set(seen[0])=={'repo'}
    assert set(seen[1])=={'repo','additional'}
    assert adapter.evidence_config['repos']==original


def test_generated_dirt_is_reported_but_never_an_overlap(tmp_path):
    """Two worktrees that both ran `npm ci` share untracked node_modules; that is not a conflict."""
    import json, subprocess
    from kp_agent_tooling._impl.navigation_workspace import workspace
    def git(root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()
    source = tmp_path / 'source'; source.mkdir()
    git(source, 'init', '-q', '-b', 'main'); git(source, 'config', 'user.name', 't'); git(source, 'config', 'user.email', 't@t')
    (source / 'a.py').write_text('x = 1\n'); git(source, 'add', '.'); git(source, 'commit', '-qm', 'base')
    revision = git(source, 'rev-parse', 'HEAD')
    for name in ('one', 'two'):
        wt = tmp_path / name
        git(source, 'worktree', 'add', '-q', '--detach', str(wt), revision)
        (wt / 'service' / 'node_modules' / 'pkg').mkdir(parents=True)
        (wt / 'service' / 'node_modules' / 'pkg' / 'index.js').write_text('1')
    (tmp_path / 'one' / 'a.py').write_text('x = 2\n')
    (tmp_path / 'two' / 'a.py').write_text('x = 3\n')
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'schema_version': 'ops.navigation-profile.v1', 'profile': 'fixture',
        'repos': {'repo': {'path': str(source), 'revision': revision}},
        'workspace_repositories': {'repo': [str(source)]}}))
    report = workspace({'repos': {'repo': {'path': str(source), 'revision': revision}}, 'navigation_profile': str(profile)}, 'repo')
    rows = {row['worktree']: row for row in report['worktrees']}
    one = rows[str((tmp_path / 'one').resolve())]
    assert one['changed_paths'] == ['a.py']
    assert one['untracked_generated_count'] == 1 and one['untracked_generated'] == ['service/']
    assert [o['paths'] for o in report['overlaps']] == [['a.py']]


def test_direct_source_selects_requested_revision_separately_from_profile(project):
    root,profile,cfg,value=project
    old=value['repos']['repo']['revision']
    (root/'a.py').write_text('VALUE = 3\n');git(root,'add','a.py');git(root,'commit','-qm','later')
    latest=git(root,'rev-parse','HEAD');value['repos']['repo']['revision']=latest
    profile.write_text(json.dumps(value))
    result=read_source(cfg,'repo',old,'a.py')
    assert result['source_revision']==result['requested_revision']==result['selected_snapshot_revision']==old
    assert result['profile_revision_at_read']==latest
    assert result['active_navigation_revision']['revision']==latest
    assert result['selected_snapshot']['scope']=='explicit-requested-revision'
