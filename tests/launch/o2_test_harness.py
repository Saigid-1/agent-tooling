"""Host harness for the O2 tests (docs/work/orders/O2-opencode-third-harness.md). Seams: tests/o2_seams.py. Lives beside tests/launch/t3_harness.py, which it builds on.

Every product interaction goes through a console script next to the test interpreter (`kp-agent-launch`,
`kp-agent-desk-registry`, `kp-agent-desk`, `kp-agent-memory`, `kp-agent-memory-queue`) or the hook command
the prepare output names, as tests/launch/t3_harness.py does. Two implementation reads are named by the
order and its census: `harness_profiles` (the packaged profiles, `PARSERS`, `validate_profile`) and
`launch_binding.CAPTURE_EVENTS`.

OpenCode itself is simulated by a fake `opencode` (FakeOpenCode): `export <session>` answers a snapshot
in the shape measured on the pinned binary (O2-S4), `--version` answers the pin. It runs `python3 -I`, so
no audit hook or PYTHONPATH reaches it: it is the export child, the one allowed reader of OpenCode's store.
The store itself (`opencode.db`, WAL) is kept beside it in OpenCode's data directory under the test HOME,
filled with the same snapshot, so a capture path that read the database would find the turn there.
"""
from __future__ import annotations

import asyncio
import copy
import itertools
import json
import os
import random
import sqlite3
import string
import subprocess
import sys
import textwrap
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest

import o2_seams as seams
from t3_harness import BIN, World, cli

PIN = '1.18.34'  # tests/image/opencode_pin.OPENCODE_VERSION; restated only for the fake's --version answer
_BASE62 = string.digits + string.ascii_uppercase + string.ascii_lowercase
_CLOCK = itertools.count(int(time.time() * 1000) * 4096)


# ------------------------------------------------------------------------------------------ the profile

def profiles():
    from kp_agent_tooling._impl.service import harness_profiles
    return harness_profiles


def opencode_profile() -> dict:
    """The packaged `opencode` profile; FAILS (the order's reason: there is no `opencode` profile) when absent."""
    found = profiles().default_profiles().get(seams.HARNESS)
    if found is None:
        pytest.fail('no packaged `opencode` harness profile (O2 R1): assets/harness-profiles.json has '
                    f'{sorted(profiles().default_profiles())}', pytrace=False)
    return found


def session_field(profile: dict) -> str:
    return profile['session_id'].get('field', 'session_id')


def parser_of(profile: dict):
    value = profile
    for key in seams.PARSER_FIELD:
        value = value.get(key) if isinstance(value, dict) else None
    return value


# ------------------------------------------------------------------------------- OpenCode-shaped data

def _id(prefix: str, *, descending=False) -> str:
    """`<prefix>_` + 12 hex of a 48-bit time counter + 14 base62 (the pinned id shape, O2-S4)."""
    value = next(_CLOCK) & ((1 << 48) - 1)
    if descending:
        value = ~value & ((1 << 48) - 1)
    return f'{prefix}_{value:012x}' + ''.join(random.choice(_BASE62) for _ in range(14))


class OpenCodeSession:
    """One OpenCode session's export snapshot, built turn by turn (the measured `export` shape)."""

    def __init__(self, workspace: Path, *, provider='o2stub', model='stub-model'):
        self.id = _id('ses', descending=True)
        self.workspace = str(Path(workspace).resolve())
        self.provider, self.model = provider, model
        self.created = int(time.time() * 1000)
        self.messages: list[dict] = []

    def _part(self, message: dict, kind: str, **fields) -> dict:
        part = {'type': kind, **fields, 'id': _id('prt'), 'sessionID': self.id, 'messageID': message['info']['id']}
        message['parts'].append(part)
        return part

    def user(self, text: str) -> dict:
        info = {'role': 'user', 'time': {'created': int(time.time() * 1000)}, 'agent': 'build',
                'model': {'providerID': self.provider, 'modelID': self.model}, 'summary': {'diffs': []},
                'id': _id('msg'), 'sessionID': self.id}
        message = {'info': info, 'parts': []}
        self._part(message, 'text', text=text)
        self.messages.append(message)
        return message

    def assistant(self, text: str | None = None, *, completed=True, reasoning: str | None = None,
                  tool: dict | None = None, synthetic: str | None = None) -> dict:
        parent = next((m['info']['id'] for m in reversed(self.messages) if m['info']['role'] == 'user'), None)
        now = int(time.time() * 1000)
        info = {'parentID': parent, 'role': 'assistant', 'mode': 'build', 'agent': 'build',
                'path': {'cwd': self.workspace, 'root': self.workspace}, 'cost': 0,
                'tokens': {'total': 2, 'input': 1, 'output': 1, 'reasoning': 0, 'cache': {'write': 0, 'read': 0}},
                'modelID': self.model, 'providerID': self.provider,
                'time': {'created': now, **({'completed': now + 1} if completed else {})},
                **({'finish': 'stop'} if completed else {}), 'id': _id('msg'), 'sessionID': self.id}
        message = {'info': info, 'parts': []}
        self._part(message, 'step-start', snapshot='o2-fixture-snapshot')
        if reasoning is not None:
            self._part(message, 'reasoning', text=reasoning, time={'start': now, 'end': now})
        if tool is not None:
            self._part(message, 'tool', callID='call_' + _id('c')[2:], tool=tool.get('tool', 'bash'),
                       state={'status': tool.get('status', 'completed'), 'input': tool.get('input', {}),
                              'output': tool.get('output', ''), 'title': tool.get('title', ''), 'metadata': {},
                              'time': {'start': now, 'end': now}})
        if synthetic is not None:
            self._part(message, 'text', text=synthetic, synthetic=True)
        if text is not None:
            self._part(message, 'text', text=text, time={'start': now, 'end': now})
        if completed:
            self._part(message, 'step-finish', reason='stop', snapshot='o2-fixture-snapshot',
                       tokens=info['tokens'], cost=0)
        self.messages.append(message)
        return message

    def complete(self, message: dict, text: str) -> None:
        """Finish an assistant message that was in progress: its streaming text part (same part id) gets
        its final text, then the step-finish part, the completion time and `finish`."""
        now = int(time.time() * 1000)
        streaming = [p for p in message['parts'] if p['type'] == 'text' and not p.get('synthetic')]
        if streaming:
            streaming[-1]['text'] = text
        else:
            self._part(message, 'text', text=text, time={'start': now, 'end': now})
        self._part(message, 'step-finish', reason='stop', snapshot='o2-fixture-snapshot',
                   tokens=message['info']['tokens'], cost=0)
        message['info']['time']['completed'] = now + 1
        message['info']['finish'] = 'stop'

    def revert_after(self, count: int) -> None:
        """OpenCode's revert: drop every message after the first `count`."""
        del self.messages[count:]

    def snapshot(self) -> dict:
        info = {'id': self.id, 'slug': 'o2-fixture', 'projectID': 'global', 'directory': self.workspace, 'path': '',
                'title': 'o2 fixture', 'agent': 'build', 'version': PIN,
                'time': {'created': self.created, 'updated': int(time.time() * 1000)}}
        return {'info': info, 'messages': copy.deepcopy(self.messages)}

    @staticmethod
    def text_parts(snapshot: dict) -> list[tuple[str, str, str]]:
        """(part id, role, text) of every text part, in order."""
        return [(p['id'], m['info']['role'], p['text']) for m in snapshot['messages'] for p in m['parts']
                if p['type'] == 'text']


# -------------------------------------------------------------------------------------- the fake binary

_FAKE = textwrap.dedent(r'''
    """Fake `opencode` (O2 TEST): export <id> from ./sessions/<id>.json, --version; every call recorded."""
    import json, os, sys, time
    HERE = os.path.dirname(os.path.abspath(__file__))
    args = sys.argv[1:]
    record = {"at": time.time(), "argv": args, "cwd": os.getcwd(), "pid": os.getpid(),
              "env": {k: v for k, v in os.environ.items()
                      if k.startswith(("OPENCODE_", "XDG_")) or k in ("HOME", "PWD")}}
    with open(os.path.join(HERE, "calls.jsonl"), "a") as out:
        out.write(json.dumps(record) + "\n")
    if args in (["--version"], ["-v"], ["version"]):
        print("__PIN__")
        raise SystemExit(0)
    VALUE_FLAGS = {"--log-level", "--format", "--port", "--hostname", "--dir", "--model", "-m", "--agent"}
    positional, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg in VALUE_FLAGS:
            skip = True
        elif not arg.startswith("-"):
            positional.append(arg)
    if not positional or positional[0] != "export":
        sys.stderr.write("fake opencode: unsupported command %r\n" % (args,))
        raise SystemExit(2)
    if len(positional) < 2:
        sys.stderr.write("fake opencode: export needs a session id (the picker needs a terminal)\n")
        raise SystemExit(1)
    session = positional[1]
    path = os.path.join(HERE, "sessions", session + ".json")
    if not os.path.isfile(path):
        sys.stderr.write("Session not found\n")
        raise SystemExit(1)
    sys.stderr.write("Exporting session: %s\n" % session)
    with open(path) as source:
        sys.stdout.write(json.dumps(json.load(source), indent=2))
''').replace('__PIN__', PIN)


class FakeOpenCode:
    """A fake `opencode` directory: put `bin` first on PATH."""

    def __init__(self, root: Path, home: Path):
        self.root = Path(root)
        self.bin = self.root / 'bin'
        self.bin.mkdir(parents=True, exist_ok=True)
        (self.root / 'sessions').mkdir(exist_ok=True)
        script = self.root / 'fake_opencode.py'
        script.write_text(_FAKE)
        executable = self.bin / 'opencode'
        executable.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{script}" "$@"\n')
        executable.chmod(0o755)
        self.calls_path = self.root / 'calls.jsonl'
        self.calls_path.touch()
        # OpenCode's default data directory under the launch's HOME (created on first serve).
        self.db = Path(home) / '.local' / 'share' / 'opencode' / 'opencode.db'

    def path_env(self, env: dict) -> dict:
        env = dict(env)
        env['PATH'] = f'{self.bin}{os.pathsep}{env.get("PATH", os.defpath)}'
        return env

    def serve(self, session: OpenCodeSession) -> dict:
        """Publish the session's current snapshot to `export` and to OpenCode's own store."""
        snapshot = session.snapshot()
        target = self.root / 'sessions' / f'{session.id}.json'
        partial = target.with_suffix('.tmp')
        partial.write_text(json.dumps(snapshot))
        partial.replace(target)
        self._store(snapshot)
        return snapshot

    def _store(self, snapshot: dict) -> None:
        # The real data directory's store (WAL, `session`/`message`/`part` with JSON `data`), written by the
        # fixture with the real connect before any audited code runs.
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS session (id TEXT PRIMARY KEY, directory TEXT, data TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, '
                       'data TEXT)')
            info = snapshot['info']
            db.execute('INSERT OR REPLACE INTO session VALUES (?,?,?)', (info['id'], info['directory'], json.dumps(info)))
            db.execute('DELETE FROM part WHERE session_id=?', (info['id'],))
            db.execute('DELETE FROM message WHERE session_id=?', (info['id'],))
            for message in snapshot['messages']:
                db.execute('INSERT INTO message VALUES (?,?,?)',
                           (message['info']['id'], info['id'], json.dumps(message['info'])))
                for part in message['parts']:
                    db.execute('INSERT INTO part VALUES (?,?,?,?)',
                               (part['id'], message['info']['id'], info['id'], json.dumps(part)))

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.calls_path.read_text().splitlines() if line.strip()]


# ------------------------------------------------------------------------------------------ the world

class OpenCodeWorld:
    """A T3 registry world with the shipped profiles, a fake `opencode` first on PATH, and launch helpers."""

    def __init__(self, tmp_path: Path, *, operator_profiles=None, fake: 'FakeOpenCode | None' = None):
        root = (Path(tmp_path) / 'world').resolve()
        self.fake = fake or FakeOpenCode(Path(tmp_path) / 'opencode-fake', root / 'home')
        self.world = World.create(root, profiles=operator_profiles)

    @property
    def profile(self) -> dict:
        return opencode_profile()

    def env(self, extra: dict | None = None) -> dict:
        env = self.fake.path_env(self.world.env())
        for name in seams.KEY_ENV_NAMES:
            env.pop(name, None)
        env.update(extra or {})
        return env

    def save_desk(self, **options) -> str:
        return self.world.save_desk(**options)

    def prepare(self, desk: str, *, harness: str = seams.HARNESS, **request) -> dict:
        body = self.world.request(harness=harness, desk_id=desk, provider='o2stub', model='stub-model', **request)
        run = cli('kp-agent-launch', '--config', self.world.operator, 'prepare', stdin=body, env=self.env())
        prepared = run.ok()
        assert isinstance(prepared, dict) and prepared.get('receipt_path'), run.describe()
        return prepared

    def launch_env(self, prepared: dict, extra: dict | None = None) -> dict:
        """The environment the plugin's hook command runs in: OpenCode's, which carries the binding's env."""
        env = self.env()
        env.update({str(k): str(v) for k, v in (prepared.get('env_additions') or {}).items()})
        env.update(extra or {})
        return env

    def hook_commands(self, prepared: dict, event: str) -> list[str]:
        files = prepared.get('files') or {}
        path = files.get(seams.HOOK_SETTINGS_FILE)
        assert path and Path(path).is_file(), \
            f'O2-S2: the prepare output names no `files.{seams.HOOK_SETTINGS_FILE}` for this launch: {prepared!r}'
        groups = (json.loads(Path(path).read_text()).get('hooks') or {}).get(event) or []
        commands = [handler['command'] for group in groups for handler in (group.get('hooks') or [])
                    if handler.get('type', 'command') == 'command']
        assert commands, f'O2-S2: no hook command for {event} in {path}: {Path(path).read_text()[:2000]}'
        return commands

    def payload(self, session: OpenCodeSession | str, event='Stop') -> dict:
        native = session.id if isinstance(session, OpenCodeSession) else session
        return {'hook_event_name': event, session_field(self.profile): native, 'cwd': str(self.world.workspace)}

    def fire(self, prepared: dict, event: str, payload: dict, *, extra_env: dict | None = None) -> list[dict]:
        """Run every command the launch configured for `event`, as the plugin would (O2-S2)."""
        runs = []
        for command in self.hook_commands(prepared, event):
            done = subprocess.run(['/bin/sh', '-c', command], input=json.dumps(payload), capture_output=True,
                                  text=True, cwd=self.world.workspace, env=self.launch_env(prepared, extra_env),
                                  timeout=300)
            runs.append({'command': command, 'code': done.returncode, 'stdout': done.stdout[-3000:],
                         'stderr': done.stderr[-3000:]})
        return runs

    def stop(self, prepared: dict, session: OpenCodeSession, event='Stop', **options) -> list[dict]:
        self.fake.serve(session)
        runs = self.fire(prepared, event, self.payload(session, event), **options)
        assert runs and all(r['code'] == 0 for r in runs), json.dumps(runs, indent=1)
        return runs

    # ------------------------------------------------------------------------------------- observing

    def bindings(self, native: str) -> list[dict]:
        return self.world.binding_for(native)

    def events(self, native: str) -> list[dict]:
        """Every captured event of the session's desk: (episode_id, event_id, role, text), via the memory MCP."""
        return memory_events(self.world.session_config(native), self.world.env())

    def queue_jobs(self, prepared: dict, native: str) -> list[dict]:
        receipt = json.loads(Path(prepared['receipt_path']).read_text())
        queue = receipt.get(seams.RECEIPT_QUEUE_FIELD)
        assert queue, f'O2-S6: the launch receipt names no `{seams.RECEIPT_QUEUE_FIELD}`: {sorted(receipt)}'
        run = cli(seams.QUEUE_SCRIPT, '--config', self.world.session_config(native), '--queue', queue, 'status',
                  env=self.world.env())
        value = run.ok()
        assert isinstance(value, dict) and isinstance(value.get('entries'), list), run.describe()
        return value['entries']


def memory_events(config: Path, env: dict) -> list[dict]:
    """All events of every episode `memory.list` shows for the config's session, with their full text."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def call(client, name, arguments):
        reply = await client.call_tool(name, arguments)
        value = reply.structuredContent
        if value is None and reply.content:
            value = json.loads(reply.content[0].text)
        assert not reply.isError, (name, arguments, value)
        return value

    async def collect():
        params = StdioServerParameters(command=str(BIN / 'kp-agent-memory'),
                                       args=['--config', str(config), 'serve'], env=env)
        events = []
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as client:
                await client.initialize()
                offset = 0
                while offset is not None:
                    page = await call(client, 'memory.list', {'kind': 'episodes', 'offset': offset, 'limit': 50})
                    for entry in page['entries']:
                        start = 0
                        while start is not None:
                            directory = await call(client, 'memory.episode_directory',
                                                   {'episode_id': entry['episode_id'], 'offset': start, 'limit': 100})
                            for item in directory['entries']:
                                text, cursor = '', 0
                                while cursor is not None:
                                    piece = await call(client, 'memory.read_event', {
                                        'episode_id': entry['episode_id'], 'event_id': item['event_id'],
                                        'start': cursor, 'length': 8000})
                                    text += piece['text']
                                    cursor = piece['next_start']
                                events.append({'episode_id': entry['episode_id'], 'event_id': item['event_id'],
                                               'role': item['role'], 'text': text})
                            start = directory['next_offset']
                    offset = page['next_offset']
        return events

    return asyncio.run(collect())


def count_text(events: list[dict], needle: str, role: str | None = None) -> int:
    return sum(1 for event in events if needle in event['text'] and (role is None or event['role'] == role))


def describe(events: list[dict]) -> str:
    return json.dumps([{k: (v[:120] if k == 'text' else v) for k, v in e.items()} for e in events], indent=1)[-6000:]
