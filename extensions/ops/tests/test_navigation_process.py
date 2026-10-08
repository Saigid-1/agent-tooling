import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from kp_agent_tooling._impl.service.navigation_process import NavigationProcessRunner, NavigationSubprocessError
from kp_agent_tooling._impl.service.serena_navigation import SerenaNavigationProvider, failure_reporting
from kp_agent_tooling_ops._impl.service.knowledge import KnowledgeService, KnowledgeRequestError


def test_runner_environment_output_and_failure(tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_DIR', '/bad/git')
    monkeypatch.setenv('PYTHONPATH', '/bad/python')
    runner = NavigationProcessRunner(tmp_path)
    raw = runner.run([sys.executable, '-c', 'import os,json; print(json.dumps(dict(os.environ)))'], cwd=tmp_path, pythonpath=False)
    env = json.loads(raw)
    assert 'GIT_DIR' not in env and 'PYTHONPATH' not in env
    assert env['PYTHONNOUSERSITE'] == '1'
    with pytest.raises(NavigationSubprocessError) as caught:
        runner.run([sys.executable, '-c', 'import sys; print("private", file=sys.stderr); sys.exit(7)'], cwd=tmp_path)
    report = failure_reporting(caught.value)
    assert report['exit_code'] == 7
    assert report['error_details_status'] == 'unavailable'
    assert report['underlying_cause'] == 'not-established'
    assert 'private' not in str(report)


def test_runner_bounds_and_deadline(tmp_path):
    # The bound case has its own runner: under load a Python child can take longer than
    # the 0.1 s deadline below just to start, which raised TimeoutError, not the bound.
    with pytest.raises(ValueError, match='output bound'):
        NavigationProcessRunner(tmp_path).run([sys.executable, '-c', 'print("x" * 100)'],
                                              cwd=tmp_path, max_bytes=10)
    runner = NavigationProcessRunner(tmp_path, timeout=0.1)
    with pytest.raises(TimeoutError):
        runner.run([sys.executable, '-c', 'import time; time.sleep(5)'], cwd=tmp_path)


def _exited_unreaped(pid):
    """True once pid has exited but is not yet reaped (a zombie)."""
    stat = Path(f'/proc/{pid}/stat')
    if stat.exists():
        return stat.read_text().rsplit(')', 1)[1].split()[0] == 'Z'
    state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout
    return state.strip().startswith('Z')


def test_runner_bound_error_survives_a_child_that_already_exited(tmp_path, monkeypatch):
    # test_runner_bounds_and_deadline raced its child's exit: Darwin answers killpg on a
    # group whose only member is an exited, unreaped leader with EPERM (Linux answers 0),
    # which replaced the bound error. Holding the kill until the leader has exited makes
    # that interleaving certain instead of occasional.
    real_killpg = os.killpg
    killed = []

    def killpg_after_exit(pgid, sig):
        deadline = time.monotonic() + 10
        while not _exited_unreaped(pgid):
            assert time.monotonic() < deadline, 'the navigation child did not exit'
            time.sleep(0.01)
        killed.append(pgid)
        return real_killpg(pgid, sig)

    monkeypatch.setattr(os, 'killpg', killpg_after_exit)
    runner = NavigationProcessRunner(tmp_path)
    with pytest.raises(ValueError, match='output bound'):
        runner.run([sys.executable, '-c', 'print("x" * 100)'], cwd=tmp_path, max_bytes=10)
    assert killed, 'the runner did not kill the process group'


@pytest.mark.parametrize('operation', ['find', 'overview', 'inspect'])
@pytest.mark.parametrize('failed', [True, False])
def test_serena_worker_uses_portable_runner(tmp_path, monkeypatch, operation, failed):
    import kp_agent_tooling._impl.service.serena_navigation as serena
    (tmp_path / 'sample.py').write_text('def thing(): pass\n')
    (tmp_path / '.serena').mkdir()
    (tmp_path / '.serena/project.yml').write_text('read_only: true\n')
    monkeypatch.setattr(serena, 'source', lambda *a: ('blob', 'def thing(): pass\n'))
    monkeypatch.setattr(serena, 'boundary', lambda *a: [])
    monkeypatch.setattr(serena, 'snapshot', lambda *a: {})
    calls = []
    def fail(self, argv, **kwargs):
        calls.append(argv)
        if failed:
            raise NavigationSubprocessError(9)
        return json.dumps({'rows': {} if operation == 'overview' else [], 'server': {}}).encode()
    monkeypatch.setattr(NavigationProcessRunner, 'run', fail)
    provider = SerenaNavigationProvider('/configured/serena')
    args = {'find': ('thing',), 'overview': ('sample.py',), 'inspect': ('sample.py::thing',)}[operation]
    result = getattr(provider, operation)(tmp_path, 'a' * 40, *args)
    assert len(calls) == 1
    if failed:
        assert result['status'] == 'unavailable'
        assert result['error_reporting']['exit_code'] == 9
        assert result['error_reporting']['error_details_status'] == 'unavailable'
    else:
        assert result['status'] == 'ok'
        if operation != 'overview':
            assert result.get('absence_verdict', result['report'].get('absence_verdict')) == 'not-established'


@pytest.mark.parametrize('navigation', [{'tool_root': '/old', 'cache_root': '/cache'}, {'provider': 'unknown'}])
def test_knowledge_rejects_retired_or_unknown_provider(navigation):
    config = {'schema_version': 'ops.knowledge-config.v1', 'repositories': {'repo': {'path': '/repo', 'ref': 'HEAD', 'corpus_scope': 'repo', 'tenant_ids': ['tenant'], 'capabilities': {}}}, 'navigation': navigation}
    with pytest.raises(KnowledgeRequestError, match='legacy AST navigation is retired'):
        KnowledgeService(config, lambda: None)


def test_knowledge_constructs_serena_without_legacy_module():
    config = {'schema_version': 'ops.knowledge-config.v1', 'repositories': {'repo': {'path': '/repo', 'ref': 'HEAD', 'corpus_scope': 'repo', 'tenant_ids': ['tenant'], 'capabilities': {}}},
              'navigation': {'provider': 'serena', 'command': '/serena', 'python': sys.executable}}
    assert isinstance(KnowledgeService(config, lambda: None)._navigation_provider, SerenaNavigationProvider)
    assert not Path(__import__('kp_agent_tooling').__file__).parent.joinpath('_impl/service/knowledge_navigation.py').exists()
