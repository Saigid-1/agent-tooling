"""Helpers for the O2 tests (docs/work/orders/O2-opencode-third-harness.md): a fake `opencode`.

The real pinned binary runs only in the image tests (tests/image/test_o2_opencode_harness_image.py).
Here a fake `opencode` answers `--pure export <session>` from a snapshot file the test writes, in the
shape the pinned binary prints (`{info, messages: [{info, parts}]}`, measured in the image), and
records its argv, working directory and OPENCODE_* environment. Like the real export child it is a
separate process that opens OpenCode's database file in its data directory; the hook process must not.

Selected by environment (the hook passes its environment to the export child):
- O2_FAKE_SNAPSHOT: the snapshot file served (required);
- O2_FAKE_LOG: a JSON-lines record of each run;
- O2_FAKE_DATA: a data directory; the fake opens its database file there, as the real export does;
- O2_FAKE_MODE: "" (serve), "fail" (exit 1, "Session not found"), "hang" (sleep), "garbage" (not JSON).
"""
from __future__ import annotations

import json
import stat
import sys
import textwrap
from pathlib import Path

DATABASE_NAME = 'opencode' + '.db'  # the store the export child alone reads; product code never names it

_FAKE = textwrap.dedent(r'''
    import json, os, sys, time
    argv = sys.argv[1:]
    record = {'argv': argv, 'cwd': os.getcwd(),
              'env': {k: v for k, v in os.environ.items() if k.startswith('OPENCODE_')}}
    data = os.environ.get('O2_FAKE_DATA')
    if data:
        os.makedirs(data, exist_ok=True)
        with open(os.path.join(data, DATABASE), 'ab'):
            record['opened_database'] = True
    log = os.environ.get('O2_FAKE_LOG')
    if log:
        with open(log, 'a') as out:
            out.write(json.dumps(record) + '\n')
    mode = os.environ.get('O2_FAKE_MODE', '')
    if argv[:2] != ['--pure', 'export'] or len(argv) != 3:
        sys.stderr.write('unexpected invocation\n'); sys.exit(2)
    snapshot = json.load(open(os.environ['O2_FAKE_SNAPSHOT']))
    if mode == 'fail' or snapshot['info']['id'] != argv[2]:
        sys.stderr.write('Exporting session: ' + argv[2] + '\nError: Session not found: ' + argv[2] + '\n'); sys.exit(1)
    if mode == 'hang':
        time.sleep(3600)
    sys.stderr.write('Exporting session: ' + argv[2] + '\n')
    sys.stdout.write('{not json' if mode == 'garbage' else json.dumps(snapshot, indent=2))
''').replace('DATABASE', repr(DATABASE_NAME))


def fake_opencode(directory: Path) -> Path:
    """A fake `opencode` executable named exactly `opencode` in ``directory`` (put it first on PATH)."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / 'fake_opencode.py'
    script.write_text(_FAKE)
    executable = directory / 'opencode'
    executable.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return executable


class Session:
    """A growing OpenCode session snapshot (ids ascending, as OpenCode's are)."""

    def __init__(self, session_id: str, directory: Path, *, parent: str | None = None):
        self.info = {'id': session_id, 'slug': 'o2-test', 'projectID': 'global', 'directory': str(directory),
                     'title': 'o2', 'version': '1.18.34', 'time': {'created': 1, 'updated': 1}}
        if parent:
            self.info['parentID'] = parent
        self.messages: list = []
        self._next = 0

    def _id(self, prefix: str) -> str:
        self._next += 1
        return f'{prefix}_{self._next:012d}AbCdEf'

    def user(self, *texts: str, **part_extra) -> list:
        parts = [{'id': self._id('prt'), 'type': 'text', 'text': text, **part_extra} for text in texts]
        self.messages.append({'info': {'id': self._id('msg'), 'sessionID': self.info['id'], 'role': 'user',
                                       'time': {'created': 1}, 'summary': {'diffs': []}}, 'parts': parts})
        return [p['id'] for p in parts]

    def assistant(self, *parts: dict, completed: bool = True) -> list:
        built = [{'id': self._id('prt'), 'sessionID': self.info['id'], **part} for part in parts]
        info = {'id': self._id('msg'), 'sessionID': self.info['id'], 'role': 'assistant', 'time': {'created': 1},
                'finish': 'stop' if completed else None, 'modelID': 'stub-model', 'providerID': 'o2stub'}
        if completed:
            info['time']['completed'] = 2
        self.messages.append({'info': info, 'parts': built})
        return [p['id'] for p in built]

    def turn(self, question: str, answer: str) -> tuple:
        user = self.user(question)
        reply = self.assistant({'type': 'step-start'}, {'type': 'reasoning', 'text': 'thinking about ' + question},
                               {'type': 'text', 'text': answer}, {'type': 'step-finish', 'reason': 'stop'})
        return user, reply

    def write(self, path: Path) -> Path:
        path.write_text(json.dumps({'info': self.info, 'messages': self.messages}))
        return path


def text(kind: str, value: str, **extra) -> dict:
    return {'type': kind, 'text': value, **extra}


def tool(name: str, status: str, value: str) -> dict:
    state = {'status': status, 'input': {}}
    state['output' if status == 'completed' else 'error'] = value
    return {'type': 'tool', 'tool': name, 'callID': 'call_' + name, 'state': state}


def hook_payload(event: str, session: str, cwd: Path) -> dict:
    """What the board's plugin (Stop, PreCompact) and the board's terminal exit (SessionEnd) send."""
    return {'hook_event_name': event, 'session_id': session, 'cwd': str(cwd)}
