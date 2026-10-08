# S5 port (subset) of OPS at the extraction commit tests/test_launch_claude_desk_memory.py: only the tests whose OPS form is not
# already covered by tests/test_launch_claude_desk_memory.py. See tests/EXTRACTED-TEST-LEDGER.md.
"""Claude memory MCP launch is bound to the actual host session before Docker."""
import json
from pathlib import Path
import pytest

from scripts.launch_claude_desk_memory import LaunchError, command_for_session, prepare_session
from kp_agent_tooling._impl.service.desk_memory_runtime import (initialize, persist_host_selection,
    components, read_context, from_local_config, admit)
from kp_agent_tooling._impl.service.desk_identity import binding_key
from kp_agent_tooling._impl.service.desk_binding import DeskLaunchUnavailable
from test_portable_desk_memory import config as memory_config


def fixture(tmp_path):
    root = tmp_path / 'sessions'
    root.mkdir(mode=0o700)
    docker = tmp_path / 'docker'
    docker.write_text('#!/bin/sh\n')
    docker.chmod(0o700)
    for session in ('session-A', 'session-B'):
        path = root / (session + '.json')
        path.write_text(json.dumps({'schema_version': 'ops.desk-memory.local.v1',
                                    'provider_session_id': session}))
        path.chmod(0o600)
    return {'session_config_root': str(root), 'docker_command': str(docker),
            'container': 'ops-agent-tooling', 'container_config_root': '/config/sessions'}


def recovery_fixture(tmp_path):
    source = memory_config(tmp_path, session='resume-A', instance='claude-local')
    initialize(source)
    selected = tmp_path / 'selections'
    selected.mkdir(mode=0o700)
    persisted = persist_host_selection(source, desk_id='implementation-desk',
        provider_id='anthropic', model_id='model-a',
        selection_path=selected / 'resume-A.selection.json')
    assert persisted['status'] == 'selection_recorded'
    store = components(source)[3]
    episode = store.capture('resume-A', source_ref='transcript:resume', events=[
        {'event_id':'one','role':'user','text':'Exact resumed source.'}])['episode_id']
    root = tmp_path / 'sessions'
    root.mkdir(mode=0o700)
    (root / 'resume-A.json').write_bytes(source.read_bytes())
    (root / 'resume-A.json').chmod(0o600)
    docker = tmp_path / 'docker'
    docker.write_text('#!/bin/sh\n')
    docker.chmod(0o700)
    options = {'session_config_root':str(root), 'selection_root':str(selected),
        'docker_command':str(docker), 'container':'ops-agent-tooling',
        'container_config_root':'/config/sessions',
        'environment':{'CLAUDE_CODE_HOST_SESSION_ID':'resume-A'}}

    def runner(argv, **kwargs):
        from types import SimpleNamespace
        path = root / 'resume-A.json'
        try:
            if 'admit' in argv:
                result = admit(path, desk_id=argv[argv.index('--desk-id')+1],
                    provider_id=argv[argv.index('--provider-id')+1],
                    model_id=argv[argv.index('--model-id')+1])
            else:
                result = read_context(path)
            return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr='')
        except Exception:
            return SimpleNamespace(returncode=1, stdout='', stderr='refused')
    return options, root, selected, episode, runner


@pytest.mark.xfail(strict=True, reason='S5 divergence: unselected-session setup server now lists every memory tool; OPS asserts only memory.connection_status (passes at OPS at the extraction commit)')
def test_missing_setup_keeps_a_diagnostic_mcp_connection(tmp_path):
    """A globally registered server must explain absent setup, not close transport."""
    import os
    import subprocess
    import sys
    options = fixture(tmp_path)
    script = Path(__file__).resolve().parents[1] / 'scripts/launch_claude_desk_memory.py'
    frames = [
        {'jsonrpc':'2.0','id':1,'method':'initialize','params':{
            'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'test','version':'1'}}},
        {'jsonrpc':'2.0','method':'notifications/initialized'},
        {'jsonrpc':'2.0','id':2,'method':'tools/list'},
        {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'memory.connection_status','arguments':{}}},
        {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'memory.search','arguments':{'query':'private'}}},
    ]
    command = [sys.executable, str(script), '--session-config-root', options['session_config_root'],
               '--docker-command', options['docker_command']]
    result = subprocess.run(command, env={**os.environ,'CLAUDE_CODE_HOST_SESSION_ID':'unselected-session'},
                            input=''.join(json.dumps(x)+'\n' for x in frames),text=True,capture_output=True,timeout=10)
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert result.returncode == 0 and len(replies) == 4, result.stderr
    assert replies[0]['result']['serverInfo']['name'] == 'ops-desk-memory-setup'
    assert [t['name'] for t in replies[1]['result']['tools']] == ['memory.connection_status']
    diagnostic = replies[2]['result']['structuredContent']
    assert diagnostic['status'] == 'not_ready'
    assert diagnostic['provider_session_id'] == 'unselected-session'
    assert diagnostic['expected_session_config'].endswith('/unselected-session.json')
    assert 'operator' in diagnostic['guidance']
    assert replies[3]['result']['isError'] is True
    assert not (Path(options['session_config_root'])/'unselected-session.json').exists()


@pytest.mark.xfail(strict=True, reason='S5 divergence: unselected-session setup server now lists every memory tool; OPS asserts only memory.connection_status (passes at OPS at the extraction commit)')
def test_native_mcp_client_can_inspect_unselected_session(tmp_path):
    import asyncio
    import os
    import sys
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    options=fixture(tmp_path)
    script=Path(__file__).resolve().parents[1]/'scripts/launch_claude_desk_memory.py'
    async def trial():
        parameters=StdioServerParameters(command=sys.executable,args=[str(script),
            '--session-config-root',options['session_config_root'],'--docker-command',options['docker_command']],
            env={**os.environ,'CLAUDE_CODE_HOST_SESSION_ID':'not-selected'})
        async with stdio_client(parameters) as (reader,writer):
            async with ClientSession(reader,writer) as client:
                initialized=await client.initialize()
                assert initialized.serverInfo.name=='ops-desk-memory-setup'
                catalog=await client.list_tools()
                assert [t.name for t in catalog.tools]==['memory.connection_status']
                result=await client.call_tool('memory.connection_status',{})
                assert not result.isError
                assert result.structuredContent['status']=='not_ready'
                denied=await client.call_tool('memory.propose',{'episode_ids':['guessed']})
                assert denied.isError and denied.structuredContent['status']=='not_ready'
                await client.send_ping()
    asyncio.run(trial())


