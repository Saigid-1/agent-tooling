import json
from pathlib import Path
from functools import partial
import pytest
from scripts.desk_session_hook import run_hook, LaunchError
from scripts.launch_claude_desk_memory import prepare_session
from test_launch_claude_desk_memory import recovery_fixture


def test_restart_preserves_claim_and_end_does_not_retire(tmp_path):
    options, root, _, _, runner = recovery_fixture(tmp_path)
    prepare = partial(prepare_session, runner=runner)
    first = run_hook({'hook_event_name':'SessionStart', 'session_id':'native-A',
                      'cwd':'/wrong-repo', 'role':'Coordinator'}, prepare=prepare, **options)
    assert first['binding']['role'] == 'implementation'
    ended = run_hook({'hook_event_name':'SessionEnd'}, prepare=prepare, **options)
    assert ended['authorization_changed'] is False
    (root / 'resume-A.json').unlink()
    resumed = run_hook({'hook_event_name':'SessionStart'}, prepare=prepare, **options)
    assert resumed['binding']['binding_key'] == first['binding']['binding_key']
    assert resumed['binding']['recovered'] is True


def test_unknown_session_cannot_inherit_other_or_directory_default(tmp_path):
    options, _, _, _, runner = recovery_fixture(tmp_path)
    options['environment'] = {'CLAUDE_CODE_HOST_SESSION_ID':'new-session'}
    with pytest.raises(LaunchError):
        run_hook({'hook_event_name':'SessionStart', 'role':'Coordinator',
                  'claimed_binding':'resume-A'}, prepare=partial(prepare_session, runner=runner), **options)


def test_inbox_uses_verified_binding_and_never_autoacks(tmp_path):
    options, _, _, _, runner = recovery_fixture(tmp_path)
    seen = []
    def read(receipt, endpoint, workspace):
        seen.append((receipt, endpoint, workspace))
        return {'status':'pending', 'events':[], 'acknowledged':False}
    result = run_hook({'hook_event_name':'SessionStart', 'tenant_id':'forged'},
        prepare=partial(prepare_session, runner=runner), inbox_reader=read,
        inbox_endpoint='http://127.0.0.1:3487', workspace_id='repo', **options)
    assert seen[0][0]['tenant_id'] == 'workspace-demo'
    assert seen[0][0]['provider_session_id'] == 'resume-A'
    assert result['inbox']['acknowledged'] is False
    run_hook({'hook_event_name':'SessionEnd'}, prepare=partial(prepare_session, runner=runner),
        inbox_reader=read, inbox_endpoint='http://127.0.0.1:3487', workspace_id='repo', **options)
    assert len(seen) == 1


def test_inbox_failure_does_not_erase_binding(tmp_path):
    options, _, _, _, runner = recovery_fixture(tmp_path)
    def unavailable(*args): raise OSError('offline')
    result = run_hook({'hook_event_name':'SessionStart'},
        prepare=partial(prepare_session, runner=runner), inbox_reader=unavailable,
        inbox_endpoint='http://127.0.0.1:3487', workspace_id='repo', **options)
    assert result['binding']['status'] == 'bound'
    assert result['inbox']['status'] == 'unavailable'


def test_two_explicit_sessions_can_share_desk_without_retiring_each_other(tmp_path):
    from test_portable_desk_memory import config
    from kp_agent_tooling._impl.service.desk_memory_runtime import admit, components
    options, root, _, _, runner = recovery_fixture(tmp_path)
    second = config(tmp_path, session='resume-B', instance='claude-local')
    admit(second, desk_id='implementation-desk', provider_id='test', model_id='other-model')
    cfg, registry, ledger, _ = components(second)
    a = ledger.resolve('resume-A', registry)
    b = ledger.resolve('resume-B', registry)
    assert a.binding_key == b.binding_key
    run_hook({'hook_event_name':'SessionEnd'}, prepare=partial(prepare_session, runner=runner), **options)
    assert ledger.resolve('resume-A', registry) == a
    assert ledger.resolve('resume-B', registry) == b


@pytest.mark.parametrize('defect', ['tenant', 'digest', 'redirect'])
def test_pending_consumer_rejects_misattributed_or_changed_text(monkeypatch, defect):
    import hashlib
    from io import BytesIO
    from scripts import desk_session_hook as hook
    receipt = {'tenant_id':'tenant', 'provider_session_id':'session'}
    event = {'tenant_id':'tenant', 'workspace_id':'repo', 'recipient_session_ids':['session'],
             'desknote':{'text':'exact', 'sha256':hashlib.sha256(b'exact').hexdigest()}}
    if defect == 'tenant': event['tenant_id'] = 'other'
    if defect == 'digest': event['desknote']['text'] = 'changed'
    class Opener:
        def open(self, request, timeout):
            return BytesIO(json.dumps({'result':{'data':{'events':[event], 'hasMore':False}}}).encode())
    monkeypatch.setattr(hook, 'build_opener', lambda *args: Opener())
    if defect == 'redirect':
        with pytest.raises(LaunchError, match='redirect'):
            hook.NoRedirect().redirect_request(None,None,302,None,None,'http://other')
    else:
        with pytest.raises(LaunchError): hook.pending_messages(receipt, 'http://127.0.0.1:3487','repo')
