import json
import subprocess
from kp_agent_tooling._impl.service.agent_tooling import AgentTooling


def test_manifest_paging_and_registry_requirement(tmp_path):
    repo=tmp_path/'repo';repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()
    git('init','-q');git('config','user.name','Test');git('config','user.email','test@example.invalid')
    (repo/'a.py').write_text('needle\n');(repo/'b.md').write_text('needle\n')
    git('add','.');git('commit','-qm','inventory');revision=git('rev-parse','HEAD')
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'schema_version':'ops.agent-tooling.v1','repos':{'repo':{'path':str(repo),'revision':revision}}}))
    adapter=AgentTooling(config)
    args={'repo_key':'repo','target_revision':revision,'limit':1}
    first=adapter.call('navigation.manifest',args)
    assert first['complete'] and not first['page_complete']
    assert first['entry_count']==2 and len(first['entries'])==1
    second=adapter.call(first['next_call']['name'],first['next_call']['arguments'])
    assert second['page_complete'] and second['remaining_entries']==0
    assert first['entries'][0]['artifact_id']!=second['entries'][0]['artifact_id']
    assert adapter.call('navigation.manifest',dict(args,offset=1))['reason']=='manifest_mismatch'
    search_args=dict(args,query='needle')
    assert adapter.call('navigation.search_page',search_args)['reason']=='navigation_registry_unconfigured'
    registry=tmp_path/'registry';registry.mkdir();adapter.config['navigation_registry_path']=str(registry)
    schema=next(tool['inputSchema'] for tool in adapter.tools(include_gateway=False)
                if tool['name']=='navigation.search_page')
    assert schema['properties']['limit']['maximum']==100
    page=adapter.call('navigation.search_page',search_args)
    assert page['continuation_token']
    assert page['pages_remaining_estimate'] >= 1
    bad=adapter.call('navigation.search_page',dict(search_args,query='different',continuation_token=page['continuation_token']))
    assert bad['status']=='error' and bad['reason']=='cursor_mismatch'
    # A new MCP process resumes the persisted cursor, using the same source contract.
    config.write_text(json.dumps(adapter.config))
    import asyncio
    import sys
    from pathlib import Path
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    script=Path(__file__).resolve().parents[1]/'packages/tooling/src/kp_agent_tooling/cli.py'
    async def resume():
        params=StdioServerParameters(command=sys.executable,args=[str(script),'--config',str(config),'serve'])
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                names={x.name for x in (await session.list_tools()).tools}
                assert {'navigation.manifest','navigation.search_page'} <= names
                reply=await session.call_tool('navigation.search_page',dict(search_args,continuation_token=page['continuation_token']))
                assert not reply.isError
                result=reply.structuredContent
                assert json.loads(reply.content[0].text)==result
                assert result['traversal_complete'] and result['search_complete']
                assert result['coverage']['total_matches']==2
                assert result['results'][0]['artifact_revision_id']
    asyncio.run(resume())
