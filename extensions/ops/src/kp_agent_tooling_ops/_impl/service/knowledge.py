"""Closed, read-only knowledge operations over configured repositories."""
from __future__ import annotations

from copy import deepcopy
from contextvars import ContextVar
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Mapping

from kp_agent_tooling_ops._impl.capability_map import compare_references, validate_manifest
from kp_agent_tooling_ops._impl.queries.document_retrieval_report import document_retrieval_report
from kp_agent_tooling_ops._impl.tool_discovery import GitSource, search


class KnowledgeRequestError(ValueError):
    """The configuration or caller request is outside the closed contract."""


_MAX_OUTPUT = 32768
_LIMITATIONS = ['Source revision identifies selected committed source, not the indexed corpus snapshot.',
                'Knowledge access covers configured repositories and read-only sources only.']
_ERROR = {'code': 'knowledge_unavailable', 'message': 'Knowledge source or backend unavailable.'}
_REFERENCE_STORE_UNSET = object()
_REFERENCE_STORE = ContextVar('knowledge_reference_store', default=_REFERENCE_STORE_UNSET)


def _text(value: object, name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeRequestError(f'{name} must be a nonblank string')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError as exc:
        raise KnowledgeRequestError(f'{name} must be UTF-8') from exc
    if size > maximum:
        raise KnowledgeRequestError(f'{name} exceeds {maximum} UTF-8 bytes')
    return value


def _keys(value: object, expected: set[str], required: set[str], name: str) -> dict:
    if not isinstance(value, Mapping) or not required <= value.keys() or value.keys() - expected:
        raise KnowledgeRequestError(f'invalid {name} fields')
    return dict(value)


def _manifest_path(value: object) -> str:
    path = _text(value, 'manifest path')
    if ('\\' in path or path.startswith('/') or
            any(part in {'', '.', '..'} for part in path.split('/')) or
            any(ord(c) < 32 or ord(c) == 127 for c in path)):
        raise KnowledgeRequestError('manifest path must be normalized and relative')
    return path


def _load_config(config: object) -> dict:
    if isinstance(config, (str, Path)):
        try:
            with Path(config).open('rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise KnowledgeRequestError('config exceeds 65536 bytes')
            config = json.loads(raw.decode('utf-8'))
        except (OSError, UnicodeError, ValueError) as exc:
            raise KnowledgeRequestError('invalid knowledge configuration') from exc
    root = _keys(config, {'schema_version', 'repositories', 'navigation', 'platforms', 'scip_indexes'}, {'schema_version', 'repositories'}, 'config')
    if root['schema_version'] != 'ops.knowledge-config.v1':
        raise KnowledgeRequestError('unsupported configuration schema')
    repos = root['repositories']
    if not isinstance(repos, dict) or not repos:
        raise KnowledgeRequestError('repositories must be a nonempty object')
    for key, repo in repos.items():
        _text(key, 'repository key', 80)
        repo = _keys(repo, {'path', 'ref', 'default_branch_ref', 'corpus_scope', 'tenant_ids', 'capabilities', 'entry_symbols', 'artifacts'},
                     {'path', 'ref', 'corpus_scope', 'tenant_ids', 'capabilities'}, 'repository')
        path = _text(repo['path'], 'repository path', 4096)
        if not Path(path).is_absolute():
            raise KnowledgeRequestError('repository path must be absolute')
        _text(repo['ref'], 'repository ref')
        if 'default_branch_ref' in repo:
            default_ref = _text(repo['default_branch_ref'], 'default branch ref')
            if not re.fullmatch(r'refs/(?:heads|remotes)/[A-Za-z0-9._/-]+', default_ref):
                raise KnowledgeRequestError('default_branch_ref must name a branch ref')
        _text(repo['corpus_scope'], 'corpus scope')
        tenants = repo['tenant_ids']
        if not isinstance(tenants, list) or not tenants:
            raise KnowledgeRequestError('tenant_ids must be nonempty list')
        for tenant in tenants:
            _text(tenant, 'tenant id', 80)
        caps = repo['capabilities']
        if not isinstance(caps, dict):
            raise KnowledgeRequestError('capabilities must be an object')
        for cap, manifest in caps.items():
            _text(cap, 'capability id', 80)
            _manifest_path(manifest)
        symbols = repo.get('entry_symbols', {})
        if not isinstance(symbols, dict) or symbols.keys() - caps.keys():
            raise KnowledgeRequestError('entry symbols require configured capabilities')
        for symbol in symbols.values():
            _text(symbol, 'entry symbol', 256)
        artifacts = repo.get('artifacts', {})
        if not isinstance(artifacts, dict):
            raise KnowledgeRequestError('artifacts must be an object')
        for artifact_path, declaration in artifacts.items():
            _manifest_path(artifact_path)
            declared = _keys(declaration, {'status', 'owner', 'replacement'}, {'status', 'owner'}, 'artifact')
            if declared['status'] not in ('maintained', 'superseded', 'historical', 'withdrawn'):
                raise KnowledgeRequestError('unknown artifact lifecycle')
            _text(declared['owner'], 'artifact owner', 120)
            if 'replacement' in declared:
                _manifest_path(declared['replacement'])
    if 'navigation' in root:
        nav = root['navigation']
        if isinstance(nav, dict) and nav.get('provider') == 'published_scip':
            nav = _keys(nav, {'provider', 'profile_path', 'published_root', 'local_root'},
                        {'provider', 'profile_path', 'published_root', 'local_root'}, 'navigation')
            paths = [nav['profile_path'], nav['published_root'], nav['local_root']]
        elif isinstance(nav, dict) and nav.get('provider') == 'serena':
            nav = _keys(nav, {'provider', 'command', 'python'}, {'provider', 'command', 'python'}, 'navigation')
            paths = [nav['command'], nav['python']]
        else:
            raise KnowledgeRequestError('navigation provider must be serena or published_scip; legacy AST navigation is retired')
        for value in paths:
            if not Path(_text(value, 'navigation path', 4096)).is_absolute():
                raise KnowledgeRequestError('navigation paths must be absolute')
    platforms = root.get('platforms', {})
    if not isinstance(platforms, dict):
        raise KnowledgeRequestError('platforms must be an object')
    from kp_agent_tooling._impl.platform_snapshot import _validate
    for anchor, manifest in platforms.items():
        try:
            _validate(manifest)
            members = manifest['sources']
            if anchor not in repos or anchor not in members or members.keys() - repos.keys():
                raise ValueError('platform repositories must be configured')
            # The public adapter authorizes the anchor. Every member must be
            # visible to every anchor tenant before exposing a combined report.
            if any(not set(repos[anchor]['tenant_ids']) <= set(repos[key]['tenant_ids']) for key in members):
                raise ValueError('platform membership exceeds anchor tenant access')
        except (ValueError, TypeError) as exc:
            raise KnowledgeRequestError('invalid platform configuration') from exc
    indexes = root.get('scip_indexes', {})
    if not isinstance(indexes, dict) or indexes.keys() - repos.keys():
        raise KnowledgeRequestError('SCIP indexes require configured repositories')
    for entries in indexes.values():
        if not isinstance(entries, list) or not 1 <= len(entries) <= 4:
            raise KnowledgeRequestError('bounded SCIP indexes required')
        for entry in entries:
            _keys(entry, {'path','revision','sha256'}, {'path','revision','sha256'}, 'SCIP index')
            if not Path(_text(entry['path'], 'index path', 4096)).is_absolute():
                raise KnowledgeRequestError('absolute configured index path required')
            if not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', _text(entry['revision'], 'index revision',64)):
                raise KnowledgeRequestError('full index revision required')
            if not re.fullmatch('[0-9a-f]{64}', _text(entry['sha256'], 'index digest',64)):
                raise KnowledgeRequestError('index digest required')
    return deepcopy(root)


def platform_anchor(config, key):
    platforms = config.get('platforms', {})
    if key in platforms:
        return key
    candidates = [anchor for anchor, spec in platforms.items() if key in spec['sources']]
    if len(candidates) != 1:
        raise KnowledgeRequestError('repository requires one unambiguous configured platform')
    return candidates[0]


def _size(value: dict) -> int:
    return len((json.dumps(value, ensure_ascii=True) + '\n').encode('utf-8'))


def _bounded(report: dict, rows_key: str | None) -> dict:
    if rows_key is None:
        if _size(report) > _MAX_OUTPUT:
            raise ValueError('report metadata exceeds output budget')
        return report
    data = report['data']
    rows = data[rows_key]
    if rows_key == 'results' and any('code_reference_coverage' in row for row in rows):
        from kp_agent_tooling_ops._impl.code_references.retrieval import LIMITATION, trim_code_reference_enrichment
        baseline = deepcopy(report)
        baseline_data = baseline['data']
        baseline_data.pop('code_reference_context', None)
        for row in baseline_data['results']:
            row.pop('code_reference_coverage', None)
            row.pop('code_references', None)
        for owner in (baseline, baseline_data):
            if isinstance(owner.get('limitations'), list):
                owner['limitations'] = [value for value in owner['limitations']
                                        if value != LIMITATION]
        while _size(baseline) > _MAX_OUTPUT and baseline_data['results']:
            baseline_data['results'].pop()
            baseline['omitted'] += 1
        baseline_data['returned'] = len(baseline_data['results'])
        trim_code_reference_enrichment(report, _MAX_OUTPUT)
        if _size(report) > _MAX_OUTPUT:
            baseline_data['code_reference_context'] = {
                'state': 'omitted', 'reason': 'response_budget_exhausted'}
            if _size(baseline) <= _MAX_OUTPUT:
                return baseline
            return {
                'schema_version': report.get('schema_version', 'ops.knowledge.v1'),
                'operation': report.get('operation'), 'repo_key': report.get('repo_key'),
                'source_revision': report.get('source_revision'), 'status': 'error',
                'data': {'error': {'code': 'response_budget_exhausted',
                    'message': 'Reference enrichment could not fit while preserving baseline rows.'}},
                'omitted': report.get('omitted', 0),
                'limitations': list(_LIMITATIONS),
            }
    while _size(report) > _MAX_OUTPUT and data.get('historical_results'):
        data['historical_results'].pop()
        report['omitted'] += 1
    while _size(report) > _MAX_OUTPUT and rows:
        rows.pop()
        report['omitted'] += 1
    if rows_key == 'results':
        if 'shown' in data:
            data['shown'] = len(rows)
            data['truncated'] = data['available'] - len(rows)
        if 'returned' in data:
            data['returned'] = len(rows)
    if _size(report) > _MAX_OUTPUT:
        raise ValueError('report metadata exceeds output budget')
    return report


def _undeclared_capability_report(operation, repo_key, capability, repo):
    """A repository with no (or another) capability is a catalog state; report it as one."""
    declared = sorted(repo.get('capabilities', {}))
    code = 'no_capabilities_declared' if not declared else 'capability_not_declared'
    message = (f'repository {repo_key!r} declares no capabilities in the knowledge catalog'
               if not declared else
               f'capability {capability!r} is not declared for repository {repo_key!r}')
    return {'schema_version': 'ops.knowledge.v1', 'operation': operation, 'repo_key': repo_key,
            'source_revision': None, 'status': 'error',
            'data': {'error': {'code': code, 'message': message, 'capability_id': capability,
                               'declared_capabilities': declared,
                               'next_action': 'Declare the capability map path under repositories.<repo>.capabilities in the knowledge catalog.'}},
            'omitted': 0, 'limitations': list(_LIMITATIONS)}


class KnowledgeService:
    def __init__(self, config: object, reader_factory, *, navigation_provider=None, lifecycle=None, telemetry=None,
                 installed_distribution_provider=None, reference_store=None,
                 reference_graph=None, reference_navigation=None):
        self.telemetry = telemetry
        self._installed_distribution_provider = installed_distribution_provider
        self.lifecycle = lifecycle
        self._reference_store = reference_store
        self._reference_graph = reference_graph
        self._reference_navigation = reference_navigation
        self._config_path = Path(config) if isinstance(config, (str, Path)) else None
        self._config = _load_config(config)
        self._reader_factory = reader_factory
        self._navigation_provider = navigation_provider
        if navigation_provider is None and 'navigation' in self._config:
            nav = self._config['navigation']
            if nav.get('provider') == 'published_scip':
                from kp_agent_tooling_ops._impl.service.published_navigation import PublishedScipNavigationProvider
                self._navigation_provider = PublishedScipNavigationProvider(nav, self._config['repositories'])
            elif nav.get('provider') == 'serena':
                from kp_agent_tooling._impl.service.serena_navigation import SerenaNavigationProvider
                self._navigation_provider = SerenaNavigationProvider(nav['command'], nav['python'])

    @property
    def repository_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._config['repositories']))

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(sorted({cap for repo in self._config['repositories'].values()
                             for cap in repo['capabilities']}))

    @property
    def capabilities_by_repository(self) -> dict[str, tuple[str, ...]]:
        """Catalog snapshot of accepted repo/capability pairs for tool schemas."""
        return {key: tuple(sorted(repo['capabilities']))
                for key, repo in sorted(self._config['repositories'].items())}

    def _retrieve_target(self, repo, requested):
        if requested is not None:
            revision = _text(requested, 'target_revision', 64)
            if re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', revision) is None:
                raise KnowledgeRequestError('target_revision requires a full lowercase commit ID')
            try:
                return GitSource(Path(repo['path']), revision), 'explicit'
            except Exception:
                return None, 'target_unavailable'
        return self._default_branch_source(repo)

    @staticmethod
    def _default_branch_source(repo):
        ref = repo.get('default_branch_ref', 'refs/remotes/origin/HEAD')
        try:
            return GitSource(Path(repo['path']), ref), ('configured_default_branch'
                                                       if 'default_branch_ref' in repo else 'remote_default_branch')
        except Exception:
            return None, 'default_branch_unavailable'

    @staticmethod
    def _configured_lineage(catalog, target):
        if catalog.revision == target.revision:
            return 'matching'
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('GIT_')}
        environment.update(GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0',
                           GIT_NO_REPLACE_OBJECTS='1')

        def ancestor(earlier, later):
            try:
                result = subprocess.run(
                    ['git', '-C', str(catalog.repository), 'merge-base', '--is-ancestor',
                     earlier, later], env=environment, capture_output=True,
                    timeout=10, check=False)
            except (OSError, subprocess.TimeoutExpired):
                return None
            return {0: True, 1: False}.get(result.returncode)

        forward = ancestor(catalog.revision, target.revision)
        if forward is True:
            return 'ancestor'
        if forward is None:
            return 'unknown'
        reverse = ancestor(target.revision, catalog.revision)
        if reverse is True:
            return 'descendant'
        return 'divergent' if reverse is False else 'unknown'

    def _capability_inventory(self, repo_key, repo):
        available = len(repo['capabilities'])
        rows = [{'capability_id': cap, 'manifest_path': path}
                for cap, path in sorted(repo['capabilities'].items())[:50]]
        report = {'schema_version': 'ops.knowledge.v1', 'operation': 'capabilities',
                  'repo_key': repo_key, 'source_revision': None,
                  'status': 'ok' if rows else 'no_results',
                  'data': {'capabilities': rows, 'available': available,
                         'absence_verdict': 'catalog-only',
                         'next_action': 'Use knowledge.context with a listed capability and exact target revision.'},
                  'omitted': available - len(rows),
                  'limitations': ['Catalog declarations do not prove runtime capability.']}
        while _size(report) > _MAX_OUTPUT and rows:
            rows.pop()
            report['omitted'] += 1
        return report

    def permits_tenant(self, repo_key: str, tenant_id: str) -> bool:
        if not isinstance(repo_key, str) or not isinstance(tenant_id, str):
            return False
        try:
            config = _load_config(self._config_path) if self._config_path else self._config
        except KnowledgeRequestError:
            return False
        repo = config['repositories'].get(repo_key)
        return repo is not None and tenant_id in repo['tenant_ids']

    def execute_for_tenant(self, operation, arguments, tenant_id):
        """Authorize and execute platform membership against one catalog snapshot."""
        config = _load_config(self._config_path) if self._config_path else self._config
        key = arguments['repo_key']
        authorized_keys = {key}
        if operation in {'symbol', 'platform'}:
            authorized_keys.add(platform_anchor(config, key))
        if (arguments.get('include_code_references') is True
                or operation in {'reference_diagnostics', 'reference_recovery'}):
            for field in ('code_revisions', 'reference_baseline_revisions'):
                value = arguments.get(field)
                if isinstance(value, Mapping):
                    authorized_keys.update(value)
        if (any(k not in config['repositories'] for k in authorized_keys) or
                any(tenant_id not in config['repositories'][k]['tenant_ids']
                    for k in authorized_keys)):
            raise PermissionError('platform tenant boundary')
        scoped = KnowledgeService(config, self._reader_factory,
            navigation_provider=self._navigation_provider, lifecycle=self.lifecycle,
            telemetry=self.telemetry,
            installed_distribution_provider=self._installed_distribution_provider,
            reference_store=self._reference_store, reference_graph=self._reference_graph,
            reference_navigation=self._reference_navigation)
        return scoped.execute(operation, arguments)

    def _retrieve_data(self, repo_key, query, count, *, target, catalog=None, default=None, paths=None,
                       include_historical=False,
                       reference_options=None, reference_prepared=None):
        repo = self._config['repositories'][repo_key]
        from kp_agent_tooling_ops._impl.service.knowledge_eligibility import eligible_paths, eligible_rows, matching_revision_rows
        declared = eligible_paths(repo, paths=paths)
        allowed = declared
        if self.lifecycle is not None:
            allowed = tuple(path for path in allowed if self.lifecycle.permits(repo['corpus_scope'],path,None))
        prepared_references = reference_prepared
        if reference_options is not None:
            from kp_agent_tooling_ops._impl.code_references.retrieval import prepare_code_reference_request
            try:
                if prepared_references is None:
                    prepared_references = prepare_code_reference_request(
                        source_repo_key=repo_key, repositories=self._config['repositories'],
                        navigation=self._reference_navigation, telemetry=self.telemetry,
                        **reference_options)
            except ValueError as exc:
                raise KnowledgeRequestError(str(exc)) from exc
        rows = []
        excluded = []
        historical = []
        catalog_lineage = self._configured_lineage(catalog, target) if catalog else 'unknown'
        if allowed:
            reader = self._reader_factory()
            requested = count
            while True:
                hits = reader.recall(query=query, top_k=requested,
                    repo_keys=(repo['corpus_scope'],), paths=allowed)
                rows = eligible_rows(repo, hits, paths=allowed)
                if self.lifecycle is not None:
                    rows = [row for row in rows if self.lifecycle.permits(row['repo_key'],row['path'],row['blob_sha'])]
                candidates = rows
                rows, excluded = matching_revision_rows(target, candidates)
                if catalog_lineage not in {'matching', 'ancestor'}:
                    excluded.extend({'path': row['path'], 'indexed_blob_sha': row['blob_sha'],
                                     'target_blob_sha': row['target_blob_sha'],
                                     'reason': 'configured_catalog_not_ancestor_of_target'}
                                    for row in rows)
                    rows = []
                if include_historical:
                    excluded_ids = {(item['path'], item['indexed_blob_sha']) for item in excluded}
                    historical = [{**row, 'document_revision': None,
                                   'corpus_lineage': 'unknown', 'revision_proof': 'unverified',
                                   'guidance_scope': 'historical_only'}
                                  for row in candidates
                                  if (row['path'], row.get('blob_sha')) in excluded_ids][:count]
                if len(rows) >= count or len(hits) < requested or requested >= 400:
                    rows = rows[:count]
                    break
                requested = min(requested * 2, 400)
        report = document_retrieval_report(query=query, repo_key=repo['corpus_scope'], top_k=count, results=rows)
        report.update(target_revision=target.revision, corpus_revision=None,
                      corpus_revision_status='unknown',
                      default_revision=default.revision if default else None,
                      target_default_lineage=(self._configured_lineage(target, default)
                                              if default else 'unknown'),
                      configured_catalog_revision=catalog.revision if catalog else None,
                      configured_catalog_lineage=catalog_lineage,
                      configured_catalog_default_lineage=(self._configured_lineage(catalog, default)
                                                          if catalog and default else 'unknown'),
                      excluded_revision_count=len(excluded),
                      excluded_revision_examples=excluded[:5])
        fallback_paths = [item['path'] for item in excluded
                          if item.get('target_blob_sha') is not None]
        if not fallback_paths and not rows:
            fallback_paths = [path for path in allowed[:5]
                              if (entry := target.entry(path)) is not None
                              and entry[0] in {'100644', '100755'}]
        report['source_fallbacks'] = [
            {'tool': 'navigation.source',
             'arguments': {'repo_key': repo_key, 'target_revision': target.revision,
                           'path': path, 'start_line': 1, 'line_count': 80},
             'availability': 'not-verified'}
            for path in dict.fromkeys(fallback_paths)][:5]
        report['guidance_scope'] = ('current_default' if default and target.revision == default.revision
                                    else 'requested_revision_only')
        if include_historical:
            report['historical_results'] = historical
            report['historical_scope'] = 'separate_unverified_evidence'
        report['limitations'].append('The document index records blob IDs but no ingestion commit; corpus lineage is unknown.')
        # An empty admission set is a catalog state, not a search outcome. Say which.
        report['declared_artifacts'] = len(declared)
        report['admitted_artifacts'] = len(allowed)
        if not allowed:
            report['status'] = 'corpus_empty'
            report['reason'] = ('no_maintained_artifacts_declared' if not declared
                                else 'all_declared_artifacts_withdrawn')
            report['next_action'] = ('Declare maintained artifacts with an owner for this repository in the '
                                     'knowledge catalog, then ingest them; retrieval admits nothing else.')
        elif not rows:
            report['reason'] = ('catalog_lineage_untrusted' if catalog_lineage not in {'matching', 'ancestor'}
                                else 'indexed_bytes_not_at_target' if excluded
                                else 'no_matching_chunks_or_not_indexed')
            report['indexed_artifacts'] = 'not-assessed'
            report['next_action'] = ('Read the exact requested revision from source or index its maintained documents; '
                                     'do not use older indexed bytes as current guidance.')
        if reference_options is not None:
            from kp_agent_tooling_ops._impl.code_references.retrieval import attach_code_references
            attach_code_references(
                report, source_repo_key=repo_key, repositories=self._config['repositories'],
                store=(_REFERENCE_STORE.get() if _REFERENCE_STORE.get() is not _REFERENCE_STORE_UNSET
                       else self._reference_store), graph=self._reference_graph,
                navigation=self._reference_navigation, telemetry=self.telemetry,
                prepared=prepared_references,
                **reference_options,
            )
        return report

    def execute(self, operation: str, arguments: Mapping[str, object]) -> dict:
        def invoke():
            if operation in {'retrieve', 'context'} and arguments.get('include_code_references') is True:
                from kp_agent_tooling_ops._impl.code_references.retrieval import reference_enrichment_scope
                from kp_agent_tooling_ops._impl.code_references.manifest import AtomicReferenceGeneration
                reference_store = self._reference_store
                if isinstance(reference_store, AtomicReferenceGeneration):
                    reference_store = reference_store.snapshot()
                token = _REFERENCE_STORE.set(reference_store)
                try:
                    with reference_enrichment_scope(self.telemetry):
                        return self._execute_guarded(operation, arguments)
                finally:
                    _REFERENCE_STORE.reset(token)
            return self._execute_guarded(operation, arguments)
        if self.telemetry is not None:
            return self.telemetry.run(operation, invoke)
        return invoke()

    def _execute_guarded(self, operation: str, arguments: Mapping[str, object]) -> dict:
        if self.lifecycle is None:
            return self._execute_snapshot(operation, arguments)
        try:
            with self.lifecycle.guard():
                return self._execute_snapshot(operation, arguments)
        except KnowledgeRequestError:
            raise
        except Exception:
            return {'schema_version':'ops.knowledge.v1', 'operation':operation,
                    'repo_key':arguments.get('repo_key'), 'source_revision':None,
                    'status':'error', 'data':{'error':_ERROR}, 'omitted':0,
                    'limitations':list(_LIMITATIONS)}

    def _execute_snapshot(self, operation: str, arguments: Mapping[str, object]) -> dict:
        if self._config_path is None:
            return self._execute(operation, arguments)
        # A request owns an immutable snapshot; never mutate shared service state.
        try:
            config = _load_config(self._config_path)
            snapshot = KnowledgeService(config, self._reader_factory,
                                        navigation_provider=self._navigation_provider, lifecycle=self.lifecycle,
                                        telemetry=self.telemetry, installed_distribution_provider=self._installed_distribution_provider,
                                        reference_store=self._reference_store,
                                        reference_graph=self._reference_graph,
                                        reference_navigation=self._reference_navigation)
            report = snapshot._execute(operation, arguments)
            if _load_config(self._config_path) != config:
                raise ValueError('catalog changed while assembling response')
            return report
        except KnowledgeRequestError:
            raise
        except Exception:
            return {'schema_version': 'ops.knowledge.v1', 'operation': operation,
                    'repo_key': arguments.get('repo_key'), 'source_revision': None,
                    'status': 'error', 'data': {'error': _ERROR}, 'omitted': 0,
                    'limitations': list(_LIMITATIONS)}

    def _execute(self, operation: str, arguments: Mapping[str, object]) -> dict:
        if operation == 'capabilities':
            args = _keys(arguments, {'repo_key'}, {'repo_key'}, 'capabilities arguments')
            repo_key = _text(args['repo_key'], 'repository key', 80)
            repo = self._config['repositories'].get(repo_key)
            if repo is None:
                raise KnowledgeRequestError('unknown repository')
            return _bounded(self._capability_inventory(repo_key, repo), None)
        if operation == 'reference_recovery':
            from kp_agent_tooling_ops._impl.code_references.manifest import AtomicReferenceGeneration
            from kp_agent_tooling_ops._impl.code_references.recovery import read_references
            store = self._reference_store
            if isinstance(store, AtomicReferenceGeneration):
                store = store.snapshot()
            try:
                return read_references(arguments, repositories=self._config['repositories'],
                    store=store, graph=self._reference_graph,
                    navigation=self._reference_navigation, lifecycle=self.lifecycle)
            except ValueError as exc:
                raise KnowledgeRequestError(str(exc)) from exc
        if operation == 'reference_diagnostics':
            from kp_agent_tooling_ops._impl.code_references.diagnostics import read_diagnostics
            from kp_agent_tooling_ops._impl.code_references.manifest import AtomicReferenceGeneration
            store = self._reference_store
            if isinstance(store, AtomicReferenceGeneration):
                store = store.snapshot()
            try:
                return read_diagnostics(arguments, repositories=self._config['repositories'],
                    store=store, lifecycle=self.lifecycle)
            except ValueError as exc:
                raise KnowledgeRequestError(str(exc)) from exc
        if operation == 'symbol':
            args = _keys(arguments, {'repo_key','target_revision','path','line'},
                         {'repo_key','target_revision','path','line'}, 'symbol arguments')
            key = _text(args['repo_key'], 'repository key',80)
            path = _manifest_path(args['path'])
            target = _text(args['target_revision'], 'revision',64)
            if not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}',target) or type(args['line']) is not int or args['line']<1:
                raise KnowledgeRequestError('exact source coordinate required')
            anchor = platform_anchor(self._config, key)
            from kp_agent_tooling._impl.service.scip_entry import symbol_report
            return _bounded(symbol_report(self._config,anchor,target,path,args['line'], repo_key=key), 'results')
        if operation == 'platform':
            args = _keys(arguments, {'repo_key'}, {'repo_key'}, 'platform arguments')
            key = _text(args['repo_key'], 'repository key', 80)
            anchor = platform_anchor(self._config, key)
            from kp_agent_tooling_ops._impl.service.platform_entry import platform_report
            return _bounded(platform_report(self._config, anchor, requested_repo=key,
                                            installed_provider=self._installed_distribution_provider), None)
        if operation == 'context':
            args = _keys(arguments, {'repo_key', 'capability_id', 'target_revision', 'query',
                                     'include_code_references', 'code_revisions',
                                     'reference_baseline_revisions'},
                         {'repo_key', 'capability_id', 'target_revision'}, 'context arguments')
            repo_key = _text(args['repo_key'], 'repository key', 80)
            capability = _text(args['capability_id'], 'capability id', 80)
            repo = self._config['repositories'].get(repo_key)
            if repo is None:
                raise KnowledgeRequestError('unknown repository')
            if capability not in repo['capabilities']:
                return _undeclared_capability_report('context', repo_key, capability, repo)
            target = _text(args['target_revision'], 'target revision', 64)
            if re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', target) is None:
                raise KnowledgeRequestError('target_revision requires a full lowercase commit ID')
            query = _text(args.get('query', capability.replace('-', ' ')), 'query', 512)
            reference_options = self._reference_options(args)
            reference_prepared = None
            if reference_options is not None:
                from kp_agent_tooling_ops._impl.code_references.retrieval import prepare_code_reference_request
                try:
                    reference_prepared = prepare_code_reference_request(
                        source_repo_key=repo_key, repositories=self._config['repositories'],
                        navigation=self._reference_navigation, telemetry=self.telemetry, **reference_options)
                except ValueError as exc:
                    raise KnowledgeRequestError(str(exc)) from exc
            from kp_agent_tooling_ops._impl.service.knowledge_context import context_report
            return context_report(self, repo_key, capability, target, query,
                                  reference_options=reference_options,
                                  reference_prepared=reference_prepared)
        fields = {
            'discover': ({'repo_key', 'query', 'limit'}, {'repo_key', 'query'}),
            'retrieve': ({'repo_key', 'query', 'top_k', 'target_revision', 'include_historical', 'include_code_references',
                          'code_revisions', 'reference_baseline_revisions'},
                         {'repo_key', 'query'}),
            'check_references': ({'repo_key', 'capability_id'}, {'repo_key', 'capability_id'}),
        }
        if operation not in fields:
            raise KnowledgeRequestError('unknown knowledge operation')
        args = _keys(arguments, *fields[operation], 'operation arguments')
        repo_key = _text(args['repo_key'], 'repo_key', 80)
        repo = self._config['repositories'].get(repo_key)
        if repo is None:
            raise KnowledgeRequestError('unknown repository')
        if operation == 'check_references':
            cap = _text(args['capability_id'], 'capability_id', 80)
            if cap not in repo['capabilities']:
                return _undeclared_capability_report('check_references', repo_key, cap, repo)
        else:
            query = _text(args['query'], 'query', 512)
            key = 'limit' if operation == 'discover' else 'top_k'
            count = args.get(key, 5)
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 50:
                raise KnowledgeRequestError(f'{key} must be an integer 1..50')
            reference_options = (self._reference_options(args)
                                 if operation == 'retrieve' else None)
            if operation == 'retrieve' and not isinstance(args.get('include_historical', False), bool):
                raise KnowledgeRequestError('include_historical must be a boolean')
        report = {'schema_version': 'ops.knowledge.v1', 'operation': operation,
                  'repo_key': repo_key, 'source_revision': None, 'status': 'error',
                  'data': {'error': _ERROR}, 'omitted': 0, 'limitations': list(_LIMITATIONS)}
        try:
            if operation == 'retrieve':
                target, selection = self._retrieve_target(repo, args.get('target_revision'))
                if target is None:
                    report['data'] = {'error': {'code': selection,
                        'message': ('Requested target commit is unavailable in the configured local repository.'
                                    if selection == 'target_unavailable' else
                                    'Configured default branch could not be resolved locally.'),
                        'next_action': ('Make the exact target commit available locally or read a revision-pinned source.'
                                        if selection == 'target_unavailable' else
                                        'Configure default_branch_ref or provide a full target_revision.')}}
                    report['target_revision'] = args.get('target_revision')
                    report['target_selection'] = selection
                    return report
                try:
                    source = GitSource(Path(repo['path']), repo['ref'])
                except Exception:
                    source = None
                report['source_revision'] = target.revision
                report['target_revision'] = target.revision
                report['target_selection'] = selection
                default, _ = self._default_branch_source(repo)
            else:
                source = GitSource(Path(repo['path']), repo['ref'])
                report['source_revision'] = source.revision
            if operation == 'discover':
                data = search(source, query)
                data['results'] = data['results'][:count]
                report['omitted'] = data['available'] - len(data['results'])
                data['shown'] = len(data['results'])
                data['truncated'] = data['available'] - data['shown']
                report['status'] = 'ok' if data['available'] else 'no_results'
                row_key = 'results'
            elif operation == 'retrieve':
                data = self._retrieve_data(repo_key, query, count,
                                           target=target, catalog=source, default=default,
                                           include_historical=args.get('include_historical', False),
                                           reference_options=reference_options)
                report['status'] = data['status']
                row_key = 'results'
            else:
                path = repo['capabilities'][cap]
                entry = source.entry(path)
                if entry is None or entry[0] not in {'100644', '100755'}:
                    raise ValueError('manifest unavailable')
                manifest = validate_manifest(json.loads(source.read_bounded(entry[1], 65536).decode('utf-8')))
                if manifest['capability_id'] != cap:
                    raise ValueError('manifest capability mismatch')
                baseline = GitSource(Path(repo['path']), manifest['source_revision'])
                if baseline.revision != manifest['source_revision']:
                    raise ValueError('manifest baseline revision mismatch')
                data = compare_references(manifest, baseline, source)
                report['status'] = 'ok' if data['status'] == 'current' else 'review_required'
                row_key = 'references'
            report['data'] = data
            report['limitations'] += data.get('limitations', [])
            return _bounded(report, row_key)
        except (KeyboardInterrupt, SystemExit):
            raise
        except KnowledgeRequestError:
            raise
        except Exception:
            return {**report, 'data': {'error': _ERROR}, 'status': 'error',
                    'omitted': 0, 'limitations': list(_LIMITATIONS)}

    def _reference_options(self, args: Mapping[str, object]) -> dict | None:
        """Disabled requests deliberately ignore revision-map contents."""
        enabled = args.get('include_code_references', False)
        if not isinstance(enabled, bool):
            raise KnowledgeRequestError('include_code_references must be a boolean')
        if not enabled:
            return None
        for field in ('code_revisions', 'reference_baseline_revisions'):
            value = args.get(field)
            if value is None:
                continue
            if not isinstance(value, Mapping) or len(value) > 8:
                raise KnowledgeRequestError(f'{field} must be an object of at most 8 entries')
            for key, revision in value.items():
                if key not in self._config['repositories']:
                    raise KnowledgeRequestError(f'{field} contains an unknown repository')
                if not isinstance(revision, str) or re.fullmatch(
                        r'[0-9a-f]{40}|[0-9a-f]{64}', revision) is None:
                    raise KnowledgeRequestError(f'{field} requires full lowercase commit IDs')
        return {
            'code_revisions': args.get('code_revisions'),
            'reference_baseline_revisions': args.get('reference_baseline_revisions'),
        }
