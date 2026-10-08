"""T11b instrument: a console script's ``main`` run under ``sys.addaudithook`` (Q1, Q4).

Order: docs/work/orders/T11b-leaf-behaviour.md, Q1 ("Each case runs ... under a
``sys.addaudithook`` on ``open``, ``os.mkdir`` and ``sqlite3.connect``, wrapped around the CLI's
main through ``python3 -c``") and Q4 ("the old template left in the volume is read (audit hook)").

``WRAPPER`` is the ``python3 -c`` program. Its first argument is a JSON spec:
``{"entry": "<module>:<function>", "argv0": "<console script name>", "watch": [<path prefixes>]}``
or ``{"code": "<python source>", "watch": [...]}``; the remaining arguments become the CLI's
``sys.argv[1:]``. It installs the hook before importing the product, runs the entry point, and on
the way out writes ONE line to stderr: ``T11B-AUDIT {"exit": <code>, "events": [...],
"crashed": <traceback or null>}``. Recorded events, in order:

- ``["open", <absolute path>, <mode string or null>, <flags or null>]`` (the builtin ``open``,
  ``io.open``, ``os.open``, ``io.open_code``; a relative path is joined to the cwd at the event);
- ``["os.mkdir", <absolute path>, <mode>]``;
- ``["sqlite3.connect", <database as given>]`` (a ``file:`` URI stays a URI).

Only paths under a ``watch`` prefix (or a ``file:`` URI) are kept, so the interpreter's own
imports do not flood the record. The hook only records: it never changes what the product does.

No product module is imported here; the host side only parses the line.
"""
from __future__ import annotations

import json
import os
import posixpath
import subprocess
import sys
from urllib.parse import unquote, urlsplit

MARK = 'T11B-AUDIT '
TRACEBACK = 'Traceback (most recent call last)'

WRAPPER = r'''
import json, os, sys
_spec = json.loads(sys.argv[1])
_events = []
_watch = tuple(_spec.get("watch") or ())
def _text(value):
    if isinstance(value, int):
        return None
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    try:
        value = os.fspath(value)
    except TypeError:
        return str(value)
    if not value.startswith("file:") and not value.startswith("/") and value != ":memory:":
        value = os.path.join(os.getcwd(), value)
    return value
def _keep(path):
    return path is not None and (path.startswith("file:") or path.startswith(_watch))
def _hook(event, args):
    if event == "open":
        path = _text(args[0])
        if _keep(path):
            mode = args[1] if len(args) > 1 and isinstance(args[1], str) else None
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else None
            _events.append(["open", path, mode, flags])
    elif event == "os.mkdir":
        path = _text(args[0])
        if _keep(path):
            _events.append(["os.mkdir", path, args[1] if len(args) > 1 else None])
    elif event == "sqlite3.connect":
        path = _text(args[0])
        if _keep(path):
            _events.append(["sqlite3.connect", path])
sys.addaudithook(_hook)
_code, _crash = 0, None
try:
    if "code" in _spec:
        sys.argv = ["-c", *sys.argv[2:]]
        exec(compile(_spec["code"], "<t11b-snippet>", "exec"), {"__name__": "__t11b__"})
    else:
        import importlib
        _module, _, _name = _spec["entry"].partition(":")
        sys.argv = [_spec.get("argv0", _module), *sys.argv[2:]]
        _code = getattr(importlib.import_module(_module), _name)()
except SystemExit as _exit:
    _code = _exit.code
except BaseException:
    import traceback
    _crash = traceback.format_exc()
    sys.stderr.write(_crash)
    _code = 99
if _code is None:
    _code = 0
elif not isinstance(_code, int):
    sys.stderr.write(str(_code) + "\n")
    _code = 1
sys.stdout.flush()
sys.stderr.flush()
os.write(2, ("\nT11B-AUDIT " + json.dumps({"exit": _code, "events": _events, "crashed": _crash}) + "\n").encode())
sys.exit(_code)
'''


def spec(entry=None, *, argv0=None, watch=(), code=None) -> str:
    value = {'watch': list(watch)}
    if code is not None:
        value['code'] = code
    else:
        value['entry'] = entry
        value['argv0'] = argv0 or entry.split(':')[0].rsplit('.', 1)[-1]
    return json.dumps(value)


def parse(stderr: str) -> dict | None:
    """The wrapper's record, or None when the wrapper never reached its end (it crashed itself)."""
    for line in reversed(stderr.splitlines()):
        if line.startswith(MARK):
            return json.loads(line[len(MARK):])
    return None


def without_record(stderr: str) -> str:
    return '\n'.join(line for line in stderr.splitlines() if not line.startswith(MARK))


def path_of(value: str) -> str | None:
    """The filesystem path an event names (a ``file:`` URI's path, its query dropped), normalised."""
    if value is None or value == ':memory:':
        return None
    if value.startswith('file:'):
        parts = urlsplit(value)
        value = unquote(parts.path)
        if not value:
            return None
    return posixpath.normpath(value)


def under(path: str | None, root: str, *, inclusive: bool) -> bool:
    if path is None:
        return False
    root = posixpath.normpath(root)
    return (inclusive and path == root) or path.startswith(root.rstrip('/') + '/')


def touches(record: dict, root: str) -> list[str]:
    """Every recorded open, SQLite connect or mkdir of a path below ``root`` (``root`` itself is the existing
    state_root: an ``os.mkdir`` of it that fails with EEXIST creates nothing, so it is not counted; the tree
    comparison of each test sees any directory that was created)."""
    found = []
    for event in record['events']:
        kind, raw = event[0], event[1]
        path = path_of(raw)
        if kind in ('open', 'sqlite3.connect', 'os.mkdir') and under(path, root, inclusive=False):
            found.append(f'{kind} {raw}' + (f' mode={event[2]!r}' if kind == 'open' and event[2] else '')
                         + (f' flags={event[3]:#x}' if kind == 'open' and event[3] is not None else ''))
    return found


def writes_under(record: dict, root: str) -> list[str]:
    """Every recorded mkdir at or below ``root``, and every open of a path below it for writing or creation."""
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
    found = []
    for event in record['events']:
        kind, raw = event[0], event[1]
        path = path_of(raw)
        if kind == 'os.mkdir' and under(path, root, inclusive=True):
            found.append(f'os.mkdir {raw}')
        elif kind == 'open' and under(path, root, inclusive=False):
            mode, flags = event[2], event[3]
            writing = (isinstance(flags, int) and flags & write_flags) or (mode and any(c in mode for c in 'wxa+'))
            if writing:
                found.append(f'open {raw} mode={mode!r} flags={flags!r}')
        elif kind == 'sqlite3.connect' and under(path, root, inclusive=False):
            found.append(f'sqlite3.connect {raw}')
    return found


def opened(record: dict, path: str) -> list[str]:
    """Every recorded open or SQLite connect of exactly ``path``."""
    target = posixpath.normpath(path)
    return [f'{e[0]} {e[1]}' for e in record['events']
            if e[0] in ('open', 'sqlite3.connect') and path_of(e[1]) == target]


def run_host(entry, argv, *, argv0=None, watch=(), env=None, stdin='', cwd=None, timeout=300):
    """The wrapper in a fresh host interpreter (the test's own): (CompletedProcess, record)."""
    proc = subprocess.run([sys.executable, '-c', WRAPPER, spec(entry, argv0=argv0, watch=watch), *map(str, argv)],
                          input=stdin, capture_output=True, text=True, env=env, cwd=cwd, timeout=timeout)
    return proc, parse(proc.stderr)


def describe(proc, record) -> str:
    argv = [('<t11b_audit.WRAPPER>' if a == WRAPPER else a) for a in proc.args] if isinstance(proc.args, list) else proc.args
    return (f'argv={argv!r}\nexit={proc.returncode}\n'
            f'stdout={proc.stdout[-2000:]}\nstderr={without_record(proc.stderr)[-3000:]}\n'
            f'audit events={json.dumps(record["events"])[:3000] if record else "<no record: the wrapper did not finish>"}')
