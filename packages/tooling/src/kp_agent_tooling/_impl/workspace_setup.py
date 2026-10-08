"""Local configuration planning, independent of the OPS app and any UI.

Operator-owned requests only. No repository hooks, package installs, host mutation,
or provider processes run here. Configuration is not runtime verification.
"""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess

from kp_agent_tooling._impl import leaf


def encoded(value):
    return leaf.canonical_bytes(value, ascii=True, allow_nan=False)


def digest(value):
    return leaf.canonical_sha256(value, ascii=True, allow_nan=False)


def catalog():
    return {'schema_version': 'ops.workspace-catalog.v1', 'capabilities': [
        {'id': 'source-navigation', 'label': 'Code navigation', 'setup': 'supported',
         'requires': [], 'coverage': 'Committed source, paths, literal search, manifest, paged search, Python imports, dependency declarations'},
        {'id': 'serena', 'label': 'Symbols and references', 'setup': 'supported',
         'requires': ['source-navigation'], 'coverage': 'Existing Serena runtime and reviewed project configuration'},
        {'id': 'typescript', 'label': 'TypeScript import context', 'setup': 'supported',
         'requires': ['source-navigation'], 'coverage': 'Existing Node runtime and explicitly pinned compiler'},
        *[{'id': key, 'label': label, 'setup': 'separate-adapter', 'requires': []}
          for key, label in [('scip', 'Cross-repository indexes'), ('journeys', 'Reviewed journeys'),
                             ('dispatch', 'Agent dispatch'), ('memory', 'Persistent memory'),
                             ('sessions', 'Session observation')]]],
        'limits': ['Supported setup writes configuration only; it does not prove runtime readiness.',
                   'Separate-adapter capabilities are not automatically provisioned.']}


def _shape(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError('invalid configuration fields')


def _absolute(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('absolute local path required')
    return Path(value).resolve()


def _executable(value):
    path = _absolute(value)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError('configured executable unavailable')
    # Preserve venv/symlink invocation semantics, especially Python prefixes.
    return str(Path(value).absolute())


def _git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    return subprocess.check_output(['git', '--no-optional-locks', '-c', 'core.fsmonitor=false', *args], cwd=repo, env=env,
                                   stderr=subprocess.PIPE, timeout=15).decode().strip()


def plan(request):
    _shape(request, ('schema_version', 'repo_key', 'repository', 'output_root', 'launcher', 'capabilities'),
           ('revision', 'serena', 'typescript'))
    if request['schema_version'] != 'ops.workspace-setup.v1' or len(encoded(request)) > 16384:
        raise ValueError('unsupported or oversized setup request')
    key = request['repo_key']
    if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,63}', key):
        raise ValueError('invalid repository key')
    selected = request['capabilities']
    if not isinstance(selected, list) or not all(isinstance(x, str) for x in selected):
        raise ValueError('capabilities must be a list of identifiers')
    if len(selected) != len(set(selected)) or 'source-navigation' not in selected:
        raise ValueError('unique capabilities including source-navigation required')
    if set(selected) - {'source-navigation', 'serena', 'typescript'}:
        raise ValueError('capability requires a separate setup adapter')
    repo, output = _absolute(request['repository']), _absolute(request['output_root'])
    if _git(repo, 'rev-parse', '--show-toplevel') != str(repo):
        raise ValueError('repository root required')
    if output == repo or repo in output.parents:
        raise ValueError('keep generated setup outside the source repository')
    revision = request.get('revision') or _git(repo, 'rev-parse', 'HEAD')
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', revision):
        raise ValueError('full source revision required')
    if revision != _git(repo, 'rev-parse', 'HEAD'):
        raise ValueError('target checkout mismatch')
    if _git(repo, 'status', '--porcelain', '--untracked-files=no'):
        raise ValueError('tracked working-tree changes must be resolved or isolated')
    launcher = request['launcher']
    _shape(launcher, ('command', 'args'))
    command = _executable(launcher['command'])
    if (not isinstance(launcher['args'], list) or len(launcher['args']) > 8 or
            not all(isinstance(x, str) and len(x) <= 4096 for x in launcher['args'])):
        raise ValueError('bounded launcher argument array required')
    registry = output / 'navigation-registry'
    delivery = output / 'delivery'
    config = {'schema_version': 'ops.agent-tooling.v1',
              'repos': {key: {'path': str(repo), 'revision': revision}},
              'navigation_registry_path': str(registry),
              'delivery_root': str(delivery),
              'enabled_tools': ['tooling.identity', 'navigation.paths', 'navigation.search',
                                'navigation.source', 'navigation.manifest', 'navigation.search_page',
                                'navigation.imports', 'navigation.dependencies',
                                'delivery.read']}
    inputs = {'revision': revision, 'tree': _git(repo, 'rev-parse', 'HEAD^{tree}')}
    gaps = ['Runtime invocation has not been tested.', 'Untracked and ignored files are outside committed source coverage.',
            'No index, maintained-document corpus, journey, dispatch or memory was provisioned.']
    for name in ('serena', 'typescript'):
        if (name in selected) != (name in request):
            raise ValueError('provider configuration and capability selection must agree: ' + name)
    if 'serena' in selected:
        provider = request['serena']
        _shape(provider, ('command', 'python', 'runtime_home'))
        config['serena'] = {'command': _executable(provider['command']), 'python': _executable(provider['python']),
                            'runtime_home': str(_absolute(provider['runtime_home']))}
        project = repo / '.serena/project.yml'
        if not project.is_file() or project.is_symlink() or project.stat().st_size > 65536:
            raise ValueError('reviewed Serena project.yml required (maximum 64KiB)')
        import yaml
        raw = project.read_bytes()
        settings = yaml.safe_load(raw)
        if not isinstance(settings, dict) or settings.get('read_only') is not True:
            raise ValueError('Serena project must declare read_only: true')
        inputs['serena_project_sha256'] = hashlib.sha256(raw).hexdigest()
        config['enabled_tools'].insert(0, 'serena.inspect')
        gaps.append('Serena project contents are operator-reviewed; read_only does not sandbox language servers.')
    if 'typescript' in selected:
        provider = request['typescript']
        _shape(provider, ('node', 'module', 'sha256'))
        compiler = _absolute(provider['module'])
        if not compiler.is_file() or compiler.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('bounded TypeScript compiler file required')
        actual = hashlib.sha256(compiler.read_bytes()).hexdigest()
        if actual != provider['sha256']:
            raise ValueError('compiler digest mismatch')
        config['typescript'] = {'node': _executable(provider['node']), 'module': str(compiler), 'sha256': actual}
        inputs['compiler_sha256'] = actual
    result = {'schema_version': 'ops.workspace-plan.v1', 'status': 'configuration-ready',
              'request': request, 'inputs': inputs, 'config': config, 'launcher': {'command': command, 'args': launcher['args']},
              'output_root': str(output), 'runtime_verification': 'not-run', 'gaps': gaps}
    result['plan_sha256'] = digest(result)
    return result


def apply(request, expected_plan_sha256):
    """Recheck the reviewed request and atomically publish a new local bundle."""
    result = plan(request)
    if result['plan_sha256'] != expected_plan_sha256:
        raise ValueError('setup inputs changed; review a new plan')
    root = Path(result['output_root'])
    root.mkdir(parents=True, exist_ok=True)
    for name in ('navigation_registry_path', 'delivery_root'):
        directory = Path(result['config'][name])
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError(name + ' must be a real directory')
        leaf.mkdir_private(directory, exist_ok=True)
    destination = root / expected_plan_sha256
    launch = result['launcher']
    registration = {'mcpServers': {'agent-tooling': {'command': launch['command'],
        'args': launch['args'] + ['--config', str(destination / 'tooling.json'), 'serve']}}}
    files = {'tooling.json': result['config'], 'mcp.json': registration, 'plan.json': result}
    expected = {name: encoded(value) + b'\n' for name, value in files.items()}
    if destination.exists():
        if destination.is_symlink() or not destination.is_dir() or set(p.name for p in destination.iterdir()) != set(expected):
            raise ValueError('existing bundle does not match plan')
        for name, data in expected.items():
            path = destination / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size != len(data) or path.read_bytes() != data:
                raise ValueError('existing bundle was changed')
    else:
        staging = Path(leaf.make_temp_dir(prefix='.setup-', dir=root))
        try:
            for name, data in expected.items():
                leaf.overwrite_private(staging / name, data)
            staging.rename(destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return {'status': 'configured', 'bundle': str(destination), 'plan_sha256': expected_plan_sha256,
            'host_registration': 'not-applied', 'runtime_verification': 'not-run', 'gaps': result['gaps']}
