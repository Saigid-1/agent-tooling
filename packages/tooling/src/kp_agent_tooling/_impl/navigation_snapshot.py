"""Immutable local selections of operator-published navigation sources."""
import hashlib
import json
from pathlib import Path
import re

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.navigation_workspace import active, SHA

ID = re.compile(r'navigation-snapshot:sha256:([0-9a-f]{64})\Z')


def _registry(path):
    root = Path(path).resolve()
    if not root.is_dir() or Path(path).is_symlink():
        raise ValueError('server-owned snapshot registry must already exist')
    return root


def _write_once(path, data):
    return leaf.publish_once_checked(path, data, temp_prefix='.snapshot-', symlink_message='snapshot registry symlink refused',
                                     collision_message='snapshot registry identity collision')


def _read_regular(path, limit=131072):
    return leaf.read_regular_bytes(path, limit, not_regular='regular snapshot registry file required',
                                   too_large='snapshot registry byte budget exceeded')


def _provider_inputs(config, repos):
    inputs={}
    for key in ('typescript',):
        row=config.get(key)
        if row and row.get('module'):
            path=Path(row['module'])
            inputs['typescript.module']=hashlib.sha256(_read_regular(path,20_000_000)).hexdigest()
    for key,repo in repos.items():
        for name in ('.serena/project.yml','pyrightconfig.json','tsconfig.json'):
            path=Path(repo['path'])/name
            if path.exists():
                inputs[key+':'+name]=hashlib.sha256(_read_regular(path,1_000_000)).hexdigest()
    return inputs


def capture(config, registry):
    """Freeze the active profile and catalog; registry is trusted server configuration."""
    root = _registry(registry)
    profile = active(config)
    if not config.get('navigation_profile'):
        raise ValueError('published navigation profile required')
    raw = _read_regular(Path(config['navigation_profile']))
    if hashlib.sha256(raw).hexdigest() != profile['profile_sha256']:
        raise ValueError('profile changed during capture')
    model = json.loads(raw)
    catalog_data = None
    if model.get('knowledge_config'):
        catalog_data = _read_regular(Path(model['knowledge_config']))
        if hashlib.sha256(catalog_data).hexdigest() != model['knowledge_config_sha256']:
            raise ValueError('catalog changed during capture')
    # Paths in the frozen profile point only to files written by this registry.
    catalog_name = None
    if catalog_data is not None:
        catalog_name = 'catalog-' + hashlib.sha256(catalog_data).hexdigest() + '.json'
        _write_once(root/catalog_name, catalog_data)
        model['knowledge_config'] = str(root/catalog_name)
    model.pop('check_status', None)  # a mutable observation, never source identity
    frozen, frozen_sha256 = leaf.canonical_bytes_sha256(model, ascii=True, allow_nan=True)
    profile_name = 'profile-' + frozen_sha256 + '.json'
    _write_once(root/profile_name, frozen)
    selection = {'schema_version':'ops.navigation-snapshot.v1',
                 'source_profile_sha256':profile['profile_sha256'],
                 'profile_file':profile_name,'catalog_file':catalog_name,
                 'repos':{k:{'path':v['path'],'revision':v['revision']}
                          for k,v in sorted(profile['repos'].items())},
                 'analysis_config':{'serena':config.get('serena'),
                                    'scip':config.get('scip'),'typescript':config.get('typescript'),
                                    'import_mappings':config.get('import_mappings')},
                 'provider_inputs':_provider_inputs(config, profile['repos']),
                 'analysis_config_sha256':leaf.canonical_sha256({
                     'serena':config.get('serena'),'scip':config.get('scip'),
                     'typescript':config.get('typescript'),
                     'import_mappings':config.get('import_mappings')}, ascii=True, allow_nan=True)}
    payload, digest = leaf.canonical_bytes_sha256(selection, ascii=True, allow_nan=True)
    _write_once(root/('selection-'+digest+'.json'), payload)
    return {'snapshot_id':'navigation-snapshot:sha256:'+digest, **selection}


def select(config, registry, snapshot_id):
    """Return a server-derived config and frozen source identity for one review."""
    match = ID.fullmatch(snapshot_id) if isinstance(snapshot_id,str) else None
    if not match:
        raise ValueError('exact snapshot id required')
    root = _registry(registry)
    try:
        raw = _read_regular(root/('selection-'+match.group(1)+'.json'))
    except FileNotFoundError as error:
        raise ValueError('unknown snapshot id') from error
    if len(raw)>131072 or hashlib.sha256(raw).hexdigest()!=match.group(1):
        raise ValueError('snapshot identity mismatch')
    selection=json.loads(raw)
    if selection.get('schema_version')!='ops.navigation-snapshot.v1' or set(selection['repos'])!=set(config['repos']):
        raise ValueError('snapshot membership mismatch')
    profile_name=selection['profile_file']
    if not re.fullmatch(r'profile-[0-9a-f]{64}\.json',profile_name):
        raise ValueError('invalid frozen profile reference')
    profile_path=root/profile_name
    if hashlib.sha256(_read_regular(profile_path)).hexdigest()!=profile_name[8:-5]:
        raise ValueError('frozen profile identity mismatch')
    frozen=json.loads(_read_regular(profile_path))
    if set(frozen['repos']) != set(selection['repos']) or any(
        frozen['repos'][k]['path'] != v['path'] or frozen['repos'][k]['revision'] != v['revision']
        for k,v in selection['repos'].items()
    ):
        raise ValueError('snapshot source mismatch')
    catalog_name=selection['catalog_file']
    if catalog_name:
        if not re.fullmatch(r'catalog-[0-9a-f]{64}\.json',catalog_name):
            raise ValueError('invalid frozen catalog reference')
        if hashlib.sha256(_read_regular(root/catalog_name)).hexdigest()!=catalog_name[8:-5]:
            raise ValueError('frozen catalog identity mismatch')
    if _provider_inputs(selection['analysis_config'], selection['repos']) != selection['provider_inputs']:
        raise ValueError('semantic provider inputs changed since snapshot capture')
    derived=dict(config)
    derived['navigation_profile']=str(profile_path)
    derived['local_knowledge_config']=str(root/catalog_name) if catalog_name else None
    derived['repos']=selection['repos']
    # Legacy snapshots predate some analysis inputs. Missing means absent at
    # capture time; never inherit a newer host setting outside snapshot identity.
    for key in ('serena','scip','typescript','import_mappings'):
        derived.pop(key,None)
    for key,value in selection['analysis_config'].items():
        if value is not None:derived[key]=value
    verified=active(derived)
    if any(verified['repos'][k]['revision']!=v['revision'] for k,v in selection['repos'].items()):
        raise ValueError('snapshot revision mismatch')
    return derived, {'snapshot_id':snapshot_id,'repos':selection['repos'],
                     'source_profile_sha256':selection['source_profile_sha256'],
                     'analysis_config_sha256':selection['analysis_config_sha256']}


def target_revision(selection, repo_key, requested_revision=None):
    """Resolve a semantic operation's target without consulting the moving default."""
    revision=selection['repos'][repo_key]['revision']
    if requested_revision is not None and requested_revision != revision:
        raise ValueError('requested revision differs from review snapshot')
    if not SHA.fullmatch(revision):
        raise ValueError('invalid frozen revision')
    return revision
