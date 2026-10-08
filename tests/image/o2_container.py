#!/usr/bin/env python3
"""The in-container half of the O2 image tests (docs/work/orders/O2-opencode-third-harness.md, F1 and F2).

Not a test module. The image tests mount this file into O3's `opencode` image and run it with the image's
own Python, which carries kp-agent-tooling and its `mcp` dependency. Every product interaction is a
console script of the image or the stdio MCP server a memory configuration names; no implementation
module is imported. Seams: tests/o2_seams.py (handed in through the spec).

    o2_container.py netrec <log> <port>...        log every TCP connection with its TLS SNI or Host header
    o2_container.py world <spec> <out>            a desk registry under the store root, with the spec's desks
    o2_container.py poll <spec> <launch-id>       wait for the launch's binding, its episode and a search hit
    o2_container.py final <spec> <launch-id>      the launch's captured events, queue jobs and receipts
    o2_container.py serve-check <request.json>    start one MCP server as OpenCode would; ask who it serves
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

ROSTER_SCHEMA = 'agent-tooling.role-roster.v1'
REGISTRY_SCHEMA = 'agent-tooling.desk-registry.v1'
MEMORY_SCHEMA = 'ops.desk-memory.local.v1'
INSTANCE = 'board-launcher'


def _private_json(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def _run(argv, *, stdin=None, timeout=180):
    done = subprocess.run([str(a) for a in argv], input=None if stdin is None else json.dumps(stdin),
                          capture_output=True, text=True, timeout=timeout)
    try:
        value = json.loads(done.stdout)
    except ValueError:
        value = None
    return done.returncode, value, done.stdout[-2000:], done.stderr[-2000:]


# ------------------------------------------------------------------------------------------- netrec

def _sni(data: bytes):
    try:
        if not data or data[0] != 0x16:
            return None
        i = 5 + 4 + 2 + 32
        i += 1 + data[i]
        i += 2 + int.from_bytes(data[i:i + 2], 'big')
        i += 1 + data[i]
        end = i + 2 + int.from_bytes(data[i:i + 2], 'big')
        i += 2
        while i + 4 <= end:
            kind, size = int.from_bytes(data[i:i + 2], 'big'), int.from_bytes(data[i + 2:i + 4], 'big')
            i += 4
            if kind == 0:
                length = int.from_bytes(data[i + 3:i + 5], 'big')
                return data[i + 5:i + 5 + length].decode('ascii', 'replace')
            i += size
    except (IndexError, ValueError):
        return None
    return None


def netrec(log: str, ports: list[int]) -> None:
    """Accept on 127.0.0.1:<port>; record each connection (port, TLS SNI or HTTP Host), then close it."""
    lock = threading.Lock()

    def serve(port):
        server = socket.socket()
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(('127.0.0.1', port))
        server.listen(128)
        while True:
            client, _ = server.accept()
            client.settimeout(2)
            try:
                data = client.recv(8192)
            except OSError:
                data = b''
            host = _sni(data)
            if host is None and data:
                for line in data.split(b'\r\n'):
                    if line.lower().startswith(b'host:'):
                        host = line[5:].strip().decode('ascii', 'replace')
            with lock, open(log, 'a') as out:
                out.write(json.dumps({'at': time.time(), 'port': port, 'host': host}) + '\n')
            client.close()

    for port in ports:
        threading.Thread(target=serve, args=(port,), daemon=True).start()
    print('netrec ready', flush=True)
    while True:
        time.sleep(3600)


# -------------------------------------------------------------------------------------------- world

def world(spec_path: str, out: str) -> None:
    """A registry operator config, its descriptor and roster, the store root, and one desk per spec desk."""
    spec = json.loads(Path(spec_path).read_text())
    store_root = Path(spec['seams']['store_root'])
    store_root.mkdir(parents=True, exist_ok=True)
    state = store_root / 'o2-desks'
    state.mkdir(mode=0o700)
    config = Path('/state/o2/config')
    config.mkdir(parents=True, mode=0o700)
    roster = _private_json(store_root / 'o2-roster.json', {'schema_version': ROSTER_SCHEMA, 'roles': [
        {'role_id': 'Scribe', 'label': 'Scribe', 'purpose': 'Record decisions as they are made.'}]})
    descriptor = _private_json(config / 'registry.json', {'schema_version': REGISTRY_SCHEMA, 'tenant_id': 'tenant-o2',
                                                          'roster_path': str(roster)})
    workspace_root = Path('/state/o2/work')
    workspace_root.mkdir(parents=True, exist_ok=True)
    operator = _private_json(config / 'operator.json', {
        'schema_version': MEMORY_SCHEMA, 'state_root': str(state), 'catalog_path': str(descriptor),
        'workspace_root': str(workspace_root), 'provider_instance': INSTANCE, 'provider_session_id': 'operator-console'})
    steps = [_run(['kp-agent-desk', '--config', operator, 'initialize']),
             _run(['kp-agent-desk-registry', '--config', operator, 'initialize'])]
    desks = []
    for index, name in enumerate(spec['desks']):
        desk_id = 'desk:' + spec['desk_uuids'][index]
        steps.append(_run(['kp-agent-desk-registry', '--config', operator, 'save'], stdin={
            'desk_id': desk_id, 'name': name, 'description': f'{name} for the O2 image run.', 'role': 'Scribe',
            'repos': ['o2-repo'], 'capture': True, 'memory_write': True, 'expected_version': 0}))
        desks.append(desk_id)
    code, listed, _, _ = _run(['kp-agent-desk-registry', '--config', operator, 'list'])
    keys = {}
    for desk in (listed or {}).get('desks', []):
        keys[desk['desk_id']] = desk.get('binding_key') or (desk.get('binding') or {}).get('binding_key')
    result = {'operator': str(operator), 'state_root': str(state), 'config_dir': str(config), 'desks': desks,
              'binding_keys': keys, 'steps': [{'code': s[0], 'stdout': s[2][-400:], 'stderr': s[3][-400:]} for s in steps]}
    Path(out).write_text(json.dumps(result, indent=1))
    if any(s[0] for s in steps):
        print(json.dumps(result), file=sys.stderr)
        raise SystemExit(1)


# ----------------------------------------------------------------------------------------- memory MCP

def _bindings(operator: str) -> list[dict]:
    code, value, _, _ = _run(['kp-agent-desk-registry', '--config', operator, 'list'])
    return (value or {}).get('bindings', []) if code == 0 else []


def session_config(world_out: dict, native: str) -> Path:
    path = Path(world_out['config_dir']) / f'session-{native}.json'
    if not path.exists():
        operator = json.loads(Path(world_out['operator']).read_text())
        _private_json(path, {**operator, 'provider_session_id': native})
    return path


async def _call(client, name, arguments):
    reply = await client.call_tool(name, arguments)
    value = reply.structuredContent
    if value is None and reply.content:
        try:
            value = json.loads(reply.content[0].text)
        except ValueError:
            value = reply.content[0].text
    return bool(reply.isError), value


async def _events(client, cache: dict) -> list[dict]:
    """Every event (episode, id, role, full text) of the episodes memory.list shows; known episodes cached."""
    events, offset = [], 0
    while offset is not None:
        error, page = await _call(client, 'memory.list', {'kind': 'episodes', 'offset': offset, 'limit': 50})
        if error:
            return events
        for entry in page['entries']:
            episode = entry['episode_id']
            if episode not in cache:
                found, start = [], 0
                while start is not None:
                    _, directory = await _call(client, 'memory.episode_directory',
                                               {'episode_id': episode, 'offset': start, 'limit': 100})
                    for item in directory['entries']:
                        text, cursor = '', 0
                        while cursor is not None:
                            _, piece = await _call(client, 'memory.read_event', {
                                'episode_id': episode, 'event_id': item['event_id'], 'start': cursor, 'length': 8000})
                            text += piece['text']
                            cursor = piece['next_start']
                        found.append({'episode_id': episode, 'event_id': item['event_id'], 'role': item['role'],
                                      'text': text})
                    start = directory['next_offset']
                cache[episode] = found
            events.extend(cache[episode])
        offset = page['next_offset']
    return events


def _session(config: Path, body):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(command='kp-agent-memory', args=['--config', str(config), 'serve'],
                                       env=dict(os.environ))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as client:
                await client.initialize()
                return await body(client)
    return asyncio.run(run())


def _launch(spec: dict, launch_id: str) -> dict:
    launch = next(item for item in spec['launches'] if item['id'] == launch_id)
    world_out = json.loads(Path(spec['world_out']).read_text())
    return launch, world_out, world_out['desks'][launch['desk']]


def poll(spec_path: str, launch_id: str) -> None:
    """Wait (deadline) for the launch's binding, then poll memory.list and memory.search every poll interval:
    the times the bound session's episode first shows the answer text and the search first finds it."""
    spec = json.loads(Path(spec_path).read_text())
    launch, world_out, desk = _launch(spec, launch_id)
    seams = spec['seams']
    started = time.monotonic()
    deadline = started + seams['capture_deadline_seconds']
    result = {'launch': launch_id, 'desk': desk, 'native': None, 'bound_after': None, 'episode_after': None,
              'search_after': None, 'polls': 0, 'search_error': None}
    while time.monotonic() < deadline:
        bound = [b for b in _bindings(world_out['operator'])
                 if b.get('desk_id') == desk and b.get('harness') == spec['seams']['harness']]
        if bound:
            result['native'] = bound[0]['native_session_id']
            result['bindings'] = bound
            result['bound_after'] = round(time.monotonic() - started, 3)
            break
        time.sleep(1)
    if result['native'] is None:
        print(json.dumps(result))
        return
    config = session_config(world_out, result['native'])

    async def body(client):
        cache = {}
        while time.monotonic() < deadline:
            result['polls'] += 1
            now = time.monotonic() - started
            if result['episode_after'] is None:
                events = await _events(client, cache)
                if any(launch['answer'] in e['text'] for e in events):
                    result['episode_after'] = round(now, 3)
            if result['episode_after'] is not None and result['search_after'] is None:
                error, value = await _call(client, 'memory.search', {'query': launch['answer']})
                if error:
                    result['search_error'] = str(value)[:500]
                elif any(launch['answer'] in json.dumps(hit) for hit in (value or {}).get('results', [])):
                    result['search_after'] = round(time.monotonic() - started, 3)
            if result['search_after'] is not None:
                return
            await asyncio.sleep(seams['poll_seconds'])
    _session(config, body)
    print(json.dumps(result))


def final(spec_path: str, launch_id: str) -> None:
    """The bound session's captured events, the desk's queue jobs (at each receipt's queue) and the receipts."""
    spec = json.loads(Path(spec_path).read_text())
    launch, world_out, desk = _launch(spec, launch_id)
    bound = [b for b in _bindings(world_out['operator'])
             if b.get('desk_id') == desk and b.get('harness') == spec['seams']['harness']]
    receipts = []
    launches = Path(world_out['state_root']) / 'launches'
    for path in sorted(launches.glob('*/launch.json')) if launches.is_dir() else []:
        try:
            receipt = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if receipt.get('desk_id') == desk:
            receipts.append({'path': str(path), 'queue': receipt.get(spec['seams']['receipt_queue_field']),
                             'harness': receipt.get('harness')})
    result = {'launch': launch_id, 'desk': desk, 'bindings': bound, 'receipts': receipts, 'events': [],
              'jobs': None, 'search_hits': None}
    if not bound:
        print(json.dumps(result))
        return
    native = bound[0]['native_session_id']
    config = session_config(world_out, native)

    async def body(client):
        result['events'] = await _events(client, {})
        error, value = await _call(client, 'memory.search', {'query': launch['answer']})
        result['search_hits'] = None if error else sum(
            1 for hit in (value or {}).get('results', []) if launch['answer'] in json.dumps(hit))
    _session(config, body)
    jobs = {}
    for queue in {r['queue'] for r in receipts if r['queue']}:
        code, value, out, err = _run(['kp-agent-memory-queue', '--config', config, '--queue', queue, 'status'])
        jobs[queue] = (value or {}).get('entries') if code == 0 else {'code': code, 'stdout': out, 'stderr': err}
    result['jobs'] = jobs
    print(json.dumps(result))


# ------------------------------------------------------------------------------------- serve-check

def serve_check(request_path: str) -> None:
    """Start one local MCP server exactly as OpenCode would (command array, launch env plus `environment`,
    launch cwd) and ask memory.connection_status and memory.bindings."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    request = json.loads(Path(request_path).read_text())
    server = request['server']
    command = server.get('command') or []
    env = {**request['env'], **{k: str(v) for k, v in (server.get('environment') or {}).items()}}
    result = {'name': request['name'], 'command': command, 'status': None, 'own': [], 'error': None}

    async def run():
        params = StdioServerParameters(command=command[0], args=command[1:], env=env, cwd=request['cwd'])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as client:
                await client.initialize()
                tools = [tool.name for tool in (await client.list_tools()).tools]
                result['tools'] = tools
                if 'memory.connection_status' not in tools:
                    return
                _, status = await _call(client, 'memory.connection_status', {})
                result['status'] = status.get('status') if isinstance(status, dict) else str(status)[:300]
                error, bindings = await _call(client, 'memory.bindings', {})
                if not error and isinstance(bindings, dict):
                    result['own'] = [b['binding_key'] for b in bindings.get('bindings', []) if b.get('own')]
    try:
        if not command:
            raise ValueError('no command')
        asyncio.run(asyncio.wait_for(run(), 90))
    except BaseException as error:  # reported, never raised: the test reads it
        result['error'] = f'{type(error).__name__}: {error}'[:800]
    print(json.dumps(result))


if __name__ == '__main__':
    action, args = sys.argv[1], sys.argv[2:]
    if action == 'netrec':
        netrec(args[0], [int(p) for p in args[1:]])
    elif action == 'world':
        world(args[0], args[1])
    elif action == 'poll':
        poll(args[0], args[1])
    elif action == 'final':
        final(args[0], args[1])
    elif action == 'serve-check':
        serve_check(args[0])
    else:
        raise SystemExit(f'unknown action {action}')
