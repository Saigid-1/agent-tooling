"""Public-surface harness for the T3 order (launch binding and harness profiles).

Contract: docs/work/orders/T3-launch-binding-harness-profiles.md.

Every product interaction goes through a console script installed next to the
test interpreter (``kp-agent-launch``, ``kp-agent-desk-registry``,
``kp-agent-desk``, ``kp-agent-memory``) or through a stdio MCP server started
exactly as a generated MCP configuration names it. No implementation module is
imported.

Harnesses are simulated by a fake executable (``FakeHarness``). It records its
argv and environment, reads the hook and MCP configuration from its argv the way
the real CLI would (Claude: ``--settings`` / ``--mcp-config`` / ``--session-id``;
Codex: ``-c key=value`` TOML overrides), and runs every configured hook command
for an event through ``/bin/sh -c`` with a payload the test constructs on stdin.
Real Claude or Codex CLIs are never used.

Readings the order leaves open are recorded where they are used and repeated in
the arm report under AMBIGUITY.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import subprocess
import sys
import textwrap
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

BIN = Path(sys.executable).parent

PROFILES_SCHEMA = 'agent-tooling.harness-profiles.v1'
ROSTER_SCHEMA = 'agent-tooling.role-roster.v1'
REGISTRY_SCHEMA = 'agent-tooling.desk-registry.v1'
BINDING_SCHEMA = 'agent-tooling.session-binding.v1'

# `prepare` stdin, exactly the fields the order lists.
PREPARE_FIELDS = ('harness', 'provider', 'model', 'desk_id', 'workspace', 'task_id', 'source',
                  'parent_session_id')
# `prepare` stdout, exactly the fields the order lists.
RESULT_FIELDS = ('receipt_path', 'native_session_id', 'argv_additions', 'env_additions', 'files')

# The operator profiles written by these tests name this receipt variable.
RECEIPT_ENV = 'T3_TEST_LAUNCH_RECEIPT'
# The registry operator config's harness instance; T2 `bind` admits for it.
INSTANCE = 'board-launcher'

ROLES = [{'role_id': 'Scribe', 'label': 'Scribe', 'purpose': 'Record decisions as they are made.'},
         {'role_id': 'Curator', 'label': 'Curator', 'purpose': 'Keep the shared archive tidy.'}]

# Inputs a tool would need to create, move or alter a binding or an admission.
BINDING_INPUTS = {'native_session_id', 'desk_id', 'harness', 'parent_session_id',
                  'provider_session_id', 'source', 'receipt', 'receipt_path'}


def private_json(path: Path, value) -> Path:
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=False)
    path.chmod(0o700)
    return path


def new_desk_id() -> str:
    return 'desk:' + str(uuid.uuid4())


def new_session_id() -> str:
    return str(uuid.uuid4())


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
                f'stdout={self.stdout[-3000:]}\nstderr={self.stderr[-3000:]}')

    def ok(self):
        assert self.code == 0, self.describe()
        return self.json

    def refused(self):
        assert self.code != 0, 'expected a refusal (non-zero exit)\n' + self.describe()
        return self


def cli(script: str, *args, stdin=None, env=None, timeout=180) -> Run:
    executable = BIN / script
    assert executable.exists(), f'console script {script} is not installed next to {sys.executable}'
    argv = [str(executable), *[str(a) for a in args]]
    body = None if stdin is None else (stdin if isinstance(stdin, str) else json.dumps(stdin))
    done = subprocess.run(argv, input=body, capture_output=True, text=True, timeout=timeout,
                          env=dict(os.environ) if env is None else env)
    return Run(argv, done.returncode, done.stdout, done.stderr)


# ---------------------------------------------------------------------------
# Harness profiles (agent-tooling.harness-profiles.v1)
# ---------------------------------------------------------------------------

def claude_profile(harness='claude', *, executable, root, enabled=True, events=('Stop', 'PreCompact')):
    return {'harness': harness, 'enabled': enabled, 'executable': str(executable),
            'session_id': {'strategy': 'mint', 'flag': '--session-id'},
            'mcp': {'strategy': 'config_file_flag', 'flag': '--mcp-config'},
            'hooks': {'strategy': 'settings_file_flag', 'flag': '--settings', 'events': list(events)},
            'capture': {'mode': 'transcript', 'parser': 'claude-jsonl', 'root': str(root)},
            'receipt_env': RECEIPT_ENV}


def profiles_document(profiles) -> dict:
    # Reading: the operator file is {schema_version, profiles: [profile, ...]}; each
    # profile carries its own id in `harness` (the order: "`harness`: the id").
    return {'schema_version': PROFILES_SCHEMA, 'profiles': list(profiles)}


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------

def claude_project_dir(root: Path, workspace: Path) -> Path:
    """Claude's native layout: <root>/<absolute cwd with non-alphanumerics as '-'>/<session>.jsonl."""
    return Path(root) / re.sub(r'[^a-zA-Z0-9]', '-', str(Path(workspace).resolve()))


def claude_transcript(root: Path, workspace: Path, session: str) -> Path:
    return claude_project_dir(root, workspace) / f'{session}.jsonl'


def claude_rows(session: str, workspace: Path, *texts) -> list:
    rows = []
    for index, text in enumerate(texts):
        role = 'user' if index % 2 == 0 else 'assistant'
        content = text if role == 'user' else [{'type': 'text', 'text': text}]
        rows.append({'type': role, 'sessionId': session, 'uuid': str(uuid.uuid4()),
                     'cwd': str(workspace), 'timestamp': '2026-09-30T12:00:00.000Z',
                     'message': {'role': role, 'content': content}})
    return rows


def codex_rollout(root: Path, session: str) -> Path:
    return Path(root) / '2026' / '09' / '30' / f'rollout-2026-09-30T12-00-00-{session}.jsonl'


def codex_rows(session: str, workspace: Path, *texts, meta_id: str | None = None) -> list:
    rows = [{'timestamp': '2026-09-30T12:00:00.000Z', 'type': 'session_meta',
             'payload': {'id': meta_id or session, 'timestamp': '2026-09-30T12:00:00.000Z',
                         'cwd': str(workspace), 'originator': 'codex_cli_rs', 'cli_version': '0.0.0',
                         'source': 'cli'}}]
    for index, text in enumerate(texts):
        role = 'user' if index % 2 == 0 else 'assistant'
        kind = 'input_text' if role == 'user' else 'output_text'
        rows.append({'timestamp': '2026-09-30T12:00:01.000Z', 'type': 'response_item',
                     'payload': {'type': 'message', 'role': role,
                                 'content': [{'type': kind, 'text': text}]}})
    return rows


def append_rows(path: Path, rows) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as out:
        for row in rows:
            out.write(json.dumps(row) + '\n')
    return path


# ---------------------------------------------------------------------------
# Fake harness executable
# ---------------------------------------------------------------------------

_FAKE_SOURCE = textwrap.dedent(r'''
    """Fake harness: records argv/env, reads hook and MCP configuration, runs hooks."""
    import json, os, re, subprocess, sys, tomllib

    flavor, argv = sys.argv[1], sys.argv[2:]
    record = {'flavor': flavor, 'argv': argv, 'env': dict(os.environ), 'cwd': os.getcwd(),
              'errors': [], 'runs': []}

    def load_json_arg(value):
        text = value if value.lstrip().startswith('{') else open(value).read()
        return json.loads(text)

    def claude_config():
        single = {'--settings': [], '--session-id': []}
        mcp = []
        i = 0
        while i < len(argv):
            arg = argv[i]
            if arg in single and i + 1 < len(argv):
                single[arg].append(argv[i + 1]); i += 2; continue
            if arg == '--mcp-config':
                # Claude's --mcp-config is variadic: it takes every following non-flag argument.
                i += 1
                while i < len(argv) and not argv[i].startswith('-'):
                    mcp.append(argv[i]); i += 1
                continue
            for flag in single:
                if arg.startswith(flag + '='):
                    single[flag].append(arg[len(flag) + 1:])
            i += 1
        hooks, servers = {}, {}
        for value in single['--settings']:
            for event, groups in (load_json_arg(value).get('hooks') or {}).items():
                hooks.setdefault(event, []).extend(groups)
        for value in mcp:
            servers.update(load_json_arg(value).get('mcpServers') or {})
        return {'settings': single['--settings'], 'mcp_configs': mcp,
                'session_ids': single['--session-id'], 'hooks': hooks, 'mcp_servers': servers}

    def key_path(key):
        probe = tomllib.loads(f'{key} = 0')
        path = []
        while isinstance(probe, dict):
            (name, probe), = probe.items()
            path.append(name)
        return path

    def codex_config():
        merged, overrides = {}, []
        i = 0
        while i < len(argv):
            arg, value = argv[i], None
            if arg in ('-c', '--config') and i + 1 < len(argv):
                value = argv[i + 1]; i += 1
            elif arg.startswith('-c') and len(arg) > 2 and not arg.startswith('--'):
                value = arg[2:]
            elif arg.startswith('--config='):
                value = arg[len('--config='):]
            i += 1
            if value is None:
                continue
            key, _, raw = value.partition('=')
            overrides.append([key, raw])
            try:
                parsed = tomllib.loads(f'v = {raw}')['v']
            except tomllib.TOMLDecodeError:
                parsed = raw  # Codex falls back to a literal string.
            target = merged
            path = key_path(key)
            for name in path[:-1]:
                if not isinstance(target.get(name), dict):
                    target[name] = {}
                target = target[name]
            target[path[-1]] = parsed  # Codex: a later override replaces the value at its path.
        return {'overrides': overrides, 'config': merged,
                'hooks': merged.get('hooks') if isinstance(merged.get('hooks'), dict) else {},
                'mcp_servers': merged.get('mcp_servers') or {}}

    def matches(group, payload):
        matcher = group.get('matcher')
        if matcher in (None, '', '*'):
            return True
        subject = payload.get('trigger') or payload.get('tool_name') or ''
        return re.fullmatch(matcher, str(subject)) is not None

    try:
        config = claude_config() if flavor == 'claude' else codex_config()
    except Exception as error:  # recorded, never raised: the test reads it
        config = {'hooks': {}, 'mcp_servers': {}}
        record['errors'].append(f'config: {type(error).__name__}: {error}')
    record['config'] = config

    plan_path = os.environ.get('T3_FAKE_PLAN')
    plan = json.load(open(plan_path)) if plan_path else []
    for step in plan:
        event, payload = step['event'], step['payload']
        cwd = payload.get('cwd') if isinstance(payload.get('cwd'), str) and os.path.isdir(payload['cwd']) else os.getcwd()
        env = dict(os.environ)
        if flavor == 'claude':
            env['CLAUDE_PROJECT_DIR'] = cwd
        for group in (config.get('hooks') or {}).get(event, []) or []:
            if not isinstance(group, dict) or not matches(group, payload):
                continue
            for handler in group.get('hooks') or []:
                if not isinstance(handler, dict) or handler.get('type', 'command') != 'command':
                    continue
                done = subprocess.run(['/bin/sh', '-c', handler['command']], input=json.dumps(payload),
                                      capture_output=True, text=True, cwd=cwd, env=env, timeout=300)
                record['runs'].append({'step': step.get('label'), 'event': event, 'command': handler['command'],
                                       'declared_timeout': handler.get('timeout'), 'code': done.returncode,
                                       'stdout': done.stdout[-4000:], 'stderr': done.stderr[-4000:]})
    with open(os.environ['T3_FAKE_RECORD'], 'w') as out:
        json.dump(record, out)
''')


@dataclass
class FakeHarness:
    """A fake `claude` or `codex` executable written under a test directory."""
    flavor: str
    executable: Path
    script: Path

    @classmethod
    def create(cls, directory: Path, flavor: str, name: str | None = None) -> 'FakeHarness':
        directory.mkdir(parents=True, exist_ok=True)
        script = directory / f'fake_{flavor}.py'
        script.write_text(_FAKE_SOURCE)
        executable = directory / (name or flavor)
        # A shell wrapper, so interpreter paths with spaces survive the shebang.
        executable.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" {flavor} "$@"\n')
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return cls(flavor, executable, script)

    def launch(self, prepared: dict, *, workspace: Path, plan=(), label='launch', env=None) -> dict:
        """Spawn the fake exactly as a launcher would: executable + argv_additions, env + env_additions."""
        work = workspace.parent / f'.fake-{label}-{uuid.uuid4().hex[:8]}'
        work.mkdir()
        plan_path = work / 'plan.json'
        plan_path.write_text(json.dumps([dict(step) for step in plan]))
        record_path = work / 'record.json'
        env = dict(os.environ if env is None else env)
        env.update({str(k): str(v) for k, v in (prepared.get('env_additions') or {}).items()})
        env.update(T3_FAKE_PLAN=str(plan_path), T3_FAKE_RECORD=str(record_path))
        argv = [str(self.executable), *[str(a) for a in prepared.get('argv_additions') or []]]
        done = subprocess.run(argv, cwd=workspace, env=env, capture_output=True, text=True, timeout=900)
        assert done.returncode == 0, f'fake {self.flavor} failed: {done.stderr[-3000:]}'
        record = json.loads(record_path.read_text())
        assert not record['errors'], record['errors']
        return record


def step(event: str, payload: dict, label: str | None = None) -> dict:
    return {'event': event, 'payload': payload, 'label': label or event}


def runs_for(record: dict, label: str) -> list:
    return [run for run in record['runs'] if run['step'] == label]


def describe_runs(runs) -> str:
    return json.dumps(runs, indent=1)[-6000:]


# ---------------------------------------------------------------------------
# A registry world built through public CLIs
# ---------------------------------------------------------------------------

@dataclass
class World:
    root: Path
    state: Path
    roster: Path
    descriptor: Path
    operator: Path
    workspace: Path
    fakes: Path
    tenant: str
    profiles_path: Path | None = None
    home: Path | None = None
    desks: dict = field(default_factory=dict)

    @classmethod
    def create(cls, root: Path, *, profiles=None, tenant='tenant-launch') -> 'World':
        """profiles: None (no operator profile file) or a list of operator profiles."""
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        state = private_dir(root / 'state')
        roster = private_json(root / 'roster.json', {'schema_version': ROSTER_SCHEMA, 'roles': ROLES})
        descriptor_value = {'schema_version': REGISTRY_SCHEMA, 'tenant_id': tenant, 'roster_path': str(roster)}
        profiles_path = None
        if profiles is not None:
            profiles_path = private_json(root / 'harness-profiles.json', profiles_document(profiles))
            # The order: an optional `harness_profiles_path` in the registry descriptor.
            descriptor_value['harness_profiles_path'] = str(profiles_path)
        descriptor = private_json(root / 'registry.json', descriptor_value)
        workspace = root / 'workspace'
        workspace.mkdir()
        operator = private_json(root / 'operator.json', {
            'schema_version': 'ops.desk-memory.local.v1', 'state_root': str(state),
            'catalog_path': str(descriptor), 'workspace_root': str(workspace),
            'provider_instance': INSTANCE, 'provider_session_id': 'operator-console'})
        home = root / 'home'
        home.mkdir()
        world = cls(root, state, roster, descriptor, operator, workspace, root / 'fakes', tenant,
                    profiles_path, home)
        world.fakes.mkdir()
        # The shipped profiles' native transcript roots under the (scratch) home, should an
        # implementation require them to exist.
        world.claude_root.mkdir(parents=True)
        world.codex_root.mkdir(parents=True)
        cli('kp-agent-desk', '--config', operator, 'initialize', env=world.env()).ok()
        world.registry('initialize').ok()
        return world

    # The native transcript roots of the shipped `claude` and `codex` profiles, under HOME.
    # Reading: the defaults follow the CLIs' own layouts (~/.claude/projects, ~/.codex/sessions);
    # HOME points into the test root, so no real home is read or written.
    @property
    def claude_root(self) -> Path:
        return self.root / 'home' / '.claude' / 'projects'

    @property
    def codex_root(self) -> Path:
        return self.root / 'home' / '.codex' / 'sessions'

    def fake(self, flavor: str, name: str | None = None) -> 'FakeHarness':
        return FakeHarness.create(self.fakes / (name or flavor), flavor, name=name)

    def launch(self, fake: 'FakeHarness', prepared: dict, plan=(), label='launch') -> dict:
        """Run the fake with this world's environment (same HOME as `prepare`)."""
        return fake.launch(prepared, workspace=self.workspace, plan=plan, label=label, env=self.env())

    def write_profiles(self, profiles) -> None:
        assert self.profiles_path is not None, 'world was created without an operator profile file'
        private_json(self.profiles_path, profiles_document(profiles))

    def env(self) -> dict:
        """Subprocess environment: HOME points into the test root so defaults never touch a real home."""
        env = dict(os.environ)
        env['HOME'] = str(self.root / 'home')
        for name in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME', RECEIPT_ENV):
            env.pop(name, None)
        return env

    def registry(self, action, payload=None) -> Run:
        return cli('kp-agent-desk-registry', '--config', self.operator, action, stdin=payload, env=self.env())

    def save_desk(self, *, role='Scribe', name='Lantern desk', capture=True, memory_write=True) -> str:
        desk_id = new_desk_id()
        self.registry('save', {'desk_id': desk_id, 'name': name, 'description': f'{name} for launch tests.',
                               'role': role, 'repos': ['workspace-repo'], 'capture': capture,
                               'memory_write': memory_write, 'expected_version': 0}).ok()
        self.desks[desk_id] = name
        return desk_id

    def request(self, *, harness, desk_id, task_id='task-1', source='board', parent=None,
                provider='anthropic', model='fixture-model', workspace=None, **extra) -> dict:
        body = {'harness': harness, 'provider': provider, 'model': model, 'desk_id': desk_id,
                'workspace': str(workspace or self.workspace), 'task_id': task_id, 'source': source,
                'parent_session_id': parent}
        body.update(extra)
        return body

    def prepare(self, request) -> Run:
        return cli('kp-agent-launch', '--config', self.operator, 'prepare', stdin=request, env=self.env())

    def prepared(self, request) -> dict:
        output = self.prepare(request).ok()
        assert isinstance(output, dict) and set(RESULT_FIELDS) <= set(output), output
        assert isinstance(output['receipt_path'], str) and Path(output['receipt_path']).is_absolute(), output
        assert isinstance(output['argv_additions'], list) and isinstance(output['env_additions'], dict), output
        return output

    def hook(self, receipt, payload, *, env=None) -> Run:
        return cli('kp-agent-launch', '--receipt', receipt, 'hook', stdin=payload, env=env or self.env())

    def bindings(self) -> list:
        """Recorded session bindings for this registry's harness instance (T2 `list`)."""
        output = self.registry('list').ok()
        assert isinstance(output, dict) and isinstance(output.get('bindings'), list), output
        return output['bindings']

    def binding_for(self, session) -> list:
        return [b for b in self.bindings() if b.get('native_session_id') == session]

    def session_config(self, session) -> Path:
        """An operator-written memory config for one exact session (the T2 public config shape)."""
        path = self.root / f'session-{session}.json'
        if not path.exists():
            private_json(path, {'schema_version': 'ops.desk-memory.local.v1', 'state_root': str(self.state),
                                'catalog_path': str(self.descriptor), 'workspace_root': str(self.workspace),
                                'provider_instance': INSTANCE, 'provider_session_id': session})
        return path


# ---------------------------------------------------------------------------
# MCP
# ---------------------------------------------------------------------------

@dataclass
class ToolReply:
    is_error: bool
    value: object


def mcp_session(command: str, args, env: dict, calls=(), list_tools=True):
    """Start a stdio MCP server; initialize; optionally list tools; make calls."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def session():
        params = StdioServerParameters(command=str(command), args=[str(a) for a in args], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as client:
                await client.initialize()
                tools = [t.model_dump() for t in (await client.list_tools()).tools] if list_tools else []
                replies = []
                for name, arguments in calls:
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


def memory(world: World, session: str, calls=(), list_tools=False):
    """Call the memory MCP as that exact session, through `kp-agent-memory --config ... serve`."""
    return mcp_session(BIN / 'kp-agent-memory', ['--config', world.session_config(session), 'serve'],
                       world.env(), calls, list_tools)


def server_env(spawn_env: dict, server: dict) -> dict:
    env = dict(spawn_env)
    env.update({str(k): str(v) for k, v in (server.get('env') or {}).items()})
    return env


def json_files_in(values) -> list:
    """Absolute JSON files named by a server's args or env (tolerant search for its memory config)."""
    found = []
    for value in values:
        for piece in str(value).split('='):
            path = Path(piece)
            if path.is_absolute() and path.suffix == '.json' and path.is_file():
                try:
                    found.append((path, json.loads(path.read_text())))
                except ValueError:
                    continue
    return found


def search_hits(reply: ToolReply, marker: str) -> list:
    assert not reply.is_error, reply.value
    return [r for r in reply.value.get('results', []) if marker in json.dumps(r)]


def episodes_total(world: World, session: str) -> int:
    _, replies = memory(world, session, calls=[('memory.list', {'kind': 'episodes'})])
    assert not replies[0].is_error, replies[0].value
    return replies[0].value['total']


def ready(world: World, session: str) -> bool:
    _, replies = memory(world, session, calls=[('memory.connection_status', {})])
    return (not replies[0].is_error) and replies[0].value.get('status') == 'ready'


def own_binding_keys(world: World, session: str) -> list:
    _, replies = memory(world, session, calls=[('memory.bindings', {})])
    assert not replies[0].is_error, replies[0].value
    return [b['binding_key'] for b in replies[0].value['bindings'] if b.get('own')]


def desk_binding_key(world: World, desk_id: str) -> str:
    output = world.registry('list').ok()
    for desk in output['desks']:
        if desk['desk_id'] == desk_id:
            return desk.get('binding_key') or desk['binding']['binding_key']
    raise AssertionError(f'desk {desk_id} not in registry list: {output["desks"]!r}')


def is_private_file(path: Path) -> bool:
    info = Path(path).lstat()
    return stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077 and info.st_uid == os.getuid()


def is_private_dir(path: Path) -> bool:
    info = Path(path).lstat()
    return stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o077 and info.st_uid == os.getuid()


def paths_in(value) -> list:
    """Every absolute path string inside `files` (a list, a mapping, or nested)."""
    found = []
    if isinstance(value, str):
        if Path(value).is_absolute():
            found.append(Path(value))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(paths_in(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(paths_in(item))
    return found


def flag_values(argv, flag) -> list:
    values = []
    for index, arg in enumerate(argv):
        if arg == flag and index + 1 < len(argv):
            values.append(argv[index + 1])
        elif isinstance(arg, str) and arg.startswith(flag + '='):
            values.append(arg[len(flag) + 1:])
    return values


def codex_override_keys(argv) -> list:
    keys = []
    for index, arg in enumerate(argv):
        if arg in ('-c', '--config') and index + 1 < len(argv):
            keys.append(argv[index + 1].partition('=')[0])
        elif arg.startswith('--config='):
            keys.append(arg[len('--config='):].partition('=')[0])
    return keys
