"""Local operator registry API; one bounded stdin request, no model calls.

roles, save-role, bind and import-catalog need an agent-tooling.desk-registry.v1
catalog. bind is an operator or launcher act; no MCP tool reaches it.
A malformed request is refused as a ValueError naming the offending field.
"""
import argparse
import json
import sys
import uuid
from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.episodic_memory_tools import from_config
from kp_agent_tooling._impl.service.desk_profiles import DeskProfiles

ACTIONS = ['initialize', 'list', 'save', 'annotate', 'search', 'roles', 'save-role', 'bind', 'import-catalog']
_REGISTRY_ONLY = {'save-role', 'bind', 'import-catalog'}


def _request():
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536: raise ValueError('request exceeds 64 KiB')
    try: return json.loads(raw)
    except (ValueError, RecursionError): raise ValueError('request must be one JSON object on stdin (invalid JSON)') from None


def _exact(kind, request, required, optional=()):
    if not isinstance(request, dict): raise ValueError(f'{kind} request must be a JSON object')
    missing = sorted(set(required) - set(request)); unexpected = sorted(set(request) - set(required) - set(optional))
    if missing or unexpected:
        parts = (['missing ' + ', '.join(missing)] if missing else []) + (['unexpected ' + ', '.join(unexpected)] if unexpected else [])
        raise ValueError(f'exact {kind} fields required: ' + '; '.join(parts))


def _expected_version(value):
    if type(value) is not int or value < 0: raise ValueError('expected_version must be a nonnegative integer')


def _field_errors(action, data):
    """Name the field of a malformed save-role or bind request before the service refuses it
    with a message that does not; never refuses a request the service would accept."""
    if action == 'save-role':
        _exact('role', data, ('role_id', 'label', 'purpose', 'expected_version'))
        _expected_version(data['expected_version'])
    elif action == 'bind':
        from kp_agent_tooling._impl.service.session_bindings import FIELDS, SCHEMA, _OPTIONAL
        _exact('session binding', data, FIELDS, _OPTIONAL)
        if 'schema_version' in data and data['schema_version'] != SCHEMA:
            raise ValueError(f'schema_version must be {SCHEMA}')
        desk_id = data['desk_id']
        try: canonical = isinstance(desk_id, str) and desk_id == 'desk:' + str(uuid.UUID(desk_id.removeprefix('desk:')))
        except ValueError: canonical = False
        if not canonical: raise ValueError('desk_id must be a canonical desk:<uuid>')
        if data['parent_session_id'] is not None and data['parent_session_id'] == data['native_session_id']:
            raise ValueError('parent_session_id must differ from native_session_id (a session cannot be its own parent)')
    return data


def _legacy(action, config):
    """Unchanged path: an admitted operator session selects the tenant."""
    adapter = from_config(config)
    try:
        registry = DeskProfiles(adapter.store, adapter.session)
        if action == 'initialize': return registry.initialize()
        if action == 'list': return registry.directory()
        if action == 'roles': return registry.roster()
        data = _request()
        if action == 'save': return registry.save(data)
        if action == 'annotate': return registry.annotate(data)
        return adapter.call('memory.search', data)
    finally:
        adapter.close()


def _registry(action, config_path, catalog, config, ledger, store):
    from kp_agent_tooling._impl.service.desk_memory_runtime import initialize
    from kp_agent_tooling._impl.service.desk_registry import RoleRoster, import_catalog
    from kp_agent_tooling._impl.service.session_bindings import bind
    if action == 'initialize':
        created = not (ledger.path.exists() and store.path.exists())
        if created: initialize(config_path)  # Refuses partial state; never resets.
        result = DeskProfiles(store, config['provider_session_id']).initialize()
        return {**result, 'state_initialized': created}
    if action == 'search':  # Unchanged: search runs as the admitted configured session.
        data = _request()
        adapter = from_config(config_path)
        try: return adapter.call('memory.search', data)
        finally: adapter.close()
    if action == 'bind': return bind(config_path, _field_errors('bind', _request()))
    registry = DeskProfiles(store, config['provider_session_id'])
    if action == 'list': return registry.directory()
    if action == 'roles': return registry.roster()
    if action == 'import-catalog': return import_catalog(registry, catalog)
    data = _request()
    if action == 'save-role': return RoleRoster(registry.registry.roster_path).save_role(_field_errors('save-role', data))
    if action == 'save': return registry.save(data)
    return registry.annotate(data)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',required=True)
    p.add_argument('action',choices=ACTIONS)
    p.add_argument('--catalog',help='ops.imported-desk-catalog.v1 file to import; only read')
    a=p.parse_args()
    if (a.action=='import-catalog')!=(a.catalog is not None):p.error('--catalog is required by, and only valid with, import-catalog')
    try:
        from kp_agent_tooling._impl.service.desk_memory_runtime import components
        from kp_agent_tooling._impl.service.desk_registry import is_registry
        config,authority,ledger,store=components(a.config)
        if is_registry(authority):result=_registry(a.action,a.config,a.catalog,config,ledger,store)
        elif a.action in _REGISTRY_ONLY:raise ValueError('this action requires an agent-tooling.desk-registry.v1 catalog')
        else:result=_legacy(a.action,a.config)
        print(json.dumps(result));return 0
    except Exception as e:
        # Paths and source text never reach the UI through error bodies.
        print(json.dumps({'status':'error','category':leaf.error_category(e),'message':str(e) if isinstance(e,ValueError) else 'Registry unavailable; inspect the operator configuration.'}));return 1

if __name__=='__main__':raise SystemExit(main())
