import json
import subprocess
from pathlib import Path
import pytest
from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeService, KnowledgeRequestError
from test_knowledge_service import setup, Reader, Hit


def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()

def commit(repo,message):
    git(repo,'add','.');git(repo,'-c','user.name=t','-c','user.email=t@t','commit','-qm',message)
    return git(repo,'rev-parse','HEAD')

@pytest.fixture
def case(setup):
    repo,config=setup
    (repo/'docs').mkdir(exist_ok=True);(repo/'docs/guide.md').write_text('Maintained guidance')
    baseline=commit(repo,'guide')
    refs=[{'id':id,'role':role,'path':path,'blob_sha':git(repo,'rev-parse',baseline+':'+path)} for id,role,path in [('guide','architecture','docs/guide.md'),('cli','entrypoint','scripts/find.py')]]
    (repo/'manifest.json').write_text(json.dumps({'schema_version':'ops.capability-map.v1','capability_id':'cap','summary':'sample','source_revision':baseline,'references':refs}))
    catalog=commit(repo,'map')
    cfg=config['repositories']['repo'];cfg.update(ref=catalog,capabilities={'cap':'manifest.json'},entry_symbols={'cap':'scripts.find.main'},artifacts={'docs/guide.md':{'status':'maintained','owner':'OPS'}})
    hit=Hit(path='docs/guide.md',blob_sha=refs[0]['blob_sha'],text='Maintained guidance')
    return repo,config,catalog,baseline,hit

class Nav:
    def __init__(self):self.calls=[]
    def inspect(self,repository,revision,symbol):
        self.calls.append((repository,revision,symbol))
        return {'status':'ok','evidence_kind':'static_ast','source_revision':revision,'tool_revision':'a'*40,'report':{'edges':[]},'limitations':['Not runtime evidence.']}

def run(case,nav=None,reader=None,target=None):
    repo,config,catalog,baseline,hit=case
    s=KnowledgeService(config,lambda:reader or Reader([hit]),navigation_provider=nav or Nav())
    return s.execute('context',{'repo_key':'repo','capability_id':'cap','target_revision':target or catalog})

def test_target_context_composes_and_preserves_revision_roles(case):
    nav=Nav();r=run(case,nav=nav)
    assert r['status']=='ok'
    d=r['data'];assert d['target_revision']==case[2] and d['catalog_revision']==case[2]
    assert d['references']['source_revision']==case[3]
    assert d['references']['checked_revision']==case[2]
    assert d['guidance'][0]['target_status']=='unchanged'
    assert d['guidance'][0]['owner']=='OPS'
    assert d['working_tree']=='excluded' and d['runtime_trace']=='not-assessed'
    assert nav.calls[0][1]==case[2]

def test_changed_target_uses_accepted_manifest_not_target_repin(case):
    repo=case[0];(repo/'scripts/find.py').write_text('"""Changed behavior"""\n')
    (repo/'manifest.json').write_text('malicious invalid replacement')
    target=commit(repo,'change');r=run(case,target=target)
    assert r['status']=='review_required'
    assert [x['status'] for x in r['data']['references']['references']]==['unchanged','changed']
    assert r['data']['target_revision']==target

@pytest.mark.parametrize('target',['HEAD','--help','a'*39,'A'*40])
def test_ref_aliases_refused_before_backend(case,target):
    with pytest.raises(KnowledgeRequestError):run(case,target=target)

def test_missing_target_is_error_without_backend(case):
    def no():pytest.fail('backend accessed')
    s=KnowledgeService(case[1],no,navigation_provider=Nav())
    r=s.execute('context',{'repo_key':'repo','capability_id':'cap','target_revision':'0'*40})
    assert r['status']=='error' and r['source_revision'] is None

def test_backend_failure_does_not_erase_reference_evidence(case):
    class Broken:
        def recall(self,**kwargs):raise RuntimeError('secret')
    r=run(case,reader=Broken());assert r['status']=='partial'
    assert r['data']['references']['status']=='current'
    assert 'retrieval_unavailable' in r['data']['gaps']
    assert 'secret' not in json.dumps(r)

def test_stale_navigation_is_not_presented_as_target_evidence(case):
    class Stale(Nav):
        def inspect(self,*args):return {'status':'ok','source_revision':'a'*40}
    r=run(case,nav=Stale());assert r['status']=='partial'
    assert r['data']['navigation']['status']=='unavailable'

def test_historical_unknown_and_changed_guidance_are_explicit(case):
    config=case[1];config['repositories']['repo']['artifacts']['docs/old.md']={'status':'superseded','owner':'OPS','replacement':'docs/guide.md'}
    hits=[case[4],Hit(path='docs/old.md'),Hit(path='docs/random.md')]
    repo=case[0];(repo/'docs/guide.md').write_text('new guide');target=commit(repo,'guide change')
    r=run(case,reader=Reader(hits),target=target);d=r['data']
    assert r['status']=='review_required' and d['guidance']==[]
    assert 'indexed_guidance_stale_for_target' in d['gaps']
    assert d['excluded_evidence'] == []  # No excluded source metadata leaves admission.
    assert all('text' not in x for x in d['excluded_evidence'])

def test_packet_bounds_keep_gap_and_revision_evidence(case):
    hit=case[4];hit.text='x'*30000
    r=run(case,reader=Reader([hit]*5))
    assert len((json.dumps(r,ensure_ascii=True)+'\n').encode())<=32768
    assert r['omitted']>0 and r['data']['target_revision']==case[2]
    assert 'output_omitted' in r['data']['gaps']






def test_large_next_steps_and_discovery_counts_are_bounded():
    from kp_agent_tooling_ops._impl.service.knowledge_context import _bound
    path='/'.join(['界'*70]*3)
    data={'target_revision':'a'*40,'gaps':[], 'next_steps':[{'action':'review_reference','path':path+str(i),'target_revision':'a'*40} for i in range(32)],'discovery':{'available':3,'omitted':0,'results':[{'text':'x'*16000}]*3},'reference_summary':{'changed':32}}
    report={'status':'review_required','data':data,'omitted':0}
    result=_bound(report)
    assert len((json.dumps(result,ensure_ascii=True)+'\n').encode())<=32768
    assert data['reference_summary']=={'changed':32}
    assert data['discovery']['omitted']==data['discovery']['available']-len(data['discovery']['results'])
    assert 'output_omitted' in data['gaps']


def test_malformed_retrieval_rows_preserve_reference_report(case):
    r=run(case,reader=Reader([{'text':'malformed'}]))
    assert r['status']=='partial'
    assert r['data']['references']['status']=='current'
    assert 'retrieval_unavailable' in r['data']['gaps']


def test_annotated_tag_baseline_is_not_silently_peeled(case):
    repo,config,catalog,baseline,hit=case
    git(repo,'-c','user.name=t','-c','user.email=t@t','tag','-a','map-baseline',baseline,'-m','tag')
    manifest=json.loads((repo/'manifest.json').read_text());manifest['source_revision']=git(repo,'rev-parse','map-baseline')
    (repo/'manifest.json').write_text(json.dumps(manifest));new=commit(repo,'tag baseline');config['repositories']['repo']['ref']=new
    r=run(case,target=new)
    assert r['status']=='error' and 'manifest_unavailable' in r['data']['gaps']


def test_context_passes_only_maintained_manifest_paths_to_reader(case):
    class ScopedReader:
        def __init__(self):self.calls=[]
        def recall(self,**kwargs):
            self.calls.append(kwargs)
            # A corpus whose first global page is dominated by other capabilities.
            return [case[4]] if kwargs.get('paths')==('docs/guide.md',) else [Hit(path='docs/elsewhere.md')]*5
    reader=ScopedReader();r=run(case,reader=reader)
    assert r['status']=='ok'
    assert reader.calls[0]['paths']==('docs/guide.md',)
    assert r['data']['guidance'][0]['path']=='docs/guide.md'

from knowledge_lifecycle_fixture import isolated_lifecycle


def test_serena_configuration_composes_through_context(case, monkeypatch):
    repo, config, revision, _, _ = case
    config['navigation'] = {'provider': 'serena', 'command': '/configured/serena', 'python': '/configured/python'}
    config['repositories']['repo']['entry_symbols']['cap'] = 'scripts/find.py::main'
    calls = []
    def inspect(self, repository, target, symbol):
        calls.append((target, symbol))
        return dict(status='ok', evidence_kind='language_server', source_revision=target,
                    report={'status': 'no_results', 'absence_verdict': 'not-established'})
    monkeypatch.setattr('kp_agent_tooling._impl.service.serena_navigation.SerenaNavigationProvider.inspect', inspect)
    service = KnowledgeService(config, lambda: Reader([case[4]]))
    report = service.execute('context', dict(repo_key='repo', capability_id='cap', target_revision=revision))
    assert calls == [(revision, 'scripts/find.py::main')]
    assert report['data']['navigation']['evidence_kind'] == 'language_server'
    assert report['data']['navigation']['report']['absence_verdict'] == 'not-established'


def test_context_preserves_catalog_exclusion_reason(case):
    result = run(case, target=case[3])
    assert result['data']['guidance'] == []
    assert any(row['reason'] == 'configured_catalog_not_ancestor_of_target'
               for row in result['data']['guidance_exclusions'])


def test_context_preserves_published_navigation_failure_reason(case):
    class Unavailable:
        def inspect(self, repo, revision, symbol):
            return {'status':'unavailable','source_revision':revision,'provider':'published_scip',
                    'reason':'target_not_in_published_profile', 'published_revision':'0'*40}
    result = run(case, nav=Unavailable())
    assert result['data']['navigation']['reason'] == 'target_not_in_published_profile'
    assert 'navigation_unavailable' in result['data']['gaps']


def test_provider_exception_is_not_reported_as_missing_configuration(case):
    class Broken:
        def inspect(self, *args):
            raise RuntimeError('private backend details')
    result = run(case, nav=Broken())
    assert result['data']['navigation']['reason'] == 'navigation_provider_failed'
    assert 'private backend' not in json.dumps(result)
