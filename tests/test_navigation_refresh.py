"""A failed refresh must preserve the published source boundary."""
import json
from pathlib import Path
import subprocess
import sys

import importlib.util
import pytest


def refresh_module():
    script = Path(__file__).resolve().parents[1] / 'packages/tooling/src/kp_agent_tooling/refresh_cli.py'
    spec = importlib.util.spec_from_file_location('refresh_navigation', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_platform_publication_requires_explicit_membership():
    module = refresh_module()
    sources = {'product': {'revision': 'a' * 40, 'artifacts': []}}
    request = {'repositories': {'product': {}}}
    assert module.platforms_for_request(request, sources) == {}
    assert module.catalog_payload(request, {'product': {}}, sources, {})['platforms'] == {}


def test_explicit_platform_preserves_named_owner_profile_and_members():
    module = refresh_module()
    sources = {'ats': {'revision': 'a' * 40, 'artifacts': []},
               'core': {'revision': 'b' * 40, 'artifacts': []}}
    request = {'repositories': {'ats': {}, 'core': {}},
               'platforms': {'ats': {'owner': 'OPS', 'profile': 'dev-current',
                                     'repositories': ['ats', 'core']}}}
    assert module.catalog_payload(request, {}, sources, {})['platforms'] == {
        'ats': {'schema': 'ops.platform-request.v1', 'owner': 'OPS', 'profile': 'dev-current',
                'sources': sources}}
    request['platforms']['ats']['repositories'] = ['ats', 'missing']
    with pytest.raises(ValueError, match='unknown repository'):
        module.platforms_for_request(request, sources)
    request['platforms']['ats']['repositories'] = ['ats', 'ats']
    with pytest.raises(ValueError, match='unique'):
        module.platforms_for_request(request, sources)


def test_single_unfamiliar_repository_refresh_has_no_invented_platform_or_core(tmp_path, monkeypatch):
    module = refresh_module()
    repo = tmp_path / 'unfamiliar'
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    git('init', '-q', '-b', 'main')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    (repo / 'main.py').write_text('def hello(): return 1\n')
    git('add', 'main.py')
    git('commit', '-qm', 'fixture')
    git('remote', 'add', 'origin', str(repo))
    revision = git('rev-parse', 'HEAD')
    toolchain = tmp_path / 'toolchain'
    toolchain.mkdir()
    (toolchain / 'package-lock.json').write_text('{}')
    template = tmp_path / 'serena.yml'
    template.write_text('read_only: true\n')
    request = {'publication': str(tmp_path / 'current.json'), 'check_status': str(tmp_path / 'status.json'),
               'output_root': str(tmp_path / 'snapshots'), 'toolchain': str(toolchain), 'node': 'node',
               'repositories': {'product': {'repository': str(repo), 'python_scope': ['main.py'],
                                            'serena_template': str(template)}}}
    real_run = module.run
    commands = []
    def fake_run(args, **kwargs):
        if args[0].endswith('/scip-python'):
            commands.append(args)
            Path(args[args.index('--output') + 1]).write_bytes(b'scip')
            return ''
        if args[0] == 'node':
            return '{}'
        return real_run(args, **kwargs)
    def fake_build(root, source_revision, key, decoded, *, provenance, prefix=''):
        from kp_agent_tooling._impl.scip_navigation import digest
        blob = subprocess.check_output(['git', '-C', root, 'rev-parse', 'HEAD:main.py'], text=True).strip()
        data = {'schema': 'ops.scip-navigation.v1', 'repo_key': key, 'revision': source_revision,
                'blobs': {'main.py': blob},
                'occurrences': [{'path': 'main.py', 'symbol': 'hello', 'definition': True,
                                 'byte_offset': 4, 'byte_length': 5, 'blob_sha': blob}],
                'gaps': [], 'limitations': []}
        return {'sha256': digest(data), 'data': data}
    monkeypatch.setattr(module, 'run', fake_run)
    monkeypatch.setattr(module, 'build_index', fake_build)
    monkeypatch.setattr(module, 'verify_index_lookup', lambda *args: {
        'status': 'ok', 'path': 'main.py', 'line': 1})
    assert module.refresh(request)['status'] == 'published'
    profile = json.loads(Path(request['publication']).read_text())
    catalog = json.loads(Path(profile['knowledge_config']).read_text())
    assert catalog['platforms'] == {}
    assert set(catalog['repositories']) == {'product'}
    assert commands[0][commands[0].index('--project-name') + 1] == 'product'
    assert commands[0][commands[0].index('--project-version') + 1] == revision
    environment = Path(commands[0][commands[0].index('--environment') + 1])
    assert json.loads(environment.read_text()) == []


def test_failed_refresh_preserves_last_good_publication(tmp_path):
    publication=tmp_path/'current.json';original=b'{"profile":"last-good"}\n';publication.write_bytes(original)
    toolchain=tmp_path/'toolchain';toolchain.mkdir();(toolchain/'package-lock.json').write_text('{}')
    template=tmp_path/'project.yml';template.write_text('read_only: true\n')
    request={'publication':str(publication),'check_status':str(tmp_path/'status.json'),'output_root':str(tmp_path/'snapshots'),
             'toolchain':str(toolchain),'repositories':{'missing':{'repository':str(tmp_path/'not-a-repo'),'serena_template':str(template)}}}
    config=tmp_path/'request.json';config.write_text(json.dumps(request))
    script=Path(__file__).resolve().parents[1]/'packages/tooling/src/kp_agent_tooling/refresh_cli.py'
    result=subprocess.run([sys.executable,str(script),'--request',str(config)],capture_output=True,text=True)
    assert result.returncode==1
    assert publication.read_bytes()==original
    status=json.loads((tmp_path/'status.json').read_text())
    assert status['status']=='refresh_failed' and status['last_good_profile']=='preserved'
    assert 'not-a-repo' not in result.stdout


def test_explicit_product_branch_never_falls_back_to_remote_head(tmp_path):
    import importlib.util
    import pytest
    spec=importlib.util.spec_from_file_location('refresh_navigation',Path(__file__).resolve().parents[1]/'packages/tooling/src/kp_agent_tooling/refresh_cli.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    repo=tmp_path/'repo';repo.mkdir()
    def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.PIPE).decode().strip()
    git('init','-q','-b','main');git('config','user.name','Test');git('config','user.email','test@example.invalid')
    (repo/'file').write_text('main');git('add','.');git('commit','-qm','main')
    main=git('rev-parse','HEAD');git('checkout','-qb','product/tooling')
    (repo/'file').write_text('product');git('commit','-am','product');product=git('rev-parse','HEAD')
    git('checkout','main');git('remote','add','origin',str(repo))
    assert module.resolve_source({'repository':str(repo)})['revision']==main
    assert module.resolve_source({'repository':str(repo),'ref':'refs/heads/product/tooling'})['revision']==product
    with pytest.raises(ValueError,match='unavailable'):
        module.resolve_source({'repository':str(repo),'ref':'refs/heads/absent'})
