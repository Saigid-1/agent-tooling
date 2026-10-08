"""Public-surface harness for the T2 order (portable desk registry and session bindings).

Every product interaction goes through a console script installed next to the
test interpreter (``kp-agent-desk-registry``, ``kp-agent-desk``,
``kp-agent-memory``, ``kp-agent-host-card``, ``kp-agent-tooling``) or through
the stdio MCP server that ``kp-agent-memory --config ... serve`` starts. No
implementation module is imported.

The order leaves a few envelopes open (see the module docstrings of the tests):
this harness reads them tolerantly and says so where it does.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import uuid
from t12b_seams import drain  # T12b B4: the one drain helper
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

BIN = Path(sys.executable).parent

# The nine OPS roles that the order moves out of the default roster.
OPS_NINE = ('Coordinator', 'Test Implementer', 'Feature Implementer', 'Verification',
            'UX Coordinator', 'Deployment Engineering', 'Security', 'Analyst/Researcher', 'Auditor')

ROSTER_SCHEMA = 'agent-tooling.role-roster.v1'
REGISTRY_SCHEMA = 'agent-tooling.desk-registry.v1'
BINDING_SCHEMA = 'agent-tooling.session-binding.v1'
IMPORTED_CATALOG_SCHEMA = 'ops.imported-desk-catalog.v1'

# Fields the order names for a session-binding record (stdin of `bind`).
BINDING_FIELDS = ('harness', 'provider', 'model', 'native_session_id', 'desk_id', 'source',
                  'workspace', 'parent_session_id', 'recorded_at')

# Words that must not be required anywhere in the portable flow (P1).
GOVERNANCE_WORDS = ('doctrine', 'approval', 'reviewer')

# One harness instance name is used both as the operator config's
# provider_instance and as the binding's `harness`, so either reading of which
# of the two keys the DeskSessionLedger admission leads to the same row.
INSTANCE = 'claude'


def binding_key(tenant_id: str, role: str, repo_key: str) -> str:
    """The documented desk identity: sha256 of 'tenant|role|repo'."""
    return 'binding:' + hashlib.sha256('|'.join((tenant_id, role, repo_key)).encode()).hexdigest()


def new_desk_id() -> str:
    return 'desk:' + str(uuid.uuid4())


def private_json(path: Path, value) -> Path:
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


@dataclass
class Run:
    argv: list
    code: int
    stdout: str
    stderr: str

    @property
    def json(self):
        try:
            return json.loads(self.stdout)
        except ValueError:
            return None

    def describe(self) -> str:
        return (f'$ {" ".join(str(a) for a in self.argv)}\nexit={self.code}\n'
                f'stdout={self.stdout[-2000:]}\nstderr={self.stderr[-2000:]}')

    def ok(self):
        assert self.code == 0, self.describe()
        return self.json

    def refused(self):
        assert self.code != 0, 'expected a refusal\n' + self.describe()
        return self


def cli(script: str, *args, stdin=None, timeout=120) -> Run:
    executable = BIN / script
    assert executable.exists(), f'console script {script} is not installed next to {sys.executable}'
    argv = [str(executable), *[str(a) for a in args]]
    body = None if stdin is None else (stdin if isinstance(stdin, str) else json.dumps(stdin))
    done = subprocess.run(argv, input=body, capture_output=True, text=True, timeout=timeout,
                          env=dict(os.environ))
    return Run(argv, done.returncode, done.stdout, done.stderr)


def memory_config(path: Path, *, state: Path, catalog: Path, workspace: Path, session: str,
                  instance: str = INSTANCE) -> Path:
    return private_json(path, {
        'schema_version': 'ops.desk-memory.local.v1', 'state_root': str(state),
        'catalog_path': str(catalog), 'workspace_root': str(workspace),
        'provider_instance': instance, 'provider_session_id': session})


def roles_of(output) -> list:
    """`roles` output: a list of roles, or an object carrying a `roles` list (order leaves it open)."""
    if isinstance(output, list):
        return output
    if isinstance(output, dict) and isinstance(output.get('roles'), list):
        return output['roles']
    raise AssertionError(f'roles output carries no role list: {output!r}')


def desks_of(output) -> list:
    if isinstance(output, dict) and isinstance(output.get('desks'), list):
        return output['desks']
    if isinstance(output, list):
        return output
    raise AssertionError(f'list output carries no desk list: {output!r}')


def report_count(report, bucket: str) -> int:
    """Importer report bucket as a count; a bucket may be a list of desks or an integer."""
    candidates = [report.get(bucket)] if isinstance(report, dict) else []
    if isinstance(report, dict) and isinstance(report.get('counts'), dict):
        candidates.append(report['counts'].get(bucket))
    for value in candidates:
        if isinstance(value, list):
            return len(value)
        if type(value) is int:
            return value
    raise AssertionError(f'import report has no {bucket!r} bucket: {report!r}')


def find_record(output, key='native_session_id'):
    """The recorded session binding inside `bind` output (the record itself or one nested object)."""
    if isinstance(output, dict):
        if key in output:
            return output
        for value in output.values():
            found = find_record(value, key) if isinstance(value, (dict, list)) else None
            if found is not None:
                return found
    if isinstance(output, list):
        for value in output:
            found = find_record(value, key)
            if found is not None:
                return found
    return None


@dataclass
class Registry:
    """A registry-backed memory state built only through public CLIs."""
    root: Path
    state: Path
    roster: Path
    descriptor: Path
    operator: Path
    tenant: str
    sessions: dict = field(default_factory=dict)

    @classmethod
    def create(cls, root: Path, roles, *, tenant='tenant-portable', state: Path | None = None,
               initialize=True) -> 'Registry':
        root.mkdir(parents=True, exist_ok=True)
        fresh = state is None
        state = state or private_dir(root / 'state')
        roster = private_json(root / 'roster.json', {'schema_version': ROSTER_SCHEMA, 'roles': list(roles)})
        descriptor = private_json(root / 'registry.json', {
            'schema_version': REGISTRY_SCHEMA, 'tenant_id': tenant, 'roster_path': str(roster)})
        operator = memory_config(root / 'operator.json', state=state, catalog=descriptor,
                                 workspace=root, session='operator-console')
        world = cls(root, state, roster, descriptor, operator, tenant)
        if initialize:
            if fresh:
                # Existing public setup of the memory state, then of the registry tables.
                cli('kp-agent-desk', '--config', operator, 'initialize').ok()
            world.registry('initialize').ok()
        return world

    def registry(self, action, payload=None, *extra) -> Run:
        return cli('kp-agent-desk-registry', '--config', self.operator, action, *extra, stdin=payload)

    def roles(self) -> list:
        return roles_of(self.registry('roles').ok())

    def listing(self):
        return self.registry('list').ok()

    def desk_input(self, *, role, name='Archive desk', description='Keeps the archive in order.',
                   repos=('workspace-repo',), capture=True, memory_write=True, context_doc=None, desk_id=None,
                   expected_version=0) -> dict:
        request = {'desk_id': desk_id or new_desk_id(), 'name': name, 'description': description,
                   'role': role, 'repos': list(repos), 'capture': capture,
                   'memory_write': memory_write, 'expected_version': expected_version}
        if context_doc is not None:
            request['context_doc'] = context_doc
        return request

    def save_desk(self, **values):
        request = self.desk_input(**values)
        return request, self.registry('save', request)

    def binding_request(self, *, desk_id, session, source='operator', parent=None, **overrides) -> dict:
        record = {'harness': INSTANCE, 'provider': 'anthropic', 'model': 'fixture-model',
                  'native_session_id': session, 'desk_id': desk_id, 'source': source,
                  'workspace': str(self.root), 'parent_session_id': parent,
                  'recorded_at': '2026-09-30T12:00:00+00:00'}
        record.update(overrides)
        return record

    def bind(self, *, desk_id, session, **overrides) -> Run:
        return self.registry('bind', self.binding_request(desk_id=desk_id, session=session, **overrides))

    def session_config(self, session) -> Path:
        path = self.root / f'session-{session}.json'
        if not path.exists():
            memory_config(path, state=self.state, catalog=self.descriptor, workspace=self.root,
                          session=session)
        return path

    def bindings(self) -> list:
        """The authority's bindings as the existing `kp-agent-desk desks` prints them."""
        return cli('kp-agent-desk', '--config', self.operator, 'desks').ok()


def capture_card(config: Path, *, session: str, text: str, event_id: str = 'ev-1',
                 instance: str = INSTANCE) -> dict:
    """Record one source episode for an admitted session through the host-card CLI."""
    event = private_json(config.parent / f'card-{session}-{event_id}.json', {
        'schema_version': 'ops.host-card-event.v1', 'provider_instance': instance,
        'host_session_id': session, 'board_id': 'board-fixture', 'workspace_id': 'workspace-fixture',
        'card_id': text, 'event_id': event_id, 'recorded_at': '2026-09-01T00:00:00Z',
        'asserted_by': 'operator:fixture'})
    return cli('kp-agent-host-card', '--config', config, '--event', event, '--apply').ok()


def memory_call(config: Path, tool: str, arguments: dict) -> Run:
    return cli('kp-agent-memory', '--config', config, 'call', '--tool', tool,
               '--arguments', json.dumps(arguments))


def proposal_from_hit(hit: dict, text='Cedar lantern evidence was observed.') -> dict:
    return {'episode_ids': [hit['episode_id']], 'unresolved_questions': [],
            'items': [{'kind': 'observation', 'text': text, 'citations': [{
                'episode_id': hit['episode_id'], 'event_id': hit['event_id'],
                'start': hit['start'], 'end': hit['end'], 'quote': hit['quote']}]}]}


@dataclass
class ToolReply:
    is_error: bool
    value: object


def mcp(config: Path, script: str = 'kp-agent-memory', calls=(), list_tools=True):
    """Start a stdio MCP server through its console script; return (tool list, replies)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def session():
        params = StdioServerParameters(command=str(BIN / script),
                                       args=['--config', str(config), 'serve'], env=dict(os.environ))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                tools = [t.model_dump() for t in (await client.list_tools()).tools] if list_tools else []
                replies = []
                for name, arguments in calls:
                    arguments = arguments(replies) if callable(arguments) else arguments
                    reply = await client.call_tool(name, arguments)
                    value = reply.structuredContent
                    if value is None and reply.content:
                        try:
                            value = json.loads(reply.content[0].text)
                        except (ValueError, AttributeError):
                            value = reply.content[0].text
                    replies.append(ToolReply(bool(reply.isError), value))
                return tools, replies

    return asyncio.run(session())


@dataclass
class Legacy:
    """A pre-T2 memory store built with the current public CLIs over an imported catalog."""
    root: Path
    state: Path
    catalog: Path
    config: Path
    rows: list
    episode_id: str
    capsule_id: str
    session: str = 'legacy-session-1'
    tenant: str = 'tenant-legacy'

    @classmethod
    def create(cls, root: Path) -> 'Legacy':
        root.mkdir(parents=True, exist_ok=True)
        tenant = 'tenant-legacy'
        state = private_dir(root / 'state')
        rows = []
        for role, repo, label, writable in (('Verification', 'ops', 'Verification desk', True),
                                            ('Deployment Engineering', 'core', 'Deploy desk', False),
                                            ('Curator', 'archive', 'Archive curator', True)):
            rows.append({'binding_key': binding_key(tenant, role, repo), 'tenant_id': tenant,
                         'role': role, 'repo_key': repo, 'desk_label': label,
                         'source': 'existing-registration', 'memory_write_allowed': writable})
        catalog = private_json(root / 'imported-catalog.json', {
            'schema_version': IMPORTED_CATALOG_SCHEMA, 'approval_ref': 'fixture-history-migration',
            'bindings': rows})
        session = 'legacy-session-1'
        config = memory_config(root / 'legacy.json', state=state, catalog=catalog, workspace=root,
                               session=session)
        cli('kp-agent-desk', '--config', config, 'initialize').ok()
        cli('kp-agent-desk', '--config', config, 'admit', '--desk-id', rows[0]['binding_key'],
            '--provider-id', 'anthropic', '--model-id', 'legacy-model').ok()
        receipt = capture_card(config, session=session, text='Cedar lantern retained history')
        drain(config)  # T12b B4: the indexer, not the capture, indexes
        found = memory_call(config, 'memory.search', {'query': 'Cedar lantern'}).ok()
        hit = next(r for r in found['results'] if r['episode_id'] == receipt['episode_id'])
        proposed = memory_call(config, 'memory.propose', proposal_from_hit(hit)).ok()
        return cls(root, state, catalog, config, rows, receipt['episode_id'], proposed['capsule_id'])

    def registry(self, root: Path | None = None, roles=None) -> Registry:
        """A registry descriptor for the same tenant over the same memory state."""
        roles = roles or [{'role_id': 'Scribe', 'label': 'Scribe', 'purpose': 'Record work.'}]
        return Registry.create(root or self.root / 'registry', roles, tenant=self.tenant,
                               state=self.state)
