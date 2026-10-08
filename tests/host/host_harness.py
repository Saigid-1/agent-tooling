"""Public-surface harness for the T4 order (host adapter).

Contract: docs/work/orders/T4-host-adapter.md.

Every product interaction goes through a console script installed next to the
test interpreter (``kp-agent-host``, ``kp-agent-launch``,
``kp-agent-desk-registry``, ``kp-agent-desk``, ``kp-agent-memory``) or through a
stdio MCP server started exactly as a generated configuration names it. No
implementation module is imported. Registries, desks and memory checks reuse the
T3 contract harness (tests/launch/t3_harness.py), which builds them through the
T2/T3 public CLIs.

Harnesses are simulated by fake ``claude`` and ``codex`` executables placed first
on PATH. ``kp-agent-host launch`` is expected to exec them. A fake records its
argv, environment and cwd, reads hook and MCP configuration the way the real CLI
does (Claude: the project's ``.claude/settings*.json`` plus ``--settings`` /
``--mcp-config`` / ``--session-id``; Codex: ``$HOME/.codex/config.toml``, the
project's ``.codex/config.toml`` and ``-c`` overrides), then runs a plan: for each
step it appends transcript rows and runs every hook command configured for the
step's event through ``/bin/sh -c`` with the step's payload on stdin. Real Claude
or Codex CLIs are never used.

Docker mode uses a local stand-in for ``docker``: ``docker exec [-i] [-e K=V]
[-w DIR] <container> <command...>`` runs the command locally, at the same paths,
with a container environment (PATH = the console scripts, HOME and TMPDIR = a
separate container home) instead of the caller's environment. Without ``-i`` the
command gets no stdin; ``-t`` without a terminal fails as Docker does; every call
is logged. In local mode the same name is a tripwire that refuses and logs.

Readings the order leaves open are recorded where they are used and repeated in
the arm report under AMBIGUITY.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import textwrap
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

_TESTS = Path(__file__).resolve().parent.parent
for _extra in (_TESTS / 'launch', _TESTS / 'install'):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

from t12b_seams import drain  # T12b B4: the one drain helper  # noqa: E402
from t3_harness import (BIN, INSTANCE, Run, World, append_rows, claude_rows, claude_transcript,  # noqa: E402
                        codex_rollout, codex_rows, desk_binding_key, json_files_in, mcp_session, memory,
                        new_session_id, private_json, search_hits, server_env)

__all__ = ['BIN', 'INSTANCE', 'Run', 'World', 'append_rows', 'claude_rows', 'claude_transcript', 'codex_rollout',
           'codex_rows', 'desk_binding_key', 'json_files_in', 'mcp_session', 'memory', 'new_session_id',
           'private_json', 'search_hits', 'server_env']

HOST_SCHEMA = 'agent-tooling.host-adapter.v1'
CONTAINER = 't4-contract-runtime'
# The shipped profiles' receipt variable (docs/LAUNCH-BINDING.md).
RECEIPT_ENV = 'KP_AGENT_LAUNCH_RECEIPT'
CLAUDE_EVENTS = ('Stop', 'PreCompact', 'SessionEnd')
CODEX_EVENTS = ('UserPromptSubmit', 'Stop')
CLAUDE_INJECTION_FLAGS = ('--session-id', '--settings', '--mcp-config')
SHELL_VARIABLES = ('PWD', 'OLDPWD', 'SHLVL', '_')
CODEX_INJECTION_FLAGS = ('-c', '--config')


def new_desk_name() -> str:
    return 'Desk ' + uuid.uuid4().hex[:6]


# ---------------------------------------------------------------------------
# Profiles with absolute transcript roots (docker mode and P5 use these in both
# modes, so `~` never depends on which HOME the runtime has).
# ---------------------------------------------------------------------------

def absolute_profiles(home: Path) -> list:
    return [
        {'harness': 'claude', 'enabled': True, 'executable': 'claude',
         'session_id': {'strategy': 'mint', 'flag': '--session-id'},
         'mcp': {'strategy': 'config_file_flag', 'flag': '--mcp-config'},
         'hooks': {'strategy': 'settings_file_flag', 'flag': '--settings', 'events': list(CLAUDE_EVENTS)},
         'capture': {'mode': 'transcript', 'parser': 'claude-jsonl', 'root': str(home / '.claude' / 'projects')},
         'receipt_env': RECEIPT_ENV},
        {'harness': 'codex', 'enabled': True, 'executable': 'codex',
         'session_id': {'strategy': 'hook', 'field': 'session_id'},
         'mcp': {'strategy': 'config_override', 'key': 'mcp_servers'},
         'hooks': {'strategy': 'config_override', 'events': list(CODEX_EVENTS)},
         'capture': {'mode': 'transcript', 'parser': 'codex-rollout', 'root': str(home / '.codex' / 'sessions')},
         'receipt_env': RECEIPT_ENV},
    ]


# ---------------------------------------------------------------------------
# Fake harness executable
# ---------------------------------------------------------------------------

_FAKE_SOURCE = textwrap.dedent(r'''
    """Fake `claude`/`codex` for the T4 host-adapter tests; never a real CLI."""
    import json, os, re, subprocess, sys, time, tomllib

    flavor, argv = sys.argv[1], sys.argv[2:]
    record = {'flavor': flavor, 'argv': argv, 'env': dict(os.environ), 'cwd': os.getcwd(), 'pid': os.getpid(),
              'errors': [], 'runs': [], 'config': {}}


    def load_json(value, what):
        if value.lstrip().startswith('{'):
            return json.loads(value)
        if not os.path.isfile(value):
            raise ValueError(f'{what} {value!r} is neither inline JSON nor an existing file; '
                             f'the real CLI would fail on it')
        with open(value) as source:
            return json.load(source)


    def claude_config():
        singles = {'--settings': [], '--session-id': []}
        mcp, i = [], 0
        while i < len(argv):
            arg = argv[i]
            if arg in singles and i + 1 < len(argv):
                singles[arg].append(argv[i + 1]); i += 2; continue
            if arg == '--mcp-config':
                # Claude's --mcp-config is variadic: it takes every following non-flag argument.
                i += 1
                while i < len(argv) and not argv[i].startswith('-'):
                    mcp.append(argv[i]); i += 1
                continue
            if arg.startswith('--mcp-config='):
                mcp.append(arg.split('=', 1)[1]); i += 1; continue
            for flag in singles:
                if arg.startswith(flag + '='):
                    singles[flag].append(arg[len(flag) + 1:])
            i += 1
        project = [os.path.join(os.getcwd(), '.claude', name) for name in ('settings.json', 'settings.local.json')]
        layers = [path for path in project if os.path.isfile(path)] + singles['--settings']
        hooks, servers = {}, {}
        for value in layers:
            try:
                document = load_json(value, 'settings')
            except Exception as error:
                record['errors'].append(f'settings: {type(error).__name__}: {error}')
                continue
            for event, groups in (document.get('hooks') or {}).items():
                hooks.setdefault(event, []).extend(groups)
        for value in mcp:
            try:
                servers.update(load_json(value, '--mcp-config value').get('mcpServers') or {})
            except Exception as error:
                record['errors'].append(f'mcp-config: {type(error).__name__}: {error}')
        return {'settings': layers, 'mcp_configs': mcp, 'session_ids': singles['--session-id'],
                'hooks': hooks, 'mcp_servers': servers}


    def key_path(key):
        probe = tomllib.loads(f'{key} = 0')
        path = []
        while isinstance(probe, dict):
            (name, probe), = probe.items()
            path.append(name)
        return path


    def codex_config():
        layers = []
        home = os.environ.get('CODEX_HOME') or os.path.join(os.environ.get('HOME', ''), '.codex')
        for path in (os.path.join(home, 'config.toml'), os.path.join(os.getcwd(), '.codex', 'config.toml')):
            if os.path.isfile(path):
                with open(path, 'rb') as source:
                    layers.append((path, tomllib.load(source)))
        merged, overrides, i = {}, [], 0
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
            target[path[-1]] = parsed  # A later override replaces the value at its path.
        layers.append(('-c', merged))
        hooks, servers = {}, {}
        for _, layer in layers:
            for event, groups in (layer.get('hooks') or {}).items():
                if isinstance(groups, list):
                    hooks.setdefault(event, []).extend(groups)
            servers.update(layer.get('mcp_servers') or {})
        return {'overrides': overrides, 'config': merged, 'layers': [path for path, _ in layers],
                'hooks': hooks, 'mcp_servers': servers}


    def matches(group, payload):
        matcher = group.get('matcher')
        if matcher in (None, '', '*'):
            return True
        subject = payload.get('trigger') or payload.get('tool_name') or ''
        return re.fullmatch(matcher, str(subject)) is not None


    def substitute(value, session):
        if isinstance(value, str):
            return value.replace('@SESSION@', session) if session else value
        if isinstance(value, list):
            return [substitute(item, session) for item in value]
        if isinstance(value, dict):
            return {key: substitute(item, session) for key, item in value.items()}
        return value


    try:
        config = claude_config() if flavor == 'claude' else codex_config()
    except Exception as error:  # recorded, never raised: the test reads it
        config = {'hooks': {}, 'mcp_servers': {}}
        record['errors'].append(f'config: {type(error).__name__}: {error}')
    record['config'] = config

    plan_path = os.environ.get('T4_FAKE_PLAN')
    plan = json.load(open(plan_path)) if plan_path else []
    minted = (config.get('session_ids') or [None])[0] if flavor == 'claude' else None
    for step in plan:
        session = minted or step.get('session')
        step = substitute(step, session)
        write = step.get('write')
        if write:
            os.makedirs(os.path.dirname(write['path']), exist_ok=True)
            with open(write['path'], 'a') as out:
                for row in write['rows']:
                    out.write(json.dumps(row) + '\n')
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
                started = time.monotonic()
                done = subprocess.run(['/bin/sh', '-c', handler['command']], input=json.dumps(payload),
                                      capture_output=True, text=True, cwd=cwd, env=env, timeout=300)
                record['runs'].append({'step': step.get('label'), 'event': event, 'session': session,
                                       'command': handler['command'], 'payload': payload,
                                       'seconds': time.monotonic() - started, 'code': done.returncode,
                                       'stdout': done.stdout[-4000:], 'stderr': done.stderr[-4000:]})
    with open(os.environ['T4_FAKE_RECORD'], 'w') as out:
        json.dump(record, out)
''')


_DOCKER_SOURCE = textwrap.dedent(r'''
    """Local stand-in for `docker` in the T4 host-adapter tests (see host_harness.py)."""
    import json, os, re, sys, time

    CONFIG = json.loads(%(config)r)
    argv = sys.argv[1:]


    def log(entry):
        with open(CONFIG['log'], 'a') as out:
            out.write(json.dumps(entry) + '\n')


    if CONFIG['mode'] == 'tripwire':
        log({'argv': argv, 'refused': 'tripwire'})
        print('docker tripwire: this runtime mode must not call docker', file=sys.stderr)
        sys.exit(125)
    if CONFIG['mode'] == 'hang':
        log({'argv': argv, 'refused': 'hang'})
        time.sleep(60)
        sys.exit(1)
    if not argv or argv[0] != 'exec':
        log({'argv': argv, 'refused': 'unsupported'})
        print('docker stand-in: only `docker exec` is provided', file=sys.stderr)
        sys.exit(125)
    i, interactive, tty, env, workdir = 1, False, False, {}, None
    while i < len(argv):
        arg = argv[i]
        if arg in ('-i', '--interactive'):
            interactive = True
        elif arg in ('-t', '--tty'):
            tty = True
        elif re.fullmatch(r'-[it]+', arg):
            interactive = interactive or 'i' in arg
            tty = tty or 't' in arg
        elif arg in ('-e', '--env') and i + 1 < len(argv):
            i += 1
            key, sep, value = argv[i].partition('=')
            env[key] = value if sep else os.environ.get(key, '')
        elif arg.startswith('--env='):
            key, sep, value = arg[len('--env='):].partition('=')
            env[key] = value if sep else os.environ.get(key, '')
        elif arg in ('-w', '--workdir') and i + 1 < len(argv):
            i += 1
            workdir = argv[i]
        elif arg.startswith('--workdir='):
            workdir = arg[len('--workdir='):]
        elif arg in ('-u', '--user') and i + 1 < len(argv):
            i += 1
        elif arg.startswith('--user='):
            pass
        elif arg.startswith('-'):
            log({'argv': argv, 'refused': f'unsupported option {arg}'})
            print(f'docker stand-in: unsupported exec option {arg}', file=sys.stderr)
            sys.exit(125)
        else:
            break
        i += 1
    container, command = (argv[i], argv[i + 1:]) if i < len(argv) else (None, [])
    log({'argv': argv, 'container': container, 'command': command, 'interactive': interactive, 'tty': tty,
         'env': env, 'workdir': workdir})
    if container != CONFIG['container']:
        print(f'Error response from daemon: No such container: {container}', file=sys.stderr)
        sys.exit(1)
    if tty and not sys.stdin.isatty():
        print('the input device is not a TTY', file=sys.stderr)
        sys.exit(1)
    if not command:
        print('docker stand-in: exec needs a command', file=sys.stderr)
        sys.exit(125)
    child = dict(CONFIG['env'])
    child.update(env)
    if not interactive:
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
    os.chdir(workdir or CONFIG['workdir'])
    try:
        os.execvpe(command[0], command, child)
    except OSError:
        print(f'OCI runtime exec failed: exec: "{command[0]}": executable file not found in $PATH', file=sys.stderr)
        sys.exit(127)
''')


def _wrapper(path: Path, script: Path, *fixed: str) -> Path:
    """A shell wrapper, so interpreter paths with spaces survive the shebang."""
    args = ' '.join(shlex.quote(a) for a in fixed)
    path.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} {args} "$@"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def write_fake(directory: Path, flavor: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / f'.fake_{flavor}.py'
    script.write_text(_FAKE_SOURCE)
    return _wrapper(directory / flavor, script, flavor)


def write_docker(directory: Path, *, mode: str, log: Path, container: str | None = None,
                 env: dict | None = None, workdir: Path | None = None) -> Path:
    """mode: 'exec' (the stand-in), 'tripwire' (refuses) or 'hang' (sleeps 60 s)."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / f'.docker_{mode}.py'
    config = {'mode': mode, 'log': str(log), 'container': container, 'env': env or {},
              'workdir': str(workdir or directory)}
    script.write_text(_DOCKER_SOURCE % {'config': json.dumps(config)})
    return _wrapper(directory / 'docker', script)


# ---------------------------------------------------------------------------
# Payloads and plan steps
# ---------------------------------------------------------------------------

def claude_payload(session, transcript, cwd, event='Stop', **extra) -> dict:
    body = {'session_id': session, 'transcript_path': str(transcript), 'cwd': str(cwd), 'hook_event_name': event,
            'permission_mode': 'default'}
    if event == 'Stop':
        body['stop_hook_active'] = False
    elif event == 'PreCompact':
        body.update({'trigger': 'auto', 'custom_instructions': ''})
    elif event == 'SessionEnd':
        body['reason'] = 'other'
    body.update(extra)
    return body


def codex_payload(session, transcript, cwd, event='Stop', **extra) -> dict:
    body = {'session_id': session, 'transcript_path': str(transcript), 'cwd': str(cwd), 'hook_event_name': event,
            'model': 'fixture-model', 'turn_id': 'turn-1'}
    if event == 'Stop':
        body['stop_hook_active'] = False
    elif event == 'UserPromptSubmit':
        body['prompt'] = 'Begin.'
    body.update(extra)
    return body


def step(label, event, payload, *, write=None, session=None) -> dict:
    body = {'label': label, 'event': event, 'payload': payload}
    if write is not None:
        body['write'] = {'path': str(write[0]), 'rows': list(write[1])}
    if session is not None:
        body['session'] = session
    return body


def codex_meta(session, cwd, **payload) -> dict:
    body = {'id': session, 'timestamp': '2026-09-30T12:00:00.000Z', 'cwd': str(cwd), 'originator': 'codex_cli_rs',
            'cli_version': '0.0.0', 'source': 'cli'}
    body.update(payload)
    return {'timestamp': '2026-09-30T12:00:00.000Z', 'type': 'session_meta', 'payload': body}


def codex_messages(*texts, start_role='user') -> list:
    rows = []
    roles = ('user', 'assistant') if start_role == 'user' else ('assistant', 'user')
    for index, text in enumerate(texts):
        role = roles[index % 2]
        kind = 'input_text' if role == 'user' else 'output_text'
        rows.append({'timestamp': '2026-09-30T12:00:01.000Z', 'type': 'response_item',
                     'payload': {'type': 'message', 'role': role, 'content': [{'type': kind, 'text': text}]}})
    return rows


# ---------------------------------------------------------------------------
# argv inspection
# ---------------------------------------------------------------------------

def split_user_args(argv, user_args):
    """(tokens before, tokens after) the one contiguous occurrence of the user's arguments."""
    argv, user_args = list(argv), list(user_args)
    if not user_args:
        return argv, []
    n = len(user_args)
    spots = [k for k in range(len(argv) - n + 1) if argv[k:k + n] == user_args]
    assert len(spots) == 1, (f'the user arguments do not appear exactly once, in order and unchanged, '
                             f'in the executed argv\nuser={user_args!r}\nargv={argv!r}')
    k = spots[0]
    return argv[:k], argv[k + n:]


def flag_pairs(tokens, flags):
    """[(flag, value)] for tokens that are only `flag value` or `flag=value` pairs of the given flags."""
    pairs, i = [], 0
    while i < len(tokens):
        token = tokens[i]
        if token in flags and i + 1 < len(tokens):
            pairs.append((token, tokens[i + 1]))
            i += 2
            continue
        name, sep, value = token.partition('=')
        if sep and name in flags:
            pairs.append((name, value))
            i += 1
            continue
        raise AssertionError(f'executed argv carries {token!r}, which is neither a user argument nor a '
                             f'profile injection ({", ".join(flags)})\ntokens={tokens!r}')
    return pairs


def exec_parts(argv):
    """(options, container, command) of a `docker exec` argv, or None."""
    argv = list(argv)
    if not argv or Path(argv[0]).name != 'docker':
        return None
    try:
        start = argv.index('exec')
    except ValueError:
        return None
    options, i = [], start + 1
    while i < len(argv) and argv[i].startswith('-'):
        options.append(argv[i])
        if argv[i] in ('-e', '--env', '-w', '--workdir', '-u', '--user') and i + 1 < len(argv):
            options.append(argv[i + 1])
            i += 1
        i += 1
    if i >= len(argv):
        return None
    return options, argv[i], argv[i + 1:]


def _short_flag(options, letter, long) -> bool:
    return any(o == long or (re.fullmatch(r'-[A-Za-z]+', o) is not None and letter in o[1:]) for o in options)


def has_tty(options) -> bool:
    return _short_flag(options, 't', '--tty')


def has_interactive(options) -> bool:
    return _short_flag(options, 'i', '--interactive')


def path_without_docker(path: str) -> str:
    return os.pathsep.join(d for d in path.split(os.pathsep)
                           if d and not os.access(os.path.join(d, 'docker'), os.X_OK))


# ---------------------------------------------------------------------------
# Tree snapshots (for "writes only inside its spool")
# ---------------------------------------------------------------------------

def snapshot(root: Path, exclude: tuple = ()) -> dict:
    out = {}
    excluded = [Path(e) for e in exclude]

    def visit(path: Path):
        if any(path == e or path.is_relative_to(e) for e in excluded):
            return
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            out[str(path)] = ('link', os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            out[str(path)] = ('dir', stat.S_IMODE(info.st_mode))
            for child in sorted(os.listdir(path)):
                visit(path / child)
        elif stat.S_ISREG(info.st_mode):
            out[str(path)] = ('file', stat.S_IMODE(info.st_mode), info.st_size,
                              hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            out[str(path)] = ('other',)

    visit(Path(root))
    return out


def snapshot_diff(before: dict, after: dict) -> list:
    return [f'{k}: {before.get(k, "absent")} -> {after.get(k, "absent")}'
            for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)]


def tree_bytes(*roots: Path) -> dict:
    """{path: bytes} of every regular file under the roots (absent roots contribute nothing)."""
    out = {}
    for root in roots:
        if not root.exists():
            continue
        for current, _, names in os.walk(root):
            for name in names:
                path = Path(current) / name
                if path.is_file() and not path.is_symlink():
                    out[str(path)] = path.read_bytes()
    return out


# ---------------------------------------------------------------------------
# The host world
# ---------------------------------------------------------------------------

def _carries(value, payload) -> bool:
    if value == payload:
        return True
    if isinstance(value, str):
        try:
            return json.loads(value) == payload
        except ValueError:
            return False
    if isinstance(value, dict):
        return any(_carries(item, payload) for item in value.values())
    if isinstance(value, list):
        return any(_carries(item, payload) for item in value)
    return False


def row_carries(row, payload) -> bool:
    """A spool row carries the payload: as a nested object, or as JSON text."""
    return _carries(row, payload)


@dataclass
class Launched:
    hw: 'HostWorld'
    harness: str
    run: Run
    record: dict | None
    env: dict
    user_args: list
    pid: int | None = None

    def describe(self) -> str:
        record = '' if self.record is None else json.dumps(
            {k: self.record.get(k) for k in ('argv', 'errors', 'runs')}, indent=1)[-6000:]
        return self.run.describe() + '\nrecord=' + record

    def ok(self) -> 'Launched':
        assert self.run.code == 0, 'kp-agent-host launch failed\n' + self.describe()
        assert self.record is not None, ('kp-agent-host launch exited 0 without executing the harness CLI\n'
                                         + self.describe())
        assert not self.record['errors'], ('the harness CLI could not read its configuration: '
                                           f'{self.record["errors"]}\n' + self.describe())
        assert_hook_exit_codes(self.record['runs'])
        return self

    @property
    def argv(self) -> list:
        return self.record['argv']

    def runs(self, label) -> list:
        return [run for run in self.record['runs'] if run['step'] == label]

    def hook_commands(self) -> dict:
        hooks = self.record['config'].get('hooks') or {}
        out = {}
        for event, groups in hooks.items():
            if not isinstance(groups, list):
                continue
            for group in groups:
                for handler in (group or {}).get('hooks') or []:
                    if isinstance(handler, dict) and handler.get('type', 'command') == 'command':
                        out.setdefault(event, []).append(handler['command'])
        return out

    @property
    def launch_id(self) -> str:
        ids = set()
        for commands in self.hook_commands().values():
            for command in commands:
                tokens = shlex.split(command)
                for index, token in enumerate(tokens):
                    if token == '--launch' and index + 1 < len(tokens):
                        ids.add(tokens[index + 1])
                    elif token.startswith('--launch='):
                        ids.add(token.split('=', 1)[1])
        assert len(ids) == 1, f'expected one launch id in the hook commands, found {ids}: {self.hook_commands()}'
        return ids.pop()

    @property
    def native(self) -> str:
        values = [value for flag, value in self.injections() if flag == '--session-id']
        assert len(values) == 1, f'expected one --session-id, found {values}: {self.argv}'
        return values[0]

    @property
    def receipt(self) -> str:
        value = self.record['env'].get(RECEIPT_ENV)
        assert value, f'{RECEIPT_ENV} is not in the harness environment'
        return value

    @property
    def servers(self) -> dict:
        return self.record['config'].get('mcp_servers') or {}

    def injections(self):
        before, after = split_user_args(self.argv, self.user_args)
        flags = CLAUDE_INJECTION_FLAGS if self.harness == 'claude' else CODEX_INJECTION_FLAGS
        return flag_pairs(before + after, flags)


class HostWorld:
    """A T3 registry world plus an adapter config, a spool, fake CLIs and a docker stand-in."""

    def __init__(self, world: World, mode: str, config: Path, spool: Path, bin_dir: Path, tmp: Path,
                 container: str | None, docker_log: Path, container_home: Path):
        self.world, self.mode, self.config, self.spool, self.bin = world, mode, config, spool, bin_dir
        self.tmp, self.container, self.docker_log, self.container_home = tmp, container, docker_log, container_home
        self.root = world.root
        self._serial = itertools.count()

    @classmethod
    def create(cls, root: Path, *, mode: str = 'local', profiles: str = 'default',
               transcript_roots=None) -> 'HostWorld':
        """profiles: 'default' (shipped profiles) or 'absolute' (shipped shape, absolute transcript roots).

        transcript_roots: the adapter config's roots; default both native roots under the world's HOME.
        """
        assert mode in ('local', 'docker')
        root = Path(root).resolve()
        world = World.create(root, profiles=absolute_profiles(root / 'home') if profiles == 'absolute' else None)
        spool = root / 'spool'
        spool.mkdir(mode=0o700)
        spool.chmod(0o700)
        bin_dir, tmp, logs = root / 'bin', root / 'tmp', root / 'logs'
        for directory in (bin_dir, tmp, logs):
            directory.mkdir()
        container_home = root / 'container-home'
        (container_home / 'tmp').mkdir(parents=True)
        container = CONTAINER if mode == 'docker' else None
        runtime = {'mode': mode, 'config_path': str(world.operator)}
        if container:
            runtime['container'] = container
        host_dir = root / 'host'
        host_dir.mkdir(mode=0o700)
        # Reading: the file carries `schema_version` like every other configuration in this package.
        config = private_json(host_dir / 'kp-agent-host.json', {
            'schema_version': HOST_SCHEMA, 'runtime': runtime, 'spool_root': str(spool),
            'transcript_roots': [str(r) for r in (transcript_roots if transcript_roots is not None
                                                  else (world.claude_root, world.codex_root))]})
        for flavor in ('claude', 'codex'):
            write_fake(bin_dir, flavor)
        docker_log = logs / 'docker.jsonl'
        container_env = {'PATH': f'{BIN}{os.pathsep}/usr/bin{os.pathsep}/bin', 'HOME': str(container_home),
                         'TMPDIR': str(container_home / 'tmp'), 'LANG': 'C.UTF-8'}
        write_docker(bin_dir, mode='exec' if mode == 'docker' else 'tripwire', log=docker_log, container=container,
                     env=container_env, workdir=container_home)
        return cls(world, mode, config, spool, bin_dir, tmp, container, docker_log, container_home)

    # -- delegation to the T3 world ------------------------------------------
    @property
    def workspace(self) -> Path:
        return self.world.workspace

    @property
    def claude_root(self) -> Path:
        return self.world.claude_root

    @property
    def codex_root(self) -> Path:
        return self.world.codex_root

    def save_desk(self, **kwargs) -> str:
        return self.world.save_desk(**kwargs)

    def desk_name(self, desk_id) -> str:
        return self.world.desks[desk_id]

    def bindings(self) -> list:
        return self.world.bindings()

    def binding_for(self, session) -> list:
        return self.world.binding_for(session)

    # -- environment ------------------------------------------------------------
    def env(self, **extra) -> dict:
        """The launching user's environment: fakes and the docker name first, console scripts on PATH."""
        env = self.world.env()
        # Shell-maintained variables: every /bin/sh on the way (console-script trampolines, the fake's
        # wrapper) rewrites them, so they are not part of the launching environment under test.
        for name in SHELL_VARIABLES:
            env.pop(name, None)
        env['PATH'] = os.pathsep.join([str(self.bin), str(BIN), os.environ.get('PATH', '/usr/bin:/bin')])
        env['TMPDIR'] = str(self.tmp)
        env.update({k: str(v) for k, v in extra.items()})
        return env

    # -- kp-agent-host ------------------------------------------------------------
    def host_argv(self, *args) -> list:
        # Reading: `kp-agent-host` takes the adapter config as a global `--config` before the action,
        # like kp-agent-launch / kp-agent-desk-registry / kp-agent-memory.
        executable = BIN / 'kp-agent-host'
        assert executable.exists(), f'console script kp-agent-host is not installed next to {sys.executable}'
        return [str(executable), '--config', str(self.config), *[str(a) for a in args]]

    def host(self, *args, cwd=None, stdin=None, env=None, timeout=300) -> Run:
        argv = self.host_argv(*args)
        body = None if stdin is None else (stdin if isinstance(stdin, str) else json.dumps(stdin))
        done = subprocess.run(argv, input=body, capture_output=True, text=True, timeout=timeout,
                              cwd=cwd or self.workspace, env=env or self.env())
        return Run(argv, done.returncode, done.stdout, done.stderr)

    def launch(self, harness, desk, *, provider=None, model=None, user_args=(), plan=(), env_extra=None,
               cwd=None) -> Launched:
        serial = next(self._serial)
        work = self.root / 'records' / f'launch-{serial}'
        work.mkdir(parents=True)
        plan_path, record_path = work / 'plan.json', work / 'record.json'
        plan_path.write_text(json.dumps(list(plan)))
        env = self.env(T4_FAKE_PLAN=plan_path, T4_FAKE_RECORD=record_path, **(env_extra or {}))
        args = ['launch', harness, '--desk', desk]
        if provider is not None:
            args += ['--provider', provider]
        if model is not None:
            args += ['--model', model]
        if user_args:
            args += ['--', *user_args]
        argv = self.host_argv(*args)
        proc = subprocess.Popen(argv, cwd=cwd or self.workspace, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = proc.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        record = json.loads(record_path.read_text()) if record_path.exists() else None
        return Launched(self, harness, Run(argv, proc.returncode, out, err), record, env, list(user_args), proc.pid)

    def native_cli(self, flavor, *, cwd, plan=(), argv=()) -> dict:
        """Start a fake CLI the way a user would, without the adapter (no injections)."""
        serial = next(self._serial)
        work = self.root / 'records' / f'native-{serial}'
        work.mkdir(parents=True)
        plan_path, record_path = work / 'plan.json', work / 'record.json'
        plan_path.write_text(json.dumps(list(plan)))
        env = self.env(T4_FAKE_PLAN=plan_path, T4_FAKE_RECORD=record_path)
        done = subprocess.run([str(self.bin / flavor), *argv], cwd=cwd, env=env, capture_output=True, text=True,
                              timeout=600)
        assert done.returncode == 0, f'fake {flavor} failed: {done.stderr[-3000:]}'
        record = json.loads(record_path.read_text())
        assert not record['errors'], record['errors']
        assert_hook_exit_codes(record['runs'])
        return record

    # -- hooks and spool ---------------------------------------------------------
    @staticmethod
    def run_hook(command, payload, *, env, cwd, raw: bytes | None = None, timeout=60):
        """Run one configured hook command as the harness does; returns (code, stdout, stderr, seconds)."""
        body = raw if raw is not None else json.dumps(payload).encode()
        started = time.perf_counter()
        done = subprocess.run(['/bin/sh', '-c', command], input=body, capture_output=True, cwd=cwd, env=env,
                              timeout=timeout)
        seconds = time.perf_counter() - started
        return done.returncode, done.stdout.decode(errors='replace'), done.stderr.decode(errors='replace'), seconds

    def spool_file(self, launch_id) -> Path:
        return self.spool / launch_id / 'events.jsonl'

    def spool_rows(self, launch_id) -> list:
        path = self.spool_file(launch_id)
        listing = sorted(str(p.relative_to(self.spool)) for p in self.spool.rglob('*'))
        assert path.is_file(), f'no spool file at {path}; spool holds {listing}'
        data = path.read_bytes()
        assert not data or data.endswith(b'\n'), f'{path} ends in a partial line'
        rows = []
        for line in data.splitlines():
            value = json.loads(line)
            assert isinstance(value, dict), f'spool row is not a JSON object: {line[:200]!r}'
            rows.append(value)
        return rows

    def docker_calls(self) -> list:
        if not self.docker_log.exists():
            return []
        return [json.loads(line) for line in self.docker_log.read_text().splitlines() if line.strip()]

    # -- ingestion ---------------------------------------------------------------
    def ingest_argv(self) -> list:
        tail = ['kp-agent-launch', 'ingest-spool', '--root', str(self.spool)]
        if self.mode == 'docker':
            # Inside the runtime (the stand-in shares paths, so the container spool path is the host path).
            return [str(self.bin / 'docker'), 'exec', self.container, *tail]
        return [str(BIN / tail[0]), *tail[1:]]

    def ingest(self, until, *, settle: float = 0.0, timeout: float = 180.0, label='ingest') -> int | None:
        """Run the ingestion watcher until `until()` holds (then `settle` more seconds), and stop it.

        Reading: `ingest-spool` is a watcher; it is stopped with SIGTERM once the expected effect is
        visible. A watcher that exits on its own is accepted when the effect is visible.
        """
        serial = next(self._serial)
        out_path, err_path = self.root / 'logs' / f'{label}-{serial}.out', self.root / 'logs' / f'{label}-{serial}.err'
        with out_path.open('wb') as out, err_path.open('wb') as err:
            proc = subprocess.Popen(self.ingest_argv(), cwd=self.root, env=self.env(), stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while True:
                code = proc.poll()
                if until():
                    break
                if code is not None:
                    if until():
                        break
                    raise AssertionError(f'kp-agent-launch ingest-spool exited {code} before the expected effect\n'
                                         f'stderr: {err_path.read_text(errors="replace")[-3000:]}')
                if time.monotonic() > deadline:
                    raise AssertionError(f'ingest-spool did not produce the expected effect within {timeout}s\n'
                                         f'stderr: {err_path.read_text(errors="replace")[-3000:]}')
                time.sleep(0.5)
            if settle and proc.poll() is None:
                time.sleep(settle)
        finally:
            _stop(proc)
        return proc.returncode

    # -- memory observations --------------------------------------------------------
    def hits(self, session, marker) -> int:
        drain(self.world.operator)  # T12b B4: the indexer, not the hook, indexes
        _, replies = memory(self.world, session, calls=[('memory.search', {'query': marker})])
        return len(search_hits(replies[0], marker))

    def hits_now(self, session, marker) -> int:
        """Tolerant probe for polling while ingestion runs."""
        try:
            return self.hits(session, marker)
        except Exception:
            return 0

    def bound_now(self, session) -> bool:
        run = self.world.registry('list')
        if run.code != 0 or not isinstance(run.json, dict):
            return False
        return any(b.get('native_session_id') == session for b in run.json.get('bindings') or [])

    def episodes(self, session) -> int:
        _, replies = memory(self.world, session, calls=[('memory.list', {'kind': 'episodes'})])
        assert not replies[0].is_error, replies[0].value
        return replies[0].value['total']

    def ready(self, session) -> bool:
        _, replies = memory(self.world, session, calls=[('memory.connection_status', {})])
        return (not replies[0].is_error) and replies[0].value.get('status') == 'ready'

    def own_keys(self, session) -> list:
        _, replies = memory(self.world, session, calls=[('memory.bindings', {})])
        assert not replies[0].is_error, replies[0].value
        return [b['binding_key'] for b in replies[0].value['bindings'] if b.get('own')]

    def desk_key(self, desk_id) -> str:
        return desk_binding_key(self.world, desk_id)

    # -- transcripts ------------------------------------------------------------------
    def claude_turn(self, label, *texts, event='Stop', session='@SESSION@', cwd=None, root=None, write=True,
                    **extra) -> dict:
        """A Claude plan step: append visible rows to the session transcript, then fire `event`."""
        cwd = Path(cwd or self.workspace)
        transcript = claude_transcript(root or self.claude_root, cwd, session)
        rows = claude_rows(session, cwd, *texts) if texts else []
        payload = claude_payload(session, transcript, cwd, event, **extra)
        return step(label, event, payload, write=(transcript, rows) if write and rows else None,
                    session=None if session == '@SESSION@' else session)

    def codex_turn(self, label, session, *texts, event='Stop', meta=None, cwd=None, transcript=None,
                   start_role=None, **extra) -> dict:
        """A Codex plan step: append rows (a leading `session_meta` when `meta` is given), then fire `event`.

        Texts alternate roles, starting with the user on a prompt event and the assistant otherwise.
        """
        cwd = Path(cwd or self.workspace)
        transcript = Path(transcript or codex_rollout(self.codex_root, session))
        start_role = start_role or ('user' if event == 'UserPromptSubmit' or meta is not None else 'assistant')
        rows = ([meta] if meta is not None else []) + codex_messages(*texts, start_role=start_role)
        payload = codex_payload(session, transcript, cwd, event, **extra)
        return step(label, event, payload, write=(transcript, rows) if rows else None, session=session)


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=15)


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------

def invokes_host_adapter(command: str) -> bool:
    """The command runs `kp-agent-host` (the console script, or its module named by the order's write scope)."""
    tokens = shlex.split(command)
    if 'kp-agent-host' in {Path(token).name for token in tokens}:
        return True
    return any(token == '-m' and tokens[i + 1:i + 2] == ['kp_agent_tooling.host_cli'] for i, token in enumerate(tokens))


def assert_hook_exit_codes(runs) -> None:
    """Coordinator clarification (2026-10-01): an adapter hook exits 0 or 1, never 2 (2 blocks the harness)."""
    blocking = [(run['step'], run['event'], run['code']) for run in runs
                if invokes_host_adapter(run['command']) and run['code'] not in (0, 1)]
    assert not blocking, f'adapter hooks exited with a code other than 0 or 1: {blocking}'


def assert_hooks_call_the_adapter(launched: Launched, events) -> dict:
    """Every profile event runs `kp-agent-host ... hook --launch <id>`, never the synchronous T3 hook or docker."""
    commands = launched.hook_commands()
    missing = [event for event in events if not commands.get(event)]
    assert not missing, f'no hook configured for {missing}: {commands}'
    for event in events:
        for command in commands[event]:
            tokens = shlex.split(command)
            names = {Path(token).name for token in tokens}
            assert invokes_host_adapter(command) and 'hook' in tokens and any(
                t == '--launch' or t.startswith('--launch=') for t in tokens), (
                f'{event} hook does not call `kp-agent-host hook --launch <id>`: {command}')
            assert '--receipt' not in tokens and 'docker' not in names, (
                f'{event} hook still runs the synchronous launch hook or docker: {command}')
    launched.launch_id  # exactly one launch id across commands
    return commands


def assert_user_args_and_env_unchanged(launched: Launched) -> None:
    """The adapter exec'd the CLI (same process) with the user's arguments and environment unchanged."""
    assert launched.record.get('pid') == launched.pid, (
        'kp-agent-host ran the CLI as a separate process instead of exec-ing it '
        f'(adapter pid {launched.pid}, CLI pid {launched.record.get("pid")})')
    before, after = split_user_args(launched.argv, launched.user_args)
    launched.injections()  # every other token is a profile injection
    changed = {k: (v, launched.record['env'].get(k)) for k, v in launched.env.items()
               if launched.record['env'].get(k) != v}
    assert not changed, f'the launching environment was altered or dropped: {changed}'
    assert Path(launched.record['cwd']).resolve() == Path(launched.hw.workspace).resolve(), (
        f'the CLI did not run in the launching directory: {launched.record["cwd"]}')


def memory_servers(launched: Launched) -> dict:
    """Generated MCP servers that serve desk memory (local: name a memory config; docker: docker exec)."""
    found = {}
    for name, server in launched.servers.items():
        argv = [server.get('command', ''), *server.get('args', [])]
        if exec_parts(argv) is not None:
            found[name] = server
            continue
        env = server_env(launched.env, server)
        configs = [v for _, v in json_files_in([*server.get('args', []), *env.values()])
                   if isinstance(v, dict) and 'provider_session_id' in v]
        if configs:
            found[name] = server
    return found


def call_server(launched: Launched, server, calls):
    env = server_env(launched.env, server)
    _, replies = mcp_session(server['command'], server.get('args', []), env, calls=calls, list_tools=False)
    return replies
