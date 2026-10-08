"""Shared rig for the T5 refresh-efficiency contract tests (tests/refresh/ only).

Order: docs/work/orders/T5-refresh-efficiency.md. These tests drive the public
``kp_agent_tooling.refresh_cli.refresh(request)`` and the ``kp-agent-refresh``
console script over temporary git repositories. Stubs enter only through seams
the base code already calls:

- ``refresh_cli.run`` for ``scip-python``, ``scip-typescript`` and the SCIP
  decoder (every git command passes through to the real runner);
- ``refresh_cli.build_index`` and ``refresh_cli.verify_index_lookup``;
- ``semantic_index.build_semantic_index``, ``semantic_index.validate_semantic_index``
  and ``embeddings.embedders.RealEmbedder``.

Observables are counted outside the code under test:

- clones: a ``git`` shim placed first on PATH logs every ``git ... clone ...`` argv;
- index runs: the stubbed runner records repository, language and timeout. The
  fake toolchain executables log and fail if any code path bypasses the runner;
- embeds: calls of the stub semantic builder.

P3's registry clauses follow the dispatcher's clarification of the order: the
request names ``snapshot_registry``; retention prunes only when it is readable.
No real scip-python, node or torch runs in this directory.
"""
from __future__ import annotations

import ast
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from kp_agent_tooling import refresh_cli

REAL_GIT = shutil.which('git')

# Published shapes at the order's base. P7: a request without the new
# fields keeps every one of these keys.
BASE_PROFILE_KEYS = frozenset({
    'schema_version', 'profile', 'request_sha256', 'published_at', 'repos', 'knowledge_config',
    'knowledge_config_sha256', 'check_status', 'workspace_repositories', 'index_metrics',
    'semantic_indexes', 'build_identities', 'repository_readiness', 'limitations'})
BASE_CATALOG_KEYS = frozenset({'schema_version', 'repositories', 'platforms', 'scip_indexes'})
BASE_CATALOG_REPOSITORY_KEYS = frozenset({'path', 'ref', 'corpus_scope', 'tenant_ids', 'capabilities'})
PYTHON_DEPENDENCY_FILES = frozenset({'requirements.txt', 'pyproject.toml'})


def git(repo, *args):
    return subprocess.check_output([REAL_GIT, '-C', str(repo), *args], stderr=subprocess.PIPE).decode().strip()


def instant(value):
    """Seconds since the epoch for a zoned ISO-8601 string, else None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo else None


def instants(value):
    """Every zoned ISO-8601 instant anywhere in a JSON value."""
    if isinstance(value, dict):
        for item in value.values():
            yield from instants(item)
    elif isinstance(value, list):
        for item in value:
            yield from instants(item)
    else:
        parsed = instant(value)
        if parsed is not None:
            yield parsed


class Rig:
    def __init__(self, tmp_path, monkeypatch):
        self.module = refresh_cli
        self.monkeypatch = monkeypatch
        self.tmp = tmp_path
        self.sources = tmp_path / 'sources'
        self.observations = tmp_path / 'observations'
        self.sources.mkdir()
        self.observations.mkdir()
        self.clone_log = self.observations / 'git-clone.log'
        self.bypass_log = self.observations / 'toolchain-bypass.log'
        self.recorded_index_runs = []
        self.embeds = []
        self.failing_index = set()
        self.semantic_failures = {}
        self.anchors = {}
        self._counter = 0
        self._install_git_shim()
        toolchain = self._install_toolchain()
        self.template = tmp_path / 'serena.yml'
        self.template.write_text('read_only: true\n')
        # Layout of deploy/examples/refresh.example.json: publication and status
        # live inside output_root, beside the generations.
        state = tmp_path / 'refresh'
        self.request = {'output_root': str(state), 'publication': str(state / 'profile.json'),
                        'check_status': str(state / 'status.json'), 'toolchain': str(toolchain),
                        'node': 'node', 'repositories': {}}
        self._install_seams()

    # -- environment ---------------------------------------------------------------
    def _install_git_shim(self):
        shim = self.tmp / 'shim-bin'
        shim.mkdir()
        log = shlex.quote(str(self.clone_log))
        (shim / 'git').write_text(
            '#!/bin/sh\n'
            'for arg in "$@"; do\n'
            '  if [ "$arg" = clone ]; then\n'
            f'    printf \'%s\\t\' "$@" >> {log}\n'
            f'    printf \'\\n\' >> {log}\n'
            '    break\n'
            '  fi\n'
            'done\n'
            f'exec {shlex.quote(REAL_GIT)} "$@"\n')
        (shim / 'git').chmod(0o755)
        self.monkeypatch.setenv('PATH', f'{shim}{os.pathsep}{os.environ.get("PATH", "")}')

    def _install_toolchain(self):
        toolchain = self.tmp / 'toolchain'
        bin_dir = toolchain / 'node_modules' / '.bin'
        bin_dir.mkdir(parents=True)
        (toolchain / 'package-lock.json').write_text('{}')
        log = shlex.quote(str(self.bypass_log))
        for tool in ('scip-python', 'scip-typescript'):
            (bin_dir / tool).write_text(f'#!/bin/sh\nprintf \'%s\\n\' {tool} >> {log}\nexit 3\n')
            (bin_dir / tool).chmod(0o755)
        return toolchain

    def _install_seams(self):
        module, rig = self.module, self
        real_run = module.run

        def run(args, **kwargs):
            executable = str(args[0])
            for language, tool in (('python', '/scip-python'), ('typescript', '/scip-typescript')):
                if executable.endswith(tool):
                    key = rig.key_for(args[args.index('--cwd') + 1])
                    rig.recorded_index_runs.append({'key': key, 'language': language,
                                                    'timeout': kwargs.get('timeout')})
                    if key in rig.failing_index:
                        raise subprocess.CalledProcessError(1, list(args), b'', b'fixture index failure')
                    Path(args[args.index('--output') + 1]).write_bytes(b'scip')
                    return ''
            if executable == rig.request.get('node'):
                return '{}'
            return real_run(args, **kwargs)

        def build_index(repo, revision, key, decoded, *, provenance, prefix='', **extra):
            from kp_agent_tooling._impl.scip_navigation import digest
            path = rig.anchors[key]
            blob = git(repo, 'rev-parse', f'{revision}:{path}')
            data = {'schema': 'ops.scip-navigation.v1', 'repo_key': key, 'revision': revision,
                    'blobs': {path: blob},
                    'occurrences': [{'path': path, 'symbol': 'hello', 'definition': True,
                                     'byte_offset': 0, 'byte_length': 3, 'blob_sha': blob}],
                    'gaps': [], 'limitations': []}
            return {'sha256': digest(data), 'data': data}

        def verify_index_lookup(index, repo, revision, key, *extra, **named):
            return {'status': 'ok', 'path': rig.anchors[key], 'line': 1}

        self.monkeypatch.setattr(module, 'run', run)
        self.monkeypatch.setattr(module, 'build_index', build_index)
        self.monkeypatch.setattr(module, 'verify_index_lookup', verify_index_lookup)

    def enable_semantic(self, selection=True):
        import kp_agent_tooling._impl.embeddings.embedders as providers
        import kp_agent_tooling._impl.semantic_index as semantic
        from kp_agent_tooling._impl.semantic_index import SemanticIndexError
        rig = self

        class Embedder:
            def __init__(self, *, model_digest, **extra):
                self.revision = SimpleNamespace(model_id='t5-fixture', model_digest=model_digest,
                                                revision_id=model_digest)
                self.dim = 2

        def build(config, key, revision, *, embedder, out_dir, deadline_seconds=None, **extra):
            rig.embeds.append(key)
            if key in rig.semantic_failures:
                raise SemanticIndexError(rig.semantic_failures[key], 'fixture semantic failure')
            sha = hashlib.sha256(f'{key}@{revision}'.encode()).hexdigest()
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
            (out / f'{key}-{revision}.semantic.json').write_text(json.dumps(
                {'repo_key': key, 'revision': revision, 'index_sha256': sha}))
            return {'index_sha256': sha, 'chunk_count': 1, 'files_indexed': 1, 'files_skipped': {},
                    'build_seconds': 0,
                    'embedding': {'model_id': embedder.revision.model_id,
                                  'model_digest': embedder.revision.model_digest, 'dim': embedder.dim}}

        def validate(index_dir, key, revision, *, expected_sha256=None, **extra):
            path = Path(index_dir) / f'{key}-{revision}.semantic.json'
            if not path.is_file():
                raise SemanticIndexError('index_unavailable', 'fixture semantic index absent')
            envelope = json.loads(path.read_text())
            if expected_sha256 is not None and envelope['index_sha256'] != expected_sha256:
                raise SemanticIndexError('index_mismatch', 'fixture semantic digest mismatch')
            return envelope

        self.validate_semantic = validate
        self.monkeypatch.setattr(providers, 'RealEmbedder', Embedder)
        self.monkeypatch.setattr(semantic, 'build_semantic_index', build)
        self.monkeypatch.setattr(semantic, 'validate_semantic_index', validate)
        self.request['semantic'] = selection

    # -- repositories ----------------------------------------------------------------
    def add_repo(self, key, files, *, anchor, scope, **spec):
        repo = self.sources / key
        repo.mkdir()
        git(repo, 'init', '-q', '-b', 'main')
        git(repo, 'config', 'user.name', 'Test')
        git(repo, 'config', 'user.email', 'test@example.invalid')
        for name, text in files.items():
            (repo / name).parent.mkdir(parents=True, exist_ok=True)
            (repo / name).write_text(text)
        git(repo, 'add', '.')
        git(repo, 'commit', '-qm', 'fixture')
        git(repo, 'remote', 'add', 'origin', str(repo))
        self.anchors[key] = anchor
        self.request['repositories'][key] = {'repository': str(repo), 'python_scope': scope,
                                             'serena_template': str(self.template), **spec}
        return repo

    def bump(self, key):
        """Commit a change to the repository's own source; returns the new revision."""
        self._counter += 1
        repo = Path(self.request['repositories'][key]['repository'])
        (repo / self.anchors[key]).write_text(f'def hello(): return {self._counter}\n')
        git(repo, 'commit', '-qam', f'change {self._counter}')
        return git(repo, 'rev-parse', 'HEAD')

    def key_for(self, path):
        for part in reversed(Path(path).parts):
            if part in self.request['repositories']:
                return part
        return '?'

    # -- running -------------------------------------------------------------------
    def refresh(self):
        self.started, result = time.time(), None
        try:
            result = self.module.refresh(self.request)
        finally:
            self.finished = time.time()
        return result

    def write_request(self):
        path = self.tmp / 'refresh-request.json'
        path.write_text(json.dumps(self.request))
        return path

    # -- observables ---------------------------------------------------------------
    @property
    def clones(self):
        if not self.clone_log.exists():
            return []
        keys = []
        known = {}
        for key, spec in self.request['repositories'].items():
            known[spec['repository']] = key
            known[str(Path(spec['repository']).resolve())] = key
        for line in self.clone_log.read_text().splitlines():
            args = line.split('\t')
            keys.append(next((known[a] for a in args if a in known), '?'))
        return keys

    @property
    def bypass_runs(self):
        return self.bypass_log.read_text().split() if self.bypass_log.exists() else []

    @property
    def index_runs(self):
        return self.recorded_index_runs + [{'key': '?', 'language': tool, 'timeout': None, 'bypass': True}
                                           for tool in self.bypass_runs]

    def published(self):
        return json.loads(Path(self.request['publication']).read_text())

    def catalog(self, profile=None):
        profile = profile or self.published()
        return json.loads(Path(profile['knowledge_config']).read_text())

    def status(self):
        return json.loads(Path(self.request['check_status']).read_text())

    @property
    def output_root(self):
        return Path(self.request['output_root']).resolve()

    def generation_of(self, path):
        """The immediate child of output_root that holds ``path`` (a generation)."""
        try:
            relative = Path(path).resolve().relative_to(self.output_root)
        except ValueError:
            return None
        return self.output_root / relative.parts[0] if relative.parts else None

    def published_generation(self, profile=None):
        profile = profile or self.published()
        return self.generation_of(profile['knowledge_config'])

    def referenced_paths(self, profile=None):
        profile = profile or self.published()
        catalog = self.catalog(profile)
        paths = [profile['knowledge_config']]
        paths += [row['path'] for row in profile['repos'].values()]
        paths += [entry['path'] for entries in catalog['scip_indexes'].values() for entry in entries]
        paths += [entry['dir'] for entry in profile.get('semantic_indexes', {}).values()]
        return paths

    def referenced_generations(self, profile=None):
        found = {self.generation_of(path) for path in self.referenced_paths(profile)}
        found.discard(None)
        return found

    def generation_like_dirs(self):
        """Directories directly under output_root that hold build output (complete or partial)."""
        found = set()
        if not self.output_root.is_dir():
            return found
        for child in self.output_root.iterdir():
            if child.is_symlink() or not child.is_dir():
                continue
            try:
                entries = list(child.iterdir())
            except OSError:
                continue
            for entry in entries:
                if (entry.name in ('knowledge.json', 'profile.json') or entry.suffix == '.scip' or
                        entry.name.endswith(('.ops.json', '-analysis-environment.json', '.semantic.json')) or
                        (entry.is_dir() and (entry / '.git').exists())):
                    found.add(child)
                    break
        return found

    # -- snapshot registry (P3 as clarified by the dispatcher) ------------------------
    def configure_registry(self):
        """An existing, readable snapshot registry directory named by the request."""
        self.registry = self.tmp / 'snapshot-registry'
        self.registry.mkdir()
        self.request['snapshot_registry'] = str(self.registry)
        return self.registry

    def capture_snapshot(self):
        """Freeze the current publication into the registry with the real navigation snapshot capture."""
        from kp_agent_tooling._impl.navigation_snapshot import capture
        profile = self.published()
        config = {'repos': {key: {'path': row['path'], 'revision': row['revision']}
                            for key, row in profile['repos'].items()},
                  'navigation_profile': self.request['publication']}
        return config, capture(config, self.registry)

    def registry_generations(self):
        """Generations named by any absolute path in any registry record."""
        found = set()
        registry = getattr(self, 'registry', None)
        if registry is None or not registry.is_dir():
            return found

        def walk(value):
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, str) and value.startswith('/'):
                found.add(self.generation_of(value))

        for record in registry.glob('*.json'):
            walk(json.loads(record.read_text()))
        found.discard(None)
        return found

    def assert_retention(self, *, retain, built, kept=()):
        """P3: only the published, the referenced and at most retain-1 most recent unreferenced remain.

        ``kept`` names directories that must survive regardless (no profile.json provenance).
        """
        profile = self.published()
        for path in self.referenced_paths(profile):
            assert Path(path).exists(), f'a generation referenced by the published profile was removed: {path}'
        registry = self.registry_generations()
        for generation in registry:
            assert generation.exists(), f'a generation referenced by the snapshot registry was removed: {generation}'
        for directory in kept:
            assert directory.exists(), f'a generation without profile.json provenance was removed: {directory}'
        assert Path(self.request['publication']).is_file() and Path(self.request['check_status']).is_file()
        published = self.published_generation(profile)
        referenced = self.referenced_generations(profile) | registry
        unreferenced = [g for g in built if g != published and g not in referenced]
        allowed = ({published} | referenced | set(kept) |
                   (set(unreferenced[-(retain - 1):]) if retain > 1 else set()))
        present = self.generation_like_dirs()
        extra = sorted(str(p.name) for p in present - allowed)
        assert not extra, (f'generations beyond retain_generations={retain} remain: {extra}; '
                           f'published={published.name}, referenced={sorted(g.name for g in referenced)}')

    def receipt_values(self, result):
        values = [result]
        for path in (self.request['check_status'], self.request['publication']):
            if Path(path).is_file():
                values.append(json.loads(Path(path).read_text()))
        if self.output_root.is_dir():
            for record in sorted(self.output_root.glob('*.json')):
                try:
                    values.append(json.loads(record.read_text()))
                except (OSError, ValueError):
                    continue
        return values

    def receipt_text(self, result):
        return '\n'.join(json.dumps(value) for value in self.receipt_values(result))

    def skip_receipts(self, result):
        """Objects reporting status "skipped" with a reason, anywhere in this cycle's receipts."""
        found = []

        def walk(value):
            if isinstance(value, dict):
                if value.get('status') == 'skipped' and value.get('reason'):
                    found.append(value)
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        for value in self.receipt_values(result):
            walk(value)
        return found


PYPROJECT = '[project]\nname = "fixture"\nversion = "1.0"\n'


def core_and_ops(rig, *, dependent=True):
    """Two repositories; with ``dependent`` ops declares analysis_dependencies ["core"]."""
    rig.add_repo('core', {'pyproject.toml': PYPROJECT, 'kp_core/mod.py': 'def hello(): return 0\n'},
                 anchor='kp_core/mod.py', scope=['kp_core'])
    rig.add_repo('ops', {'pyproject.toml': PYPROJECT, 'kp_ops/mod.py': 'def hello(): return 0\n'},
                 anchor='kp_ops/mod.py', scope=['kp_ops'],
                 analysis_dependencies=['core'] if dependent else [])


def solo(rig):
    rig.add_repo('solo', {'pyproject.toml': PYPROJECT, 'solo/mod.py': 'def hello(): return 0\n'},
                 anchor='solo/mod.py', scope=['solo'])


WEB_FILES = {
    'pyproject.toml': PYPROJECT,
    'app/mod.py': 'def hello(): return 0\n',
    'web/package.json': '{"name": "web"}\n',
    'web/package-lock.json': '{"lockfileVersion": 3}\n',
    'web/index.ts': 'export const x = 1;\n',
    'package.json': '{"name": "root"}\n',
    'pnpm-lock.yaml': 'lockfileVersion: 9\n',
    # Decoys at the historical hard-coded layout; the web repository does not declare them.
    'studio/package.json': '{"name": "decoy"}\n',
    'studio/package-lock.json': '{"lockfileVersion": 3}\n',
}


def web(rig, **spec):
    """A repository whose TypeScript prefix is ``web``; published through a declared platform."""
    rig.add_repo('web', WEB_FILES, anchor='app/mod.py', scope=['app'], typescript_prefix='web', **spec)
    rig.request['platforms'] = {'web': {'owner': 'OPS', 'profile': 'dev-current', 'repositories': ['web']}}


def dependency_artifacts(rig, key):
    """The dependency artifacts the published catalog lists for ``key`` (through its platforms)."""
    rows = [row for platform in rig.catalog()['platforms'].values()
            for member, source in platform['sources'].items() if member == key
            for row in source['artifacts']]
    assert rows or key in {m for p in rig.catalog()['platforms'].values() for m in p['sources']}
    return rows


def layout_scan_files(module):
    """refresh_cli's source plus every kp_agent_tooling module it imports directly."""
    source = Path(module.__file__)
    files = [source]
    tree = ast.parse(source.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module or '').startswith('kp_agent_tooling'):
                base = node.module
            elif node.level == 1:
                base = 'kp_agent_tooling' + ('.' + node.module if node.module else '')
            else:
                continue
            names.add(base)
            names.update(f'{base}.{alias.name}' for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names if alias.name.startswith('kp_agent_tooling'))
    for name in sorted(names):
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            continue
        if spec and spec.origin and spec.origin.endswith('.py') and Path(spec.origin) not in files:
            files.append(Path(spec.origin))
    return files


def string_literals(path):
    """(line, value) for every str constant in ``path`` except docstrings."""
    tree = ast.parse(Path(path).read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and
                    isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value


def console_script():
    script = Path(sys.executable).parent / 'kp-agent-refresh'
    if not (script.is_file() and os.access(script, os.X_OK)):
        pytest.fail(f'console script kp-agent-refresh is not installed next to {sys.executable}')
    return script
