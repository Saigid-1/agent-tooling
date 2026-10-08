"""S1a harness: a test world, a loopback provider stub, a network guard and the role, run in-process.

Order: docs/work/orders/S1a-scheduled-summarizer.md. Every name the tests assume about the role lives in
`s1a_seams`; this module holds only mechanics.

Instruments:

- `NetworkGuard`: a `sys.addaudithook` hook, active only while the role runs. It records every outbound
  socket connection attempt (`socket.connect`, which covers `connect_ex`), every name lookup
  (`socket.getaddrinfo`, `gethostbyname*`, `gethostbyaddr`, `getnameinfo`), every datagram send
  (`socket.sendto`, `socket.sendmsg`) and every child process the role starts (`subprocess.Popen`,
  `os.system`, `os.exec`, `os.posix_spawn`, `os.spawn`, `os.fork*`). It lets through only a connection to
  the stub provider's loopback address and a lookup of `127.0.0.1`; it refuses everything else by raising
  in the hook, so nothing leaves the host. A child process is recorded and reported, because the hook
  cannot observe the network of another process.
- `StubProvider`: an OpenRouter-shaped loopback HTTP server on 127.0.0.1 with a distinctive base path,
  recording every connection it accepts and every request (method, path, query, the credential it
  carried by identity only, and the body, kept in memory). GET `<base>/models` answers the listing with the
  approved model and one other; POST `<base>/chat/completions` answers one completion whose content is a
  valid empty summary proposal, reporting the requested model unless the test names another one.
- statements: tests/t10_instruments.py (the T12c instrument), when the test asks for it.

The test world: a state root with the existing stores, a desk registry (`agent-tooling.desk-registry.v1`,
the runtime's registry kind) with one desk per tag, a host session per desk (instance `s1a-host`) that
seals episodes and enqueues jobs through the existing queue, the gateway configuration with a 0600 key
file holding a generated test value, the approval, and the role's admissions made with the existing
`admit`. Nothing here is a real provider or a real key.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import inspect
import io
import json
import os
import secrets
import signal
import socket
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import s1a_seams as seams

_REAL_CONNECT = sqlite3.connect
TENANT = 's1a-tenant'
HOST_INSTANCE = 's1a-host'
HOST_PROVIDER = 'fixture'
HOST_MODEL = 'fixture-model'
OTHER_MODEL = 's1a/other-model'
BASE_PATH = '/s1a-route/api/v1'
GATEWAY_SCHEMA = 'agent-tooling.model-gateway.v1'
REGISTRY_SCHEMA = 'agent-tooling.desk-registry.v1'
_DESK_NAMESPACE = uuid.UUID('5a1a0000-0000-4000-8000-00000000c0de')
DECOY_ENV = ('OPENROUTER_API_KEY', 'OPENAI_API_KEY', 'KP_AGENT_MODELS_API_KEY', 'SUMMARIZER_API_KEY')
GENEROUS_BUDGET = {'max_calls_per_hour': 100, 'max_usd_per_day': 5.0, 'estimated_usd_per_call': 0.01}


def desk_id(tag):
    return 'desk:' + str(uuid.uuid5(_DESK_NAMESPACE, tag))


def private_write(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, indent=1, sort_keys=True))
    path.chmod(0o600)
    return path


def iter_dicts(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from iter_dicts(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_dicts(item)


# ======================================================================================= network guard

_INET = (socket.AF_INET, socket.AF_INET6)
_LOOKUPS = {'socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyname_ex', 'socket.gethostbyaddr',
            'socket.getnameinfo'}
_SENDS = {'socket.sendto', 'socket.sendmsg'}
_CHILDREN = {'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn', 'os.spawn', 'os.fork',
             'os.forkpty', 'pty.spawn'}
_GUARD = None


@dataclass
class NetEvent:
    kind: str       # connect | lookup | send | child | unix-connect
    detail: str
    allowed: bool


class NetworkGuard:
    def __init__(self, allowed_address):
        self.allowed_address = allowed_address  # (host, port)
        self.events: list[NetEvent] = []
        self._lock = threading.Lock()

    def _add(self, kind, detail, allowed):
        with self._lock:
            self.events.append(NetEvent(kind, detail, allowed))

    def hook(self, event, args):
        if event == 'socket.connect':
            sock, address = args[0], args[1]
            family = getattr(sock, 'family', None)
            if family in _INET:
                host, port = address[0], address[1]
                allowed = (host, port) == self.allowed_address
                self._add('connect', f'{host}:{port}', allowed)
                if not allowed:
                    raise ConnectionRefusedError('S1a network guard: connection refused before it left the host')
            else:
                self._add('unix-connect', str(address), True)
        elif event in _LOOKUPS:
            host = args[0]
            allowed = event == 'socket.getaddrinfo' and host in ('127.0.0.1', b'127.0.0.1')
            self._add('lookup', f'{event}:{host!r}', allowed)
            if not allowed:
                raise socket.gaierror('S1a network guard: name lookup refused')
        elif event in _SENDS:
            self._add('send', f'{event}:{args[1]!r}' if len(args) > 1 else event, False)
            raise PermissionError('S1a network guard: datagram refused')
        elif event in _CHILDREN:
            self._add('child', f'{event}:{args[0]!r}' if args else event, False)

    # views
    def connections(self):
        return [e for e in self.events if e.kind == 'connect']

    def network(self):
        """Every network event: connection attempts, name lookups and datagrams."""
        return [e for e in self.events if e.kind in ('connect', 'lookup', 'send')]

    def refused(self):
        return [e for e in self.events if e.kind in ('connect', 'lookup', 'send') and not e.allowed]

    def children(self):
        return [e for e in self.events if e.kind == 'child']


def _audit(event, args):
    guard = _GUARD
    if guard is not None:
        guard.hook(event, args)


if not getattr(sys, '_s1a_audit_installed', False):
    sys.addaudithook(_audit)
    sys._s1a_audit_installed = True


@contextlib.contextmanager
def guarded(guard):
    global _GUARD
    _GUARD = guard
    try:
        yield guard
    finally:
        _GUARD = None


# ======================================================================================= stub provider

@dataclass
class Request:
    method: str
    raw_path: str
    path: str
    query: str
    credential: str | None    # 'key-file', 'decoy', 'none' or 'other'; never the value
    body: bytes = field(repr=False)

    def json(self):
        try:
            return json.loads(self.body)
        except ValueError:
            return None


def listing_entry(model):
    return {'id': model, 'name': model, 'created': 1_700_000_000, 'context_length': 131072,
            'top_provider': {'context_length': 131072, 'max_completion_tokens': 16384, 'is_moderated': False},
            'supported_parameters': ['max_tokens', 'temperature', 'response_format', 'structured_outputs',
                                     'reasoning', 'include_reasoning', 'tools', 'tool_choice'],
            'reasoning': {'mandatory': False, 'supported_efforts': ['low', 'medium', 'high']},
            'pricing': {'prompt': '0.0000001', 'completion': '0.0000004', 'request': '0', 'image': '0'},
            'architecture': {'input_modalities': ['text'], 'output_modalities': ['text'],
                             'modality': 'text->text'}}


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def process_request(self, request, client_address):
        self.stub._accepted()
        super().process_request(request, client_address)


class StubProvider:
    def __init__(self, *, key_value, decoys):
        self.key_value, self.decoys = key_value, dict(decoys)
        self.reported_model = None          # None: report the requested model
        self.requests: list[Request] = []
        self.accepted = 0
        self._lock = threading.Lock()
        self._posts = 0
        self.server = _Server(('127.0.0.1', 0), self._handler())
        self.server.stub = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def address(self):
        return ('127.0.0.1', self.server.server_address[1])

    @property
    def base_url(self):
        return f'http://127.0.0.1:{self.server.server_address[1]}{BASE_PATH}'

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def _accepted(self):
        with self._lock:
            self.accepted += 1

    def _credential(self, headers):
        value = headers.get('Authorization') or ''
        token = value.split(' ', 1)[1] if ' ' in value else value
        if not value:
            return 'none'
        if token == self.key_value:
            return 'key-file'
        if token in self.decoys.values():
            return 'decoy'
        return 'other'

    def _record(self, handler, body):
        parts = urlsplit(handler.path)
        request = Request(handler.command, handler.path, parts.path, parts.query,
                          self._credential(handler.headers), body)
        with self._lock:
            self.requests.append(request)
        return request

    def _completion(self, request):
        sent = request.json() if isinstance(request.json(), dict) else {}
        with self._lock:
            self._posts += 1
            ordinal = self._posts
        model = self.reported_model or (sent.get('model') if isinstance(sent.get('model'), str) else 'unknown')
        proposal = {'items': [], 'unresolved_questions': ['S1a stub: no durable item in this source.']}
        return {'id': f'gen-s1a-{ordinal}', 'object': 'chat.completion', 'created': int(time.time()),
                'model': model, 'provider': 'S1aStub',
                'choices': [{'index': 0, 'finish_reason': 'stop', 'native_finish_reason': 'stop',
                             'message': {'role': 'assistant', 'content': json.dumps(proposal)}}],
                'usage': {'prompt_tokens': 120, 'completion_tokens': 24, 'total_tokens': 144, 'cost': 0.0001}}

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'

            def log_message(self, *_):
                pass

            def _send(self, status, value):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                request = stub._record(self, b'')
                if request.path == BASE_PATH + seams.LISTING_SUFFIX:
                    self._send(200, {'data': [listing_entry(seams.APPROVED_MODEL), listing_entry(OTHER_MODEL)]})
                else:
                    self._send(404, {'error': {'message': 'S1a stub: not found', 'code': 404}})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get('Content-Length') or 0)
                request = stub._record(self, self.rfile.read(length) if length else b'')
                if request.path == BASE_PATH + seams.COMPLETION_SUFFIX:
                    self._send(200, stub._completion(request))
                else:
                    self._send(404, {'error': {'message': 'S1a stub: not found', 'code': 404}})

        return Handler


# ========================================================================================= the world

class World:
    """One state root, a desk registry, host sessions, the gateway, the key, the approval, the role."""

    def __init__(self, root: Path, tags=('alpha',)):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.nonce = secrets.token_hex(4)
        self.tags = tuple(tags)
        self.state = self._dir('state')
        # The role's state directory: the operator creates it, private; the role never does.
        self.role_state = self.state / seams.ROLE_STATE_DIR
        self.role_state.mkdir(mode=seams.ROLE_STATE_MODE)
        self.role_state.chmod(seams.ROLE_STATE_MODE)
        self.config = self._dir('config')
        self.operator = self._dir('operator')
        self.secrets = self._dir('secrets')
        self.artifacts = self._dir('artifacts')
        self.descriptor = private_write(self.config / 'launch' / 'registry.json', {
            'schema_version': REGISTRY_SCHEMA, 'tenant_id': TENANT,
            'roster_path': str(self.config / 'launch' / 'roster.json')})
        self.gateway_path = self.config / 'summarizer' / 'model-gateway.json'
        self.approval_path = self.config / 'summarizer' / seams.APPROVAL_FILE
        self.key_path = self.secrets / 'openrouter.key'
        # A generated test value: never a real key, never printed.
        self.key_value = 'sk-or-v1-' + secrets.token_hex(32)
        self.decoys = {name: 'sk-or-v1-' + secrets.token_hex(32) for name in DECOY_ENV}
        self.stub = StubProvider(key_value=self.key_value, decoys=self.decoys)
        self.host_configs = {tag: private_write(self.operator / f'host-{tag}.json', {
            'schema_version': 'ops.desk-memory.local.v1', 'state_root': str(self.state),
            'catalog_path': str(self.descriptor), 'workspace_root': str(self.root),
            'provider_instance': HOST_INSTANCE, 'provider_session_id': f'host-{tag}'}) for tag in self.tags}
        self.bindings: dict[str, str] = {}
        self.markers: dict[str, list[str]] = {tag: [] for tag in self.tags}
        self.episodes: dict[str, list[str]] = {tag: [] for tag in self.tags}
        self.role_sessions: dict[str, list[str]] = {tag: [] for tag in self.tags}

    def _dir(self, name):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        return path

    def close(self):
        self.stub.stop()

    # ---------------------------------------------------------------------------- operator steps
    def initialize(self):
        from kp_agent_tooling._impl.service.desk_memory_runtime import admit, components, initialize
        from kp_agent_tooling._impl.service.desk_profiles import DeskProfiles
        from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue
        first = self.tags[0]
        initialize(self.host_configs[first])
        store = components(self.host_configs[first])[3]
        profiles = DeskProfiles(store, f'host-{first}')
        profiles.initialize()
        for tag in self.tags:
            profiles.save(dict(desk_id=desk_id(tag), name=f'S1a {tag} desk', description=f'S1a {tag} desk.',
                               role='general', expected_version=0))
        for tag in self.tags:
            self.bindings[tag] = admit(self.host_configs[tag], desk_id=desk_id(tag), provider_id=HOST_PROVIDER,
                                       model_id=HOST_MODEL)['binding_key']
        ConsolidationQueue(self.queue_path, store=store).initialize()
        self.write_key()
        self.write_gateway()
        return self

    def build(self, *, approved=None, admitted=None, jobs=None):
        """A configured world: every tag approved, admitted and with one queued job, unless narrowed."""
        self.initialize()
        self.write_approval(self.tags if approved is None else approved)
        for tag in (self.tags if admitted is None else admitted):
            self.admit_role(tag)
        for tag in (self.tags if jobs is None else jobs):
            self.enqueue(tag, [self.seal(tag)])
        return self

    def write_key(self, mode=0o600):
        self.key_path.write_text(self.key_value + '\n')
        self.key_path.chmod(mode)

    def remove_key(self):
        self.key_path.unlink()

    def write_gateway(self, *, route=True, budget=GENEROUS_BUDGET, params=None, model=seams.APPROVED_MODEL):
        entry = {'provider': seams.GATEWAY_PROVIDER, 'model': model, 'operation': seams.ROUTE_OPERATION}
        if params is not None:
            entry['params'] = params
        private_write(self.gateway_path, {
            'schema_version': GATEWAY_SCHEMA,
            'providers': {seams.GATEWAY_PROVIDER: {'kind': 'openrouter', 'base_url': self.stub.base_url,
                                                   'api_key_file': str(self.key_path), 'timeout_seconds': 30}},
            'routes': {seams.CAPABILITY: entry} if route else {},
            'budgets': {} if budget is None else {seams.CAPABILITY: dict(budget)},
            'artifact_root': str(self.artifacts)})

    def write_approval(self, tags, **options):
        desks = [(self.bindings[t], self.role_config(t)) for t in tags]
        private_write(self.approval_path, seams.approval_document(desks, **options))

    def remove_approval(self):
        self.approval_path.unlink()

    def role_config(self, tag, generation=1):
        """The role's memory configuration for one desk (operator-written; admission is separate)."""
        session = seams.desk_session_id(tag, generation)
        return private_write(self.operator / f'{session}.json', {
            'schema_version': 'ops.desk-memory.local.v1', 'state_root': str(self.state),
            'catalog_path': str(self.descriptor), 'workspace_root': str(self.root),
            'provider_instance': seams.ROLE_PROVIDER_INSTANCE, 'provider_session_id': session})

    def admit_role(self, tag, *, model=seams.APPROVED_MODEL, generation=1):
        """The operator's admission of the role for one desk (the existing `admit`)."""
        from kp_agent_tooling._impl.service.desk_memory_runtime import admit
        session = seams.desk_session_id(tag, generation)
        config = self.role_config(tag, generation)
        admit(config, desk_id=desk_id(tag), provider_id=seams.GATEWAY_PROVIDER, model_id=model)
        self.role_sessions[tag].append(session)
        return session

    def store(self, tag):
        from kp_agent_tooling._impl.service.desk_memory_runtime import components
        return components(self.host_configs[tag])[3]

    def seal(self, tag):
        """One sealed episode for the desk, carrying a unique marker text."""
        marker = f'S1A-MARKER-{tag}-{self.nonce}-{len(self.markers[tag])}'
        episode = self.store(tag).capture(f'host-{tag}', source_ref=f'visible:{tag}:{len(self.markers[tag])}',
                                          events=[{'event_id': 'e1', 'role': 'user',
                                                   'text': f'{marker}: we decided to keep pool B.'}])['episode_id']
        self.markers[tag].append(marker)
        self.episodes[tag].append(episode)
        return episode

    def enqueue(self, tag, episode_ids):
        from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue
        queue = ConsolidationQueue(self.queue_path, store=self.store(tag))
        return queue.enqueue(f'host-{tag}', episode_ids=list(episode_ids), reason='session_end')['job_id']

    def prefill_ledger(self, calls, *, budget=None):
        """`calls` succeeded calls for the capability in the gateway's own ledger (its existing API)."""
        from decimal import Decimal
        from kp_agent_tooling._impl.service.model_gateway import Budget, BudgetLedger
        ledger = BudgetLedger(self.artifacts)
        budget = budget or Budget(1_000_000, Decimal('1000000'), None, Decimal('0.0001'))
        for _ in range(calls):
            call_id = secrets.token_hex(16)
            now = time.time()
            decision, _, _ = ledger.reserve(call_id=call_id, capability=seams.CAPABILITY,
                                            provider=seams.GATEWAY_PROVIDER, model=seams.APPROVED_MODEL,
                                            budget=budget, confirm=None, digest='sha256:' + '0' * 64, now=now)
            assert decision == 'proceed', 'fixture: the ledger refused the prefill'
            ledger.finish(call_id, 'succeeded', Decimal('0.0001'), now)

    # ---------------------------------------------------------------------------- paths and reads
    @property
    def queue_path(self):
        return self.state / seams.QUEUE_FILE

    @property
    def episodes_path(self):
        return self.state / seams.EPISODES_FILE

    @property
    def sessions_path(self):
        return self.state / seams.SESSIONS_FILE

    @property
    def ledger_path(self):
        return self.artifacts.joinpath(*seams.GATEWAY_LEDGER)

    def _read(self, path, sql, args=()):
        uri = Path(path).resolve().as_uri() + '?mode=ro'
        with contextlib.closing(_REAL_CONNECT(uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, args)]

    def jobs(self, tag=None):
        rows = self._read(self.queue_path, 'SELECT id, binding, state, worker, generation, lease_until, '
                                           'capsule_id, error FROM jobs ORDER BY created, id')
        return [r for r in rows if tag is None or r['binding'] == self.bindings[tag]]

    def job(self, tag, index=0):
        return self.jobs(tag)[index]

    def attempts(self, job_id):
        return self._read(self.queue_path, 'SELECT ordinal, state FROM attempts WHERE job=? ORDER BY ordinal',
                          (job_id,))

    def capsule_count(self, tag=None):
        if tag is None:
            return self._read(self.episodes_path, 'SELECT count(*) AS n FROM capsules')[0]['n']
        return self._read(self.episodes_path, 'SELECT count(*) AS n FROM capsules WHERE binding=?',
                          (self.bindings[tag],))[0]['n']

    def untouched(self, job):
        """A queued job no worker has claimed."""
        return (job['state'] == 'queued' and job['worker'] is None and job['generation'] == 0
                and job['lease_until'] is None and not self.attempts(job['id']))

    def files(self):
        found = {}
        for path in self.root.rglob('*'):
            try:
                if path.is_file() and not path.is_symlink():
                    info = path.stat()
                    found[path] = (info.st_mtime_ns, info.st_size, info.st_ino)
            except OSError:
                continue
        return found


# ========================================================================================= the role

@dataclass
class RoleRun:
    code: object
    exception: BaseException | None
    stdout: str
    stderr: str
    documents: list
    changed: list
    guard: NetworkGuard
    requests: list
    accepted: int
    sleeps: int
    recorder: object = None
    key_value: str = field(default='', repr=False)

    # network
    def connections(self):
        return self.guard.connections()

    def network(self):
        return self.guard.network()

    def posts(self):
        return [r for r in self.requests if r.method == 'POST']

    def gets(self):
        return [r for r in self.requests if r.method == 'GET']

    # status
    def status_nodes(self, value=seams.NOT_CONFIGURED):
        found = []
        for document in self.documents:
            for node in iter_dicts(document):
                if any(isinstance(v, str) and v.strip().lower().startswith(value) for v in node.values()) \
                        or (value in node and node[value] not in (None, False, [], {}, '')):
                    found.append(node)
        return found

    def leaked_key(self):
        """Every place the key value appears: output, or any file the run created or changed."""
        places = []
        needle = self.key_value
        if needle and (needle in self.stdout or needle in self.stderr):
            places.append('role output')
        for path in self.changed:
            try:
                if needle.encode() in path.read_bytes():
                    places.append(str(path))
            except OSError:
                continue
        return places

    def describe(self):
        events = [f'{e.kind} {e.detail} ({"allowed" if e.allowed else "REFUSED"})' for e in self.guard.events]
        requests = [f'{r.method} {r.raw_path} credential={r.credential}' for r in self.requests]
        exc = None if self.exception is None else f'{type(self.exception).__name__}: {self.exception}'
        return (f'exit={self.code!r} exception={exc}\nnetwork/child events: {events}\nstub requests: {requests}\n'
                f'stdout (tail): {self.stdout[-1500:]}\nstderr (tail): {self.stderr[-1500:]}\n'
                f'status documents: {json.dumps(self.documents)[:2000]}')


def role_entry():
    """The role's console-script function, or a test failure naming the absent role."""
    found = importlib.metadata.entry_points(group='console_scripts', name=seams.ROLE_SCRIPT)
    if not found:
        pytest.fail(f'order S1a: there is no role; the console script {seams.ROLE_SCRIPT} is not installed '
                    '(no console_scripts entry point in this environment)', pytrace=False)
    return next(iter(found)).load()


def _documents(text):
    text = text.strip()
    if not text:
        return []
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    found = []
    for line in text.splitlines():
        line = line.strip()
        if line[:1] in '{[':
            try:
                found.append(json.loads(line))
            except ValueError:
                continue
    return found


def _file_documents(path):
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if data.startswith(b'SQLite format 3') or len(data) > 1_000_000:
        return []
    return _documents(data.decode('utf-8', errors='replace'))


@contextlib.contextmanager
def _environment(world):
    saved = dict(os.environ)
    try:
        os.environ.pop('AGENT_MEMORY_VOLUME', None)
        os.environ.update(world.decoys)
        for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
            os.environ[name] = 'http://127.0.0.1:9'
        os.environ['NO_PROXY'] = os.environ['no_proxy'] = '127.0.0.1,localhost'
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


@contextlib.contextmanager
def _pause_hook(between):
    """While the role runs, its pause between ticks (`time.sleep`, or a timed `threading.Event.wait` in the
    role's own thread, s1a_seams) is counted, acts between ticks and never really waits."""
    real_sleep, real_wait = time.sleep, threading.Event.wait
    role_thread = threading.current_thread()
    count = [0]

    def pause():
        count[0] += 1
        if between is not None:
            between(count[0])

    def sleep(seconds):
        pause()

    def wait(event, timeout=None):
        if timeout is None or threading.current_thread() is not role_thread:
            return real_wait(event, timeout)
        pause()
        return real_wait(event, 0)

    time.sleep = sleep
    threading.Event.wait = wait
    try:
        yield count
    finally:
        time.sleep = real_sleep
        threading.Event.wait = real_wait


def run_role(world, *, watch_passes=None, between=None, record=False):
    """Run the role in-process (one tick, or `watch_passes` ticks) under the guard; never raises."""
    function = role_entry()
    argv = seams.role_argv(approval=world.approval_path, gateway=world.gateway_path, state=world.role_state,
                           watch_passes=watch_passes)
    before = world.files()
    first_request = len(world.stub.requests)
    accepted = world.stub.accepted
    guard = NetworkGuard(world.stub.address)
    out, err = io.StringIO(), io.StringIO()
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    saved_argv = sys.argv
    code = exception = recorder = None
    stack = contextlib.ExitStack()
    with stack:
        stack.enter_context(_environment(world))
        if record:
            import t10_instruments
            recorder = stack.enter_context(t10_instruments.recording())
        sleeps = stack.enter_context(_pause_hook(between))
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        stack.enter_context(guarded(guard))
        try:
            if inspect.signature(function).parameters:
                code = function(list(argv))
            else:
                sys.argv = [seams.ROLE_SCRIPT, *argv]
                code = function()
        except SystemExit as stop:
            code = stop.code
        except Exception as error:  # noqa: BLE001 - recorded; the tests assert on it
            exception = error
        finally:
            sys.argv = saved_argv
    for sig, handler in handlers.items():
        signal.signal(sig, handler)
    after = world.files()
    changed = sorted(p for p, sig in after.items() if before.get(p) != sig and p != world.key_path)
    documents = _documents(out.getvalue()) + _documents(err.getvalue())
    for path in changed:
        documents += _file_documents(path)
    return RoleRun(code, exception, out.getvalue(), err.getvalue(), documents, changed, guard,
                   list(world.stub.requests[first_request:]), world.stub.accepted - accepted, sleeps[0],
                   recorder, world.key_value)


def assert_no_child_process(run):
    assert not run.guard.children(), (
        'the role started a child process; the in-process network guard cannot observe it:\n' + run.describe())


@pytest.fixture
def make_world(tmp_path):
    made = []

    def make(tags=('alpha',)):
        world = World(tmp_path / f'world-{len(made)}', tags)
        made.append(world)
        return world

    yield make
    for world in made:
        world.close()


def content_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
                          .encode()).hexdigest()
