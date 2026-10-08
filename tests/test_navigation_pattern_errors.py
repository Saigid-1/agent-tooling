import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from kp_agent_tooling._impl.navigation_discovery import search, DiscoveryBudgetExceeded
from kp_agent_tooling._impl.service.agent_tooling import AgentTooling
from test_navigation_discovery_snapshot import setup,git


def adapter_for(config,tmp_path):
    registry=tmp_path/'search-registry';registry.mkdir(exist_ok=True)
    path=tmp_path/'tooling.json'
    path.write_text(json.dumps(dict(config,schema_version='ops.agent-tooling.v1',
        navigation_registry_path=str(registry),
        enabled_tools=['navigation.search','navigation.search_page'])))
    return AgentTooling(path),path


def test_recursive_pattern_accepted_and_root_semantics_explicit(setup):
    repo,_,config,_,_=setup
    (repo/'kp_core').mkdir()
    (repo/'kp_core'/'module.py').write_text('needle\n')
    git(repo,'add','.');git(repo,'commit','-qm','nested')
    revision=git(repo,'rev-parse','HEAD')
    assert [r['path'] for r in search(config,'repo',revision,'needle',path_pattern='**/*.py')['results']]==['kp_core/module.py']
    assert len(search(config,'repo',revision,'needle',path_pattern='*.py')['results'])==2


def test_budget_error_has_executable_same_scope_paged_recovery(setup,tmp_path,monkeypatch):
    repo,_,config,_,_=setup
    (repo/'pkg').mkdir()
    (repo/'pkg'/'one.py').write_text('needle\n'*3)
    git(repo,'add','.');git(repo,'commit','-qm','nested')
    revision=git(repo,'rev-parse','HEAD')
    adapter,_=adapter_for(config,tmp_path)
    import kp_agent_tooling._impl.navigation_discovery as discovery
    original=discovery.search
    monkeypatch.setattr(discovery,'search',lambda *a,**kw:original(*a,**kw,line_limit=1))
    result=adapter.call('navigation.search',{'repo_key':'repo','target_revision':revision,
        'query':'needle','path_pattern':'**/*.py','limit':1})
    # With a registry configured (adapter_for provides one) the budget failure is no
    # longer handed back for the caller to re-issue: page one of the same-scope paged
    # search arrives directly, carrying the budget that forced the degrade (2026-09-22).
    assert result['status']=='incomplete' and result.get('reason') is None
    assert result['absence_verdict']=='not-established'
    assert result['degraded_from']['reason']=='search_budget_exceeded'
    assert result['degraded_from']['budget']=={'name':'line_limit','limit':1,'observed':2}
    assert result['degraded_from']['name']=='navigation.search'
    recovered=list(result['results'])
    call=result.get('next_call')
    while call is not None:
        assert call['name']=='navigation.search_page' and call['arguments']['path_pattern']=='**/*.py'
        page=adapter.call(call['name'],call['arguments'])
        recovered.extend(page['results'])
        call=None if page['continuation_token'] is None else {'name':'navigation.search_page',
            'arguments':dict(call['arguments'],continuation_token=page['continuation_token'])}
    assert len(recovered)==3 and page['search_complete']
    # The un-degraded envelope (no registry) keeps its executable recovery shape.
    bare=DiscoveryBudgetExceeded('line_limit',1,2,'search limit reached').result(
        'repo',revision,'**/*.py',query='needle',limit=1)
    assert bare['reason']=='search_budget_exceeded' and bare['pattern_status']=='accepted'
    assert bare['next_call']['arguments']['path_pattern']=='**/*.py'


@pytest.mark.parametrize('pattern',['/absolute/*.py','../*.py','**/\\*.py','x\0*.py'])
def test_invalid_pattern_returns_safe_format_examples(setup,tmp_path,pattern):
    _,_,config,_,revision=setup
    adapter,_=adapter_for(config,tmp_path)
    result=adapter.call('navigation.search',{'repo_key':'repo','target_revision':revision,
        'query':'needle','path_pattern':pattern})
    assert result['reason']=='invalid_path_pattern' and result['message']
    assert result['examples']['all_python_including_root']=='*.py'
    assert result['absence_verdict']=='not-established'


def test_recovery_preserves_snapshot_identity():
    result=DiscoveryBudgetExceeded('line_limit',1,2,'search limit reached').result(
        'repo','a'*40,'**/*.py',query='needle',limit=500,snapshot_id='snapshot')
    assert result['next_call']['arguments']['snapshot_id']=='snapshot'
    assert result['next_call']['arguments']['limit']==100


def test_native_mcp_reports_actionable_pattern_error(setup,tmp_path):
    _,_,config,_,revision=setup
    _,path=adapter_for(config,tmp_path)
    script=Path(__file__).resolve().parents[1]/'packages/tooling/src/kp_agent_tooling/cli.py'
    async def run():
        params=StdioServerParameters(command=sys.executable,args=[str(script),'--config',str(path),'serve'])
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=20)) as client:
                await client.initialize()
                result=await client.call_tool('navigation.search',{'repo_key':'repo',
                    'target_revision':revision,'query':'needle','path_pattern':'**/\\*.py'})
                assert result.isError
                assert result.structuredContent['reason']=='invalid_path_pattern'
                assert result.structuredContent['message']
                assert json.loads(result.content[0].text)==result.structuredContent
    asyncio.run(run())
