"""Resolve declared platform sources without conflating checkout and deployment.

Repository locations are operator configuration. Identity excludes machine paths
and checkout dirt; committed artifacts are read through the existing citation seam.
"""
import hashlib
from pathlib import Path
import re
import subprocess

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.source_citations import source
from kp_agent_tooling._impl.installed_dependencies import installed_observations


def _exact(value, fields):
    if not isinstance(value, dict) or set(value)!=set(fields):
        raise ValueError('explicit platform manifest fields required: '+', '.join(sorted(fields)))


def _text(value):
    if not isinstance(value,str) or not value.strip() or len(value)>256:
        raise ValueError('nonempty bounded coordinate required')


def _validate(model):
    required = {'schema','owner','profile','sources'}
    if not isinstance(model, dict) or set(model) - required - {'distributions'} or not required <= set(model):
        raise ValueError('unexpected platform manifest fields')
    if 'distributions' not in model:
        _exact(model, required)
    if model['schema']!='ops.platform-request.v1':raise ValueError('unsupported platform schema')
    _text(model['owner']);_text(model['profile'])
    if not isinstance(model['sources'],dict) or not 1<=len(model['sources'])<=32:
        raise ValueError('1..32 explicit sources required')
    for key,row in model['sources'].items():
        _text(key);_exact(row,{'revision','artifacts'})
        if not isinstance(row['revision'],str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',row['revision']):
            raise ValueError('full source commit required')
        if not isinstance(row['artifacts'],list) or not 1<=len(row['artifacts'])<=32:
            raise ValueError('1..32 declared artifacts per source required')
        paths=set()
        for artifact in row['artifacts']:
            _exact(artifact,{'path','role'});path=artifact['path']
            if (not isinstance(path,str) or not path or len(path)>4096 or '\\' in path
                    or any(p in ('','.','..') for p in path.split('/')) or path in paths):
                raise ValueError('unique canonical artifact paths required')
            paths.add(path)
            if artifact['role'] not in {'dependency','configuration','registration','build_definition'}:
                raise ValueError('unsupported artifact role')
    if 'distributions' in model:
        distributions = model['distributions']
        if not isinstance(distributions, dict) or len(distributions) > 32:
            raise ValueError('distribution map must be bounded')
        for name, repo_key in distributions.items():
            _text(name); _text(repo_key)
            if repo_key not in model['sources']:
                raise ValueError('distribution repository must be configured')


def _git(path,*args):
    return subprocess.check_output(['git',*args],cwd=path,stderr=subprocess.PIPE,timeout=15)


def inspect_platform(model, checkouts, installed_provider=None):
    _validate(model)
    resolved={};observations={};gaps=[];drift=False
    for key,row in sorted(model['sources'].items()):
        try:
            path=Path(checkouts[key]).resolve(strict=True)
            if _git(path,'rev-parse',row['revision']+'^{commit}').decode().strip()!=row['revision']:
                raise ValueError('commit required')
            artifacts=[]
            for artifact in sorted(row['artifacts'],key=lambda a:a['path']):
                blob,text=source(path,row['revision'],artifact['path'])
                artifacts.append({**artifact,'blob_sha':blob,
                    'sha256':hashlib.sha256(text.encode()).hexdigest()})
            head=_git(path,'rev-parse','HEAD').decode().strip()
            dirty=bool(_git(path,'status','--porcelain','--untracked-files=normal'))
            observations[key]=dict(status='matched' if head==row['revision'] else 'revision_mismatch',
                path=str(path),head=head,dirty=dirty)
            drift |= dirty or head!=row['revision']
            resolved[key]=dict(revision=row['revision'],artifacts=artifacts)
        except (KeyError,ValueError,OSError,UnicodeError,subprocess.SubprocessError):
            observations[key]=dict(status='unavailable')
            gaps.append(dict(repo_key=key,reason='source_or_artifact_unavailable'))
    snapshot=dict(schema='ops.platform-snapshot.v1',owner=model['owner'],profile=model['profile'],sources=resolved)
    identity=None
    if not gaps:
        identity=leaf.content_id('platform-snapshot', snapshot, ascii=True, allow_nan=True)
    installed = installed_observations(model, installed_provider)
    return dict(schema='ops.platform-inspection.v1',status='partial' if gaps else 'review_required' if drift else 'ok',
        snapshot_id=identity,snapshot=snapshot,checkouts=observations,gaps=gaps,
        installed=installed,
        stages=dict(proposed='declared',source='partial' if gaps else 'resolved',checked_out='partial' if gaps else 'diverged' if drift else 'matched',
                    built='not-assessed',deployed='not-assessed',observed='not-assessed'),
        limitations=['Selected committed artifacts only; profile label is not verified runtime configuration.',
                     'Checkout HEAD does not establish installed dependency, build, deployment or execution identity.',
                     'Endpoint reads are not an atomic multi-repository snapshot; no persistent graph writes.'])
