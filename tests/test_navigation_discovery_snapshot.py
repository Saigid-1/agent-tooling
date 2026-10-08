import json
import hashlib
import subprocess

import pytest

from kp_agent_tooling._impl.navigation_discovery import paths, search
from kp_agent_tooling._impl.navigation_snapshot import capture, select, target_revision


def git(root,*args):
    return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()


@pytest.fixture
def setup(tmp_path):
    repo=tmp_path/'repo';repo.mkdir()
    git(repo,'init','-q','-b','main');git(repo,'config','user.name','Test');git(repo,'config','user.email','test@example.invalid')
    (repo/'old.py').write_text('needle = 1\n')
    git(repo,'add','.');git(repo,'commit','-qm','old')
    old=git(repo,'rev-parse','HEAD')
    git(repo,'mv','old.py','new.py');(repo/'new.py').write_text('needle = 2\n')
    git(repo,'add','.');git(repo,'commit','-qm','new')
    new=git(repo,'rev-parse','HEAD')
    profile=tmp_path/'profile.json'
    profile.write_text(json.dumps({'schema_version':'ops.navigation-profile.v1','profile':'dev-current',
        'repos':{'repo':{'path':str(repo),'revision':old}},'published_at':'fixture'}))
    config={'repos':{'repo':{'path':str(repo),'revision':old}},'navigation_profile':str(profile)}
    return repo,profile,config,old,new


def test_discovery_is_committed_bounded_and_revision_pinned(setup):
    repo,profile,config,old,new=setup
    (repo/'new.py').write_text('dirty secret\n')
    assert [x['path'] for x in paths(config,'repo',old)['results']]==['old.py']
    assert [x['path'] for x in paths(config,'repo',new)['results']]==['new.py']
    hit=search(config,'repo',new,'needle')['results'][0]
    assert hit['path']=='new.py' and hit['line']==1 and hit['excerpt']=='needle = 2'
    assert search(config,'repo',new,'dirty')['results']==[]
    assert search(config,'repo',new,'needle',max_blob_bytes=1)['truncated']
    with pytest.raises(ValueError,match='budget'):paths(config,'repo',new,scan_limit=0)
    with pytest.raises(Exception):paths(config,'repo','f'*40)


def test_snapshot_survives_profile_advance(setup,tmp_path):
    repo,profile,config,old,new=setup
    registry=tmp_path/'registry';registry.mkdir()
    frozen=capture(config,registry)
    value=json.loads(profile.read_text());value['repos']['repo']['revision']=new;profile.write_text(json.dumps(value))
    derived,selection=select(config,registry,frozen['snapshot_id'])
    assert target_revision(selection,'repo')==old
    assert paths(derived,'repo',old)['results'][0]['path']=='old.py'
    with pytest.raises(ValueError,match='differs'):target_revision(selection,'repo',new)
    with pytest.raises(ValueError):select(config,registry,'navigation-snapshot:sha256:'+'f'*64)


def test_snapshot_detects_tamper_and_provider_drift(setup,tmp_path):
    repo,profile,config,old,new=setup
    (repo/'pyrightconfig.json').write_text('{"typeCheckingMode":"strict"}')
    registry=tmp_path/'registry';registry.mkdir()
    frozen=capture(config,registry)
    (repo/'pyrightconfig.json').write_text('{}')
    with pytest.raises(ValueError,match='provider inputs changed'):
        select(config,registry,frozen['snapshot_id'])
    (repo/'pyrightconfig.json').write_text('{"typeCheckingMode":"strict"}')
    selection_file=registry/('selection-'+frozen['snapshot_id'].split(':')[-1]+'.json')
    selection_file.write_text('{}')
    with pytest.raises(ValueError,match='identity mismatch'):
        select(config,registry,frozen['snapshot_id'])


def test_glob_search_and_profile_failure(setup):
    repo,profile,config,old,new=setup
    result=search(config,'repo',new,'needle*2',mode='glob',path_pattern='*.py')
    assert result['results'][0]['path']=='new.py'
    assert paths(config,'repo',new,pattern='*.md')['results']==[]
    profile.write_text('{}')
    with pytest.raises(ValueError):search(config,'repo',new,'needle')


def test_binary_symlink_long_line_and_budgets(setup):
    repo,profile,config,old,new=setup
    (repo/'long.txt').write_text('x'*600+' target\nsecond target\n')
    (repo/'binary.bin').write_bytes(b'prefix\0target\n')
    (repo/'linked.txt').symlink_to('long.txt')
    git(repo,'add','.');git(repo,'commit','-qm','mixed inputs')
    revision=git(repo,'rev-parse','HEAD')
    listed=paths(config,'repo',revision)
    assert listed['excluded_nonregular_entries']==1
    assert 'linked.txt' not in [x['path'] for x in listed['results']]
    hit=search(config,'repo',revision,'target',path_pattern='*.txt',limit=1)
    assert hit['omitted_results']==1 and hit['truncated']
    assert hit['shortened_excerpts']==1 and hit['results'][0]['excerpt_truncated']
    assert hit['results'][0]['line']==1
    binary=search(config,'repo',revision,'target',path_pattern='*.bin')
    assert binary['results']==[] and binary['skipped_blobs']==1 and binary['truncated']
    with pytest.raises(ValueError,match='search budget'):
        search(config,'repo',revision,'target',file_limit=1)
    with pytest.raises(ValueError,match='line budget'):
        search(config,'repo',revision,'target',path_pattern='*.txt',line_limit=1)
    with pytest.raises(ValueError,match='glob'):
        paths(config,'repo',revision,pattern='../outside')


def test_fixed_pins_discovery_and_snapshot_requires_profile(setup,tmp_path):
    repo,profile,config,old,new=setup
    config.pop('navigation_profile')
    assert paths(config,'repo',old)['results'][0]['path']=='old.py'
    registry=tmp_path/'registry';registry.mkdir()
    with pytest.raises(ValueError,match='published navigation profile'):
        capture(config,registry)


def test_selection_removes_unfrozen_provider_config(setup,tmp_path):
    repo,profile,config,old,new=setup
    registry=tmp_path/'registry';registry.mkdir()
    frozen=capture(config,registry)
    config['typescript']={'module':'/unexpected/new/provider.js'}
    derived,_=select(config,registry,frozen['snapshot_id'])
    assert 'typescript' not in derived


def test_selection_restores_frozen_import_mapping(setup,tmp_path):
    repo,profile,config,old,new=setup
    config['import_mappings']={'package_root':{'repo_key':'repo','distribution':'package-dist'}}
    registry=tmp_path/'registry';registry.mkdir()
    frozen=capture(config,registry)
    config['import_mappings']={}
    derived,selection=select(config,registry,frozen['snapshot_id'])
    assert derived['import_mappings']=={
        'package_root':{'repo_key':'repo','distribution':'package-dist'}}
    assert selection['analysis_config_sha256']==frozen['analysis_config_sha256']


def test_legacy_snapshot_does_not_inherit_host_import_mapping(setup,tmp_path):
    """GREEN-IF a pre-mapping snapshot cannot acquire current host mappings."""
    repo,profile,config,old,new=setup
    registry=tmp_path/'registry';registry.mkdir()
    frozen=capture(config,registry)
    selection_path=registry/('selection-'+frozen['snapshot_id'].rsplit(':',1)[1]+'.json')
    legacy=json.loads(selection_path.read_text())
    legacy['analysis_config'].pop('import_mappings')
    legacy['analysis_config_sha256']=hashlib.sha256(json.dumps(
        legacy['analysis_config'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    payload=json.dumps(legacy,sort_keys=True,separators=(',',':')).encode()
    digest=hashlib.sha256(payload).hexdigest()
    (registry/('selection-'+digest+'.json')).write_bytes(payload)
    config['import_mappings']={'package_root':{'repo_key':'repo','distribution':'package-dist'}}
    derived,_=select(config,registry,'navigation-snapshot:sha256:'+digest)
    assert 'import_mappings' not in derived


def test_search_reports_each_exclusion_reason_and_bounded_details(setup):
    repo, profile, config, old, new = setup
    (repo/'binary.bin').write_bytes(b'needle\0')
    (repo/'encoding.bin').write_bytes(b'needle\xff')
    (repo/'large.txt').write_text('needle'*100)
    for n in range(22):
        (repo/f'excluded{n}.bin').write_bytes(b'\0')
    git(repo,'add','.'); git(repo,'commit','-qm','exclusion diagnostics')
    result = search(config,'repo',git(repo,'rev-parse','HEAD'),'needle',max_blob_bytes=100)
    assert result['coverage']['exclusion_counts'] == {'binary_nul':23,'invalid_utf8':1,'max_blob_bytes':1}
    assert len(result['coverage']['exclusions']) == 20
    assert result['coverage']['omitted_exclusion_details'] == 5
    assert result['coverage']['traversal_complete'] is True
    assert result['search_complete'] is False
    assert result['absence_verdict'] == 'not-established'
    assert 'does not establish time-budget exhaustion' in result['message']
    assert 'navigation.search_page' in result['next_action']
    complete = search(config,'repo',new,'needle')
    assert complete['search_complete'] is True
    assert complete['coverage']['exclusion_counts'] == {}
