"""Operator-owned default-branch refresh; atomic source/index publication.

No repository build hooks, pip/npm installs, document ingestion or evidence repinning.
With --watch, checks remotes periodically and rebuilds only when default tips change.
Each artifact is reused under its own identity: a semantic index follows its source
revision and semantic build identity, never the SCIP identity. Rebuilds can be
debounced, and generations are pruned to a bound after each successful publish.
Started before its --request file exists (the compose `refresh` role on a new
runtime root), it reports `not_configured`, naming the file, and with --watch
waits for it instead of exiting.
"""
import argparse
import fcntl
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
import tomllib

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.leaf import sha256_file as digest_file
from kp_agent_tooling._impl.refresh_retention import prune as prune_generations, remove_generation
from kp_agent_tooling._impl.scip_navigation import build_index, load_partitioned_index, lookup, write_partitioned_index


def now():return datetime.now(timezone.utc)


def stamp():return now().isoformat()


def run(args, *, cwd=None, timeout=120):
    return subprocess.check_output(args,cwd=cwd,stderr=subprocess.PIPE,timeout=timeout).decode().strip()


def atomic(path,value):
    return leaf.replace_file(Path(path), leaf.json_writer(value, newline=True, indent=2), text=True, temp_prefix='.publish-',
                             make_parents=True, cleanup='if-exists')


class IndexCoverageError(ValueError):
    def __init__(self, reason, repo_key):
        super().__init__(reason)
        self.reason = reason
        self.repo_key = repo_key


class RefreshStepError(ValueError):
    def __init__(self, repo_key, stage, error):
        super().__init__(f'{stage} failed for {repo_key}: {type(error).__name__}')
        self.repo_key, self.stage = repo_key, stage
        self.reason = getattr(error, 'reason', type(error).__name__)
        self.detail = str(error) if getattr(error, 'reason', None) else None
        self.exit_code = error.returncode if isinstance(error, subprocess.CalledProcessError) else None
        if isinstance(error, subprocess.CalledProcessError):
            stderr = error.stderr or b''
            if isinstance(stderr, bytes): stderr = stderr.decode('utf-8', errors='replace')
            self.reason = ('indexer_out_of_memory' if 'heap out of memory' in stderr.lower()
                           else 'indexer_process_failed')


def failure_receipt(error):
    result = {'status':'refresh_failed','error_type':type(error).__name__,
              'checked_at':stamp(),'last_good_profile':'preserved'}
    if isinstance(error, RefreshStepError):
        result.update(repo_key=error.repo_key, stage=error.stage, reason=error.reason)
        if error.exit_code is not None: result['exit_code'] = error.exit_code
        if error.detail:
            result['detail'] = error.detail[:240]
        result['remediation'] = ('Inspect the named repository build input and artifact; '
                                 'the previous published profile remains available.')
    if isinstance(error, IndexCoverageError):
        result.update(reason=error.reason, repo_key=error.repo_key,
            remediation='Operator: use physical (resolved) checkout paths, verify python_scope '
                        'and pyright includes/excludes, then rebuild. Agents: use source navigation '
                        'or Serena until a populated index is published; do not infer absence.')
    cleanup = getattr(error, 'generation_cleanup', None)
    if isinstance(cleanup, dict):
        result['generation_cleanup'] = cleanup
    return result


def digest_json(value):
    return leaf.canonical_sha256(value, ascii=True, allow_nan=True)


def platforms_for_request(request, sources):
    """Publish only operator-declared platform membership over pinned sources."""
    declarations = request.get('platforms', {})
    if not isinstance(declarations, dict):
        raise ValueError('platforms must be an object')
    platforms = {}
    for anchor, declaration in declarations.items():
        if not isinstance(anchor, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', anchor):
            raise ValueError('invalid platform key')
        if not isinstance(declaration, dict) or set(declaration) != {'owner', 'profile', 'repositories'}:
            raise ValueError('platform requires owner, profile and repositories')
        for field in ('owner', 'profile'):
            value = declaration[field]
            if (not isinstance(value, str) or not value or value.strip() != value or
                    len(value) > 128 or not value.isprintable()):
                raise ValueError('invalid platform ' + field)
        members = declaration['repositories']
        if (not isinstance(members, list) or not 1 <= len(members) <= 32 or
                not all(isinstance(key, str) for key in members)):
            raise ValueError('platform repositories must be a nonempty bounded list')
        if len(set(members)) != len(members):
            raise ValueError('platform repositories must be unique')
        if any(key not in request['repositories'] or key not in sources for key in members):
            raise ValueError('platform references unknown repository')
        platforms[anchor] = {'schema': 'ops.platform-request.v1',
                             'owner': declaration['owner'], 'profile': declaration['profile'],
                             'sources': {key: sources[key] for key in members}}
    return platforms


def catalog_payload(request, repositories, sources, indexes):
    return {'schema_version': 'ops.knowledge-config.v1', 'repositories': repositories,
            'platforms': platforms_for_request(request, sources), 'scip_indexes': indexes}


def analysis_settings(request):
    """Validate the explicit cross-repository Python analysis edges."""
    repositories = request['repositories']
    result = {}
    for key, spec in repositories.items():
        name = spec.get('python_package_name', key)
        version_source = spec.get('python_version_source', 'revision')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name):
            raise ValueError('invalid python package name for ' + key)
        if version_source not in ('revision', 'pyproject'):
            raise ValueError('invalid python version source for ' + key)
        settings = {'python_package_name': name, 'python_version_source': version_source}
        for field in ('analysis_dependencies', 'python_extra_paths'):
            members = spec.get(field, [])
            if (not isinstance(members, list) or len(members) > 32 or
                    not all(isinstance(member, str) for member in members)):
                raise ValueError(field + ' must be a bounded list of repository keys')
            if len(set(members)) != len(members):
                raise ValueError(field + ' requires unique repository keys')
            if any(member not in repositories for member in members):
                raise ValueError(field + ' references unknown repository')
            settings[field] = members
        result[key] = settings
    return result


def installed_version(distribution):
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


DEFAULT_SEMANTIC_DEADLINE_SECONDS = 900
DEFAULT_SEMANTIC_RETRY_AFTER_SECONDS = 86400
DEFAULT_PYTHON_INDEX_TIMEOUT_SECONDS = 300
TYPESCRIPT_INDEX_TIMEOUT_SECONDS = 300
DEFAULT_MIN_REBUILD_INTERVAL_SECONDS = 0
DEFAULT_RETAIN_GENERATIONS = 3
RETENTION_REFERENCES_LIMIT = 32
# The refresh container's operator mounts; reference files must live under one.
RETENTION_REFERENCE_MOUNTS = ('config', 'state')
# Defaults that name deployment data (the compatibility tenant) are data, not code.
REQUEST_DEFAULTS = Path(__file__).parent/'_impl/refresh_defaults.json'
REBUILD_STATE = 'rebuild-state.json'
RETENTION_RECEIPT = 'retention-receipt.json'
SEMANTIC_GAP_FIELDS = ('semantic', 'semantic_reason', 'semantic_retry_after', 'remediation')


def bounded_int(container, field, default, low, high):
    value = container.get(field, default)
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{field} must be an integer from {low} to {high}')
    return value


def semantic_deadline_seconds(spec):
    """Bound a repository's full semantic build without changing its indexed scope."""
    return bounded_int(spec, 'semantic_deadline_seconds', DEFAULT_SEMANTIC_DEADLINE_SECONDS, 60, 3600)


def repository_relative_path(value, field):
    if (not isinstance(value, str) or not value or len(value) > 4096 or '\\' in value or '\0' in value
            or value.startswith('/') or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError(field + ' requires canonical repository-relative paths')
    return value


def typescript_package_files(spec):
    """Declared package files, else the package manifest and lock under typescript_prefix."""
    if 'typescript_package_files' in spec:
        files = spec['typescript_package_files']
        if not isinstance(files, list) or len(files) > 32:
            raise ValueError('typescript_package_files must be a bounded list of repository paths')
        files = [repository_relative_path(path, 'typescript_package_files') for path in files]
        if len(set(files)) != len(files):
            raise ValueError('typescript_package_files requires unique paths')
        return files
    prefix = spec.get('typescript_prefix')
    if not prefix:
        return []
    return [str(PurePosixPath(prefix)/name) for name in ('package.json', 'package-lock.json')]


def request_tenant_id(request):
    if 'tenant_id' in request:
        value = request['tenant_id']
    else:
        value = json.loads(REQUEST_DEFAULTS.read_text())['tenant_id']
    if (not isinstance(value, str) or not value or value.strip() != value or
            len(value.encode('utf-8')) > 80 or not value.isprintable()):
        raise ValueError('tenant_id must be a nonblank printable string of at most 80 bytes')
    return value


def retention_references(request):
    """Operator files whose mentioned generations retention keeps; None when absent.

    Each is a canonical absolute path to a file under the refresh container's
    /config or /state mounts. Retention reads them afresh on every pass.
    """
    if 'retention_references' not in request:
        return None
    paths = request['retention_references']
    message = (f'retention_references must be a list of at most {RETENTION_REFERENCES_LIMIT} '
               'canonical absolute file paths under /config or /state')
    if not isinstance(paths, list) or len(paths) > RETENTION_REFERENCES_LIMIT:
        raise ValueError(message)
    for path in paths:
        if not isinstance(path, str) or not path.startswith('/') or len(path) > 4096 or '\0' in path:
            raise ValueError(message)
        parts = path[1:].split('/')
        if (len(parts) < 2 or parts[0] not in RETENTION_REFERENCE_MOUNTS or
                any(part in ('', '.', '..') for part in parts)):
            raise ValueError(message)
    return list(paths)


def refresh_settings(request):
    """Validate the optional efficiency and layout fields before any source or model work."""
    registry = request.get('snapshot_registry')
    if registry is not None and (not isinstance(registry, str) or not os.path.isabs(registry)):
        raise ValueError('snapshot_registry must be an absolute directory path')
    repositories = request['repositories']
    return {
        'retention_references': retention_references(request),
        'min_rebuild_interval_seconds': bounded_int(request, 'min_rebuild_interval_seconds',
                                                    DEFAULT_MIN_REBUILD_INTERVAL_SECONDS, 0, 86400),
        'retain_generations': bounded_int(request, 'retain_generations', DEFAULT_RETAIN_GENERATIONS, 1, 50),
        'semantic_retry_after_seconds': bounded_int(request, 'semantic_retry_after_seconds',
                                                    DEFAULT_SEMANTIC_RETRY_AFTER_SECONDS, 300, 604800),
        'tenant_id': request_tenant_id(request),
        'snapshot_registry': registry,
        'python_index_timeout_seconds': {
            key: bounded_int(spec, 'python_index_timeout_seconds', DEFAULT_PYTHON_INDEX_TIMEOUT_SECONDS, 60, 3600)
            for key, spec in repositories.items()},
        'typescript_package_files': {key: typescript_package_files(spec) for key, spec in repositories.items()}}


def semantic_gap(previous, key):
    """The recorded semantic failure fields only; this cycle's SCIP readiness is its own."""
    row = previous['repository_readiness'][key]
    return {field: row[field] for field in SEMANTIC_GAP_FIELDS if field in row}


def recent_semantic_gap(previous, key, identity, retry_after_seconds):
    """A failure at (source revision, semantic identity) waits out its own backoff.

    The semantic identity includes the source revision. SCIP identity and dependency
    pins are deliberately absent: they neither permit nor reset a retry.
    """
    try:
        if (previous['build_identities'][key]['semantic'] != identity or
                key in previous['semantic_indexes'] or
                previous['repository_readiness'][key]['semantic'] != 'unavailable' or
                not previous['repository_readiness'][key]['semantic_reason']):
            return False
        recorded = previous['repository_readiness'][key].get('semantic_retry_after')
        retry = (datetime.fromisoformat(recorded) if recorded else
                 datetime.fromisoformat(previous['published_at']) + timedelta(seconds=retry_after_seconds))
        return now() < retry
    except (KeyError, TypeError, ValueError):
        return False


def last_rebuild_started(path):
    try:
        value = json.loads(Path(path).read_text())['last_rebuild_started_at']
        started = datetime.fromisoformat(value)
        return started if started.tzinfo is not None else None
    except (OSError, KeyError, TypeError, ValueError):
        return None


def source_valid(row, revision):
    """Only reuse a snapshot with the declared commit and unmodified tracked source."""
    path = Path(row.get('path', ''))
    if not path.is_dir() or not (path/'.git').exists():
        return False
    try:
        return (run(['git','-C',str(path),'rev-parse','HEAD']) == revision and
                subprocess.run(['git','-C',str(path),'diff-index','--quiet','HEAD','--'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0)
    except (OSError, subprocess.SubprocessError):
        return False


def previous_catalog(previous):
    if not previous:
        return None
    try:
        path = Path(previous['knowledge_config'])
        if digest_file(path) != previous['knowledge_config_sha256']:
            return None
        return json.loads(path.read_text())
    except (KeyError, OSError, ValueError):
        return None


def reusable_scip(previous, catalog, key, row, identity, languages):
    if not previous or not catalog:
        return None
    try:
        old = previous['repos'][key]
        if (old['revision'] != row['revision'] or old['default_ref'] != row['default_ref'] or
                previous['build_identities'][key]['scip'] != identity or
                not source_valid(old, row['revision'])):
            return None
        entries = catalog['scip_indexes'][key]
        old_metrics = [m for m in previous['index_metrics'] if m.get('repo_key') == key and m.get('language') in languages]
        if len(entries) != len(languages) or {m['language'] for m in old_metrics} != set(languages):
            return None
        for entry, language in zip(entries, languages):
            if entry['revision'] != row['revision']:
                return None
            envelope = load_partitioned_index(entry['path'], expected_sha256=entry['sha256'], hydrate=False)
            if envelope['data']['repo_key'] != key:
                return None
            smoke = next(m['lookup_smoke'] for m in old_metrics if m['language'] == language)
            checked = lookup(envelope, revision=row['revision'], repo=old['path'],
                             path=smoke['path'], line=smoke['line'], limit=1)
            if checked['status'] != 'ok':
                return None
        return old, entries, old_metrics
    except (KeyError, IndexError, OSError, ValueError, TypeError):
        return None


def reusable_semantic(previous, key, row, identity):
    if not previous:
        return None
    try:
        from kp_agent_tooling._impl.semantic_index import validate_semantic_index
        old = previous['semantic_indexes'][key]
        if (previous['build_identities'][key]['semantic'] != identity or
                old['revision'] != row['revision']):
            return None
        validate_semantic_index(old['dir'], key, row['revision'], expected_sha256=old['index_sha256'])
        return old
    except (KeyError, OSError, ValueError, TypeError):
        return None


def python_index_command(toolchain, repo, key, version, environment, output, project_name=None):
    # Pyright resolves input files; scip-python 0.6.6 compares them with --cwd.
    # A symlink spelling of --cwd silently excludes all physical file paths.
    return [str(toolchain/'node_modules/.bin/scip-python'),'index','--cwd',str(repo.resolve()),
            '--project-name',project_name or key,'--project-version',version,
            '--environment',str(environment.resolve()),'--output',str(output.resolve()),'--quiet']


def validate_python_coverage(index, key):
    data = index['data']
    if not data['blobs'] or not data['occurrences']:
        raise IndexCoverageError('empty_python_index', key)


def verify_index_lookup(index, repo, revision, key):
    rows = index['data']['occurrences']
    if not rows:
        raise IndexCoverageError('index_lookup_unavailable', key)
    row = next((r for r in rows if r['definition']), rows[0])
    text = (repo/row['path']).read_bytes()
    line = text[:row['byte_offset']].count(b'\n') + 1
    result = lookup(index, revision=revision, repo=str(repo), path=row['path'], line=line, limit=50)
    if result['status'] != 'ok' or not any(r['symbol']==row['symbol'] for r in result['occurrences']):
        raise IndexCoverageError('index_lookup_failed', key)
    return {'path':row['path'],'line':line,'status':'ok'}


def resolve_source(spec):
    """Remote default unless the operator explicitly selects a product branch."""
    selected = spec.get('ref')
    if selected is not None:
        if not isinstance(selected, str) or not selected.startswith('refs/heads/'):
            raise ValueError('configured source ref must be a full branch ref')
        run(['git', 'check-ref-format', selected])
        remote = run(['git','-C',spec['repository'],'ls-remote','origin',selected],timeout=45)
        rows = [line.split() for line in remote.splitlines()]
        matches = [row[0] for row in rows if len(row)==2 and row[1]==selected]
        if len(matches)!=1 or len(matches[0]) not in (40,64):
            raise ValueError('configured source ref unavailable')
        return {'revision':matches[0],'default_ref':selected,'selection':'operator_branch'}
    remote=run(['git','-C',spec['repository'],'ls-remote','--symref','origin','HEAD'],timeout=45)
    ref=next(line.split()[1] for line in remote.splitlines() if line.startswith('ref:'))
    revision=next(line.split()[0] for line in remote.splitlines() if not line.startswith('ref:') and line.endswith('\tHEAD'))
    if not ref.startswith('refs/heads/') or len(revision) not in (40,64):raise ValueError('invalid default ref')
    return {'revision':revision,'default_ref':ref,'selection':'remote_default'}


def refresh(request):
    """One refresh cycle. A generation that never reached publication is removed."""
    context={}
    try:
        return _refresh(request,context)
    except BaseException as error:
        generation=context.get('generation')
        if generation is not None and not context.get('published'):
            failure=remove_generation(generation.parent,generation.name)
            try:
                error.generation_cleanup={'generation':generation.name,'reason':'partial_build',
                                          'removed':failure is None,**({'error':failure} if failure else {})}
            except AttributeError:
                pass
        raise


def _refresh(request, context):
    # Validate every repository budget before model setup, source fetch or publication.
    semantic_deadlines={key:semantic_deadline_seconds(spec)
                        for key,spec in request['repositories'].items()}
    analysis = analysis_settings(request)
    options = refresh_settings(request)
    platforms_for_request(request, {key: {} for key in request['repositories']})
    publication=Path(request['publication']);root=Path(request['output_root']).resolve()
    root.mkdir(parents=True,exist_ok=True);resolved={}
    inputs={'request':request,'script':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'index_builder':hashlib.sha256((Path(__file__).parent/'_impl/scip_navigation.py').read_bytes()).hexdigest(),
            'toolchain':hashlib.sha256((Path(request['toolchain'])/'package-lock.json').read_bytes()).hexdigest(),
            'templates':{k:hashlib.sha256(Path(v['serena_template']).read_bytes()).hexdigest() for k,v in request['repositories'].items()},
            'tenant_id':options['tenant_id']}
    request_sha256=leaf.sorted_sha256(inputs)
    for key,spec in request['repositories'].items():
        resolved[key]=resolve_source(spec)
    previous=json.loads(publication.read_text()) if publication.exists() else None
    catalog=previous_catalog(previous)
    repository_manifest=None;semantic_index=None
    embedder=None;semantic_setup_error=None
    if request.get('semantic'):
        try:
            from kp_agent_tooling._impl import repository_manifest, semantic_index
            from kp_agent_tooling._impl.embeddings import embedders
            from kp_agent_tooling._impl.embeddings.embedders import RealEmbedder, REAL_MODEL_WEIGHTS_DIGEST
            embedder=RealEmbedder(model_digest=REAL_MODEL_WEIGHTS_DIGEST)
        except Exception as error:
            semantic_setup_error=error
    build_identities={}
    for key,spec in request['repositories'].items():
        common={'revision':resolved[key]['revision'],'ref':resolved[key]['default_ref'],
                'template_sha256':inputs['templates'][key]}
        scip={'source':common,'refresh_sha256':inputs['script'],'builder_sha256':inputs['index_builder'],
              'toolchain_lock_sha256':inputs['toolchain'],
              'decoder_sha256':digest_file(Path(__file__).parent/'assets/decode_scip.cjs'),
              'python_scope':spec['python_scope'],'typescript_prefix':spec.get('typescript_prefix'),
              'typescript_package_files':options['typescript_package_files'][key],
              'node':request['node'],
              'analysis': analysis[key],
              'analysis_dependency_pins': {member: resolved[member]['revision']
                                           for member in analysis[key]['analysis_dependencies']},
              'analysis_dependency_settings': {member: {'package': analysis[member]['python_package_name'],
                                                       'version_source': analysis[member]['python_version_source'],
                                                       'python_scope': request['repositories'][member]['python_scope']}
                                               for member in analysis[key]['analysis_dependencies']},
              'python_extra_path_pins': {member: resolved[member]['revision']
                                         for member in analysis[key]['python_extra_paths']}}
        semantic={'source_revision':resolved[key]['revision'],'refresh_sha256':inputs['script'],
                  'deadline_seconds':semantic_deadlines[key],
                  'policy':None if repository_manifest is None else repository_manifest.POLICY_VERSION,
                  'policy_sha256':None if repository_manifest is None else digest_file(repository_manifest.__file__),
                  'chunking':None if semantic_index is None else semantic_index.CHUNKING,
                  'builder_sha256':None if semantic_index is None else digest_file(semantic_index.__file__),
                  'embedder_sha256':None if embedder is None else digest_file(embedders.__file__),
                  'provider_package':None if embedder is None else installed_version('sentence-transformers'),
                  'embedding':None if embedder is None else {'model_id':embedder.revision.model_id,
                    'model_digest':embedder.revision.model_digest,'revision_id':embedder.revision.revision_id,
                    'dim':embedder.dim}}
        build_identities[key]={'scip':digest_json(scip),'semantic':digest_json(semantic)}
    reused={};indexes={};metrics=[]
    for key,spec in request['repositories'].items():
        languages=['python']+(['typescript'] if spec.get('typescript_prefix') else [])
        old=reusable_scip(previous,catalog,key,resolved[key],build_identities[key]['scip'],languages)
        if old:
            resolved[key]['path']=old[0]['path'];reused[key]=True
            indexes[key]=old[1];metrics.extend(old[2])
    semantic_selected={key for key in resolved if request.get('semantic') and
                       (key in request['semantic'] if isinstance(request['semantic'],list) else True)}
    # Semantic reuse and failure backoff follow the semantic identity (which carries
    # the source revision), independent of SCIP reuse or dependency pins.
    old_semantic={}
    if semantic_setup_error is None:
        for key in semantic_selected:
            candidate=reusable_semantic(previous,key,resolved[key],build_identities[key]['semantic'])
            if candidate:
                old_semantic[key]=candidate
    negative_semantic={key for key in semantic_selected if
                       recent_semantic_gap(previous,key,build_identities[key]['semantic'],
                                           options['semantic_retry_after_seconds'])}
    if (previous and previous.get('request_sha256')==request_sha256 and
            set(reused)==set(resolved) and
            (set(old_semantic)|negative_semantic)==semantic_selected):
        profile_sha256=hashlib.sha256(publication.read_bytes()).hexdigest()
        readiness={k:{'scip':'validated_reuse','semantic':
                    'validated_reuse' if k in old_semantic else 'not_requested'} for k in resolved}
        for key in negative_semantic:
            readiness[key].update(semantic_gap(previous,key))
        atomic(request['check_status'],{'status':'current','checked_at':stamp(),'source_revisions':resolved,
                                        'profile_sha256':profile_sha256,
                                        'repository_readiness':readiness})
        return {'status':'current','source_revisions':resolved,'repository_readiness':readiness}
    # Debounce: at most one rebuild starts per interval; a cycle inside it builds nothing.
    interval=options['min_rebuild_interval_seconds']
    started=last_rebuild_started(root/REBUILD_STATE)
    checked=now()
    if interval and started is not None:
        eligible=min(started,checked)+timedelta(seconds=interval)
        if checked<eligible:
            pending={'status':'pending_rebuild','checked_at':checked.isoformat(),
                     'next_eligible_at':eligible.isoformat(),'last_rebuild_started_at':started.isoformat(),
                     'min_rebuild_interval_seconds':interval,'source_revisions':resolved,
                     'last_good_profile':'preserved'}
            if publication.exists():
                pending['profile_sha256']=hashlib.sha256(publication.read_bytes()).hexdigest()
            atomic(request['check_status'],pending)
            return {'status':'pending_rebuild','next_eligible_at':pending['next_eligible_at'],
                    'source_revisions':resolved}
    atomic(root/REBUILD_STATE,{'schema_version':'agent-tooling.refresh-rebuild-state.v1',
                               'last_rebuild_started_at':checked.isoformat()})
    generation=Path(leaf.make_temp_dir(prefix='snapshot-',dir=root))
    context['generation']=generation
    for key,spec in request['repositories'].items():
        if key in reused:
            continue
        path=generation/key
        try:
            run(['git','clone','--quiet','--no-hardlinks','--no-checkout',spec['repository'],str(path)])
        except Exception as error:
            raise RefreshStepError(key,'source_snapshot',error) from error
        alternates=path/'.git/objects/info/alternates'
        if spec.get('extra_workspaces'):
            with alternates.open('a') as f:
                for extra in spec['extra_workspaces']:
                    f.write(run(['git','-C',extra,'rev-parse','--path-format=absolute','--git-path','objects'])+'\n')
        url=run(['git','-C',spec['repository'],'remote','get-url','origin'])
        run(['git','-C',str(path),'remote','set-url','origin',url])
        run(['git','-C',str(path),'fetch','--quiet','--no-tags','origin',resolved[key]['default_ref']])
        if run(['git','-C',str(path),'rev-parse','FETCH_HEAD'])!=resolved[key]['revision']:raise ValueError('default changed during build')
        run(['git','-C',str(path),'checkout','--quiet','--detach',resolved[key]['revision']])
        resolved[key]['path']=str(path)
        (path/'.serena').mkdir(exist_ok=True)
        if not (path/'.serena/project.yml').exists():shutil.copyfile(spec['serena_template'],path/'.serena/project.yml')
        # Serena creates this on first use; provision it before publishing the boundary.
        if not (path/'.serena/.gitignore').exists():
            (path/'.serena/.gitignore').write_text('/cache\n/project.local.yml\n')
    for key, spec in request['repositories'].items():
        if key in reused:
            continue
        path = Path(resolved[key]['path'])
        if not (path/'pyrightconfig.json').exists():
            settings={'include':spec['python_scope'],'pythonVersion':'3.12'}
            if analysis[key]['python_extra_paths']:
                settings['extraPaths']=[resolved[member]['path']
                                        for member in analysis[key]['python_extra_paths']]
            (path/'pyrightconfig.json').write_text(json.dumps(settings))
    versions = {}
    for key, setting in analysis.items():
        if setting['python_version_source'] == 'pyproject':
            project = Path(resolved[key]['path'])/'pyproject.toml'
            versions[key] = tomllib.loads(project.read_text())['project']['version']
        else:
            versions[key] = resolved[key]['revision']
    envfiles = {}
    for key, setting in analysis.items():
        environment = []
        for member in setting['analysis_dependencies']:
            member_repo = Path(resolved[member]['path'])
            scopes = request['repositories'][member]['python_scope']
            files = run(['git','-C',str(member_repo),'ls-files',*scopes]).splitlines()
            environment.append({'name': analysis[member]['python_package_name'],
                                'version': versions[member], 'files': files})
        envfile = generation/(key+'-analysis-environment.json')
        envfile.write_text(json.dumps(environment))
        envfiles[key] = envfile
    toolchain=Path(request['toolchain'])
    def index(key,language,prefix=''):
        repo=Path(resolved[key]['path']);raw=generation/(key+'-'+language+'.scip')
        if language=='python':
            settings=repo/'pyrightconfig.json';environment=envfiles[key]
            cmd=python_index_command(toolchain,repo,key,versions[key],environment,raw,
                                     project_name=analysis[key]['python_package_name'])
            timeout=options['python_index_timeout_seconds'][key]
        else:
            # Settings: tsconfig, else the prefix manifest, else none (--infer-tsconfig).
            # Environment: the declared package files present at this revision.
            settings=next((path for path in (repo/prefix/'tsconfig.json',repo/prefix/'package.json')
                           if path.is_file()),None)
            environment=None
            cmd=[str(toolchain/'node_modules/.bin/scip-typescript'),'index','--cwd',str(repo/prefix),'--infer-tsconfig','--no-progress-bar','--output',str(raw)]
            timeout=TYPESCRIPT_INDEX_TIMEOUT_SECONDS
        try:
            atomic(request['check_status'], {'status':'refreshing','stage':language+'_index',
                   'repo_key':key,'checked_at':stamp(),'last_good_profile':'preserved'})
            run(cmd,timeout=timeout)
            decoded=json.loads(run([request['node'],str(Path(__file__).parent/'assets/decode_scip.cjs'),str(toolchain),str(raw)],timeout=120))
            provenance={name:None if path is None else hashlib.sha256(path.read_bytes()).hexdigest() for name,path in {'scip_sha256':raw,'settings_sha256':settings,'environment_sha256':environment,'toolchain_lock_sha256':toolchain/'package-lock.json'}.items()}
            if language=='typescript':
                provenance['environment_sha256']=digest_json({path:digest_file(repo/path)
                    for path in options['typescript_package_files'][key] if (repo/path).is_file()})
            built=build_index(str(repo),resolved[key]['revision'],key,decoded,provenance=provenance,prefix=prefix)
            if language=='python':validate_python_coverage(built,key)
            smoke=verify_index_lookup(built,repo,resolved[key]['revision'],key)
            out=generation/(key+'-'+language+'.ops.json');written=write_partitioned_index(out,built)
            published=load_partitioned_index(out,expected_sha256=written['sha256'],hydrate=False)
            published_smoke=lookup(published,revision=resolved[key]['revision'],repo=str(repo),
                                   path=smoke['path'],line=smoke['line'],limit=50)
            if published_smoke['status'] != 'ok':
                raise IndexCoverageError('published_index_lookup_failed',key)
        except Exception as error:
            raise RefreshStepError(key,language+'_index',error) from error
        indexes.setdefault(key,[]).append({'path':str(out),'revision':resolved[key]['revision'],'sha256':written['sha256']})
        metrics.append({'repo_key':key,'language':language,'documents':len(built['data']['blobs']),'occurrences':len(built['data']['occurrences']),'gaps':len(built['data']['gaps']),'lookup_smoke':smoke})
    for key in resolved:
        if key not in reused:index(key,'python')
    for key,spec in request['repositories'].items():
        if key not in reused and spec.get('typescript_prefix'):index(key,'typescript',spec['typescript_prefix'])
    sources={};repos={}
    for key,row in resolved.items():
        artifacts=[]
        for path in dict.fromkeys(('requirements.txt','pyproject.toml',*options['typescript_package_files'][key])):
            if subprocess.run(['git','-C',row['path'],'cat-file','-e',row['revision']+':'+path],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:artifacts.append({'path':path,'role':'dependency'})
        sources[key]={'revision':row['revision'],'artifacts':artifacts}
        repos[key]={'path':row['path'],'ref':row['revision'],'corpus_scope':key,'tenant_ids':[options['tenant_id']],'capabilities':{}}
    # Opt-in semantic (concept-to-location) indexes, built from the same generation's
    # committed trees with the pinned open-weights embedder. Published beside the SCIP
    # indexes; the facade resolves them through profile['semantic_indexes'].
    semantic_indexes=dict(old_semantic)
    readiness={key:{'scip':'validated_reuse' if key in reused else 'built',
                    'semantic':'not_requested'} for key in resolved}
    def publish(complete):
        catalog=catalog_payload(request,repos,sources,indexes)
        catpath=generation/'knowledge.json';atomic(catpath,catalog)
        profile={'schema_version':'ops.navigation-profile.v1','profile':'dev-current','request_sha256':request_sha256,'published_at':stamp(),'repos':resolved,'knowledge_config':str(catpath),'knowledge_config_sha256':hashlib.sha256(catpath.read_bytes()).hexdigest(),'check_status':request['check_status'],
                 'workspace_repositories':{k:s.get('workspace_observation_roots',[s['repository'],*s.get('extra_workspaces',[])]) for k,s in request['repositories'].items()},'index_metrics':metrics,'semantic_indexes':semantic_indexes,
                 'build_identities':build_identities,'repository_readiness':readiness,
                 'limitations':['Indexes cover configured paths only.','Analysis package identity is not installed or deployed equivalence.','Historical verification evidence is not repinned.']}
        atomic(generation/'profile.json',profile);atomic(publication,profile)
        context['published']=True
        atomic(request['check_status'],{'status':'published' if complete else 'refreshing','stage':'complete' if complete else 'optional_semantic',
                                        'source_navigation':'published','checked_at':stamp(),'source_revisions':resolved,
                                        'profile_sha256':hashlib.sha256(publication.read_bytes()).hexdigest(),
                                        'repository_readiness':readiness})
        return {'status':'published' if complete else 'refreshing','profile':str(publication),'source_revisions':resolved,
                'index_metrics':metrics,'repository_readiness':readiness}
    if semantic_selected:
        for key in semantic_selected:
            if key in old_semantic:
                readiness[key]['semantic'] = 'validated_reuse'
            elif key in negative_semantic:
                readiness[key].update(semantic_gap(previous,key))  # the gap stays reported
            else:
                readiness[key]['semantic'] = 'building'
        # Optional embedding must not withhold newly validated source/SCIP navigation.
        publish(False)
    if semantic_selected:
        os.environ.setdefault('HF_HUB_OFFLINE','1');os.environ.setdefault('TRANSFORMERS_OFFLINE','1')
        nav_config={'repos':{k:{'path':r['path'],'revision':r['revision']} for k,r in resolved.items()},
                    'navigation_profile':None}
        for key in semantic_selected:
            row=resolved[key]
            if old_semantic.get(key):
                semantic_indexes[key]=old_semantic[key]
                metrics.extend(m for m in previous['index_metrics'] if m.get('repo_key')==key and m.get('language')=='semantic')
                readiness[key]['semantic']='validated_reuse'
                continue
            if key in negative_semantic:
                readiness[key].update(semantic_gap(previous,key))
                metrics.extend(m for m in previous['index_metrics'] if m.get('repo_key')==key and m.get('language')=='semantic')
                continue
            try:
                if semantic_setup_error is not None:
                    raise semantic_setup_error
                summary=semantic_index.build_semantic_index({**nav_config,'_active_override':{'profile':'building','repos':nav_config['repos']}},
                                             key,row['revision'],embedder=embedder,out_dir=generation,
                                             deadline_seconds=semantic_deadlines[key])
                semantic_index.validate_semantic_index(generation,key,row['revision'],expected_sha256=summary['index_sha256'])
                semantic_indexes[key]={'dir':str(generation),'revision':row['revision'],
                                       'index_sha256':summary['index_sha256'],'chunk_count':summary['chunk_count'],
                                       'embedding':summary['embedding']}
                metrics.append({'repo_key':key,'language':'semantic','documents':summary['files_indexed'],
                                'occurrences':summary['chunk_count'],'gaps':sum(summary['files_skipped'].values()),
                                'lookup_smoke':{'status':'ok','build_seconds':summary['build_seconds']}})
                readiness[key]['semantic']='built'
            except Exception as error:  # semantic is optional, but omission is specific and visible
                reason=getattr(error,'reason',type(error).__name__)
                readiness[key]['semantic']='unavailable'
                readiness[key]['semantic_reason']=reason
                readiness[key]['semantic_retry_after']=(now()+
                    timedelta(seconds=options['semantic_retry_after_seconds'])).isoformat()
                readiness[key]['remediation']=('Reduce the selected committed text scope or raise the reviewed '
                    'semantic source budget, then refresh.' if reason in {'byte_budget','file_budget','chunk_budget'}
                    else 'Inspect the named semantic provider or artifact and refresh.')
                metrics.append({'repo_key':key,'language':'semantic','status':'skipped','reason':reason})
            # Expose each completed repository without waiting for the remaining builds.
            publish(False)
    result=publish(True)
    # Bounded retention after the successful publish; the receipt lists every removal.
    # Housekeeping never turns a completed publication into a refresh failure.
    # Listed reference files are passed only when the request names them (absent: as before).
    references=({} if options['retention_references'] is None else
                {'retention_references':options['retention_references']})
    try:
        result['retention']=prune_generations(root,publication=publication,retain=options['retain_generations'],
                                              registry=options['snapshot_registry'],checked_at=stamp(),
                                              **references)
        atomic(root/RETENTION_RECEIPT,result['retention'])
    except Exception as error:
        result['retention']={'status':'skipped','reason':type(error).__name__,'checked_at':stamp()}
    return result


# How often a not-configured watch looks for its request file again.
NOT_CONFIGURED_POLL_SECONDS=10


def runtime_path(path):
    """A path as the operator names it: relative to the runtime root when the role
    declares where that root's directories are mounted (AGENT_RUNTIME_ROOT_MOUNTS)."""
    try:mounts=json.loads(os.environ.get('AGENT_RUNTIME_ROOT_MOUNTS') or '{}')
    except ValueError:mounts={}
    pure=PurePosixPath(path)
    for target,relative in (mounts.items() if isinstance(mounts,dict) else ()):
        if isinstance(target,str) and isinstance(relative,str) and pure.is_absolute() and pure.is_relative_to(target):
            return str(PurePosixPath(relative,pure.relative_to(target)))
    return str(path)


def unwritten(path):
    """An operator file not yet written: nothing at the path, or an empty regular file."""
    try:info=os.lstat(path)
    except FileNotFoundError:return True
    return stat.S_ISREG(info.st_mode) and info.st_size==0


def not_configured(path):
    return {'status':'not_configured','missing':[runtime_path(path)],'missing_paths':[str(path)],
            'detail':'refresh waits for its operator request file and starts once it exists; nothing is fetched until then'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--request',required=True);p.add_argument('--watch',action='store_true');p.add_argument('--interval',type=int,default=300);args=p.parse_args()
    if args.interval<60:p.error('minimum check interval is 60 seconds')
    if unwritten(args.request):
        # Not configured: report the unwritten operator file; a watch waits for it without exiting.
        print(json.dumps(not_configured(args.request)),flush=True)
        if not args.watch:return 1
        while unwritten(args.request):time.sleep(min(NOT_CONFIGURED_POLL_SECONDS,args.interval))
    request=json.loads(Path(args.request).read_text())
    Path(request['output_root']).mkdir(parents=True,exist_ok=True)
    lock=open(Path(request['output_root'])/'.refresh.lock','a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({'status':'refresh_already_running'}));return 1
    while True:
        try:
            atomic(request['check_status'], {'status':'refreshing','stage':'source_resolution',
                   'checked_at':stamp(),'last_good_profile':'preserved',
                   'observation':'attempt started; not a process-liveness guarantee'})
            print(json.dumps(refresh(request)),flush=True)
        except Exception as error:
            # Never emit remote URLs, credential-bearing stderr or source contents.
            failure=failure_receipt(error)
            atomic(request['check_status'],failure);print(json.dumps(failure),flush=True)
            if not args.watch:return 1
        if not args.watch:return 0
        time.sleep(args.interval)


if __name__=='__main__':raise SystemExit(main())
