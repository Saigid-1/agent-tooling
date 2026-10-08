"""Operator-owned local desk registration and memory composition; no dispatch.

The file owner is the authority in this single-owner deployment. This is not a
multi-tenant authentication boundary against processes running as that owner.
"""
from __future__ import annotations
import json
import os
from contextlib import closing
from pathlib import Path
from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.desk_identity import BindingRecord, binding_key
from kp_agent_tooling._impl.service.desk_binding import DeskSessionLedger, DeskLaunchReceipt, call_memo, call_scope
from kp_agent_tooling._impl.service.episodic_memory import EpisodeStore
from kp_agent_tooling._impl.service.desk_registry import DESCRIPTOR_SCHEMA, load_descriptor, registry_desks


# A private operator JSON file; read at most once per memory call.
def private_json(path):
    return leaf.read_private_json(path, memo=call_memo())


def _per_call(authority, name, read):
    """Read an authority file once per memory call; outside a call, read every time.

    Only the current call sees the value, so a withdrawn or edited catalog
    changes the next call.
    """
    memo = call_memo()
    if memo is None:
        return read()
    key = ('authority', name, id(authority))
    if key not in memo:
        memo[key] = read()
    return memo[key]


class WorkspaceDeskAuthority:
    """Approved desks from the recently verified workspace context builder."""
    def __init__(self, path, workspace_root):
        self.path, self.workspace_root = Path(path), Path(workspace_root)

    def catalog(self):
        from kp_agent_tooling._impl.service.workspace_context import load_workspace_catalog
        def read():
            private_json(self.path)
            return load_workspace_catalog(self.path)
        return _per_call(self, 'workspace-catalog', read)

    def list_bindings(self):
        catalog = self.catalog()
        return tuple(BindingRecord(d.binding_id, catalog.project.tenant_id,
            d.role_id, catalog.project.repo_key, None, d.label, catalog.team_approval_ref)
            for d in catalog.desks)

    def resolve(self, *, tenant_id, role, repo_key):
        key = binding_key(tenant_id=tenant_id, role=role, repo_key=repo_key)
        for record in self.list_bindings():
            if record.binding_key == key:
                return record
        from kp_agent_tooling._impl.service.desk_binding import DeskLaunchUnavailable
        raise DeskLaunchUnavailable('desk is not in the approved context catalog')

    def context(self, desk_id):
        from kp_agent_tooling._impl.service.workspace_context import build_launch_context
        return build_launch_context(self.catalog(), desk_id=desk_id,
            workspace_root=self.workspace_root, memory_query='Session desk context',
            memory_resolver=None)


class ImportedDeskAuthority:
    """Explicit operator catalog retaining original multi-repository coordinates.

    Catalog approval grants memory admission only, never a role charter or board
    privileges. No live graph dependency and no role normalization.
    """
    def __init__(self, path):
        self.path = Path(path)

    def _catalog(self):
        return _per_call(self, 'imported-catalog', self._read_catalog)

    def _read_catalog(self):
        catalog = private_json(self.path)
        if (set(catalog) != {'schema_version', 'approval_ref', 'bindings'} or
                catalog['schema_version'] != 'ops.imported-desk-catalog.v1' or
                not isinstance(catalog['approval_ref'], str) or not catalog['approval_ref'] or
                not isinstance(catalog['bindings'], list) or not 1 <= len(catalog['bindings']) <= 1000):
            raise ValueError('invalid imported desk catalog')
        seen = set()
        for row in catalog['bindings']:
            if set(row) != {'binding_key','tenant_id','role','repo_key','desk_label','source','memory_write_allowed'}:
                raise ValueError('invalid imported binding fields')
            if any(not isinstance(row[k],str) or not row[k] or len(row[k])>512
                   for k in ('tenant_id','role','repo_key','desk_label','source')):
                raise ValueError('bounded original binding coordinates required')
            expected = binding_key(tenant_id=row['tenant_id'],role=row['role'],repo_key=row['repo_key'])
            if row['binding_key'] != expected or expected in seen or type(row['memory_write_allowed']) is not bool:
                raise ValueError('conflicting imported binding or write authority')
            seen.add(expected)
        return catalog

    def list_bindings(self):
        return tuple(BindingRecord(row['binding_key'],row['tenant_id'],row['role'],
            row['repo_key'],None,row['desk_label'],row['source']) for row in self._catalog()['bindings'])

    def resolve(self, *, tenant_id, role, repo_key):
        key = binding_key(tenant_id=tenant_id,role=role,repo_key=repo_key)
        for row in self.list_bindings():
            if row.binding_key == key: return row
        from kp_agent_tooling._impl.service.desk_binding import DeskLaunchUnavailable
        raise DeskLaunchUnavailable('desk is not in the operator catalog')

    def allows_memory_write(self, key):
        return any(row['binding_key']==key and row['memory_write_allowed']
                   for row in self._catalog()['bindings'])

    def context(self, desk_id):
        import hashlib
        catalog = self._catalog()
        row = next((row for row in catalog['bindings'] if row['binding_key']==desk_id), None)
        if row is None: raise ValueError('select an exact registered binding key')
        body = {'schema_version':'ops.imported-desk-admission.v1',
            'project':{'tenant_id':row['tenant_id'],'repo_key':row['repo_key']},
            'desk':{'binding_id':row['binding_key'],'role_id':row['role'],'desk_id':row['binding_key']},
            'approval_ref':catalog['approval_ref'],'binding_source':row['source'],
            'memory_write_allowed':row['memory_write_allowed'],
            'authority':'operator memory admission only; no role privileges or doctrine inferred'}
        body['context_digest']=leaf.canonical_sha256(body, ascii=True, allow_nan=True)
        return body


class RegistryDeskAuthority:
    """Portable desk registry (``agent-tooling.desk-registry.v1``).

    Desks are data in this state root's memory store and each has exactly one
    binding. No doctrine, approval or reviewer is required. The authority never
    admits a session: only an operator or launcher bind does.
    """
    kind = DESCRIPTOR_SCHEMA

    def __init__(self, path, state_root):
        self.path = Path(path)
        descriptor = load_descriptor(self.path)
        self.tenant_id = descriptor['tenant_id']
        self.roster_path = leaf.mark_store(descriptor['roster_path'])
        self.store_path = leaf.store_file(state_root, leaf.EPISODES_DB)

    def desks(self):
        # Re-read the descriptor once per call so withdrawing or retargeting it is live.
        def read():
            if load_descriptor(self.path)['tenant_id'] != self.tenant_id:
                raise ValueError('registry tenant changed; reopen the configuration')
            return registry_desks(self.store_path, self.tenant_id)
        return _per_call(self, 'registry-desks', read)

    @staticmethod
    def _record(profile, binding):
        return BindingRecord(binding['binding_key'], binding['tenant_id'], binding['role'],
            binding['repo_key'], None, profile['name'], binding['source'])

    def list_bindings(self):
        return tuple(self._record(p, b) for p, b in self.desks())

    def resolve(self, *, tenant_id, role, repo_key):
        key = binding_key(tenant_id=tenant_id, role=role, repo_key=repo_key)
        for profile, binding in self.desks():
            if binding['binding_key'] == key:
                return self._record(profile, binding)
        from kp_agent_tooling._impl.service.desk_binding import DeskLaunchUnavailable
        raise DeskLaunchUnavailable('desk is not in the desk registry')

    def _flag(self, key, name):
        return any(b['binding_key'] == key and p.get(name) is True for p, b in self.desks())

    def allows_memory_write(self, key):
        return self._flag(key, 'memory_write')

    def allows_capture(self, key):
        return self._flag(key, 'capture')

    def context(self, desk_id):
        import hashlib
        match = next(((p, b) for p, b in self.desks() if desk_id in (p['desk_id'], b['binding_key'])), None)
        if match is None: raise ValueError('select a desk registered in this registry')
        profile, binding = match
        body = {'schema_version':'agent-tooling.desk-admission.v1',
            'project':{'tenant_id':binding['tenant_id'],'repo_key':binding['repo_key']},
            'desk':{'binding_id':binding['binding_key'],'role_id':binding['role'],'desk_id':profile['desk_id'],
                    'name':profile['name'],'role':profile['role'],'repos':profile.get('repos', [])},
            'binding_source':binding['source'],
            'memory_write_allowed':profile.get('memory_write') is True,
            'capture':profile.get('capture') is True,
            'context_doc':profile.get('context_doc'),
            'authority':'operator desk registry admission; memory only, no doctrine, approval or role privilege inferred'}
        body['context_digest']=leaf.canonical_sha256(body, ascii=True, allow_nan=True)
        return body


def components(path):
    config = private_json(path)
    base = {'schema_version', 'state_root', 'catalog_path', 'workspace_root', 'provider_instance', 'provider_session_id'}
    assistant = config.get('schema_version') == 'ops.assistant-memory.local.v1'
    if set(config) != (base | {'assistant_policy_path'} if assistant else base) or config['schema_version'] not in {'ops.desk-memory.local.v1', 'ops.assistant-memory.local.v1'}:
        raise ValueError('invalid local desk memory configuration')
    root = leaf.mark_store(config['state_root'])
    if not root.is_absolute() or root.is_symlink() or not root.is_dir() or leaf.shared_bits(root.stat().st_mode):
        raise ValueError('existing private absolute state directory required (0700); no automatic fallback')
    if root.stat().st_uid != os.getuid():
        raise ValueError('state directory must belong to running user')
    catalog_schema = private_json(config['catalog_path']).get('schema_version')
    registry = (ImportedDeskAuthority(config['catalog_path'])
                if catalog_schema == 'ops.imported-desk-catalog.v1' else
                RegistryDeskAuthority(config['catalog_path'], root)
                if catalog_schema == DESCRIPTOR_SCHEMA else
                WorkspaceDeskAuthority(config['catalog_path'], config['workspace_root']))
    registry.list_bindings()
    marker = root / 'assistant-owner.json'
    if assistant:
        from kp_agent_tooling._impl.service.assistant_memory_policy import load_policy
        load_policy(config['assistant_policy_path'])
        bindings = registry.list_bindings()
        if not isinstance(registry, ImportedDeskAuthority) or len(bindings) != 1 or bindings[0].role != 'assistant':
            raise ValueError('assistant memory requires one isolated imported assistant binding')
        if Path(config['assistant_policy_path']).resolve().is_relative_to(root.resolve()):
            raise ValueError('assistant policy must remain outside mutable memory state')
        expected = {'schema_version':'ops.assistant-state-owner.v1',
                    'binding_key':bindings[0].binding_key}
        if marker.exists():
            if private_json(marker) != expected:
                raise ValueError('assistant state owner does not match selected binding')
        elif any(root.iterdir()):
            raise ValueError('assistant state root contains unowned data; use a new empty root')
    elif marker.exists() or marker.is_symlink():
        raise ValueError('assistant state cannot be opened by a desk configuration')
    ledger = DeskSessionLedger(leaf.store_file(root, leaf.SESSIONS_DB), provider_instance=config['provider_instance'])
    store = EpisodeStore(leaf.store_file(root, leaf.EPISODES_DB), session_ledger=ledger, registry=registry)
    return config, registry, ledger, store


def initialize(path):
    config, registry, ledger, store = components(path)
    if ledger.path.exists() or store.path.exists():
        raise ValueError('state already exists; partial state requires operator review, never reset')
    ledger.initialize()
    store.initialize()
    with closing(leaf.sqlite_connect(ledger.path, mode='rw', resolve=True)) as db, db:
        db.execute("CREATE TABLE desk_contexts (instance TEXT, session TEXT, context TEXT NOT NULL, PRIMARY KEY(instance,session))")
    if config['schema_version'] == 'ops.assistant-memory.local.v1':
        marker = leaf.store_file(config['state_root'], 'assistant-owner.json')
        value = {'schema_version':'ops.assistant-state-owner.v1',
                 'binding_key':registry.list_bindings()[0].binding_key}
        leaf.write_new_json(marker, value)
    return {'status':'initialized', 'dispatch':False}


def admit(path, *, desk_id, provider_id, model_id):
    config, registry, ledger, store = components(path)
    context = registry.context(desk_id)
    binding = registry.resolve(tenant_id=context['project']['tenant_id'],
        role=context['desk']['role_id'], repo_key=context['project']['repo_key'])
    # Admission authorizes memory only. It never creates a provider session or worktree.
    receipt = DeskLaunchReceipt(config['provider_session_id'], config['provider_instance'],
        binding.binding_key, binding.tenant_id, binding.role, binding.repo_key, provider_id, model_id)
    ledger.admit(receipt)
    with closing(leaf.sqlite_connect(ledger.path, mode='rw', resolve=True)) as db, db:
        db.execute("INSERT OR IGNORE INTO desk_contexts VALUES (?,?,?)", (ledger.provider_instance, config['provider_session_id'], json.dumps(context)))
    return {'status':'admitted', 'binding_key':binding.binding_key, 'dispatch':False,
            'context': read_context(path)}


def persist_host_selection(path, *, desk_id, provider_id, model_id, selection_path):
    """Seal the operator's admitted choice for this exact host session.

    The selection is host-owned recovery input, never a model tool or a memory
    claim. A different choice cannot replace an existing selection.
    """
    config, registry, ledger, _ = components(path)
    target = Path(selection_path)
    parent = target.parent
    if (not target.is_absolute() or target.is_symlink() or
            target.name != config['provider_session_id'] + '.selection.json' or
            not parent.is_dir() or parent.is_symlink() or parent.stat().st_uid != os.getuid() or
            leaf.shared_bits(parent.stat().st_mode)):
        raise ValueError('selection needs an existing private host-owned session directory')
    context = registry.context(desk_id)
    binding = registry.resolve(tenant_id=context['project']['tenant_id'],
        role=context['desk']['role_id'], repo_key=context['project']['repo_key'])
    selection = {'schema_version':'ops.desk-host-selection.v1', 'config':config,
        'desk_id':desk_id, 'provider_id':provider_id, 'model_id':model_id,
        'binding_key':binding.binding_key, 'tenant_id':binding.tenant_id,
        'role':binding.role, 'repo_key':binding.repo_key}
    data = leaf.canonical_bytes(selection, ascii=True, allow_nan=True)
    if target.exists():
        if private_json(target) != selection:
            raise ValueError('host selection already exists with different coordinates')
    receipt = admit(path, desk_id=desk_id, provider_id=provider_id, model_id=model_id)
    admitted = ledger.resolve(config['provider_session_id'], registry)
    context = receipt['context']
    if (context['desk']['binding_id'] != admitted.binding_key or
            context['project']['tenant_id'] != admitted.tenant_id or
            context['project']['repo_key'] != admitted.repo_key or
            context['desk']['role_id'] != admitted.role):
        raise ValueError('admitted context does not match the desk selection')
    if target.exists():
        if private_json(target) != selection:
            raise ValueError('host selection already exists with different coordinates')
        return {'status':'selection_recorded', 'binding_key':admitted.binding_key,
                'provider_session_id':admitted.provider_session_id, 'idempotent':True}
    # Never overwrite a selection created by a concurrent host invocation.
    leaf.link_private_once(target, data, temp_prefix='.selection-', read=private_json, expected=selection,
                           conflict=lambda: ValueError('host selection already exists with different coordinates'))
    return {'status':'selection_recorded', 'binding_key':admitted.binding_key,
            'provider_session_id':admitted.provider_session_id, 'idempotent':False}


def from_local_config(path):
    from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools
    # One construction resolves the configuration, catalog and admission once.
    with call_scope():
        config, registry, ledger, store = components(path)
        _read_context(config, registry, ledger)
        return EpisodicMemoryTools(store, config['provider_session_id'])


def read_context(path):
    with call_scope():
        config, registry, ledger, _ = components(path)
        return _read_context(config, registry, ledger)


def _read_context(config, registry, ledger):
    import hashlib
    admitted = ledger.resolve(config['provider_session_id'], registry)
    with closing(leaf.sqlite_connect(ledger.path, mode='ro', resolve=True)) as db:
        row = db.execute('SELECT context FROM desk_contexts WHERE instance=? AND session=?',
            (ledger.provider_instance, config['provider_session_id'])).fetchone()
    if not row: raise ValueError('session context unavailable; operator admission required')
    context = json.loads(row[0]); digest = context.pop('context_digest')
    actual = leaf.canonical_sha256(context, ascii=True, allow_nan=True)
    if actual != digest or context['desk']['binding_id'] != admitted.binding_key:
        raise ValueError('session context integrity mismatch')
    return {**context, 'context_digest':digest}
