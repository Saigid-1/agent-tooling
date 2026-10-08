import json
from types import SimpleNamespace
import pytest
from scripts.claude_capture_host import LaunchError, run_hook, main


@pytest.fixture
def setup(tmp_path):
    tmp_path = tmp_path.resolve()
    root = tmp_path / 'sessions'
    root.mkdir(mode=0o700)
    admitted = root / 'local_a.json'
    admitted.write_text(json.dumps({'schema_version':'ops.desk-memory.local.v1','provider_session_id':'local_a'}))
    admitted.chmod(0o600)
    registry = tmp_path / 'local_a.json'
    registry.write_text(json.dumps({'sessionId':'local_a','cliSessionId':'native-a','cwd':'/repo'}))
    transcript = tmp_path / 'native-a.jsonl'
    transcript.write_text('secret source text')
    docker = tmp_path / 'docker'
    docker.write_text('#!/bin/sh\n')
    docker.chmod(0o700)
    config = {'schema_version':'ops.claude-capture-host.v1','host_session_id':'local_a',
              'native_session_id':'native-a','registry_path':str(registry),'cwd':'/repo',
              'host_transcript_path':str(transcript),'container_transcript_path':'/source/native-a.jsonl',
              'session_config_root':str(root),'docker_command':str(docker),'container':'memory-1',
              'container_config_root':'/config/sessions','capture_ledger':'/state/capture.sqlite3',
              'queue':'/state/queue.sqlite3','telemetry':'/state/telemetry.sqlite3'}
    path = tmp_path / 'bridge.json'
    path.write_text(json.dumps(config)); path.chmod(0o600)
    payload = {'hook_event_name':'Stop','session_id':'native-a','transcript_path':str(transcript),'cwd':'/repo'}
    calls = []
    def runner(argv, **kwargs):
        calls.append((argv,kwargs))
        return SimpleNamespace(returncode=0, stdout='{"suppressOutput":true}')
    return path, config, payload, calls, {'environment':{'CLAUDE_CODE_HOST_SESSION_ID':'local_a'},'runner':runner}


@pytest.mark.parametrize('event',['Stop','PreCompact','SessionEnd'])
def test_exact_mapping_passes_fixed_config_and_docker_path(setup,event):
    path, config, payload, calls, options = setup
    payload.update(hook_event_name=event, memory_config='/forged', role='forged')
    assert run_hook(path,payload,**options) == {'suppressOutput':True}
    argv, kwargs = calls[0]
    assert argv[argv.index('--memory-config')+1] == '/config/sessions/local_a.json'
    assert argv[argv.index('--transcript')+1] == '/source/native-a.jsonl'
    assert argv[argv.index('--native-session-id')+1] == 'native-a'
    assert kwargs['timeout'] == 45
    assert 'secret source text' not in kwargs['input']


@pytest.mark.parametrize('field,value',[('session_id','wrong'),('transcript_path','/wrong'),('cwd','/wrong')])
def test_payload_identity_fails_closed(setup,field,value):
    path, config, payload, calls, options = setup
    payload[field]=value
    with pytest.raises(LaunchError): run_hook(path,payload,**options)
    assert not calls


def test_other_session_is_not_applicable_missing_environment_is_refused(setup):
    path, config, payload, calls, options = setup
    options['environment']={'CLAUDE_CODE_HOST_SESSION_ID':'other'}
    assert run_hook(path,payload,**options)['status']=='not_applicable'
    options['environment']={}
    with pytest.raises(LaunchError): run_hook(path,payload,**options)
    assert not calls


def test_registry_mutation_refused_but_nonidentity_changes_allowed(setup):
    path, config, payload, calls, options = setup
    registry = __import__('pathlib').Path(config['registry_path'])
    record=json.loads(registry.read_text()); record['lastActivity']='changed'
    registry.write_text(json.dumps(record))
    run_hook(path,payload,**options)
    record['cliSessionId']='wrong'; registry.write_text(json.dumps(record))
    with pytest.raises(LaunchError): run_hook(path,payload,**options)
    assert len(calls)==1


def test_backend_failure_does_not_leak_source(setup):
    path, config, payload, calls, options = setup
    options['runner']=lambda *a,**k: SimpleNamespace(returncode=1,stdout='secret source',stderr='secret source')
    with pytest.raises(LaunchError,match='capture/index completion') as error: run_hook(path,payload,**options)
    assert 'secret source' not in str(error.value)


def test_private_config_and_symlink_rejected(setup):
    path, config, payload, calls, options = setup
    path.chmod(0o644)
    with pytest.raises(LaunchError): run_hook(path,payload,**options)
    path.chmod(0o600)
    link=path.with_name('alias.json'); link.symlink_to(path)
    with pytest.raises(LaunchError): run_hook(link,payload,**options)
    assert not calls


def test_settings_quotes_paths_and_preserves_event_scope(setup,capsys):
    import shlex
    path, config, payload, calls, options=setup
    assert main(['--config',str(path),'--settings'])==0
    settings=json.loads(capsys.readouterr().out)
    assert set(settings['hooks'])=={'Stop','PreCompact','SessionEnd'}
    for entries in settings['hooks'].values():
        entry=entries[0]['hooks'][0]
        argv=shlex.split(entry['command'])
        assert argv[-2:]==['--config',str(path)]
        assert entry['timeout']==60


@pytest.mark.parametrize('event,code',[('PreCompact',2),('Stop',1),('SessionEnd',1)])
def test_cli_failure_is_sanitized_and_nonzero(setup,monkeypatch,capsys,event,code):
    import io
    from scripts import claude_capture_host as bridge
    path, config, payload, calls, options=setup
    payload['hook_event_name']=event
    monkeypatch.setattr(bridge.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())))
    monkeypatch.delenv('CLAUDE_CODE_HOST_SESSION_ID',raising=False)
    assert main(['--config',str(path)])==code
    output=capsys.readouterr()
    assert output.out==''
    assert json.loads(output.err)['status']=='error'
    assert 'secret source' not in output.err
