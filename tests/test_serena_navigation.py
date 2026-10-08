import json
import subprocess

import pytest

from kp_agent_tooling._impl.service.serena_navigation import SerenaNavigationProvider, normalize


@pytest.fixture
def repo(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'sample.py').write_text('class Example:\n    def read(self):\n        return 1\n')
    subprocess.run(['git', '-C', str(tmp_path), 'add', 'sample.py'], check=True)
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture'], check=True)
    revision = subprocess.check_output(['git', '-C', str(tmp_path), 'rev-parse', 'HEAD']).decode().strip()
    (tmp_path / '.serena').mkdir()
    (tmp_path / '.serena/project.yml').write_text('read_only: true\n')
    return tmp_path, revision


def row():
    return dict(relative_path='sample.py', name_path='Example/read',
                body_location=dict(start_line=1, end_line=2), body='def read(self):\n        return 1')


def test_exact_body_and_one_based_citation(repo):
    root, revision = repo
    result = normalize(root, revision, 'sample.py', [row()])[0]
    assert (result['start_line'], result['end_line']) == (2, 3)
    assert result['excerpt'] == '    def read(self):\n        return 1'
    assert result['revision'] == revision
    assert len(result['blob_sha']) == 40


@pytest.mark.parametrize('change', [
    {'body': 'def read(self):\n    return 1'},
    {'relative_path': '../sample.py'},
    {'body_location': {'start_line': True, 'end_line': 2}},
    {'body_location': {'start_line': 1, 'end_line': 9}},
])
def test_reject_misleading_provider_evidence(repo, change):
    with pytest.raises(ValueError):
        normalize(*repo, 'sample.py', [{**row(), **change}])


def test_dirty_source_rejected(repo):
    root, revision = repo
    (root / 'sample.py').write_text('changed\n')
    assert SerenaNavigationProvider('/unused').inspect(root, revision, 'sample.py::Example/read')['status'] == 'unavailable'


@pytest.mark.xfail(strict=True, reason='S5 divergence: knowledge_navigation was retired (the legacy-navigation retirement commit); CodeNavigationProvider no longer exists to patch or import')
@pytest.mark.parametrize('rows, expected', [([], 'no_results'), ([row()], 'ok')])
def test_provider_contract(repo, monkeypatch, rows, expected):
    monkeypatch.setattr('kp_agent_tooling._impl.service.knowledge_navigation.CodeNavigationProvider._run',
        lambda *args, **kwargs: json.dumps(dict(rows=rows, server={'name': 'Serena', 'version': 'test'})).encode())
    result = SerenaNavigationProvider('/configured/serena').inspect(*repo, 'sample.py::Example/read')
    assert result['status'] == 'ok'
    assert result['report']['status'] == expected
    assert result['report']['absence_verdict'] == 'not-established'


def test_read_only_required(repo):
    root, revision = repo
    (root / '.serena/project.yml').write_text('read_only: false\n')
    result = SerenaNavigationProvider('/unused').inspect(root, revision, 'sample.py::Example/read')
    assert result['status'] == 'unavailable'
    assert 'read_only_project_required' in result['detail']


def test_wrong_target_rejected(repo):
    result = SerenaNavigationProvider('/unused').inspect(repo[0], '0' * 40, 'sample.py::Example/read')
    assert result['status'] == 'unavailable'


def test_truncation_and_symlink_rejected(repo):
    with pytest.raises(ValueError):
        normalize(*repo, 'sample.py', {'message': 'too long'})
    root, revision = repo
    original = root / 'saved.py'
    (root / 'sample.py').rename(original)
    (root / 'sample.py').symlink_to(original)
    with pytest.raises(ValueError):
        normalize(root, revision, 'sample.py', [row()])


def test_reference_hop_validates_exact_source_and_bounds(repo):
    from kp_agent_tooling._impl.service.serena_navigation import normalize_references
    refs = {'sample.py': {'Function': [dict(name_path='Example/read',
        content_around_reference='...   1:    def read(self):\\n  >   2:        return 1')]}}
    result = normalize_references(*repo, refs, 'sample.py::Example/read')
    assert result['edges'][0]['citation']['start_line'] == 3
    assert result['edges'][0]['kind'] == 'references'
    assert result['depth'] == 1
    refs['sample.py']['Function'] *= 13
    result = normalize_references(*repo, refs, 'target')
    assert len(result['edges']) == 12 and result['omitted'] == 1
    refs['sample.py']['Function'][0]['content_around_reference'] = '  >   2:        return 2'
    with pytest.raises(ValueError, match='source_mismatch'):
        normalize_references(*repo, refs, 'target')


def test_reference_context_can_include_terminal_empty_line(repo):
    from kp_agent_tooling._impl.service.serena_navigation import normalize_references
    refs = {'sample.py': {'Function': [dict(name_path='Example/read',
        content_around_reference='  >   2:        return 1\\n...   3:')]}}
    result = normalize_references(*repo, refs, 'sample.py::Example/read')
    assert len(result['edges']) == 1
    assert result['edges'][0]['citation']['start_line'] == 3
    refs['sample.py']['Function'][0]['content_around_reference'] = '  >   3:'
    with pytest.raises(ValueError, match='source_mismatch'):
        normalize_references(*repo, refs, 'sample.py::Example/read')


def test_no_body_and_overview_children_remain_metadata_only(repo):
    child = dict(name='read', body_location=dict(start_line=1, end_line=2))
    parent = dict(relative_path='sample.py', name_path='Example',
        body_location=dict(start_line=0, end_line=2),
        children={'Method': [child]})
    citations = normalize(*repo, 'sample.py', [parent], require_body=False)
    assert [item['name_path'] for item in citations] == ['Example', 'Example/read']
    assert all('excerpt' not in item for item in citations)
    assert citations[1]['relation'] == 'descendant'
    assert citations[1]['retrieval']['arguments']['line_count'] == 2


def test_truncated_reference_response_is_not_empty_coverage(repo):
    from kp_agent_tooling._impl.service.serena_navigation import normalize_references
    with pytest.raises(ValueError):
        normalize_references(*repo, 'too long', 'target')


def test_malformed_compound_symbol_never_starts_worker(repo):
    result = SerenaNavigationProvider('/unused').inspect(*repo, 'path::sample.py::Example/read')
    assert result['invocation'] == {'worker_attempted': False, 'provider_response_received': False}
    assert 'do not prepend' in result['detail']


@pytest.mark.skip(reason='S5 excluded: needs OPS-only scripts/serena_navigation.py')
def test_cli_rejects_ambiguous_arguments_and_returns_failure(repo, tmp_path, monkeypatch):
    from scripts.serena_navigation import main
    argv = ['--repo', str(repo[0]), '--revision', repo[1], '--command', '/unused',
            '--python', '/unused', '--path', 'path::sample.py', '--symbol', 'Example/read',
            '--out', str(tmp_path/'result.json')]
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    argv[argv.index('path::sample.py')] = 'sample.py'
    monkeypatch.setattr(SerenaNavigationProvider, 'inspect', lambda *a: {'status': 'unavailable'})
    assert main(argv) == 1
    monkeypatch.setattr(SerenaNavigationProvider, 'inspect', lambda *a: {'status': 'ok', 'report': {'status': 'no_results'}})
    assert main(argv) == 0


@pytest.mark.xfail(strict=True, reason='S5 divergence: knowledge_navigation was retired (the legacy-navigation retirement commit); CodeNavigationProvider no longer exists to patch or import')
def test_same_path_provider_config_mutation_refused(repo, monkeypatch):
    root, revision = repo
    def run(*args, **kwargs):
        (root / '.serena/project.yml').write_text('read_only: true\nignored_paths: [sample.py]\n')
        return json.dumps(dict(rows=[row()], server={'name':'Serena'})).encode()
    monkeypatch.setattr('kp_agent_tooling._impl.service.knowledge_navigation.CodeNavigationProvider._run', run)
    result = SerenaNavigationProvider('/unused').inspect(root, revision, 'sample.py::Example/read')
    assert result['status'] == 'unavailable'
    assert result['detail'] == 'workspace_changed_during_query'
    assert result['environment']['changes'][0]['path'] == '.serena/project.yml'
    assert result['environment']['untracked_added'] == []


@pytest.mark.xfail(strict=True, reason='S5 divergence: knowledge_navigation was retired (the legacy-navigation retirement commit); CodeNavigationProvider no longer exists to patch or import')
def test_generated_provider_file_is_disclosed_and_refused(repo, monkeypatch):
    root, revision = repo
    def run(*args, **kwargs):
        (root / '.serena/.gitignore').write_text('cache/\n')
        return json.dumps(dict(rows=[row()], server={'name':'Serena'})).encode()
    monkeypatch.setattr('kp_agent_tooling._impl.service.knowledge_navigation.CodeNavigationProvider._run', run)
    result = SerenaNavigationProvider('/unused').inspect(root, revision, 'sample.py::Example/read')
    assert result['status'] == 'unavailable'
    assert result['environment']['untracked_added'] == ['.serena/.gitignore']
    assert result['environment']['changes'][0]['before']['status'] == 'absent'


@pytest.mark.xfail(strict=True, reason='S5 divergence: knowledge_navigation was retired (the legacy-navigation retirement commit); CodeNavigationProvider no longer exists to patch or import')
def test_environment_identity_has_no_config_contents(repo, monkeypatch):
    monkeypatch.setattr('kp_agent_tooling._impl.service.knowledge_navigation.CodeNavigationProvider._run',
        lambda *a, **kw: json.dumps(dict(rows=[row()], server={'name':'Serena'})).encode())
    result = SerenaNavigationProvider('/unused').inspect(*repo, 'sample.py::Example/read')
    assert result['status'] == 'ok'
    assert len(result['environment']['inputs']['.serena/project.yml']['sha256']) == 64
    assert 'read_only' not in json.dumps(result['environment'])


@pytest.mark.xfail(strict=True, reason='S5 divergence: knowledge_navigation was retired (the legacy-navigation retirement commit); CodeNavigationProvider no longer exists to patch or import')
def test_selective_compact_lookup_retains_source_provenance(repo, monkeypatch):
    captured = {}
    def run(_runner, args, **_kwargs):
        captured.update(json.loads(args[-1]))
        return json.dumps(dict(rows=[{key: value for key, value in row().items() if key != 'body'}],
            server={'name': 'Serena'}, references={'state': 'not_requested'})).encode()
    monkeypatch.setattr('kp_agent_tooling._impl.service.knowledge_navigation.CodeNavigationProvider._run', run)
    result = SerenaNavigationProvider('/unused').inspect(*repo, 'sample.py::Example/read',
        include_body=False, include_references=False, response_mode='compact')
    assert result['status'] == 'ok'
    assert captured['include_body'] is False and captured['include_references'] is False
    assert result['diagnostic']['reference_state'] == 'not_requested'
    citation = result['report']['citations'][0]
    assert 'excerpt' not in citation
    assert citation['retrieval']['operation'] == 'navigation.source'
    assert citation['revision'] == repo[1] and len(citation['excerpt_sha256']) == 64


@pytest.mark.xfail(strict=True, reason='S5 divergence: knowledge_navigation was retired (the legacy-navigation retirement commit); CodeNavigationProvider no longer exists to patch or import')
def test_shortened_provider_result_is_explicitly_incomplete(repo, monkeypatch):
    monkeypatch.setattr('kp_agent_tooling._impl.service.knowledge_navigation.CodeNavigationProvider._run',
        lambda *a, **kw: json.dumps(dict(rows='Too many symbols', server={'name': 'Serena'})).encode())
    result = SerenaNavigationProvider('/unused').inspect(*repo, 'sample.py::read')
    assert result['status'] == 'unavailable'
    assert result['diagnostic']['state'] == 'truncated'


@pytest.mark.xfail(strict=True, reason='S5 divergence: knowledge_navigation was retired (the legacy-navigation retirement commit); CodeNavigationProvider no longer exists to patch or import')
def test_reference_error_is_visible(repo, monkeypatch):
    monkeypatch.setattr('kp_agent_tooling._impl.service.knowledge_navigation.CodeNavigationProvider._run',
        lambda *a, **kw: json.dumps(dict(rows=[row()], server={'name': 'Serena'},
            references={'state': 'provider_error'})).encode())
    result = SerenaNavigationProvider('/unused').inspect(*repo, 'sample.py::Example/read')
    assert result['status'] == 'ok'
    assert result['diagnostic']['reference_state'] == 'provider_error'
    assert result['report']['traversal']['completeness'] == 'not-established'


@pytest.mark.xfail(strict=True, reason='S5 divergence: knowledge_navigation was retired (the legacy-navigation retirement commit); CodeNavigationProvider no longer exists to patch or import')
@pytest.mark.parametrize('operation', ['find', 'overview', 'inspect'])
def test_subprocess_missing_diagnostics_are_explicit(repo, monkeypatch, operation):
    from kp_agent_tooling._impl.service.knowledge_navigation import NavigationSubprocessError
    def fail(*args, **kwargs):
        raise NavigationSubprocessError(17)
    monkeypatch.setattr('kp_agent_tooling._impl.service.knowledge_navigation.CodeNavigationProvider._run', fail)
    provider = SerenaNavigationProvider('/configured/serena')
    arg = 'sample.py' if operation == 'overview' else 'sample.py::Example/read' if operation == 'inspect' else 'read'
    result = getattr(provider, operation)(*repo, arg)
    assert result['status'] == 'unavailable'
    report = result['error_reporting']
    assert report['error_details_status'] == 'unavailable'
    assert report['exit_code'] == 17
    assert report['underlying_cause'] == 'not-established'
    assert 'stderr was not captured' in report['message']
    assert result['absence_verdict'] == 'not-established'


@pytest.mark.xfail(strict=True, reason='S5 divergence: knowledge_navigation was retired (the legacy-navigation retirement commit); CodeNavigationProvider no longer exists to patch or import')
def test_real_subprocess_reports_exit_without_leaking_stderr(tmp_path):
    import sys
    from kp_agent_tooling._impl.service.knowledge_navigation import CodeNavigationProvider, NavigationSubprocessError
    runner = CodeNavigationProvider(tmp_path, tmp_path)
    with pytest.raises(NavigationSubprocessError) as caught:
        runner._run([sys.executable, '-c', 'import sys; sys.stderr.write("private diagnostic"); sys.exit(17)'], cwd=tmp_path)
    assert caught.value.returncode == 17
    assert 'private diagnostic' not in str(caught.value)


def test_empty_exception_explicitly_reports_missing_detail():
    from kp_agent_tooling._impl.service.serena_navigation import failure_reporting
    report = failure_reporting(ValueError())
    assert report['error_details_status'] == 'unavailable'
    assert 'No error detail was supplied' in report['message']
    assert report['exit_code'] is None


@pytest.mark.parametrize('operation', ['find', 'inspect'])
def test_match_limit_error_crosses_real_worker_and_mcp_boundary(repo, tmp_path, operation):
    import sys
    import shlex
    server = tmp_path / 'fake-serena'
    program = '''from mcp.server.fastmcp import FastMCP
m = FastMCP('fixture')
@m.tool()
def find_symbol(name_path_pattern: str, relative_path: str, include_body: bool,
                include_info: bool, depth: int, max_matches: int, max_answer_chars: int):
    raise ValueError(f'Matched 8>max_matches={max_matches} symbols. SECRET_SOURCE_MUST_NOT_LEAK')
m.run(transport='stdio')
'''
    server.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(program) + ' \"$@\"\n')
    server.chmod(0o755)
    provider = SerenaNavigationProvider(str(server))
    result = getattr(provider, operation)(*repo, 'read' if operation == 'find' else 'sample.py::Example/read', max_matches=5)
    assert result['status'] == 'unavailable'
    assert result['absence_verdict'] == 'not-established'
    diagnostic = result['error_reporting']
    assert diagnostic['underlying_cause'] == 'match_limit_exceeded'
    assert diagnostic['matched_count'] == 8
    assert diagnostic['requested_limit'] == 5
    assert diagnostic['error_details_status'] == 'reported'
    assert 'Narrow path' in diagnostic['next_action']
    assert 'SECRET_SOURCE' not in json.dumps(result)


def test_unrecognized_provider_error_does_not_expose_raw_content():
    from kp_agent_tooling._impl.service.serena_navigation import provider_failure, ProviderToolError, failure_reporting
    result = failure_reporting(ProviderToolError(provider_failure('private/token=secret source text')))
    assert result['underlying_cause'] == 'provider_tool_error'
    assert result['error_details_status'] == 'unavailable'
    assert 'secret' not in json.dumps(result)


@pytest.mark.xfail(strict=True, reason='S5 divergence: knowledge_navigation was retired (the legacy-navigation retirement commit); CodeNavigationProvider no longer exists to patch or import')
@pytest.mark.parametrize('limit', [None, 100])
def test_raised_match_limits_reach_worker(repo, monkeypatch, limit):
    seen = []
    def run(self, argv, **kwargs):
        seen.append(json.loads(argv[-1])['max_matches'])
        return json.dumps(dict(rows=[], server={})).encode()
    monkeypatch.setattr('kp_agent_tooling._impl.service.knowledge_navigation.CodeNavigationProvider._run', run)
    result = SerenaNavigationProvider('/unused').find(*repo, 'read', **({} if limit is None else {'max_matches':limit}))
    assert result['status'] == 'ok'
    assert seen == [25 if limit is None else limit]


def test_match_limit_remains_bounded(repo):
    assert SerenaNavigationProvider('/unused').find(*repo, 'read', max_matches=101)['status'] == 'unavailable'
