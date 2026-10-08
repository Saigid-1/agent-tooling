import hashlib
import json
import subprocess
from pathlib import Path
import pytest
from kp_agent_tooling._impl.service.agent_tooling import AgentTooling
from kp_agent_tooling_ops._impl.review_ledger import specification


def git(root,*args):return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()


@pytest.fixture
def configured(tmp_path):
    repo=tmp_path/'repo';repo.mkdir();git(repo,'init','-q','-b','main');git(repo,'config','user.name','Test');git(repo,'config','user.email','test@example.invalid')
    (repo/'api.py').write_text('from old import thing\nVALUE = 1\n')
    git(repo,'add','.');git(repo,'commit','-qm','initial');sha=git(repo,'rev-parse','HEAD')
    profile=tmp_path/'profile.json';value={'schema_version':'ops.navigation-profile.v1','profile':'dev-current','published_at':'first','repos':{'repo':{'path':str(repo),'revision':sha,'default_ref':'refs/heads/main'}}}
    profile.write_text(json.dumps(value))
    config=tmp_path/'tooling.json';config.write_text(json.dumps({'schema_version':'ops.agent-tooling.v1','repos':{'repo':{'path':str(repo),'revision':sha}},'navigation_profile':str(profile),'review_contracts':{'review':{'specification':specification([{'id':'D8','text':'Empty delta preserves graph.'}]),'approved_rules':[]}}}))
    return AgentTooling(config),repo,profile,value,sha


def test_snapshot_batch_survives_default_advance_and_preserves_exact_imports(configured):
    adapter,repo,profile,value,sha=configured
    snap=adapter.call('navigation.snapshot',{'mode':'capture'})
    (repo/'api.py').write_text('from newer import thing\nVALUE = 2\n');git(repo,'add','.');git(repo,'commit','-qm','new default')
    value['repos']['repo']['revision']=git(repo,'rev-parse','HEAD');profile.write_text(json.dumps(value))
    common={'repo_key':'repo','target_revision':sha}
    r=adapter.call('navigation.batch',{'snapshot_id':snap['snapshot_id'],'requests':[
      {'name':'navigation.source','arguments':dict(common,path='api.py')},
      {'name':'navigation.search','arguments':dict(common,query='VALUE',path_pattern='*.py')},
      {'name':'navigation.imports','arguments':dict(common,path='api.py')} ]})
    assert r['status']=='ok'
    assert 'VALUE = 1' in r['results'][0]['result']['excerpt']
    source=r['results'][0]['result']
    assert source['selected_snapshot_revision']==sha
    assert source['selected_snapshot']['snapshot_id']==snap['snapshot_id']
    assert source['selected_snapshot']['scope']=='frozen-navigation-snapshot'
    assert source['active_navigation_revision']['status']=='observed'
    assert source['active_navigation_revision']['revision']==value['repos']['repo']['revision']
    assert source['active_navigation_revision']['observed_at']
    assert source['default_revision']==sha
    assert source['default_revision_role']=='deprecated alias for selected_snapshot_revision'
    assert r['results'][1]['result']['results'][0]['excerpt']=='VALUE = 1'
    assert r['results'][2]['result']['imports'][0]['module']=='old'
    frozen = adapter.call('navigation.imports',dict(common,path='api.py',snapshot_id=snap['snapshot_id'],target_revision=value['repos']['repo']['revision']))
    assert frozen['reason']=='requested_revision_not_selected' and frozen['selected_revision']==sha
    current = adapter.call('navigation.imports',dict(common,path='api.py'))
    assert current['reason']=='requested_revision_not_selected' and current['selected_revision']==value['repos']['repo']['revision']


def test_review_contract_not_supplied_by_reviewing_agent(configured):
    a,repo,profile,value,sha=configured
    assert a.call('verification.review',{'mode':'schema'})['contract_ids']==['review']
    contract=a.call('verification.review',{'mode':'contract','contract_id':'review'})
    ledger={'schema_version':'ops.review-ledger.v1','specification':contract['specification'],'approved_normalization_rules':[],
            'reviews':[{'requirement_id':'D8','requirement_text':'Empty delta preserves graph.','decision':'unverified','oracle':'Compare both graph projections after an empty delta.','evidence':[],'normalization_rule_ids':[]}]}
    assert a.call('verification.review',{'mode':'validate','contract_id':'review','ledger':ledger})['status']=='valid'
    ledger['reviews'][0]['requirement_text']='Check live workspace drift.'
    r=a.call('verification.review',{'mode':'validate','contract_id':'review','ledger':ledger})
    assert r['status']=='invalid' and any(e['code']=='requirement_text_changed' for e in r['errors'])


def test_batch_errors_are_per_item_and_mutating_tools_forbidden(configured):
    a,repo,profile,value,sha=configured;snap=a.call('navigation.snapshot',{'mode':'capture'})
    r=a.call('navigation.batch',{'snapshot_id':snap['snapshot_id'],'requests':[{'name':'navigation.source','arguments':{'repo_key':'repo','target_revision':'f'*40,'path':'api.py'}},{'name':'navigation.paths','arguments':{'repo_key':'repo','target_revision':sha,'pattern':'*.py'}}]})
    assert r['status']=='partial' and r['results'][1]['result']['results'][0]['path']=='api.py'
    with pytest.raises(Exception):a.call('navigation.batch',{'snapshot_id':snap['snapshot_id'],'requests':[{'name':'verification.review','arguments':{}}]})


def test_search_optional_pattern_covers_root_and_nested_committed_files(configured):
    a,repo,profile,value,sha=configured
    (repo/'nested').mkdir(); (repo/'nested/more.py').write_text('VALUE = 2\n')
    git(repo,'add','.');git(repo,'commit','-qm','nested')
    revision=git(repo,'rev-parse','HEAD')
    args=dict(repo_key='repo',target_revision=revision,query='VALUE')
    omitted=a.call('navigation.search',args)
    explicit=a.call('navigation.search',dict(args,path_pattern='*'))
    assert omitted==explicit
    assert {r['path'] for r in omitted['results']}=={'api.py','nested/more.py'}
    schema=next(t for t in a.tools() if t['name']=='navigation.search')['inputSchema']
    assert schema['properties']['path_pattern']['default']=='*'


@pytest.mark.parametrize('budget,limit', [('file_limit',1),('line_limit',1)])
def test_search_budget_failure_is_actionable_not_absence(configured,monkeypatch,budget,limit):
    import functools
    import kp_agent_tooling._impl.navigation_discovery as discovery
    a,repo,profile,value,sha=configured
    (repo/'other.py').write_text('VALUE = 3\n');git(repo,'add','.');git(repo,'commit','-qm','more')
    revision=git(repo,'rev-parse','HEAD')
    monkeypatch.setattr(discovery,'search',functools.partial(discovery.search,**{budget:limit}))
    result=a.call('navigation.search',dict(repo_key='repo',target_revision=revision,query='VALUE'))
    assert result['status']=='error'
    assert result['reason']=='search_budget_exceeded'
    assert result['budget']['name']==budget and result['budget']['limit']==limit
    assert result['source_revision']==revision and result['path_pattern']=='*'
    assert result['search_complete'] is False
    assert result['absence_verdict']=='not-established'
    assert 'path_pattern' in result['next_action']
    assert 'results' not in result


def test_search_budget_survives_mcp_text_and_structured_transport(configured):
    import asyncio
    import sys
    from datetime import timedelta
    from pathlib import Path
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    a,repo,profile,value,sha=configured
    for filename in ('a.txt','b.txt'):
        (repo/filename).write_text('\n'*110000)
    git(repo,'add','.');git(repo,'commit','-qm','line budget')
    revision=git(repo,'rev-parse','HEAD')
    config=profile.parent/'tooling.json'
    script=Path(__file__).resolve().parents[3]/'packages/tooling/src/kp_agent_tooling/cli.py'

    async def check():
        params=StdioServerParameters(command=sys.executable,args=[str(script),'--config',str(config),'serve'])
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=30)) as session:
                await session.initialize()
                reply=await session.call_tool('navigation.search',dict(repo_key='repo',target_revision=revision,query='VALUE'))
                result=reply.structuredContent
                assert json.loads(reply.content[0].text)==result
                assert result['reason']=='search_budget_exceeded'
                assert result['budget']['name']=='line_limit'
                assert result['absence_verdict']=='not-established'
                assert result['source_revision']==revision
                assert reply.isError
    asyncio.run(check())


def test_search_budget_auto_pages_when_registry_configured(configured,monkeypatch,tmp_path):
    """2026-09-22 trial: a whole-repo literal search over core exceeded the unpaged
    line budget and the arm had to re-issue the paged search by hand. With a
    registry configured, navigation.search now returns that first page itself."""
    import functools
    import kp_agent_tooling._impl.navigation_discovery as discovery
    a,repo,profile,value,sha=configured
    (repo/'other.py').write_text('VALUE = 3\n');git(repo,'add','.');git(repo,'commit','-qm','more')
    revision=git(repo,'rev-parse','HEAD')
    registry=tmp_path/'registry';registry.mkdir()
    config_path=profile.parent/'tooling.json'
    config=json.loads(config_path.read_text());config['navigation_registry_path']=str(registry)
    config_path.write_text(json.dumps(config));a=AgentTooling(config_path)
    monkeypatch.setattr(discovery,'search',functools.partial(discovery.search,line_limit=1))
    page=a.call('navigation.search',dict(repo_key='repo',target_revision=revision,query='VALUE'))
    assert page['status']=='ok'
    assert page['degraded_from']['name']=='navigation.search'
    assert page['degraded_from']['reason']=='search_budget_exceeded'
    assert page['degraded_from']['budget']['name']=='line_limit'
    assert {r['path'] for r in page['results']}=={'api.py','other.py'}
    assert page['traversal_complete'] is True and page['search_complete'] is True
    assert page['source_revision']==revision if 'source_revision' in page else True
    # without a registry the budget failure is unchanged (covered above); a paths
    # budget failure never degrades, because only search has a paged twin
    monkeypatch.setattr(discovery,'paths',functools.partial(discovery.paths,scan_limit=1))
    failed=a.call('navigation.paths',dict(repo_key='repo',target_revision=revision,pattern='*.py'))
    assert failed['status']=='error' and failed['reason']=='search_budget_exceeded'
