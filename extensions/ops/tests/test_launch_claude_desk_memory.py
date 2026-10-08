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


def test_only_matching_host_session_config_can_launch(tmp_path):
    options = fixture(tmp_path)
    command, argv = command_for_session(**options,
        environment={'CLAUDE_CODE_HOST_SESSION_ID': 'session-A'})
    assert argv == [command, 'exec', '-i', 'ops-agent-tooling',
                    'kp-agent-memory', '--config', '/config/sessions/session-A.json', 'serve']
    assert 'session-B' not in ' '.join(argv)
    a = Path(options['session_config_root']) / 'session-A.json'
    a.write_text(json.dumps({'schema_version': 'ops.desk-memory.local.v1',
                             'provider_session_id': 'session-B'}))
    with pytest.raises(LaunchError, match='different Claude host session'):
        command_for_session(**options, environment={'CLAUDE_CODE_HOST_SESSION_ID': 'session-A'})


@pytest.mark.parametrize('environment', [{}, {'CLAUDE_CODE_HOST_SESSION_ID': 'missing'},
                                         {'CLAUDE_CODE_HOST_SESSION_ID': '../session-B'},
                                         {'CLAUDE_CODE_HOST_SESSION_ID': 'session-A/../session-B'}])
def test_missing_unknown_or_pathlike_host_session_fails(tmp_path, environment):
    options = fixture(tmp_path)
    with pytest.raises(LaunchError):
        command_for_session(**options, environment=environment)


def test_private_root_and_file_modes_and_symlink_are_required(tmp_path):
    options = fixture(tmp_path)
    env = {'CLAUDE_CODE_HOST_SESSION_ID': 'session-A'}
    root = Path(options['session_config_root'])
    root.chmod(0o755)
    with pytest.raises(LaunchError, match='private'):
        command_for_session(**options, environment=env)
    root.chmod(0o700)
    path = root / 'session-A.json'
    path.chmod(0o644)
    with pytest.raises(LaunchError, match='private regular file'):
        command_for_session(**options, environment=env)
    path.unlink()
    path.symlink_to(root / 'session-B.json')
    with pytest.raises(LaunchError, match='unavailable'):
        command_for_session(**options, environment=env)


def test_container_path_and_docker_are_explicit(tmp_path):
    options = fixture(tmp_path)
    env = {'CLAUDE_CODE_HOST_SESSION_ID': 'session-A'}
    for bad in ('config/sessions', '/', '/config/../sessions', '/config//sessions'):
        with pytest.raises(LaunchError, match='container config root'):
            command_for_session(**{**options, 'container_config_root': bad}, environment=env)
    with pytest.raises(LaunchError, match='docker command'):
        command_for_session(**{**options, 'docker_command': 'docker'}, environment=env)


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


def test_resume_reuses_trusted_selection_and_memory_access(tmp_path):
    options, root, selected, episode, runner = recovery_fixture(tmp_path)
    (root / 'resume-A.json').unlink()
    _, argv, receipt = prepare_session(**options, runner=runner)
    assert receipt['status'] == 'bound' and receipt['recovered'] is True
    assert receipt['provider_session_id'] == 'resume-A'
    assert argv[-2] == '/config/sessions/resume-A.json'
    tools = from_local_config(root / 'resume-A.json')
    assert tools.call('memory.read_event', {'episode_id':episode,'event_id':'one'})['text'] == 'Exact resumed source.'
    proposal = tools.call('memory.propose', {'episode_ids':[episode],
        'items':[{'kind':'observation', 'text':'Resumed session can write under its admitted desk.',
                  'citations':[{'episode_id':episode,'event_id':'one','start':0,
                                'end':len('Exact resumed source.'),'quote':'Exact resumed source.'}]}],
        'unresolved_questions':[]})
    assert tools.call('memory.handoff', {'capsule_id':proposal['capsule_id']})['items'][0]['kind'] == 'observation'
    assert prepare_session(**options, runner=runner)[2] == {**receipt, 'recovered':False}
    assert persist_host_selection(root / 'resume-A.json', desk_id='implementation-desk',
        provider_id='anthropic', model_id='model-a',
        selection_path=selected / 'resume-A.selection.json')['idempotent'] is True


def test_resume_without_selection_or_with_forged_session_refuses(tmp_path):
    options, root, selected, _, runner = recovery_fixture(tmp_path)
    (root / 'resume-A.json').unlink()
    (selected / 'resume-A.selection.json').unlink()
    with pytest.raises(LaunchError, match='no trusted host selection'):
        prepare_session(**options, runner=runner)
    assert not (root / 'resume-A.json').exists()
    other = memory_config(tmp_path, session='other-session', instance='claude-local')
    bad = json.loads(other.read_text())
    selection = {'schema_version':'ops.desk-host-selection.v1','config':bad,
        'desk_id':'implementation-desk','provider_id':'anthropic','model_id':'model-a',
        'tenant_id':'fixture','role':'implementation','repo_key':'fixture/repo',
        'binding_key':binding_key(tenant_id='fixture',role='implementation',repo_key='fixture/repo')}
    target = selected / 'resume-A.selection.json'
    target.write_text(json.dumps(selection)); target.chmod(0o600)
    with pytest.raises(LaunchError, match='no trusted host selection'):
        prepare_session(**options, runner=runner)
    assert not (root / 'resume-A.json').exists()


@pytest.mark.parametrize('field,value', [('tenant_id','wrong-tenant'),('role','verification')])
def test_resume_wrong_tenant_or_role_refuses_without_memory_binding(tmp_path, field, value):
    options, root, selected, _, runner = recovery_fixture(tmp_path)
    (root / 'resume-A.json').unlink()
    target = selected / 'resume-A.selection.json'
    selection = json.loads(target.read_text())
    selection[field] = value
    selection['binding_key'] = binding_key(tenant_id=selection['tenant_id'],
        role=selection['role'], repo_key=selection['repo_key'])
    target.write_text(json.dumps(selection))
    with pytest.raises(LaunchError, match='conflicts'):
        prepare_session(**options, runner=runner)
    assert not (root / 'resume-A.json').exists()


def test_conflicting_selection_is_rejected_before_admission(tmp_path):
    path = memory_config(tmp_path, session='fresh', instance='claude-local')
    initialize(path)
    selected = tmp_path / 'selections'
    selected.mkdir(mode=0o700)
    target = selected / 'fresh.selection.json'
    target.write_text('{}'); target.chmod(0o600)
    with pytest.raises(ValueError, match='already exists'):
        persist_host_selection(path, desk_id='implementation-desk',
            provider_id='anthropic', model_id='model-a', selection_path=target)
    with pytest.raises(DeskLaunchUnavailable, match='no desk admission'):
        components(path)[2].resolve('fresh', components(path)[1])


def test_operator_cli_rejected_selection_does_not_admit(tmp_path, monkeypatch, capsys):
    import importlib.util
    import sys
    import types
    from kp_agent_tooling._impl.service import desk_memory_runtime, episodic_memory, desk_binding

    path = memory_config(tmp_path, session='cli-fresh', instance='claude-local')
    initialize(path)
    from kp_agent_tooling import desk_cli
    script = Path(desk_cli.__file__)
    # Load the installed entry module against the local service implementation.
    for name in ('kp_agent_tooling', 'kp_agent_tooling._impl',
                 'kp_agent_tooling._impl.service'):
        package = types.ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    monkeypatch.setitem(sys.modules,
        'kp_agent_tooling._impl.service.desk_memory_runtime', desk_memory_runtime)
    monkeypatch.setitem(sys.modules, 'kp_agent_tooling._impl.service.episodic_memory', episodic_memory)
    monkeypatch.setitem(sys.modules, 'kp_agent_tooling._impl.service.desk_binding', desk_binding)
    spec = importlib.util.spec_from_file_location('desk_cli_under_test', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, 'argv', ['kp-agent-desk', '--config', str(path), 'admit',
        '--desk-id', 'implementation-desk', '--provider-id', 'anthropic',
        '--model-id', 'model-a', '--selection-output', str(tmp_path / 'missing' / 'cli-fresh.selection.json')])
    assert module.main() == 1
    output=capsys.readouterr()
    assert not output.out and 'private host-owned session directory' in output.err
    assert 'Traceback' not in output.err
    with pytest.raises(DeskLaunchUnavailable, match='no desk admission'):
        components(path)[2].resolve('cli-fresh', components(path)[1])


def test_parallel_same_session_resume_keeps_one_config_and_binding(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    options, root, _, _, runner = recovery_fixture(tmp_path)
    (root / 'resume-A.json').unlink()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: prepare_session(**options, runner=runner)[2], range(2)))
    assert all(row['status'] == 'bound' for row in results)
    assert results[0]['binding_key'] == results[1]['binding_key']
    assert read_context(root / 'resume-A.json')['desk']['binding_id'] == results[0]['binding_key']


def test_resume_refuses_catalog_withdrawal_and_missing_ledger(tmp_path):
    options, root, _, _, runner = recovery_fixture(tmp_path)
    (root / 'resume-A.json').unlink()
    catalog = tmp_path / 'catalog.json'
    data = json.loads(catalog.read_text())
    data['desks'] = [row for row in data['desks'] if row['role_id'] != 'implementation']
    data['team']['roles'] = [row for row in data['team']['roles']
                            if row['role_id'] != 'implementation']
    catalog.write_text(json.dumps(data))
    with pytest.raises(LaunchError, match='admission is unavailable'):
        prepare_session(**options, runner=runner)
    assert not (root / 'resume-A.json').exists()
    # Restore the catalog, but leave the durable ledger unavailable.
    from pathlib import Path
    source = Path(__file__).resolve().parents[3] / 'config/desk-context/catalog.example.json'
    catalog.write_bytes(source.read_bytes())
    (tmp_path / 'state/sessions.sqlite3').unlink()
    with pytest.raises(LaunchError, match='admission is unavailable'):
        prepare_session(**options, runner=runner)
    assert not (root / 'resume-A.json').exists()


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
    assert {t['name'] for t in replies[1]['result']['tools']} == {t['name'] for t in __import__('kp_agent_tooling._impl.service.episodic_memory_tools', fromlist=['TOOLS']).TOOLS}
    diagnostic = replies[2]['result']['structuredContent']
    assert diagnostic['status'] == 'not_ready'
    assert diagnostic['provider_session_id'] == 'unselected-session'
    assert diagnostic['expected_session_config'].endswith('/unselected-session.json')
    assert 'operator' in diagnostic['guidance']
    assert replies[3]['result']['isError'] is True
    assert not (Path(options['session_config_root'])/'unselected-session.json').exists()


def test_setup_diagnostics_reject_identity_overrides_and_survive_bad_frames(tmp_path, monkeypatch):
    import io
    from scripts.launch_claude_desk_memory import serve_setup_error
    monkeypatch.delenv('CLAUDE_CODE_HOST_SESSION_ID', raising=False)
    frames = [
        b'not-json\n',
        json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25'}}).encode()+b'\n',
        json.dumps({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'memory.connection_status','arguments':{'session_id':'session-B'}}}).encode()+b'\n',
        b'x'*70000+b'\n',
        json.dumps({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'memory.connection_status'}}).encode()+b'\n',
        json.dumps({'jsonrpc':'2.0','id':4,'method':'ping'}).encode()+b'\n',
    ]
    out=io.StringIO()
    assert serve_setup_error('host identity missing',session_config_root=str(tmp_path),
                             reader=io.BytesIO(b''.join(frames)),writer=out)==0
    results=[json.loads(x) for x in out.getvalue().splitlines()]
    assert results[0]['error']['code']==-32700
    assert results[2]['error']['code']==-32602
    assert results[3]['error']['code']==-32600
    diagnostic=results[4]['result']['structuredContent']
    assert diagnostic['provider_session_id'] is None and diagnostic['expected_session_config'] is None
    assert 'trusted host' in diagnostic['guidance']
    assert results[5]['result']=={}
    assert not list(tmp_path.iterdir())


def test_admitted_launcher_still_executes_the_original_server(monkeypatch, capsys):
    import scripts.launch_claude_desk_memory as launcher
    receipt={'status':'bound','provider_session_id':'existing'}
    command='/docker';argv=[command,'exec','-i','ops-agent-tooling','kp-agent-memory','--config','/config/sessions/existing.json','serve']
    monkeypatch.setattr(launcher,'prepare_session',lambda **kwargs:(command,argv,receipt))
    class Executed(Exception):pass
    def execute(executable,parameters):
        assert executable==command and parameters==argv
        raise Executed()
    monkeypatch.setattr(launcher.os,'execv',execute)
    monkeypatch.setattr(launcher,'serve_setup_error',lambda *a,**k:pytest.fail('admitted execution entered diagnostics'))
    with pytest.raises(Executed):
        launcher.main(['--session-config-root','/private/config','--docker-command','/docker'])
    output=capsys.readouterr()
    assert not output.out and 'memory binding receipt:' in output.err


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
                assert {'memory.connection_status','memory.search','memory.propose'} <= {t.name for t in catalog.tools}
                result=await client.call_tool('memory.connection_status',{})
                assert not result.isError
                assert result.structuredContent['status']=='not_ready'
                denied=await client.call_tool('memory.propose',{'episode_ids':['guessed']})
                assert denied.isError and denied.structuredContent['status']=='not_ready'
                await client.send_ping()
    asyncio.run(trial())


def test_desk_preflight_cannot_consume_the_mcp_initialize_packet(tmp_path):
    import subprocess
    import sys
    packet='{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
    program='''
import json,subprocess,sys
from scripts.launch_claude_desk_memory import _invoke_desk
child = "import json,sys; print(json.dumps({'consumed':len(sys.stdin.read())}))"
receipt=_invoke_desk(subprocess.run,[sys.executable,'-c',child])
print(json.dumps({'preflight':receipt,'remaining':sys.stdin.readline()}))
'''
    result=subprocess.run([sys.executable,'-c',program],input=packet,capture_output=True,
                          text=True,timeout=10,cwd=Path(__file__).resolve().parents[1],check=True)
    body=json.loads(result.stdout)
    assert body['preflight']['consumed']==0
    assert body['remaining']==packet


def test_failed_resume_preserves_concurrently_replaced_config(tmp_path):
    from types import SimpleNamespace
    options, root, _, _, _ = recovery_fixture(tmp_path)
    path = root / 'resume-A.json'
    path.unlink()
    replacement = b'{"concurrent_owner":"retained"}'
    def runner(argv, **kwargs):
        path.unlink()
        path.write_bytes(replacement)
        path.chmod(0o600)
        return SimpleNamespace(returncode=1, stdout='', stderr='refused')
    with pytest.raises(LaunchError, match='admission'):
        prepare_session(**options, runner=runner)
    assert path.read_bytes() == replacement
    assert list(root.glob('.resume-*')) == []
