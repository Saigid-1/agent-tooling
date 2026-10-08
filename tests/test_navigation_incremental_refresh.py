"""Repository reuse is admitted by source, input identity and artifact integrity."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import zipfile
import shutil

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'packages/tooling/src/kp_agent_tooling/refresh_cli.py'


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE).decode().strip()


def commit(repo, name, text):
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text)
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'fixture')


@pytest.fixture
def rig(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('refresh_navigation_incremental', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    roots = {}
    for key in ('core', 'ops'):
        repo = tmp_path / key
        repo.mkdir()
        git(repo, 'init', '-q', '-b', 'main')
        git(repo, 'config', 'user.name', 'Test')
        git(repo, 'config', 'user.email', 'test@example.invalid')
        commit(repo, 'pyproject.toml', '[project]\nname="fixture"\nversion="1.0"\n')
        commit(repo, ('kp_core' if key == 'core' else 'kp_ops') + '/mod.py', 'def hello(): return 1\n')
        git(repo, 'remote', 'add', 'origin', str(repo))
        roots[key] = repo
    toolchain = tmp_path / 'toolchain'
    toolchain.mkdir()
    (toolchain / 'package-lock.json').write_text('{}')
    template = tmp_path / 'project.yml'
    template.write_text('read_only: true\n')
    request = {'publication': str(tmp_path / 'current.json'), 'check_status': str(tmp_path / 'status.json'),
               'output_root': str(tmp_path / 'snapshots'), 'toolchain': str(toolchain), 'node': 'node',
               'repositories': {key: {'repository': str(repo), 'python_scope': ['kp_core' if key == 'core' else 'kp_ops'],
                                      'serena_template': str(template),
                                      'analysis_dependencies': ['core']}
                                for key, repo in roots.items()}}
    request['repositories']['core'].update(python_package_name='kp-core',
                                            python_version_source='pyproject')
    real_run = module.run
    calls = []

    def fake_run(args, **kwargs):
        if args[0].endswith('/scip-python'):
            Path(args[args.index('--output') + 1]).write_bytes(b'scip')
            return ''
        if args[0] == 'node':
            return '{}'
        return real_run(args, **kwargs)

    def fake_build(repo, revision, key, decoded, *, provenance, prefix=''):
        from kp_agent_tooling._impl.scip_navigation import digest
        calls.append(key)
        path = ('kp_core' if key == 'core' else 'kp_ops') + '/mod.py'
        data = {'schema': 'ops.scip-navigation.v1', 'repo_key': key, 'revision': revision,
                'blobs': {path: git(repo, 'rev-parse', 'HEAD:' + path)},
                'occurrences': [{'path': path, 'symbol': 'hello', 'definition': True,
                                 'byte_offset': 0, 'byte_length': 3,
                                 'blob_sha': git(repo, 'rev-parse', 'HEAD:' + path)}],
                'gaps': [], 'limitations': []}
        return {'sha256': digest(data), 'data': data}

    monkeypatch.setattr(module, 'run', fake_run)
    monkeypatch.setattr(module, 'build_index', fake_build)
    monkeypatch.setattr(module, 'verify_index_lookup', lambda index, repo, revision, key: {
        'status': 'ok', 'path': ('kp_core' if key == 'core' else 'kp_ops') + '/mod.py', 'line': 1})
    return module, request, roots, calls


def test_unchanged_fastpath_validates_without_build(rig):
    module, request, roots, calls = rig
    assert module.refresh(request)['status'] == 'published'
    assert calls == ['core', 'ops']
    assert module.refresh(request)['status'] == 'current'
    assert calls == ['core', 'ops']


def test_changed_one_repo_keeps_other_exact_artifacts(rig):
    module, request, roots, calls = rig
    module.refresh(request)
    first = json.loads(Path(request['publication']).read_text())
    first_catalog = json.loads(Path(first['knowledge_config']).read_text())
    commit(roots['ops'], 'kp_ops/mod.py', 'def hello(): return 2\n')
    assert module.refresh(request)['status'] == 'published'
    second = json.loads(Path(request['publication']).read_text())
    second_catalog = json.loads(Path(second['knowledge_config']).read_text())
    assert calls == ['core', 'ops', 'ops']
    assert second['repos']['core']['path'] == first['repos']['core']['path']
    assert second_catalog['scip_indexes']['core'] == first_catalog['scip_indexes']['core']
    assert second['repository_readiness']['core']['scip'] == 'validated_reuse'


@pytest.mark.parametrize('damage', ['missing', 'corrupt'])
def test_invalid_old_partition_forces_only_affected_rebuild(rig, damage):
    module, request, roots, calls = rig
    module.refresh(request)
    profile = json.loads(Path(request['publication']).read_text())
    catalog = json.loads(Path(profile['knowledge_config']).read_text())
    index = Path(catalog['scip_indexes']['core'][0]['path'])
    manifest = json.loads(index.read_text())
    part = index.parent / manifest['data']['parts'][0]['file']
    if damage == 'missing':
        part.unlink()
    else:
        part.write_bytes(b'corrupt')
    assert module.refresh(request)['status'] == 'published'
    assert calls == ['core', 'ops', 'core']
    assert module.refresh(request)['status'] == 'current'


def test_analysis_dependency_change_rebuilds_python_indexes(rig):
    module, request, roots, calls = rig
    module.refresh(request)
    commit(roots['core'], 'kp_core/mod.py', 'def hello(): return 3\n')
    module.refresh(request)
    assert calls == ['core', 'ops', 'core', 'ops']


def test_analysis_dependency_contract_change_rebuilds_dependents(rig):
    module, request, roots, calls = rig
    module.refresh(request)
    request['repositories']['core']['python_package_name'] = 'renamed-core'
    module.refresh(request)
    assert calls == ['core', 'ops', 'core', 'ops']


def test_failed_changed_repo_preserves_last_good(rig, monkeypatch):
    module, request, roots, calls = rig
    module.refresh(request)
    original = Path(request['publication']).read_bytes()
    commit(roots['ops'], 'kp_ops/mod.py', 'def hello(): return 4\n')
    original_build = module.build_index

    def failing_build(repo, revision, key, decoded, **kwargs):
        if key == 'ops':
            raise RuntimeError('fixture index failure')
        return original_build(repo, revision, key, decoded, **kwargs)

    monkeypatch.setattr(module, 'build_index', failing_build)
    with pytest.raises(module.RefreshStepError) as failure:
        module.refresh(request)
    assert Path(request['publication']).read_bytes() == original
    receipt = module.failure_receipt(failure.value)
    assert receipt['repo_key'] == 'ops' and receipt['stage'] == 'python_index'


def semantic_fixture(rig, monkeypatch):
    module, request, roots, calls = rig
    import kp_agent_tooling._impl.embeddings.embedders as providers
    import kp_agent_tooling._impl.semantic_index as semantic
    from kp_agent_tooling._impl.semantic_index import SemanticIndexError
    semantic_calls = []

    class Embedder:
        def __init__(self, *, model_digest):
            self.revision = SimpleNamespace(model_id='fixture', model_digest=model_digest,
                                            revision_id=model_digest)
            self.dim = 2

    def build(config, key, revision, *, embedder, out_dir, deadline_seconds):
        semantic_calls.append(key)
        build.deadlines.append((key, deadline_seconds))
        if key == 'ops' and request.get('fail_semantic'):
            raise SemanticIndexError('byte_budget', '32000001 bytes exceeds 32000000')
        return {'index_sha256': 'a' * 64, 'chunk_count': 1, 'files_indexed': 1,
                'files_skipped': {}, 'build_seconds': 0, 'embedding': {'model_digest': embedder.revision.model_digest}}

    build.deadlines = []
    monkeypatch.setattr(providers, 'RealEmbedder', Embedder)
    monkeypatch.setattr(semantic, 'build_semantic_index', build)
    monkeypatch.setattr(semantic, 'validate_semantic_index', lambda *args, **kwargs: {'index_sha256': 'a' * 64})
    request['semantic'] = True
    return semantic_calls, providers, semantic


@pytest.mark.parametrize('change', ['provider', 'policy', 'chunking'])
def test_semantic_identity_change_rebuilds_without_reindexing_scip(rig, monkeypatch, change):
    semantic_calls, providers, semantic = semantic_fixture(rig, monkeypatch)
    module, request, roots, calls = rig
    module.refresh(request)
    assert module.refresh(request)['status'] == 'current'
    assert semantic_calls == ['core', 'ops'] or semantic_calls == ['ops', 'core']
    if change == 'provider':
        monkeypatch.setattr(providers, 'REAL_MODEL_WEIGHTS_DIGEST', 'sha256:new-provider')
    elif change == 'policy':
        import kp_agent_tooling._impl.repository_manifest as manifest
        monkeypatch.setattr(manifest, 'POLICY_VERSION', 'test-policy-v2')
    else:
        monkeypatch.setattr(semantic, 'CHUNKING', {**semantic.CHUNKING, 'window_lines': 30})
    result = module.refresh(request)
    assert result['status'] == 'published'
    assert sorted(semantic_calls) == ['core', 'core', 'ops', 'ops']
    assert calls == ['core', 'ops']


def test_semantic_budget_is_per_repository_actionable_readiness(rig, monkeypatch):
    semantic_calls, _, _ = semantic_fixture(rig, monkeypatch)
    module, request, roots, calls = rig
    request['fail_semantic'] = True
    result = module.refresh(request)
    assert result['status'] == 'published'
    readiness = result['repository_readiness']
    assert readiness['ops']['semantic_reason'] == 'byte_budget'
    assert 'source budget' in readiness['ops']['remediation']
    assert readiness['core']['semantic'] == 'built'
    assert sorted(semantic_calls) == ['core', 'ops']
    second = module.refresh(request)
    assert second['status'] == 'current'
    assert second['repository_readiness']['ops']['semantic'] == 'unavailable'
    assert sorted(semantic_calls) == ['core', 'ops']
    profile_path = Path(request['publication'])
    profile = json.loads(profile_path.read_text())
    profile['repository_readiness']['ops']['semantic_retry_after'] = '2000-01-01T00:00:00+00:00'
    profile_path.write_text(json.dumps(profile))
    assert module.refresh(request)['status'] == 'published'
    assert semantic_calls.count('ops') == 2


def test_repository_semantic_deadline_preserves_selected_scope_and_invalidates_failed_retry(rig, monkeypatch):
    semantic_calls, _, semantic = semantic_fixture(rig, monkeypatch)
    module, request, roots, calls = rig
    request['fail_semantic'] = True
    first = module.refresh(request)
    assert first['repository_readiness']['ops']['semantic'] == 'unavailable'
    assert sorted(semantic.build_semantic_index.deadlines) == [('core', 900), ('ops', 900)]
    request['repositories']['ops']['semantic_deadline_seconds'] = 2400
    second = module.refresh(request)
    assert second['status'] == 'published'
    assert semantic_calls.count('ops') == 2
    assert semantic_calls.count('core') == 1
    assert semantic.build_semantic_index.deadlines[-1] == ('ops', 2400)
    assert calls == ['core', 'ops']  # A budget change cannot alter SCIP source scope.


@pytest.mark.parametrize('budget', [None, True, False, 59, 3601, 900.0, '900'])
def test_invalid_semantic_deadline_fails_before_source_or_model_work(rig, monkeypatch, budget):
    module, request, roots, calls = rig
    request['semantic'] = True
    request['repositories']['ops']['semantic_deadline_seconds'] = budget
    import kp_agent_tooling._impl.embeddings.embedders as providers
    monkeypatch.setattr(providers, 'RealEmbedder',
                        lambda **kwargs: (_ for _ in ()).throw(AssertionError('model initialized')))
    monkeypatch.setattr(module, 'resolve_source',
                        lambda spec: (_ for _ in ()).throw(AssertionError('source fetched')))
    with pytest.raises(ValueError, match='semantic_deadline_seconds'):
        module.refresh(request)
    assert not Path(request['publication']).exists()
    assert not Path(request['output_root']).exists()
    assert calls == []


def test_unavailable_embedder_still_publishes_scip(rig, monkeypatch):
    module, request, roots, calls = rig
    import kp_agent_tooling._impl.embeddings.embedders as providers
    monkeypatch.setattr(providers, 'RealEmbedder', lambda **kwargs: (_ for _ in ()).throw(RuntimeError('unavailable')))
    request['semantic'] = True
    result = module.refresh(request)
    assert result['status'] == 'published'
    assert calls == ['core', 'ops']
    assert all(row['semantic_reason'] == 'RuntimeError' for row in result['repository_readiness'].values())
    assert module.refresh(request)['status'] == 'current'


def test_refresh_algorithm_change_invalidates_reuse(rig, monkeypatch):
    module, request, roots, calls = rig
    module.refresh(request)
    original_read = Path.read_bytes

    def changed_script(self):
        raw = original_read(self)
        return raw + b'\n# changed build rules' if self == SCRIPT else raw

    monkeypatch.setattr(Path, 'read_bytes', changed_script)
    module.refresh(request)
    assert calls == ['core', 'ops', 'core', 'ops']


@pytest.mark.skip(reason='S5 excluded: builds the OPS-only portable_tooling/ extraction tree')
def test_wheel_refresh_uses_installed_policy_and_semantic_sources(tmp_path):
    root = SCRIPT.parents[1]
    staged = tmp_path / 'portable_tooling'
    shutil.copytree(root / 'portable_tooling', staged,
                    ignore=shutil.ignore_patterns('build', 'dist', '*.egg-info', '__pycache__'))
    setup = staged / 'setup.py'
    setup.write_text(setup.read_text().replace(
        'ROOT = Path(__file__).resolve().parents[1]', f'ROOT = Path({str(root)!r})'))
    wheels = tmp_path / 'wheels'
    wheels.mkdir()
    subprocess.check_call([sys.executable, '-m', 'pip', 'wheel', '--no-build-isolation', '--no-deps',
                           '-w', str(wheels), str(staged)], stdout=subprocess.DEVNULL)
    wheel, = wheels.glob('kp_agent_tooling-*.whl')
    installed = tmp_path / 'installed'
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed)
    toolchain = tmp_path / 'toolchain'
    toolchain.mkdir()
    (toolchain / 'package-lock.json').write_text('{}')
    template = tmp_path / 'project.yml'
    template.write_text('read_only: true\n')
    program = '''
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
from kp_agent_tooling import refresh_cli
root = pathlib.Path(sys.argv[2])
refresh_cli.resolve_source = lambda spec: {'revision':'a'*40,'default_ref':'refs/heads/main','selection':'remote_default'}
def stop(args, **kwargs):
    if args[:2] == ['git', 'clone']:
        raise RuntimeError('identity-complete')
    raise AssertionError(args)
refresh_cli.run = stop
request = {'publication':str(root/'current.json'),'check_status':str(root/'check.json'),
    'output_root':str(root/'snapshots'),'toolchain':str(root/'toolchain'),'node':'node',
    'semantic':True,'repositories':{'core':{'repository':str(root/'source'),
      'python_scope':['kp_core'],'serena_template':str(root/'project.yml')}}}
try:
    refresh_cli.refresh(request)
except refresh_cli.RefreshStepError as error:
    assert error.stage == 'source_snapshot'
    assert str(error.__cause__) == 'identity-complete'
else:
    raise AssertionError('identity stage did not finish')
'''
    subprocess.check_call([sys.executable, '-I', '-c', program, str(installed), str(tmp_path)])


def test_validated_source_indexes_publish_before_optional_semantic_work(rig, monkeypatch):
    module, request, roots, calls = rig
    _, _, semantic = semantic_fixture(rig, monkeypatch)
    original = semantic.build_semantic_index
    observed = []
    def inspect(config, key, revision, **kwargs):
        profile = json.loads(Path(request['publication']).read_text())
        status = json.loads(Path(request['check_status']).read_text())
        assert profile['repos'][key]['revision'] == revision
        assert profile['repository_readiness'][key]['scip'] == 'built'
        assert status['source_navigation'] == 'published' and status['stage'] == 'optional_semantic'
        assert set(profile['semantic_indexes']) == set(observed)  # only completed repos published
        assert all(profile['repository_readiness'][done]['semantic'] == 'built' for done in observed)
        observed.append(key)
        return original(config, key, revision, **kwargs)
    monkeypatch.setattr(semantic, 'build_semantic_index', inspect)
    result = module.refresh(request)
    assert set(observed) == {'core', 'ops'} and result['status'] == 'published'
