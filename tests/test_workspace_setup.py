import json
from pathlib import Path
import subprocess
import sys

import pytest

from kp_agent_tooling._impl.workspace_setup import apply, catalog, plan
from kp_agent_tooling._impl.service.agent_tooling import AgentTooling


def git(repo, *args):
    return subprocess.check_output(['git', *args], cwd=repo, text=True).strip()


@pytest.fixture
def request_data(tmp_path):
    repo = tmp_path / 'product with spaces'
    repo.mkdir()
    git(repo, 'init', '-q')
    (repo / 'main.py').write_text('import pathlib\n')
    git(repo, 'add', 'main.py')
    git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'source')
    return {'schema_version': 'ops.workspace-setup.v1', 'repo_key': 'product',
            'repository': str(repo), 'output_root': str(tmp_path / 'configured workspace'),
            'launcher': {'command': sys.executable, 'args': ['/operator/tooling/cli.py']},
            'capabilities': ['source-navigation']}


def test_plan_then_apply_is_idempotent_and_actual_catalog_is_scoped(request_data):
    p = plan(request_data)
    assert not Path(request_data['output_root']).exists()
    first = apply(request_data, p['plan_sha256'])
    assert apply(request_data, p['plan_sha256']) == first
    folder = Path(first['bundle'])
    config = json.loads((folder / 'tooling.json').read_text())
    assert 'serena' not in config and 'gateway_config' not in config
    adapter = AgentTooling(folder / 'tooling.json')
    assert adapter.doctor()['status'] == 'ready'
    assert {t['name'] for t in adapter.tools()} == set(config['enabled_tools'])
    assert adapter.call('navigation.imports', {'repo_key': 'product', 'path': 'main.py'})
    dependencies = adapter.call('navigation.dependencies', {'repo_key': 'product', 'path': 'main.py'})
    assert dependencies['status'] == 'partial'
    assert dependencies['provider']['reason'] == 'provider_not_configured'
    registration = json.loads((folder / 'mcp.json').read_text())['mcpServers']['agent-tooling']
    assert registration['command'] == request_data['launcher']['command']
    assert registration['args'][-3:] == ['--config', str(folder / 'tooling.json'), 'serve']
    assert first['runtime_verification'] == 'not-run'


def test_fresh_repo_exposes_committed_source_and_paged_discovery(request_data):
    planned = plan(request_data)
    expected = {'tooling.identity', 'navigation.imports', 'navigation.dependencies',
                'delivery.read', 'navigation.source',
                'navigation.paths', 'navigation.search', 'navigation.manifest',
                'navigation.search_page'}
    assert set(planned['config']['enabled_tools']) == expected
    configured = apply(request_data, planned['plan_sha256'])
    adapter = AgentTooling(Path(configured['bundle']) / 'tooling.json')
    revision = planned['inputs']['revision']
    assert adapter.delivery.root.parent == Path(request_data['output_root']) / 'delivery'
    assert len(adapter.delivery.root.name) == 64
    assert {tool['name'] for tool in adapter.tools()} == expected
    assert adapter.call('tooling.identity', {})['status'] in {'known', 'unknown'}
    assert adapter.call('navigation.paths', {'repo_key': 'product', 'target_revision': revision})['results'][0]['path'] == 'main.py'
    assert adapter.call('navigation.source', {'repo_key': 'product', 'target_revision': revision,
                                              'path': 'main.py'})['status'] == 'ok'
    assert adapter.call('navigation.search', {'repo_key': 'product', 'target_revision': revision,
                                              'query': 'import pathlib'})['results'][0]['path'] == 'main.py'
    assert adapter.call('navigation.manifest', {'repo_key': 'product', 'target_revision': revision})['entries'][0]['path'] == 'main.py'
    page = adapter.call('navigation.search_page', {'repo_key': 'product', 'target_revision': revision,
                                                   'query': 'import pathlib'})
    assert page['status'] == 'ok' and page['results'][0]['path'] == 'main.py'
    availability = adapter.doctor()['tool_availability']
    assert availability['navigation.semantic']['reason'] == 'semantic_index_unavailable'
    assert availability['navigation.semantic']['next_action']
    assert availability['serena.inspect']['reason'] == 'provider_not_configured'


def test_direct_cli_plans_and_applies_without_json_request(request_data):
    script = Path(__file__).resolve().parents[1] / 'packages/tooling/src/kp_agent_tooling/setup_cli.py'
    options = ['--repository', request_data['repository'], '--repo-key', request_data['repo_key'],
               '--output-root', request_data['output_root'],
               '--launcher-command', request_data['launcher']['command']]
    options += [item for argument in request_data['launcher']['args']
                for item in ('--launcher-arg', argument)]
    planned = json.loads(subprocess.check_output([sys.executable, str(script), 'plan', *options], text=True))
    assert planned['config']['repos']['product']['revision'] == planned['inputs']['revision']
    applied = json.loads(subprocess.check_output([sys.executable, str(script), 'apply', *options,
                                                   '--expected-plan-sha256', planned['plan_sha256']], text=True))
    assert Path(applied['bundle'], 'tooling.json').is_file()


def test_doctor_names_configured_provider_loss(request_data):
    repo = Path(request_data['repository'])
    (repo / '.serena').mkdir()
    (repo / '.serena/project.yml').write_text('read_only: true\n')
    request_data['capabilities'].append('serena')
    request_data['serena'] = {'command': sys.executable, 'python': sys.executable,
                             'runtime_home': str(repo.parent / 'provider')}
    planned = plan(request_data)
    configured = apply(request_data, planned['plan_sha256'])
    adapter = AgentTooling(Path(configured['bundle']) / 'tooling.json')
    adapter.config['serena']['command'] = str(repo.parent / 'missing-serena')
    status = adapter.doctor()
    assert status['status'] == 'partial'
    assert status['tool_availability']['serena.inspect']['reason'] == 'provider_executable_unavailable'
    assert status['tool_availability']['serena.inspect']['next_action']


def test_doctor_respects_local_scip_catalog_without_gateway(request_data):
    planned = plan(request_data)
    configured = apply(request_data, planned['plan_sha256'])
    config_path = Path(configured['bundle']) / 'tooling.json'
    config = json.loads(config_path.read_text())
    catalog_path = Path(request_data['output_root']) / 'local-scip.json'
    catalog_path.write_text(json.dumps({'repositories': {'product': {'path': request_data['repository']}},
                                        'platforms': {}, 'scip_indexes': {}}))
    config['local_knowledge_config'] = str(catalog_path)
    config['enabled_tools'].append('knowledge.symbol')
    config_path.write_text(json.dumps(config))
    adapter = AgentTooling(config_path)
    assert 'knowledge.symbol' in {tool['name'] for tool in adapter.tools()}
    availability = adapter.doctor()['tool_availability']['knowledge.symbol']
    assert availability == {'status': 'configured', 'runtime_verification': 'not-assessed'}


def test_doctor_marks_requested_but_unadvertised_tool_partial(tmp_path):
    config = tmp_path / 'tooling.json'
    config.write_text(json.dumps({'schema_version': 'ops.agent-tooling.v1', 'repos': {},
                                  'enabled_tools': ['knowledge.symbol']}))
    report = AgentTooling(config).doctor()
    assert report['status'] == 'partial'
    assert report['tools'] == []
    assert report['tool_availability']['knowledge.symbol']['reason'] == 'gateway_not_configured'


def test_delivery_root_requires_physical_path(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(real, target_is_directory=True)
    config = tmp_path / 'tooling.json'
    config.write_text(json.dumps({'schema_version': 'ops.agent-tooling.v1', 'repos': {},
                                  'delivery_root': str(alias / 'delivery')}))
    with pytest.raises(ValueError, match='delivery_root'):
        AgentTooling(config)


def test_configured_delivery_root_isolated_by_config_identity(tmp_path):
    base = tmp_path / 'delivery'
    base.mkdir(mode=0o700)
    first_path, second_path = tmp_path / 'first.json', tmp_path / 'second.json'
    common = {'schema_version': 'ops.agent-tooling.v1', 'repos': {},
              'delivery_root': str(base), 'enabled_tools': ['delivery.read']}
    first_path.write_text(json.dumps({**common, 'source_identity': 'first'}))
    second_path.write_text(json.dumps({**common, 'source_identity': 'second'}))
    first, second = AgentTooling(first_path), AgentTooling(second_path)
    continuation = first.delivery.deliver({'source': 'first', 'body': 'x' * 50000})
    assert continuation['status'] == 'continued'
    assert first.delivery.read(continuation['token'])['status'] == 'ok'
    assert first.delivery.root.parent == second.delivery.root.parent == base
    assert first.delivery.root != second.delivery.root
    with pytest.raises(ValueError, match='delivery unavailable'):
        second.delivery.read(continuation['token'])


def test_doctor_marks_nonwritable_navigation_registry_partial(request_data):
    planned = plan(request_data)
    configured = apply(request_data, planned['plan_sha256'])
    registry = Path(request_data['output_root']) / 'navigation-registry'
    registry.chmod(0o500)
    try:
        report = AgentTooling(Path(configured['bundle']) / 'tooling.json').doctor()
        assert report['status'] == 'partial'
        availability = report['tool_availability']['navigation.search_page']
        assert availability['reason'] == 'navigation_registry_not_writable'
        assert availability['runtime_verification'] == 'not-assessed'
    finally:
        registry.chmod(0o700)


def test_head_change_or_dirty_source_prevents_apply(request_data):
    p = plan(request_data)
    repo = Path(request_data['repository'])
    (repo / 'main.py').write_text('import sys\n')
    with pytest.raises(ValueError, match='working-tree'):
        apply(request_data, p['plan_sha256'])
    git(repo, 'add', 'main.py')
    git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'change')
    with pytest.raises(ValueError, match='inputs changed'):
        apply(request_data, p['plan_sha256'])
    assert not Path(request_data['output_root']).exists()


def test_changed_bundle_is_never_overwritten(request_data):
    p = plan(request_data)
    first = apply(request_data, p['plan_sha256'])
    file = Path(first['bundle']) / 'tooling.json'
    file.write_text('{}')
    with pytest.raises(ValueError, match='was changed'):
        apply(request_data, p['plan_sha256'])
    assert file.read_text() == '{}'


def test_no_repo_script_execution_or_unsupported_capability(request_data):
    repo = Path(request_data['repository'])
    (repo / 'setup.sh').write_text('exit 99\n')
    assert plan(request_data)['status'] == 'configuration-ready'
    request_data['capabilities'].append('dispatch')
    with pytest.raises(ValueError, match='separate setup adapter'):
        plan(request_data)
    request_data['capabilities'].pop()
    request_data['hooks'] = ['setup.sh']
    with pytest.raises(ValueError, match='fields'):
        plan(request_data)


def test_output_cannot_pollute_source(request_data):
    request_data['output_root'] = request_data['repository'] + '/generated'
    with pytest.raises(ValueError, match='outside'):
        plan(request_data)


def test_serena_configuration_is_bound_to_reviewed_plan(request_data):
    repo = Path(request_data['repository'])
    (repo / '.serena').mkdir()
    project = repo / '.serena/project.yml'
    project.write_text('read_only: true\nlanguage_servers: [python]\n')
    request_data['capabilities'].append('serena')
    request_data['serena'] = {'command': sys.executable, 'python': sys.executable,
                             'runtime_home': str(repo.parent / 'provider')}
    p = plan(request_data)
    assert 'serena.inspect' in p['config']['enabled_tools']
    project.write_text('read_only: true\nlanguage_servers: [typescript]\n')
    with pytest.raises(ValueError, match='inputs changed'):
        apply(request_data, p['plan_sha256'])
    project.write_text('read_only: false\n')
    with pytest.raises(ValueError, match='read_only'):
        plan(request_data)


def test_compiler_digest_and_unknown_capabilities(request_data):
    from hashlib import sha256
    compiler = Path(request_data['repository']).parent / 'compiler.js'
    compiler.write_text('// fixture, never executed by setup')
    request_data['capabilities'].append('typescript')
    request_data['typescript'] = {'node': sys.executable, 'module': str(compiler),
                                  'sha256': sha256(compiler.read_bytes()).hexdigest()}
    p = plan(request_data)
    assert p['config']['typescript']['sha256'] == request_data['typescript']['sha256']
    compiler.write_text('// changed')
    with pytest.raises(ValueError, match='digest mismatch'):
        apply(request_data, p['plan_sha256'])
    assert next(x for x in catalog()['capabilities'] if x['id'] == 'memory')['setup'] == 'separate-adapter'
