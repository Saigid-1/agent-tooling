"""Shared plumbing for the T11a goldens (P1 corpus, P3 audit sequences, P4 connection
profiles, P7 rollout capture): one serialisation, one byte-for-byte comparison, and the
collaborator patches the generators use. Standard library only.

A golden is generated at base by a committed generator under tests/fixtures/t11a/
(``python tests/fixtures/t11a/<generator>.py`` rewrites its JSON) and recomputed by the
test at head; the test compares the two texts byte for byte.
"""
from __future__ import annotations

import contextlib
import difflib
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
FIXTURES = TESTS / 'fixtures' / 't11a'

for _path in (str(TESTS), str(ROOT / 'packages' / 'tooling' / 'src')):
    if _path not in sys.path:
        sys.path.insert(0, _path)


def dump(value):
    """The one golden text: sorted keys, ASCII, one key per line, trailing newline."""
    return json.dumps(value, sort_keys=True, ensure_ascii=True, indent=1, allow_nan=False) + '\n'


def golden(value):
    """A golden's text. It carries no interpreter stamp: the goldens are compared on every CPython
    >= 3.11 (measured identical on 3.11, 3.12 and 3.14 once the recorders normalise the one
    version-dependent stdlib message, see generate_identity_corpus.py)."""
    return dump(value)


def load_generator(name):
    path = FIXTURES / f'{name}.py'
    spec = importlib.util.spec_from_file_location(f't11a_fixture_{name}', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_matches_golden(actual_text, golden_name, *, context=12):
    path = FIXTURES / golden_name
    expected = path.read_text(encoding='utf-8')
    if actual_text == expected:
        return
    diff = ''.join(difflib.unified_diff(expected.splitlines(True), actual_text.splitlines(True),
                                        f'{golden_name} (base)', f'{golden_name} (this tree)', n=context))
    lines = diff.splitlines()
    shown = '\n'.join(lines[:400]) + (f'\n... {len(lines) - 400} more diff lines' if len(lines) > 400 else '')
    raise AssertionError(f'{golden_name} differs from the base golden byte for byte:\n{shown}')


def write_golden(text, golden_name):
    (FIXTURES / golden_name).write_text(text, encoding='utf-8')


@contextlib.contextmanager
def patched(target, name, value):
    """Set ``target.name`` (module or object attribute) for the duration; always restored."""
    missing = object()
    old = getattr(target, name, missing)
    setattr(target, name, value)
    try:
        yield value
    finally:
        if old is missing:
            delattr(target, name)
        else:
            setattr(target, name, old)


class Probe:
    """Stands where the product compares its computed digest; records what it was compared
    with and reports equality, so the product proceeds as on a match. Works on either side
    of ``==``/``!=`` (the reflected operator is tried when ``str`` declines)."""

    def __init__(self, sink):
        self.sink = sink

    def __eq__(self, other):
        self.sink.append(other)
        return True

    def __ne__(self, other):
        self.sink.append(other)
        return False

    __hash__ = None


def outcome(call, *args, **kwargs):
    """{'value': ...} or {'error': 'Type: message'}; values made JSON-safe."""
    try:
        return {'value': jsonable(call(*args, **kwargs))}
    except Exception as error:  # the type and the message are part of the golden
        return {'error': f'{type(error).__name__}: {error}'}


def jsonable(value):
    if isinstance(value, bytes):
        return {'bytes_hex': value.hex()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, float) and value != value:
        return 'float:nan'
    if isinstance(value, float) and value in (float('inf'), float('-inf')):
        return f'float:{value}'
    if isinstance(value, Path):
        return {'path': str(value)}
    return value


@contextlib.contextmanager
def scratch(prefix):
    """A private scratch directory under TMPDIR (the OWC drive when the dispatcher sets it)."""
    with tempfile.TemporaryDirectory(prefix=prefix) as name:
        yield Path(name).resolve()


def normalise_paths(text, *roots):
    """Replace each scratch root (resolved and as given, plus its file: URI form) by <root>."""
    for index, root in enumerate(roots):
        token = '<root>' if len(roots) == 1 else f'<root{index}>'
        for form in sorted({str(root), str(Path(root).resolve()), Path(root).resolve().as_uri(),
                            Path(root).as_uri() if Path(root).is_absolute() else str(root)}, key=len, reverse=True):
            text = text.replace(form, token)
    return text


O_FLAG_NAMES = ('O_APPEND', 'O_CREAT', 'O_EXCL', 'O_TRUNC', 'O_NOFOLLOW', 'O_CLOEXEC', 'O_NONBLOCK',
                'O_NOCTTY', 'O_DIRECTORY', 'O_SYNC', 'O_DSYNC', 'O_BINARY', 'O_NOINHERIT', 'O_TEMPORARY')


def flag_names(flags):
    """Symbolic open flags: the access mode plus the named bits; unnamed bits as hex."""
    if not isinstance(flags, int):
        return flags
    names = [{os.O_RDONLY: 'O_RDONLY', os.O_WRONLY: 'O_WRONLY', os.O_RDWR: 'O_RDWR'}.get(flags & 3, 'O_?')]
    rest = flags & ~3
    for name in O_FLAG_NAMES:
        bit = getattr(os, name, 0)
        if bit and rest & bit == bit:
            names.append(name)
            rest &= ~bit
    if rest:
        names.append(hex(rest))
    return '|'.join(names)


HEX64 = re.compile(r'\b[0-9a-f]{64}\b')
