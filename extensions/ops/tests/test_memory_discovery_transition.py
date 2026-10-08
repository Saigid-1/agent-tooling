"""Discovery survives admission and withdrawal without reconnecting transport."""
import asyncio
import io
import json
import os
from pathlib import Path
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_portable_desk_memory import config, initialize, bind
from kp_agent_tooling._impl.service.episodic_memory_tools import TOOLS


def test_host_catalog_matches_backend():
    assert json.loads((Path(__file__).parents[1]/'scripts/memory_tools.json').read_text()) == TOOLS


def test_native_connection_survives_admission_and_withdrawal(tmp_path):
    path=config(tmp_path);initialize(path)
    async def trial():
        params=StdioServerParameters(command=sys.executable,args=['-m','kp_agent_tooling.memory_cli','--config',str(path),'serve'],env=dict(os.environ))
        async with stdio_client(params) as (r,w):
            async with ClientSession(r,w) as client:
                await client.initialize()
                before=await client.list_tools()
                assert (await client.call_tool('memory.connection_status',{})).structuredContent['status']=='not_ready'
                assert (await client.call_tool('memory.status',{})).isError
                bind(path)
                assert (await client.call_tool('memory.connection_status',{})).structuredContent['status']=='ready'
                assert not (await client.call_tool('memory.status',{})).isError
                registry=tmp_path/'catalog.json';data=json.loads(registry.read_text());data['desks']=[];registry.write_text(json.dumps(data))
                assert (await client.call_tool('memory.status',{})).isError
                assert (await client.call_tool('memory.connection_status',{})).structuredContent['status']=='not_ready'
                assert before == await client.list_tools()
    asyncio.run(trial())


def test_host_setup_connection_retries_without_changing_catalog(tmp_path,monkeypatch):
    import scripts.launch_claude_desk_memory as launcher
    monkeypatch.setenv('CLAUDE_CODE_HOST_SESSION_ID','exact-session')
    attempts=[]
    def prepare(**options):
        attempts.append(options)
        if len(attempts)==1:raise launcher.LaunchError('not admitted')
        return '/docker',['/docker','exec','-i','container','kp-agent-memory','--config','/exact.json','serve'],{}
    def run(args,**kwargs):
        assert args[-4:]==['--tool','memory.status','--arguments','{}']
        assert kwargs['stdin']==launcher.subprocess.DEVNULL
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0,stdout='{"status":"ready"}')
    monkeypatch.setattr(launcher.subprocess,'run',run)
    frames=[{'jsonrpc':'2.0','id':1,'method':'initialize'}, {'jsonrpc':'2.0','id':2,'method':'tools/list'},
            *[{'jsonrpc':'2.0','id':i,'method':'tools/call','params':{'name':'memory.status','arguments':{}}} for i in [3,4]],
            {'jsonrpc':'2.0','id':5,'method':'tools/list'}]
    out=io.StringIO();launcher.serve_setup_error('not admitted',session_config_root=str(tmp_path),reader=io.BytesIO(('\n'.join(map(json.dumps,frames))+'\n').encode()),writer=out,retry_options={'session_config_root':str(tmp_path)},retry=prepare)
    results=[json.loads(x)['result'] for x in out.getvalue().splitlines()]
    assert results[2]['isError'] and not results[3]['isError']
    assert results[1]==results[4]
