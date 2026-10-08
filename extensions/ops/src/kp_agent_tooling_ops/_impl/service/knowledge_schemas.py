"""Portable schemas extracted from the accepted knowledge adapter; no auth transport dependency."""
from .knowledge_operations import KNOWLEDGE_OPERATION_DECLARATIONS
from collections.abc import Mapping

def knowledge_tools(service):
    declarations = {row["operation"]:row for row in KNOWLEDGE_OPERATION_DECLARATIONS}
    descriptors = []
    repo_capabilities = service.capabilities_by_repository
    if not isinstance(repo_capabilities, Mapping):
        raise ValueError('knowledge capability catalog unavailable')
    for operation, declaration in declarations.items():
        description = declaration['description']
        properties = {'repo_key': {'type': 'string', 'enum': list(service.repository_keys)}}
        required = ['repo_key']
        if operation in {'reference_diagnostics', 'reference_recovery'}:
            properties.update({
                'path': {'type':'string','minLength':1,'maxLength':512},
                'blob_sha': {'type':'string','pattern':'^[0-9a-f]{40}$|^[0-9a-f]{64}$'},
                'byte_offset': {'type':'integer','minimum':0},
                'byte_length': {'type':'integer','minimum':1},
                'generation_digest': {'type':'string','maxLength':128},
                'code_revisions': {'type':'object','minProperties':1,'maxProperties':8,
                    'propertyNames': {'enum':list(service.repository_keys)},
                    'additionalProperties': {'type':'string','pattern':'^[0-9a-f]{40}$|^[0-9a-f]{64}$'}},
                'offset': {'type':'integer','minimum':0},
                'limit': {'type':'integer','minimum':1,'maximum':20}})
            required.extend(['path','blob_sha','byte_offset','byte_length','generation_digest','code_revisions'])
        elif operation == 'symbol':
            properties.update({'target_revision':{'type':'string','pattern':'^(?:[0-9a-f]{40}|[0-9a-f]{64})$'},
                'path':{'type':'string','minLength':1,'maxLength':512},'line':{'type':'integer','minimum':1}})
            required.extend(['target_revision','path','line'])
        elif operation in {'check_references', 'context'}:
            properties['capability_id'] = {'type': 'string',
                'description': 'Call knowledge.capabilities with this repo_key for valid capability IDs.'}
            required.append('capability_id')
            if operation == 'context':
                properties['target_revision'] = {'type': 'string', 'pattern': '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'}
                properties['query'] = {'type': 'string', 'minLength': 1, 'maxLength': 512}
                required.append('target_revision')
        elif operation not in {'platform', 'capabilities'}:
            properties['query'] = {'type': 'string', 'minLength': 1, 'maxLength': 512}
            required.append('query')
            properties['limit' if operation == 'discover' else 'top_k'] = {
                'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 5}
        if operation == 'retrieve':
            properties['target_revision'] = {'type': 'string', 'pattern': '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'}
            properties['include_historical'] = {'type': 'boolean', 'default': False}
        if operation in {'retrieve', 'context'}:
            properties.update({
                'include_code_references': {'type': 'boolean', 'default': False},
                # Kept unconstrained while disabled so the service can honor
                # the contract to ignore these values without validation.
                'code_revisions': {},
                'reference_baseline_revisions': {},
            })
        input_schema = {'type': 'object', 'properties': properties,
                        'required': required, 'additionalProperties': False}
        if operation in {'retrieve', 'context'}:
            revision_map = {
                'type': 'object', 'maxProperties': 8,
                'propertyNames': {'enum': list(service.repository_keys)},
                'additionalProperties': {
                    'type': 'string',
                    'pattern': '^(?:[0-9a-f]{40}|[0-9a-f]{64})$',
                },
            }
            input_schema['allOf'] = [{
                'if': {'properties': {'include_code_references': {'const': True}},
                       'required': ['include_code_references']},
                'then': {'properties': {
                    'code_revisions': revision_map,
                    'reference_baseline_revisions': revision_map,
                }},
            }]
        if operation in {'check_references', 'context'}:
            # A flat repo enum crossed with the union of all capabilities
            # advertises pairs that the service itself rejects. Keep the
            # optional query and other operation fields unchanged.
            # Keep complete top-level fields visible to MCP schema consumers.
            # Sparse oneOf branches caused hosts to discard target/enrichment fields.
            # A repository with no declared capabilities used to get `{'not': {}}`, so
            # every request for it failed schema validation as `malformed_request`.
            # The service now answers such a pair with a named catalog error, which
            # is only reachable if the schema lets the request through.
            input_schema.setdefault('allOf', []).extend([
                {'if': {'properties': {'repo_key': {'const': key}}, 'required':['repo_key']},
                 'then': {'properties': {'capability_id': {'enum': list(capabilities)}}}}
                for key, capabilities in sorted(repo_capabilities.items()) if capabilities])
        descriptors.append({'name':'knowledge.' + operation, 'description':description, 'inputSchema':input_schema})
    return descriptors
