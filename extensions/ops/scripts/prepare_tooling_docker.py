#!/usr/bin/env python3
"""One-time migration of a configured local deployment to physical container paths.

Copies a generation and standalone Git repositories; never rewrites source evidence
or existing memory. Run on the host, then use Docker for normal operation.

The coordination repository (the one whose navigation scope is the legacy kp_ops tree,
pinned to an integration branch) is named by the operator at run time, never by this
tree: set PREPARE_TOOLING_DOCKER_COORDINATION_REPO to its repository key in the
navigation profile, and PREPARE_TOOLING_DOCKER_COORDINATION_REF to the branch ref to
pin. Without them the script uses the example key `example-repo` (as in
config/desk-context/catalog.example.json) and the placeholder ref
`refs/heads/example-branch`. Legacy launcher code: a one-time migration, with no other
use in this repository.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib
import yaml

COORDINATION_REPO_ENV = 'PREPARE_TOOLING_DOCKER_COORDINATION_REPO'
COORDINATION_REF_ENV = 'PREPARE_TOOLING_DOCKER_COORDINATION_REF'
EXAMPLE_REPO = 'example-repo'
EXAMPLE_REF = 'refs/heads/example-branch'


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')
    path.chmod(0o600)


def git(*args):
    return subprocess.check_output(['git', *map(str,args)], stderr=subprocess.PIPE).decode().strip()


def physical(path):
    p = Path(path).expanduser()
    if not p.exists():
        raise ValueError('Required input is absent: ' + str(p))
    return p.resolve(strict=True)


def clone(source, target, revision):
    # --no-local transfers objects, including alternates, without preserving their paths.
    git('clone', '--quiet', '--no-local', '--no-checkout', source, target)
    git('-C', target, 'fetch', '--quiet', source, revision)
    git('-C', target, 'checkout', '--quiet', '--detach', revision)
    if (target/'.git/objects/info/alternates').exists():
        raise ValueError('Export unexpectedly retains object alternates')
    if git('-C',target,'rev-parse','HEAD') != revision:
        raise ValueError('Source revision mismatch')


def prepare(config_path, destination):
    root = Path(destination)
    if not root.is_absolute() or root.exists() or root.parent.resolve() != root.parent:
        raise ValueError('Use a new absolute directory under a physical mounted parent')
    # Explicit host storage requirement; no mkdir fallback under /Volumes.
    if str(root).startswith('/Volumes/'):
        volume = Path(*root.parts[:3])
        if not os.path.ismount(volume):
            raise ValueError('Required external volume is not mounted')
    original = json.loads(physical(config_path).read_text())
    profile = json.loads(physical(original['navigation_profile']).read_text())
    catalog = json.loads(physical(profile['knowledge_config']).read_text())
    os.umask(0o077)
    root.mkdir(mode=0o700)
    state = root/'state'; state.mkdir(mode=0o700)
    (root/'config').mkdir(mode=0o700)
    (state/'.ops-tooling-volume').write_text('ops-tooling-state-v1\n')
    for directory in ('tmp','snapshots','search','memory','navigation','models','repositories','retained','templates'):
        (state/directory).mkdir(mode=0o700)
    generation = state/'navigation/initial'; generation.mkdir()
    request = dict(repositories={}, publication='/state/navigation/current.json',
                   check_status='/state/navigation/refresh-status.json',
                   output_root='/state/navigation/generations', toolchain='/opt/toolchain',
                   node='/usr/local/bin/node', semantic=True)
    coordination_repo = os.environ.get(COORDINATION_REPO_ENV, EXAMPLE_REPO)
    coordination_ref = os.environ.get(COORDINATION_REF_ENV, EXAMPLE_REF)
    scopes = {'ats':['product','deploy'], 'core':['kp_core'], coordination_repo:['kp_ops','scripts','portable_tooling']}
    for key,row in profile['repos'].items():
        source = physical(row['path']); target = generation/key
        clone(source,target,row['revision'])
        # Provider configuration is operator data, not a claimed committed artifact.
        (target/'.serena').mkdir(exist_ok=True)
        template=source/'.serena/project.yml'
        provider = yaml.safe_load(template.read_text())
        if provider.get('read_only') is not True:
            raise ValueError('Read-only provider configuration required')
        if key == 'ats' and 'typescript' not in provider.get('language_servers', []):
            provider.setdefault('language_servers', []).append('typescript')
        provider_text = json.dumps(provider, indent=2) + '\n'
        (target/'.serena/project.yml').write_text(provider_text)
        (state/'templates'/f'{key}.yml').write_text(provider_text)
        (target/'.serena/.gitignore').write_text('/cache\n/project.local.yml\n')
        row['path']='/state/navigation/initial/'+key
        catalog['repositories'][key]['path']=row['path']
        seed = state/'repositories'/key
        clone(source,seed,row['revision'])
        url=git('-C',source,'remote','get-url','origin')
        if not url.startswith('https://github.com/') or '@' in url:
            raise ValueError('Expected credential-free GitHub remote')
        git('-C',seed,'remote','set-url','origin',url)
        spec=dict(repository='/state/repositories/'+key,python_scope=scopes[key],serena_template='/state/templates/'+key+'.yml')
        if key=='ats':spec['typescript_prefix']='studio'
        if key==coordination_repo:spec['ref']=coordination_ref
        request['repositories'][key]=spec
    for entries in catalog['scip_indexes'].values():
        for entry in entries:
            source=physical(entry['path']);shutil.copy2(source,generation/source.name)
            entry['path']='/state/navigation/initial/'+source.name
    # Semantic envelopes keep their bytes/hashes; only config locations change.
    for key,entry in profile.get('semantic_indexes',{}).items():
        source=physical(entry['dir'])
        for file in source.glob('*semantic*'):
            if file.is_file():shutil.copy2(file,generation/file.name)
        entry['dir']='/state/navigation/initial'
    write(generation/'knowledge.json',catalog)
    profile['knowledge_config']='/state/navigation/initial/knowledge.json'
    profile['knowledge_config_sha256']=hashlib.sha256((generation/'knowledge.json').read_bytes()).hexdigest()
    profile['check_status']='/state/navigation/refresh-status.json'
    profile['workspace_repositories']={}
    profile['limitations'].append('Imported generation; remote freshness requires the container refresh receipt. Host in-flight workspaces are not mounted by this migration.')
    write(state/'navigation/current.json',profile)
    write(state/'navigation/refresh-status.json',{'status':'imported_not_refreshed','source':'one-time physical export'})
    config=dict(original)
    config['navigation_profile']='/state/navigation/current.json'
    config['local_knowledge_config']=profile['knowledge_config']
    config['repos']={k:{'path':r['path'],'revision':r['revision']} for k,r in profile['repos'].items()}
    for key,row in config.get('retained_evidence_repos',{}).items():
        clone(physical(row['path']),state/'retained'/key,row['revision'])
        row['path']='/state/retained/'+key
    if config.get('evidence_repo'):
        source=physical(config['evidence_repo'])
        clone(source,state/'evidence',git('-C',source,'rev-parse','HEAD'))
        config['evidence_repo']='/state/evidence'
        if config.get('observation_registry'):
            relative=physical(config['observation_registry']).relative_to(source)
            # Registry may contain local observations outside the commit.
            shutil.copytree(source/relative,state/'evidence'/relative,dirs_exist_ok=True)
            config['observation_registry']='/state/evidence/'+str(relative)
    config['serena']={'command':'/opt/serena/bin/serena','python':'/opt/serena/bin/python','runtime_home':'/state'}
    config['typescript']['node']='/usr/local/bin/node'
    config['typescript']['module']='/opt/toolchain/node_modules/typescript/lib/typescript.js'
    config['snapshot_registry']='/state/snapshots';config['navigation_registry_path']='/state/search'
    if config.get('gateway_config'):
        gateway=tomllib.loads(physical(config.pop('gateway_config')).read_text())['mcp_servers']['ops-gateway']
        endpoint=gateway['url'].replace('://127.0.0.1:', '://host.docker.internal:').replace('://localhost:', '://host.docker.internal:')
        config['gateway_endpoint']=endpoint
        if gateway.get('http_headers'):
            write(root/'config/gateway-headers.json',gateway['http_headers'])
            config['gateway_headers_file']='/config/gateway-headers.json'
    write(root/'config/navigation.json',config)
    write(root/'config/refresh.json',request)
    write(root/'import-receipt.json',{'schema':'ops.docker-import.v1','status':'prepared_not_validated',
        'source_config_sha256':hashlib.sha256(physical(config_path).read_bytes()).hexdigest(),
        'navigation_revisions':{k:r['revision'] for k,r in profile['repos'].items()},
        'omissions':['Existing memory stores and admissions require a separate explicit migration.',
                     'Existing snapshot/search handles are not copied; request new handles.',
                     'Host in-flight workspace observations require explicit read-only mounts.']})
    print(json.dumps({'status':'prepared','root':str(root)}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True);p.add_argument('--destination',required=True)
    args=p.parse_args();prepare(args.config,args.destination)
