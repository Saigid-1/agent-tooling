"""P3 golden: the os call sequence of every private-file helper (T11a order, P3).

Generated at base by:

    python tests/fixtures/t11a/generate_private_file_sequences.py

which rewrites tests/fixtures/t11a/private_file_sequences.json;
tests/test_t11a_p3_private_files.py replays the same inputs in this tree and compares byte for
byte.

Each of the census's 30 private-file helpers (t11a_census.CENSUS_PRIVATE_HELPERS) is called
through its old name (P9) on fixed inputs in a fresh scratch directory. ``sys.addaudithook``
records, in order, every ``open``, ``os.chmod``, ``os.rename``, ``os.link``, ``os.mkdir`` and
``os.remove`` event under that directory (descriptor-only events as ``<fd>``), with flags
spelled symbolically and modes in octal, interleaved with every ``os.fsync`` and
``fcntl.flock`` call (which raise no audit event; recorded by wrapping the attributes). The golden also holds the call's outcome (value, or
exception type and message) and the directory left behind (type, mode, size, content digest).

Normalised: the scratch directory (``<case>``) and every name the helper itself invents (a
temporary file), numbered by first appearance (``<tmp1>``...).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # tests/

from t11a_golden import flag_names, golden, jsonable, scratch, write_golden  # noqa: E402

# The core part runs with the core alone (the CI core job); the ops part needs extensions/ops.
GOLDENS = {'core': 'private_file_sequences.json', 'ops': 'private_file_sequences_ops.json'}
EVENTS = ('open', 'os.chmod', 'os.rename', 'os.link', 'os.mkdir', 'os.remove')


def _audit(event, args):
    """One process-wide hook (audit hooks cannot be removed); the active case lives on ``sys`` so
    every load of this module (each test loads it afresh) records through the same hook."""
    recorder = getattr(sys, '_t11a_p3_active', None)
    if recorder is not None and event in ('open', 'os.chmod', 'os.rename', 'os.link', 'os.mkdir', 'os.remove'):
        recorder.raw.append((event, args))


if not getattr(sys, '_t11a_p3_hook', False):
    sys.addaudithook(_audit)
    sys._t11a_p3_hook = True


class Case:
    def __init__(self, root):
        self.root = root
        self.raw = []
        self.known = set()

    def remember(self):
        self.known.update(KNOWN_NAMES)
        for path in self.root.rglob('*'):
            self.known.add(path.name)

    @contextmanager
    def recording(self, *names):
        """Audit events, plus ``os.fsync`` and ``fcntl.flock`` (which raise none), in call order.
        Both are looked up by attribute at call time, as the order requires of the leaf."""
        import fcntl
        self.remember()
        self.known.update(names)
        real_fsync, real_flock = os.fsync, fcntl.flock

        def fsync(fd):
            self.raw.append(('os.fsync', (fd,)))
            return real_fsync(fd)

        def flock(fd, operation):
            names = [n for n in ('LOCK_SH', 'LOCK_EX', 'LOCK_UN', 'LOCK_NB') if operation & getattr(fcntl, n)]
            self.raw.append(('fcntl.flock', ('|'.join(names),)))
            return real_flock(fd, operation)

        os.fsync, fcntl.flock = fsync, flock
        sys._t11a_p3_active = self
        try:
            yield
        finally:
            sys._t11a_p3_active = None
            os.fsync, fcntl.flock = real_fsync, real_flock

    def _path(self, value, tokens):
        if isinstance(value, int):
            return '<fd>'
        if isinstance(value, bytes):
            value = os.fsdecode(value)
        text = os.fspath(value)
        if not os.path.isabs(text):
            return text
        try:
            relative = Path(text).relative_to(self.root)
        except ValueError:
            try:
                relative = Path(os.path.realpath(text)).relative_to(self.root)
            except ValueError:
                return None  # outside the case: not recorded
        parts = []
        for part in relative.parts:
            if part not in self.known and not DERIVED_NAME.match(part):
                tokens.setdefault(part, f'<tmp{len(tokens) + 1}>')
                part = tokens[part]
            parts.append(part)
        return '/'.join(['<case>'] + parts)

    def events(self, tokens):
        out = []
        for event, args in self.raw:
            if event == 'open':
                path = self._path(args[0], tokens)
                if path is None:
                    continue
                out.append([event, path, args[1], flag_names(args[2]) if len(args) > 2 else None])
            elif event in ('os.chmod', 'os.mkdir'):
                path = self._path(args[0], tokens)
                if path is None:
                    continue
                out.append([event, path, oct(args[1]) if isinstance(args[1], int) else args[1]])
            elif event in ('os.rename', 'os.link'):
                paths = [self._path(a, tokens) for a in args[:2]]
                if paths == [None, None]:
                    continue
                out.append([event] + paths)
            elif event == 'os.remove':
                path = self._path(args[0], tokens)
                if path is not None:
                    out.append([event, path])
            elif event == 'os.fsync':
                out.append([event, '<fd>'])
            elif event == 'fcntl.flock':
                out.append([event, args[0]])
        return out

    def tree(self, tokens):
        """Each entry left behind: a symlink as its kind and target only (its own permission
        bits carry no meaning and differ by OS: 0o755 on macOS, 0o777 on Linux); a directory or
        regular file with its mode (deterministic: the build pins the umask); a file also with
        its size and content digest; anything else as its kind only."""
        rows = []
        for path in sorted(self.root.rglob('*')):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                rows.append([self._path(path, tokens), 'link',
                             self._path(target if os.path.isabs(target) else path.parent / target, tokens)])
                continue
            if stat.S_ISDIR(info.st_mode):
                rows.append([self._path(path, tokens), 'dir', oct(stat.S_IMODE(info.st_mode))])
            elif stat.S_ISREG(info.st_mode):
                rows.append([self._path(path, tokens), 'file', oct(stat.S_IMODE(info.st_mode)), info.st_size,
                             hashlib.sha256(path.read_bytes()).hexdigest()])
            else:
                rows.append([self._path(path, tokens), 'other'])
        return rows

    def normalise_text(self, text):
        return text.replace(str(self.root), '<case>')


def run(case, call):
    with case.recording():
        try:
            value = call()
            result = {'value': jsonable(value)}
        except Exception as error:
            result = {'error': f'{type(error).__name__}: {error}'}
    tokens = {}
    events = case.events(tokens)
    tree = case.tree(tokens)
    text = case.normalise_text(json.dumps(result, ensure_ascii=False, sort_keys=True))
    for name, token in tokens.items():
        text = text.replace(name, token)
    return {'events': events, 'outcome': json.loads(text), 'tree': tree}


def private_dir(root, name='private', mode=0o700):
    path = root / name
    path.mkdir(mode=mode)
    path.chmod(mode)
    return path


def private_file(root, name, data, mode=0o600):
    path = root / name
    path.write_bytes(data)
    path.chmod(mode)
    return path


DOC = {'b': 1, 'a': ['é', None, {'nested': True}]}
# Every name these cases pass to a helper; any other name a helper creates is its own invention
# (a temporary file) and is numbered. Names derived from content digests are deterministic.
KNOWN_NAMES = {'private', 'open', 'roster', 'state', 'launches', 'record.json', 'config.toml', 'desk.json',
               'output.bin', 'compose.yaml', 'empty', 'index.json', 'blob', 'elsewhere', 'status.json',
               'roles.json', 'launch.lock', 'operator.json', 'hooks.json', 'key', 'snap', 'rollout.jsonl',
               'rollout.txt', 'spool-ingest.sqlite3.lock'}
DERIVED_NAME = re.compile(r'^(search|manifest)-[0-9a-f]{64}\.json$')
RAW = 'payload é ☃\n'.encode()


def entered(manager):
    with manager:
        return 'entered'


def cases(part='core'):
    """(helper, case) -> setup(root) returning a zero-argument call."""
    if part == 'ops':
        return ops_cases()
    from kp_agent_tooling._impl.service import launch_binding as lb, host_adapter as ha
    from kp_agent_tooling._impl.service import desk_catalog_setup as dcs, model_gateway as mg
    from kp_agent_tooling._impl.service import desk_memory_runtime as dmr, desk_registry as dr
    from kp_agent_tooling._impl.service import native_history_import as nhi, spool_ingest as si
    from kp_agent_tooling._impl import runtime_install as ri, scip_navigation as sn, semantic_index as smi
    from kp_agent_tooling._impl import navigation_snapshot as ns, navigation_search_pages as nsp
    from kp_agent_tooling import refresh_cli

    def new(name):
        return lambda root: root / name

    table = {}

    def case(helper, label):
        def register(setup):
            table[(helper, label)] = setup
            return setup
        return register

    # -- write helpers
    for helper, function in (('launch_binding.write_private', lb.write_private),
                             ('launch_binding._write_once', lb._write_once),
                             ('host_adapter._write_new', ha._write_new)):
        table[(helper, 'new file')] = lambda root, f=function: (lambda d=private_dir(root): f(d / 'record.json', DOC))
        table[(helper, 'existing file')] = lambda root, f=function: (
            lambda p=private_file(private_dir(root), 'record.json', json.dumps(DOC).encode()): f(p, DOC))
    table[('launch_binding._write_once', 'existing, different value')] = lambda root: (
        lambda p=private_file(private_dir(root), 'record.json', b'{"other": 1}'): lb._write_once(p, DOC))
    for mode in (0o600, 0o644):
        table[('host_adapter._replace', f'new file at {oct(mode)}')] = lambda root, mode=mode: (
            lambda d=private_dir(root): ha._replace(d / 'config.toml', RAW, mode))
        table[('host_adapter._replace', f'existing file at {oct(mode)}')] = lambda root, mode=mode: (
            lambda p=private_file(private_dir(root), 'config.toml', b'old', 0o640): ha._replace(p, RAW, mode))
    table[('desk_catalog_setup._write_private_json', 'new file')] = lambda root: (
        lambda d=private_dir(root): dcs._write_private_json(d / 'desk.json', DOC))
    table[('desk_catalog_setup._write_private_json', 'existing, same document')] = lambda root: (
        lambda p=private_file(private_dir(root), 'desk.json', (json.dumps(DOC, indent=2, sort_keys=True) + '\n').encode()):
        dcs._write_private_json(p, DOC))
    table[('desk_catalog_setup._write_private_json', 'existing, different document')] = lambda root: (
        lambda p=private_file(private_dir(root), 'desk.json', b'{"x": 2}'): dcs._write_private_json(p, DOC))
    table[('desk_catalog_setup._write_private_json', 'parent not private')] = lambda root: (
        lambda d=private_dir(root, 'open', 0o755): dcs._write_private_json(d / 'desk.json', DOC))
    table[('model_gateway._write_private', 'new file')] = lambda root: (
        lambda d=private_dir(root): mg._write_private(d / 'output.bin', RAW))
    table[('model_gateway._write_private', 'existing file')] = lambda root: (
        lambda p=private_file(private_dir(root), 'output.bin', b'old'): mg._write_private(p, RAW))
    table[('runtime_install._write', 'new file')] = lambda root: (
        lambda d=private_dir(root): ri._write(d / 'compose.yaml', RAW))
    table[('runtime_install._write', 'replace existing')] = lambda root: (
        lambda p=private_file(private_dir(root), 'compose.yaml', b'old', 0o644): ri._write(p, RAW))
    table[('runtime_install._placeholder', 'new file')] = lambda root: (
        lambda d=private_dir(root): ri._placeholder(d / 'empty'))
    table[('runtime_install._placeholder', 'existing file')] = lambda root: (
        lambda p=private_file(private_dir(root), 'empty', b'x'): ri._placeholder(p))
    table[('scip_navigation._atomic', 'new file')] = lambda root: (
        lambda d=private_dir(root): sn._atomic(d / 'index.json', RAW))
    table[('scip_navigation._atomic', 'replace existing')] = lambda root: (
        lambda p=private_file(private_dir(root), 'index.json', b'old'): sn._atomic(p, RAW))
    for helper, function in (('semantic_index._write_once', smi._write_once),
                             ('navigation_snapshot._write_once', ns._write_once)):
        table[(helper, 'new file')] = lambda root, f=function: (lambda d=private_dir(root): f(d / 'blob', RAW))
        table[(helper, 'existing, same bytes')] = lambda root, f=function: (
            lambda p=private_file(private_dir(root), 'blob', RAW): f(p, RAW))
        table[(helper, 'existing, different bytes')] = lambda root, f=function: (
            lambda p=private_file(private_dir(root), 'blob', b'other'): f(p, RAW))
    table[('navigation_snapshot._write_once', 'symlink target')] = lambda root: (
        lambda d=private_dir(root): (os.symlink(d / 'elsewhere', d / 'blob'), ns._write_once(d / 'blob', RAW))[1])
    table[('refresh_cli.atomic', 'new file, parent created')] = lambda root: (
        lambda: refresh_cli.atomic(root / 'state' / 'status.json', DOC))
    table[('refresh_cli.atomic', 'replace existing')] = lambda root: (
        lambda p=private_file(private_dir(root), 'status.json', b'{}'): refresh_cli.atomic(p, DOC))

    def roster(root):
        directory = private_dir(root, 'roster')
        roles = dr.default_roster()['roles']
        return lambda: dr.RoleRoster(directory / 'roles.json')._write(roles, 7)
    table[('desk_registry.RoleRoster._write', 'new roster')] = roster

    def roster_existing(root):
        directory = private_dir(root, 'roster')
        private_file(directory, 'roles.json', b'{}', 0o644)
        roles = dr.default_roster()['roles']
        return lambda: dr.RoleRoster(directory / 'roles.json')._write(roles, 8)
    table[('desk_registry.RoleRoster._write', 'replace existing')] = roster_existing
    table[('navigation_search_pages._save', 'new cursor')] = lambda root: (
        lambda d=private_dir(root): nsp._save(d, {'query': 'é', 'offset': 3}))

    def saved_twice(root):
        directory = private_dir(root)
        nsp._save(directory, {'query': 'é', 'offset': 3})
        return lambda: nsp._save(directory, {'query': 'é', 'offset': 3})
    table[('navigation_search_pages._save', 'existing cursor')] = saved_twice
    manifest = {'repo_key': 'répo', 'source_revision': 'a' * 40, 'files': ['é.py']}
    table[('navigation_search_pages._store_manifest', 'new manifest')] = lambda root: (
        lambda d=private_dir(root): nsp._store_manifest(d, manifest))

    def stored_twice(root):
        directory = private_dir(root)
        nsp._store_manifest(directory, manifest)
        return lambda: nsp._store_manifest(directory, manifest)
    table[('navigation_search_pages._store_manifest', 'existing manifest')] = stored_twice

    # -- directory and lock helpers
    table[('launch_binding.private_dir', 'new directory')] = lambda root: (lambda: lb.private_dir(root / 'launches'))
    table[('launch_binding.private_dir', 'existing private')] = lambda root: (
        lambda p=private_dir(root, 'launches'): lb.private_dir(p))
    table[('launch_binding.private_dir', 'existing 0755')] = lambda root: (
        lambda p=private_dir(root, 'launches', 0o755): lb.private_dir(p))
    table[('launch_binding._locked', 'new lock file')] = lambda root: (
        lambda d=private_dir(root): entered(lb._locked(d / 'launch.lock')))
    table[('launch_binding._locked', 'existing lock file')] = lambda root: (
        lambda p=private_file(private_dir(root), 'launch.lock', b''): entered(lb._locked(p)))
    table[('host_adapter._private_dir', 'private')] = lambda root: (lambda p=private_dir(root): ha._private_dir(p))
    table[('host_adapter._private_dir', '0755')] = lambda root: (
        lambda p=private_dir(root, 'open', 0o755): ha._private_dir(p))
    table[('desk_catalog_setup._private_parent', 'private parent')] = lambda root: (
        lambda p=private_dir(root): dcs._private_parent(p / 'desk.json'))
    table[('desk_catalog_setup._private_parent', '0755 parent')] = lambda root: (
        lambda p=private_dir(root, 'open', 0o755): dcs._private_parent(p / 'desk.json'))
    table[('desk_registry.RoleRoster._locked', 'roster directory')] = lambda root: (
        lambda d=private_dir(root, 'roster'): entered(dr.RoleRoster(d / 'roles.json')._locked()))
    table[('spool_ingest.Cursors.locked', 'new lock')] = lambda root: (
        lambda d=private_dir(root, 'state'): entered(si.Cursors(d).locked()))

    # -- read and validate helpers
    # desk_memory_runtime._private_json is not called by its old name: at base private_json (no
    # memo) is exactly it (identical sequences, outcomes and trees in the base golden), and P9 keeps
    # an old name only where a test or another repository imports it; this one had neither.
    for helper, function in (('desk_memory_runtime.private_json', dmr.private_json),
                             ('desk_registry._private_json', dr._private_json)):
        table[(helper, 'private file')] = lambda root, f=function: (
            lambda p=private_file(private_dir(root), 'operator.json', json.dumps(DOC).encode()): f(p))
        table[(helper, '0644 file')] = lambda root, f=function: (
            lambda p=private_file(private_dir(root), 'operator.json', b'{}', 0o644): f(p))
        table[(helper, 'relative path')] = lambda root, f=function: (lambda: f('operator.json'))
    table[('host_adapter._owned', 'file')] = lambda root: (
        lambda p=private_file(private_dir(root), 'hooks.json', b'{}'): ha._owned(p, 'file').st_size)
    table[('host_adapter._owned', 'group-writable file')] = lambda root: (
        lambda p=private_file(private_dir(root), 'hooks.json', b'{}', 0o620): ha._owned(p, 'file'))
    table[('host_adapter._read_json', 'within bound')] = lambda root: (
        lambda p=private_file(private_dir(root), 'hooks.json', json.dumps(DOC).encode()): ha._read_json(p))
    table[('host_adapter._read_json', 'over bound')] = lambda root: (
        lambda p=private_file(private_dir(root), 'hooks.json', json.dumps(DOC).encode()): ha._read_json(p, 4))
    table[('model_gateway.read_api_key', 'private key file')] = lambda root: (
        lambda p=private_file(private_dir(root), 'key', b'sk-test-not-a-secret\n'): len(mg.read_api_key(p)))
    table[('model_gateway.read_api_key', '0644 key file')] = lambda root: (
        lambda p=private_file(private_dir(root), 'key', b'sk-test\n', 0o644): mg.read_api_key(p))
    table[('model_gateway.read_api_key', 'missing')] = lambda root: (
        lambda d=private_dir(root): mg.read_api_key(d / 'key'))
    table[('navigation_snapshot._read_regular', 'regular file')] = lambda root: (
        lambda p=private_file(private_dir(root), 'snap', RAW): ns._read_regular(p))
    table[('navigation_snapshot._read_regular', 'over limit')] = lambda root: (
        lambda p=private_file(private_dir(root), 'snap', RAW): ns._read_regular(p, 4))
    table[('native_history_import._regular', 'jsonl file')] = lambda root: (
        lambda p=private_file(private_dir(root), 'rollout.jsonl', b'{}\n'): ns_name(nhi._regular(p)))
    table[('native_history_import._regular', 'not jsonl')] = lambda root: (
        lambda p=private_file(private_dir(root), 'rollout.txt', b'{}\n'): nhi._regular(p))
    return table


def ops_cases():
    from kp_agent_tooling_ops import knowledge_publish_cli as kpc
    return {
        ('knowledge_publish_cli._write', 'new file'): lambda root: (
            lambda d=private_dir(root): kpc._write(d / 'record.json', DOC)),
        ('knowledge_publish_cli._write', 'existing file'): lambda root: (
            lambda p=private_file(private_dir(root), 'record.json', json.dumps(DOC).encode()): kpc._write(p, DOC)),
    }


def ns_name(path):
    return Path(path).name


def build(part='core'):
    out = {}
    umask = os.umask(0o022)  # the tree's modes must not depend on the caller's umask
    try:
        for (helper, label), setup in sorted(cases(part).items()):
            with scratch('t11a-p3-') as root:
                case = Case(root)
                call = setup(root)
                out.setdefault(helper, {})[label] = run(case, call)
    finally:
        os.umask(umask)
    return golden({'helpers': out})


def main():
    for part, name in GOLDENS.items():
        text = build(part)
        write_golden(text, name)
        print(f'wrote tests/fixtures/t11a/{name} ({len(text)} bytes)')


if __name__ == '__main__':
    main()
