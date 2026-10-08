"""Separate committed declarations from selected installed provider packages."""
import hashlib
import json
from pathlib import PurePosixPath
import re
import subprocess
import tomllib
from kp_agent_tooling._impl.source_citations import source

MANIFESTS = ('pyproject.toml','requirements.txt','requirements.lock','uv.lock','package.json','package-lock.json',
             'pnpm-lock.yaml','yarn.lock','tsconfig.json','jsconfig.json')

_PIN = re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[A-Za-z0-9_,.-]+\])?\s*(==|===|~=|>=|<=|>|<|!=)\s*([A-Za-z0-9][A-Za-z0-9._+!*,-]*)$')
_INCLUDE = re.compile(r'^-(r|c)\s+([^\s#]+)$|^--(requirement|constraint)\s+([^\s#]+)$')


def _requirements(repo, revision, path, names, active, budget, group='requirements', loaded=None):
    """Inspect only committed, canonical local files; never invoke pip."""
    if path in active:
        return [], [{'path': path, 'reason': 'include_cycle'}]
    if len(active) >= 16 or budget[0] <= 0 or budget[1] <= 0:
        return [], [{'path': path, 'reason': 'include_budget_exceeded'}]
    active = active | {path}
    budget[1] -= 1
    try:
        blob, text = loaded if loaded is not None else source(repo, revision, path,
                                                              max_bytes=min(256_000, budget[0]))
    except ValueError as exc:
        reason = 'include_size_exceeded' if str(exc) == 'source byte budget exceeded' else 'include_unavailable'
        return [], [{'path': path, 'reason': reason}]
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return [], [{'path': path, 'reason': 'include_unavailable'}]
    if len(text.encode()) > 256_000 or len(text.encode()) > budget[0]:
        return [], [{'path': path, 'reason': 'include_size_exceeded'}]
    budget[0] -= len(text.encode())
    rows, gaps = [], []
    for number, raw in enumerate(text.splitlines(), 1):
        if number > 2000:
            gaps.append({'path': path, 'reason': 'line_budget_exceeded'}); break
        line = raw.split(' #', 1)[0].strip()
        if not line or line.startswith('#'):
            continue
        include = _INCLUDE.fullmatch(line)
        if include:
            target = include.group(2) or include.group(4)
            parts = list(PurePosixPath(path).parent.parts) + target.split('/')
            normalized = []
            escaped = False
            for part in parts:
                if part == '..':
                    if normalized: normalized.pop()
                    else: escaped = True; break
                elif part not in ('', '.'): normalized.append(part)
            candidate = '/'.join(normalized)
            if (escaped or not candidate or target.startswith('/') or '\\' in target or ':' in target
                    or candidate not in names or len(candidate) > 4096):
                gaps.append({'path': path, 'line': number, 'reason': 'include_unresolved'}); continue
            nested, missing = _requirements(repo, revision, candidate, names, active, budget,
                                            'constraints' if include.group(1) == 'c' or include.group(3) == 'constraint' else group)
            rows.extend(nested); gaps.extend(missing)
            continue
        pin = _PIN.fullmatch(line)
        if pin:
            rows.append({'name': pin.group(1), 'group': group, 'requirement': line,
                         'origin': {'path': path, 'line': number, 'revision': revision, 'blob_sha': blob}})
        else:
            gaps.append({'path': path, 'line': number, 'reason': 'unsupported_requirement'})
    return rows, gaps


def declared(repo, revision, path):
    source(repo, revision, path)  # validates commit and canonical path first
    tree = subprocess.check_output(['git','ls-tree','-rz','--name-only',revision],cwd=repo,timeout=15).decode().split('\0')
    names = set(tree)
    dirs = [p.as_posix() for p in PurePosixPath(path).parents]
    files=[]
    for directory in dirs:
        for name in MANIFESTS:
            candidate = name if directory == '.' else directory+'/'+name
            if candidate not in names: continue
            try:
                blob,text=source(repo,revision,candidate,
                                 max_bytes=256_000 if name == 'requirements.txt' else None)
            except ValueError as exc:
                if str(exc) != 'source byte budget exceeded':
                    raise
                files.append({'path':candidate,'status':'manifest_budget_exceeded'})
                continue
            row={'path':candidate,'blob_sha':blob,'sha256':hashlib.sha256(text.encode()).hexdigest()}
            if len(text.encode()) > 8_000_000:
                row['status']='manifest_budget_exceeded';files.append(row);continue
            dependencies=[]
            try:
                if name == 'requirements.txt':
                    dependencies, gaps = _requirements(repo, revision, candidate, names, set(), [512_000, 64],
                                                       loaded=(blob, text))
                    row['unresolved'] = gaps[:100]
                    row['unresolved_omitted'] = max(0, len(gaps)-100)
                elif name == 'package.json':
                    data=json.loads(text)
                    for group in ('dependencies','devDependencies','peerDependencies','optionalDependencies'):
                        for key,value in data.get(group,{}).items():
                            value=str(value)
                            dependencies.append({'name':key,'group':group,
                                'requirement':value if re.fullmatch(r'[\w .<>=~^*|,+!-]+',value) else 'non-version-reference; see source',
                                'requirement_sha256':hashlib.sha256(value.encode()).hexdigest()})
                elif name == 'pyproject.toml':
                    data=tomllib.loads(text).get('project',{})
                    groups={'dependencies':data.get('dependencies',[]),**data.get('optional-dependencies',{})}
                    for group,values in groups.items():
                        for value in values:
                            dependencies.append({'group':group,'requirement':value if re.fullmatch(r'[\w .<>=~^*|,+!\[\]-]+',value) else 'complex requirement; see source',
                                'requirement_sha256':hashlib.sha256(value.encode()).hexdigest()})
                row.update(status='partial' if name == 'requirements.txt' and row['unresolved'] else 'identified',
                           declarations=dependencies[:100],omitted=max(0,len(dependencies)-100))
            except (ValueError,TypeError,AttributeError):
                row['status']='manifest_parse_failed'
            files.append(row)
    return {'source_revision':revision,'manifests':files,'coverage':'committed ancestor manifests only; lockfiles identified by digest, not resolved',
            'installed_equivalence':'not-established'}


def provider(python):
    script = '''import importlib.metadata as m,json,sys
names=('serena-agent','mcp','jedi','python-lsp-server','basedpyright')
rows=[]
for name in names:
 try: rows.append({'name':name,'version':m.version(name),'status':'installed'})
 except m.PackageNotFoundError: rows.append({'name':name,'status':'not-installed-in-this-interpreter'})
print(json.dumps({'python':sys.version.split()[0],'packages':rows}))'''
    result=subprocess.run([python,'-I','-c',script],capture_output=True,text=True,timeout=15)
    if result.returncode or len(result.stdout)>20_000:
        return {'status':'unavailable','reason':'provider_environment_probe_failed'}
    return dict(json.loads(result.stdout),status='observed',
        scope='configured Serena Python metadata; not separate language-server environments or product runtime')
