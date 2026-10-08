"""Bounded syntax context; compiler navigation retains declaration authority."""
import ast
import hashlib
import importlib.util
import os
import platform
import re
import subprocess
import time
from dataclasses import dataclass
from kp_agent_tooling._impl.source_citations import source


SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
DEFAULT_RESOLUTION_LIMITS = {
    'files': 16, 'source_bytes': 2_000_000, 'recursion': 8, 'seconds': 10.0,
}


class ResolutionBudgetExceeded(Exception):
    def __init__(self, reason):
        self.reason = reason


@dataclass
class _ResolutionBudget:
    limits: dict
    started: float
    files: int = 0
    source_bytes: int = 0
    max_recursion: int = 0

    @classmethod
    def create(cls, limits):
        effective = dict(DEFAULT_RESOLUTION_LIMITS)
        if limits:
            effective.update(limits)
        if (not isinstance(effective['files'], int) or effective['files'] < 1 or
                not isinstance(effective['source_bytes'], int) or effective['source_bytes'] < 1 or
                not isinstance(effective['recursion'], int) or effective['recursion'] < 1 or
                not isinstance(effective['seconds'], (int, float)) or
                not 0 < float(effective['seconds']) <= 60):
            raise ValueError('invalid dependency resolution limits')
        effective['seconds'] = float(effective['seconds'])
        return cls(effective, time.monotonic())

    def remaining_seconds(self):
        remaining = self.limits['seconds'] - (time.monotonic() - self.started)
        if remaining <= 0:
            raise ResolutionBudgetExceeded('resolution_elapsed_budget_exceeded')
        return remaining

    def enter(self, depth):
        self.remaining_seconds()
        self.max_recursion = max(self.max_recursion, depth)
        if depth > self.limits['recursion']:
            raise ResolutionBudgetExceeded('resolution_recursion_budget_exceeded')

    def read(self, repo, revision, path):
        self.remaining_seconds()
        if self.files >= self.limits['files']:
            raise ResolutionBudgetExceeded('resolution_file_budget_exceeded')
        remaining = self.limits['source_bytes'] - self.source_bytes
        if remaining <= 0:
            raise ResolutionBudgetExceeded('resolution_source_byte_budget_exceeded')
        self.files += 1
        try:
            if not SHA.fullmatch(revision):
                raise ValueError('full source commit required')
            if (not isinstance(path,str) or not path or path.startswith('/') or
                    any(part in ('','.','..') for part in path.split('/'))):
                raise ValueError('canonical repository-relative path required')
            if _git(repo,'rev-parse',revision+'^{commit}',budget=self) != revision:
                raise ValueError('revision must identify a commit')
            entry=_git(repo,'ls-tree',revision,'--',path,budget=self).rstrip('\n')
            if not entry or '\t' not in entry:
                raise ValueError('source not found')
            metadata,actual=entry.split('\t',1)
            mode,kind,blob=metadata.split()
            if mode not in ('100644','100755') or kind!='blob' or actual!=path:
                raise ValueError('regular source file required')
            size=int(_git(repo,'cat-file','-s',blob,budget=self))
            if size>remaining:
                raise ResolutionBudgetExceeded('resolution_source_byte_budget_exceeded')
            raw=_git_blob(repo,blob,self)
            text=raw.decode('utf-8')
        except subprocess.TimeoutExpired as error:
            raise ResolutionBudgetExceeded('resolution_elapsed_budget_exceeded') from error
        size = len(raw)
        self.source_bytes += size
        if self.source_bytes > self.limits['source_bytes']:
            raise ResolutionBudgetExceeded('resolution_source_byte_budget_exceeded')
        self.remaining_seconds()
        return blob, text

    def usage(self):
        return {'files': self.files, 'source_bytes': self.source_bytes,
                'max_recursion': self.max_recursion,
                'elapsed_seconds': round(time.monotonic() - self.started, 6)}


def _git(root, *args, budget=None):
    timeout = min(10, budget.remaining_seconds()) if budget else 10
    env={key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_TERMINAL_PROMPT='0',GIT_OPTIONAL_LOCKS='0')
    try:
        return subprocess.run(['git', '--no-optional-locks', *args], cwd=root,env=env,check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=timeout, text=True).stdout.strip()
    except subprocess.TimeoutExpired as error:
        if budget:
            raise ResolutionBudgetExceeded('resolution_elapsed_budget_exceeded') from error
        raise


def _git_blob(root, blob, budget):
    env={key:value for key,value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_TERMINAL_PROMPT='0',GIT_OPTIONAL_LOCKS='0')
    try:
        raw=subprocess.run(['git','--no-optional-locks','cat-file','blob',blob],cwd=root,
            env=env,check=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            timeout=min(10,budget.remaining_seconds())).stdout
    except subprocess.TimeoutExpired as error:
        raise ResolutionBudgetExceeded('resolution_elapsed_budget_exceeded') from error
    return raw


def _normal_distribution(value):
    return re.sub(r'[-_.]+', '-', value).lower()


def _committed_pin(repo, revision, distribution, budget):
    """Read the bounded root requirements file without importing consumer code."""
    try:
        blob, text = budget.read(repo, revision, 'requirements.txt')
    except ResolutionBudgetExceeded:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, 'pin_file_unavailable'
    matches=[]
    includes=False
    wanted=_normal_distribution(distribution)
    for raw in text.splitlines():
        line=raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith(('-r','--requirement','-c','--constraint')):
            includes=True
            continue
        candidate=line.split('#',1)[0].strip()
        package=re.split(r'[<>=!~;\s\[]', candidate, maxsplit=1)[0]
        if _normal_distribution(package)!=wanted:
            continue
        if ';' in candidate:
            return None, 'unsupported_pin_marker'
        exact=re.fullmatch(r'([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([A-Za-z0-9][A-Za-z0-9._+-]*)',candidate)
        if not exact:
            return None, 'unsupported_pin_specifier'
        matches.append(exact.group(2))
    if includes:
        return None, 'unsupported_requirements_include'
    if not matches:
        return None, 'unsupported_requirements_include' if includes else 'dependency_pin_not_found'
    if len(set(matches))!=1 or len(matches)>1:
        return None, 'conflicting_exact_pins'
    return {'pin_file':'requirements.txt','pin_blob_sha':blob,
            'package':distribution,'version':matches[0]}, None


def _select_revision(consumer_repo, consumer_revision, repositories, mapping, explicit, budget):
    target_key=mapping['repo_key']
    if target_key not in repositories:
        return None, 'target_repo_unconfigured'
    target=repositories[target_key]
    if target_key in explicit:
        revision=explicit[target_key]
        try:_git(target['path'],'cat-file','-e',revision+'^{commit}',budget=budget)
        except ResolutionBudgetExceeded:raise
        except (OSError,subprocess.SubprocessError):return None,'explicit_revision_unavailable'
        return ('explicit',revision,{'resolved_commit':revision}),None
    pin,reason=_committed_pin(consumer_repo,consumer_revision,mapping['distribution'],budget)
    if reason:return None,reason
    tag='v'+pin['version']
    try:resolved=_git(target['path'],'rev-parse','--verify',f'refs/tags/{tag}^{{commit}}',budget=budget)
    except ResolutionBudgetExceeded:raise
    except (OSError,subprocess.SubprocessError):return None,'pin_tag_unavailable'
    if not SHA.fullmatch(resolved):return None,'pin_tag_unavailable'
    provenance={**pin,'tag':tag,'resolved_commit':resolved}
    return ('pinned',resolved,provenance),None


def _module_source(repo, revision, module, budget):
    paths=[module.replace('.','/')+'.py',module.replace('.','/')+'/__init__.py']
    found=[]
    for path in paths:
        try:found.append((path, *budget.read(repo,revision,path)))
        except ResolutionBudgetExceeded:raise
        except (OSError,ValueError,subprocess.SubprocessError):continue
    if len(found)>1:return None,'ambiguous_module_layout'
    if not found:return None,'target_module_not_found'
    return found[0],None


def _bound_names(node):
    if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
        return [node.name]
    if isinstance(node,(ast.Assign,ast.AnnAssign)):
        targets=node.targets if isinstance(node,ast.Assign) else [node.target]
        return [target.id for target in targets if isinstance(target,ast.Name)]
    if isinstance(node,ast.Import):
        return [alias.asname or alias.name.split('.')[0] for alias in node.names]
    if isinstance(node,ast.ImportFrom):
        return [alias.asname or alias.name for alias in node.names]
    if isinstance(node,(ast.Delete,ast.AugAssign)):
        targets=node.targets if isinstance(node,ast.Delete) else [node.target]
        return [target.id for target in targets if isinstance(target,ast.Name)]
    return []


def _relative_module(current_module, path, node):
    if not node.level:
        return node.module or ''
    package=current_module if path.endswith('/__init__.py') else current_module.rpartition('.')[0]
    try:return importlib.util.resolve_name('.'*node.level+(node.module or ''),package)
    except (ImportError,ValueError):return None


def _declaration(repo, revision, module, imported_name, budget, depth=1, seen=None,
                 chain=None, mapped_root=None):
    budget.enter(depth)
    seen=set() if seen is None else seen
    chain=[] if chain is None else chain
    identity=(module,imported_name)
    if identity in seen:return None,'declaration_reexport_cycle'
    seen={*seen,identity}
    loaded,reason=_module_source(repo,revision,module,budget)
    if reason:return None,reason
    path,blob,text=loaded
    if imported_name is None:
        budget.remaining_seconds()
        return {'path':path,'start_line':1,'end_line':max(1,len(text.splitlines())),
                'blob_sha':blob,'declaration_kind':'module','reexport_chain':chain},None
    try:tree=ast.parse(text)
    except (SyntaxError,RecursionError):return None,'target_source_parse_failed'
    budget.remaining_seconds()
    direct=[];exports=[]
    for node in tree.body:
        names=_bound_names(node)
        if imported_name not in names:
            continue
        if isinstance(node,ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name)==imported_name:
                    exports.append((node,alias))
        elif isinstance(node,ast.Import):
            return None,'module_reexport_unsupported'
        elif isinstance(node,(ast.Delete,ast.AugAssign)):
            return None,'ambiguous_declaration'
        else:direct.append(node)
    matches=len(direct)+len(exports)
    if matches>1:return None,'ambiguous_declaration'
    if any(isinstance(node,ast.ImportFrom) and any(alias.name=='*' for alias in node.names)
           for node in tree.body):
        return None,'star_reexport_unsupported'
    for node in tree.body:
        if isinstance(node,(ast.If,ast.Try,ast.For,ast.AsyncFor,ast.While,ast.With,ast.AsyncWith,ast.Match)):
            if any(imported_name in _bound_names(child) for child in ast.walk(node)):
                return None,'conditional_declaration_unsupported'
    if direct:
        node=direct[0]
        kind=('function' if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) else
              'class' if isinstance(node,ast.ClassDef) else 'assignment')
        budget.remaining_seconds()
        return {'path':path,'start_line':node.lineno,
                'end_line':getattr(node,'end_lineno',node.lineno),'blob_sha':blob,
                'declaration_kind':kind,'reexport_chain':chain},None
    if exports:
        node,alias=exports[0]
        target_module=_relative_module(module,path,node)
        if not target_module:return None,'relative_reexport_unsupported'
        if mapped_root and target_module.split('.',1)[0] != mapped_root:
            return None,'reexport_outside_mapped_root'
        budget.remaining_seconds()
        step={'module':module,'path':path,'line':node.lineno,
              'imported_name':alias.name,'exported_name':imported_name}
        return _declaration(repo,revision,target_module,alias.name,budget,depth+1,seen,
                            chain+[step],mapped_root)
    if any(isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name=='__getattr__'
           for node in tree.body):
        return None,'dynamic_exports_unsupported'
    return None,'declaration_not_found'


def import_context(repo, revision, repo_key, path, symbol, citations, limit=24, *,
                   repositories=None, import_mappings=None, dependency_revisions=None,
                   resolution_limits=None):
    result = {'status': 'unavailable', 'imports': [], 'omitted': 0,
              'source_revision': revision, 'runtime_resolution': 'not-assessed',
              'limitations': ['Import syntax is not a resolved binding. Follow compiler continuations.',
                             'Shadowing, conditional imports, star imports and dynamic dispatch may be unresolved.']}
    if not path.endswith('.py'):
        return dict(result, reason='language_not_supported')
    blob, text = source(repo, revision, path)
    result.update(blob_sha=blob, path=path, parser={'name': 'python.ast', 'version': platform.python_version()},
                  source_sha256=hashlib.sha256(text.encode()).hexdigest())
    if any(c.get('revision') != revision or c.get('path') != path or c.get('blob_sha') != blob for c in citations):
        return dict(result, reason='citation_source_mismatch')
    if len(text.encode()) > 1_000_000:
        return dict(result, reason='source_budget_exceeded')
    try:
        tree = ast.parse(text)
    except (SyntaxError, RecursionError):
        return dict(result, reason='source_parse_failed')
    # Use verified Serena ranges, including decorators/defaults where they fall
    # within the range. No scope resolution is inferred from ast.Name.
    ranges = [(c['start_line'], c['end_line']) for c in citations]
    uses = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and any(a <= node.lineno <= b for a,b in ranges):
            uses.setdefault(node.id, node.lineno)
    if not ranges:
        uses[symbol] = None  # Imported names need not be Serena declarations.
    rows = []
    # The dependency budget begins after the already-bounded consumer source is
    # parsed; its usage covers pin selection and mapped target declarations.
    budget=_ResolutionBudget.create(resolution_limits)
    dependency_pins={};selection_cache={}
    repositories=repositories or {}
    import_mappings=import_mappings or {}
    dependency_revisions=dependency_revisions or {}
    lines = text.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for alias in node.names:
            local = alias.asname or (alias.name.split('.')[0] if isinstance(node, ast.Import) else alias.name)
            if symbol is not None and local not in uses and alias.name != '*':
                continue
            module = ('.' * node.level + (node.module or '')) if isinstance(node, ast.ImportFrom) else alias.name
            line = uses.get(local) or alias.lineno
            row={'local_name': local, 'module': module, 'imported_name': alias.name,
                         'import_line': alias.lineno, 'lookup_line': line,
                         'excerpt': '\n'.join(lines[node.lineno-1:node.end_lineno])[:2000],
                         'binding_status': 'unresolved',
                         'next_call': {'tool': 'knowledge.symbol', 'arguments': {
                             'repo_key': repo_key, 'target_revision': revision, 'path': path, 'line': line}}}
            root=module.split('.',1)[0] if not module.startswith('.') else None
            mapping=import_mappings.get(root)
            if mapping and len(rows)<limit:
                cache_key=(mapping['repo_key'],mapping['distribution'])
                if cache_key not in selection_cache:
                    try:
                        selection_cache[cache_key]=_select_revision(
                            repo,revision,repositories,mapping,dependency_revisions,budget)
                    except ResolutionBudgetExceeded as error:
                        selection_cache[cache_key]=(None,error.reason)
                selected,reason=selection_cache[cache_key]
                if selected:
                    selection,target_revision,provenance=selected
                    try:
                        declaration,reason=_declaration(
                            repositories[mapping['repo_key']]['path'],target_revision,
                            alias.name if isinstance(node,ast.Import) else module,
                            None if isinstance(node,ast.Import) else alias.name,budget,
                            mapped_root=root)
                    except ResolutionBudgetExceeded as error:
                        declaration,reason=None,error.reason
                    if declaration:
                        target_key=mapping['repo_key']
                        row.update(binding_status='resolved_cross_repo',target_repo_key=target_key,
                                   target_revision=target_revision,selection=selection,
                                   revision_provenance=provenance,**declaration)
                        profile_revision=repositories[target_key].get('revision')
                        row['configured_target_revision']=profile_revision
                        row['continuation_snapshot_compatible']=(profile_revision==target_revision)
                        row['next_call']={'tool':'navigation.source','arguments':{
                            'repo_key':target_key,'target_revision':target_revision,
                            'path':declaration['path'],'start_line':declaration['start_line'],
                            'line_count':min(200,max(20,declaration['end_line']-declaration['start_line']+1))}}
                        if selection=='pinned':dependency_pins[target_key]=provenance
                if reason:row['resolution_reason']=reason
            rows.append(row)
    rows.sort(key=lambda row: (row['lookup_line'], row['import_line'], row['local_name']))
    return dict(result, status='ok' if rows else 'no_results', imports=rows[:limit],
                dependency_pins=dependency_pins,
                resolution_limits=budget.limits,resolution_usage=budget.usage(),
                omitted=max(0, len(rows)-limit), absence_verdict='not-established',
                coverage='all file imports; syntax only' if symbol is None else 'imports matching names in verified symbol ranges; syntax only')
