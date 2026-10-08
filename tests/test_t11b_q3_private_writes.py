"""T11b Q3: private writes close their windows (docs/work/orders/T11b-leaf-behaviour.md, Q3, L3).

Order: "Create with mode. Every private file is created with its mode, so there is no
create-then-chmod window. No write follows a symlinked final component." "Durability of
publications only. fsync is ruled for publication writes alone: publish-once (link) and replace
(rename). Each fsyncs the file, then its parent directory." Falsifier: "a private file observable
with another mode between creation and close (audit hook on os.chmod after open); a write through
a symlinked final component that succeeds; a publication without a file fsync and a
parent-directory fsync." Carried from T11a: "The wider P3 primitive set ... T11b's Q3 adopts it
as a property, with T11a's guard as the instrument."

Drivers: every case of T11a's P3 golden (tests/fixtures/t11a/generate_private_file_sequences.py
``cases()``: the census's private-file helpers through their old names, core and ops), plus three
product flows that create private stores: the desk-memory ``initialize`` (sessions and episodes
databases), the model gateway's budget ledger (``BudgetLedger.reserve``) and a registry launch
(``launch_binding.prepare``, codex). The umask is pinned to 0o022, as the P3 golden pins it.

Instruments:
- window: one process-wide ``sys.addaudithook`` (``open``, ``os.mkdir``, ``sqlite3.connect``,
  ``os.chmod``). An entry is "created in the call" when an ``open`` with O_CREAT, an ``os.mkdir`` or
  a ``sqlite3.connect`` names a path that does not exist at that event. At every ``os.chmod`` (by
  path or descriptor) of such an entry to a private mode (no group/other bits), the hook reads the
  entry's mode at that instant (the hook runs before the chmod): a different mode is a window. A
  flow's created entries must also end private (all are at base), so a window is not "closed" by
  dropping the chmod;
- symlinked final component, two instruments. (i) Pre-placed: each write, publish and lock helper is
  pointed at a target that is a symlink, once to an existing victim file and once dangling. A write
  through it is: the victim's bytes or mode changed, the dangling target created, or an ``open`` of
  the link path for writing that follows it (write access or O_CREAT, without O_NOFOLLOW and without
  O_EXCL). At base every helper already refuses or replaces the link, so this one is GREEN at base and
  guards the head. (ii) Swapped into the window (the race a create-then-chmod leaves open): at the
  call's first ``os.chmod`` BY PATH of a file it created, the hook moves the file aside and puts a
  symlink to a 0644 victim in its place before the chmod runs; the chmod must not reach the victim;
  its limit (Verification MV7, meet): the "created" set holds a replace's ``mkstemp`` temporary, not the
  rename's destination, so a chmod BY PATH of the destination after an ``os.replace`` is not swapped
  here; the regression set and T11a's P3 reading see that one;
- fsync (N4): ``os.fsync`` is wrapped in-process; each call records ``os.fstat(fd)``'s
  (st_dev, st_ino). A publication is an ``os.link``/``os.rename`` (``os.replace``) whose destination,
  after the call, is a regular file with the identity its source had at the event. GREEN needs an
  fsync of the published file's identity and, after it and after the publishing event, an fsync of
  the parent directory's identity. (T11a F1's path-based fsync comparison is not used: Amendment 1.)
- the wider P3 primitive set: tests/test_t11_leaf_single_home.py's ``--report`` reading "the rest
  of the census set outside the allowlist" (13 at base), asserted zero.

Readings (repeated under AMBIGUITY): a file created with a non-private final mode (the Codex
config replaced at 0644) is not a private file; a directory rename (model_gateway.write_outputs,
allowlisted ``directory_rename``) is not a file publication; a lock file opened O_RDWR|O_CREAT
through a symlink is a write through it; a chmod by path after creation is a write that can follow a
symlinked final component (instrument ii); SQLite stores a flow creates are private files.
"""
from __future__ import annotations

import hashlib
import os
import stat
import sys
from collections import Counter
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / 'launch'))

from t11a_golden import load_generator, scratch  # noqa: E402
from t11b_audit import path_of  # noqa: E402

DOC = {'b': 1, 'a': ['é', None, {'nested': True}]}
RAW = 'payload é ☃\n'.encode()
VICTIM = b'victim bytes, never to be written through a link\n'


# ------------------------------------------------------------------------------- the recorder

def _hook(event, args):
    """One process-wide hook (audit hooks cannot be removed); the active recording lives on ``sys``."""
    recording = getattr(sys, '_t11b_q3_active', None)
    if recording is not None and event in ('open', 'os.mkdir', 'sqlite3.connect', 'os.chmod', 'os.rename', 'os.link'):
        recording.audit(event, args)


if not getattr(sys, '_t11b_q3_hook', False):
    sys.addaudithook(_hook)
    sys._t11b_q3_hook = True


def _identity(info):
    return (info.st_dev, info.st_ino)


def _text(value):
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    return os.path.abspath(os.fspath(value))


class Recording:
    def __init__(self, swap_to=None):
        self.events, self.created, self.windows = [], [], []
        # The race instrument: at the first chmod BY PATH of an entry created in the call, the entry is moved
        # aside and a symlink to ``swap_to`` put in its place, before the chmod runs (what another process can
        # do in a create-then-chmod window). A chmod by descriptor is never swapped.
        self.swap_to, self.swapped, self.busy = swap_to, [], False

    def _created(self, path):
        if not os.path.lexists(path):
            self.created.append(path)

    def audit(self, event, args):
        if self.busy:
            return
        if event == 'os.chmod' and self.swap_to is not None and not self.swapped and not isinstance(args[0], int):
            path = _text(args[0])
            if path in self.created and os.path.isfile(path) and not os.path.islink(path):
                self.busy = True
                try:
                    os.rename(path, path + '.t11b-moved-aside')
                    os.symlink(self.swap_to, path)
                    self.swapped.append(path)
                finally:
                    self.busy = False
        if event == 'open':
            if isinstance(args[0], int):
                return
            path = _text(args[0])
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            if flags & os.O_CREAT:
                self._created(path)
            self.events.append(('open', path, flags))
        elif event == 'os.mkdir':
            path = _text(args[0])
            self._created(path)
            self.events.append(('os.mkdir', path))
        elif event == 'sqlite3.connect':
            raw = os.fsdecode(args[0]) if isinstance(args[0], bytes) else os.fspath(args[0])
            path = path_of(raw) if raw.startswith('file:') else (None if raw == ':memory:' else os.path.abspath(raw))
            if path is not None:
                self._created(path)
        elif event == 'os.chmod':
            target, mode = args[0], args[1]
            try:
                info = os.fstat(target) if isinstance(target, int) else os.stat(_text(target))
            except (OSError, TypeError, ValueError):
                return
            current = stat.S_IMODE(info.st_mode)
            wanted = mode & 0o7777
            for path in self.created:
                try:
                    if _identity(os.lstat(path)) != _identity(info):
                        continue
                except OSError:
                    continue
                if not wanted & 0o077 and current != wanted:
                    self.windows.append(f'{path}: {oct(current)} until chmod {oct(wanted)}')
                break
            self.events.append(('os.chmod', target if isinstance(target, int) else _text(target), wanted))
        elif event in ('os.rename', 'os.link'):
            source, destination = _text(args[0]), _text(args[1])
            try:
                identity = _identity(os.lstat(source))
            except OSError:
                identity = None
            self.events.append((event, source, destination, identity))

    @contextmanager
    def active(self):
        real = os.fsync

        def fsync(fd):
            try:
                info = os.fstat(fd)
                self.events.append(('os.fsync', _identity(info), stat.S_ISDIR(info.st_mode)))
            except OSError:
                self.events.append(('os.fsync', None, None))
            return real(fd)

        os.fsync = fsync
        umask = os.umask(0o022)
        sys._t11b_q3_active = self
        try:
            yield self
        finally:
            sys._t11b_q3_active = None
            os.umask(umask)
            os.fsync = real

    def publications(self):
        """(event index, kind, destination) of every link/rename that took effect on a regular file."""
        found = []
        for index, event in enumerate(self.events):
            if event[0] in ('os.rename', 'os.link') and event[3] is not None:
                try:
                    info = os.lstat(event[2])
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode) and _identity(info) == event[3]:
                    found.append((index, event[0], event[2]))
        return found

    def unsynced(self, root):
        problems = []
        fsyncs = [(i, e[1]) for i, e in enumerate(self.events) if e[0] == 'os.fsync']
        for index, kind, destination in self.publications():
            target = Path(destination)
            mine = _identity(os.lstat(target))
            parent = _identity(os.stat(target.parent))
            first = next((i for i, identity in fsyncs if identity == mine), None)
            after = max(index, first if first is not None else index)
            directory = next((i for i, identity in fsyncs if identity == parent and i > after), None)
            shown = str(target).replace(str(root), '<case>')
            if first is None:
                problems.append(f'{kind} {shown}: the published file was never fsynced')
            if directory is None:
                problems.append(f'{kind} {shown}: no fsync of its parent directory after the file and the {kind}')
        return problems


# ------------------------------------------------------------------------------------ drivers

def _flows():
    """Product flows that create private stores, beyond the helper cases."""
    def desk_memory(root):
        from test_portable_desk_memory import config
        from kp_agent_tooling._impl.service.desk_memory_runtime import initialize
        path = config(root, 'session-q3', 'q3')
        return lambda: initialize(path)

    def budget_ledger(root):
        from kp_agent_tooling._impl.service.model_gateway import Budget, BudgetLedger
        (root / 'gateway').mkdir(mode=0o700)
        ledger = BudgetLedger(root / 'gateway')
        budget = Budget(max_calls_per_hour=10, max_usd_per_day=Decimal('1'), confirm_over_usd=None,
                        estimated_usd_per_call=Decimal('0.01'))
        return lambda: ledger.reserve(call_id='call-1', capability='text', provider='p', model='m', budget=budget,
                                      confirm=False, digest='d' * 64, now=1_790_000_000.0)

    def launch(root):
        from t3_harness import World
        from kp_agent_tooling._impl.service import launch_binding
        world = World.create(root / 'world')
        desk = world.save_desk()
        request = world.request(harness='codex', desk_id=desk)

        def call():
            saved = {name: os.environ.get(name) for name in ('HOME', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME')}
            os.environ['HOME'] = str(world.root / 'home')
            os.environ.pop('CLAUDE_CONFIG_DIR', None)
            os.environ.pop('CODEX_HOME', None)
            try:
                return launch_binding.prepare(world.operator, request)
            finally:
                for name, value in saved.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
        return call

    return {('flow', 'desk memory initialize'): desk_memory, ('flow', 'budget ledger reserve'): budget_ledger,
            ('flow', 'launch prepare codex'): launch}


def all_cases():
    generator = load_generator('generate_private_file_sequences')
    table = dict(generator.cases('core'))
    try:
        import kp_agent_tooling_ops  # noqa: F401
    except ImportError:
        pass
    else:
        table.update(generator.cases('ops'))
    table.update(_flows())
    return table


def run_cases():
    out = {}
    for (helper, label), setup in sorted(all_cases().items()):
        with scratch('t11b-q3-') as root:
            umask = os.umask(0o022)
            try:
                call = setup(root)
            finally:
                os.umask(umask)
            recording = Recording()
            with recording.active():
                try:
                    call()
                except Exception:
                    pass  # refusals are part of the helpers' contracts; the instruments read the events
            windows = list(recording.windows)
            if helper == 'flow':  # a flow's stores and launch files stay private: no window is not "no privacy"
                for path in recording.created:
                    if os.path.lexists(path) and os.lstat(path).st_mode & 0o077:
                        windows.append(f'{path}: created and left {oct(stat.S_IMODE(os.lstat(path).st_mode))}')
            out[f'{helper} | {label}'] = (windows, recording.unsynced(root))
    return out


@pytest.fixture(scope='module')
def recorded():
    return run_cases()


def test_q3_no_private_file_is_observable_with_another_mode_before_its_chmod(recorded):
    """GREEN-IF no helper case or flow chmods an entry it created to a private mode while that entry has another
    mode (every private file and directory is created with its final mode), and every entry a flow creates (its
    stores, lock files, launch directory and files: all private at base) ends with no group or other bits."""
    found = [f'{case}: {window}' for case, (windows, _) in recorded.items() for window in windows]
    assert not found, f'{len(found)} create-then-chmod windows:\n' + '\n'.join(found)


def test_q3_every_publication_fsyncs_the_file_then_its_parent_directory(recorded):
    """GREEN-IF every link or rename publication of a regular file in the helper cases and flows is preceded by an
    fsync of the published file's (st_dev, st_ino) and followed by an fsync of its parent directory's."""
    found = [f'{case}: {problem}' for case, (_, problems) in recorded.items() for problem in problems]
    assert not found, f'{len(found)} publications are not durable:\n' + '\n'.join(found)


def test_instrument_q3_drivers_publish_by_link_and_by_rename():
    """Instrument check (not a property): the drivers do publish by link and by rename, so the fsync test above is
    not vacuously green. GREEN-IF at least one link and one rename publication is seen."""
    kinds = Counter()
    for (helper, label), setup in sorted(all_cases().items()):
        if helper == 'flow':
            continue
        with scratch('t11b-q3-kinds-') as root:
            call = setup(root)
            recording = Recording()
            with recording.active():
                try:
                    call()
                except Exception:
                    pass
            kinds.update(kind for _, kind, _ in recording.publications())
    assert kinds['os.link'] and kinds['os.rename'], f'publications seen: {dict(kinds)}'


# --------------------------------------------------------------------------- symlinked targets

def _symlink_targets():
    """name -> (target name in a private directory, call(target)) for every write, publish and lock helper."""
    from kp_agent_tooling._impl.service import launch_binding as lb, host_adapter as ha
    from kp_agent_tooling._impl.service import desk_catalog_setup as dcs, model_gateway as mg
    from kp_agent_tooling._impl.service import desk_registry as dr, spool_ingest as si
    from kp_agent_tooling._impl import runtime_install as ri, scip_navigation as sn, semantic_index as smi
    from kp_agent_tooling._impl import navigation_snapshot as ns
    from kp_agent_tooling import refresh_cli

    def entered(manager):
        with manager:
            return 'entered'

    table = {
        'launch_binding.write_private': ('record.json', lambda t: lb.write_private(t, DOC)),
        'launch_binding._write_once': ('record.json', lambda t: lb._write_once(t, DOC)),
        'host_adapter._write_new': ('record.json', lambda t: ha._write_new(t, DOC)),
        'host_adapter._replace': ('config.toml', lambda t: ha._replace(t, RAW, 0o600)),
        'desk_catalog_setup._write_private_json': ('desk.json', lambda t: dcs._write_private_json(t, DOC)),
        'model_gateway._write_private': ('output.bin', lambda t: mg._write_private(t, RAW)),
        'runtime_install._write': ('compose.yaml', lambda t: ri._write(t, RAW)),
        'runtime_install._placeholder': ('empty', lambda t: ri._placeholder(t)),
        'scip_navigation._atomic': ('index.json', lambda t: sn._atomic(t, RAW)),
        'semantic_index._write_once': ('blob', lambda t: smi._write_once(t, RAW)),
        'navigation_snapshot._write_once': ('blob', lambda t: ns._write_once(t, RAW)),
        'refresh_cli.atomic': ('status.json', lambda t: refresh_cli.atomic(t, DOC)),
        'desk_registry.RoleRoster._write': ('roles.json',
                                            lambda t: dr.RoleRoster(t)._write(dr.default_roster()['roles'], 7)),
        'launch_binding._locked': ('launch.lock', lambda t: entered(lb._locked(t))),
        'spool_ingest.Cursors.locked': ('spool-ingest.sqlite3.lock', lambda t: entered(si.Cursors(t.parent).locked())),
    }
    try:
        from kp_agent_tooling_ops import knowledge_publish_cli as kpc
    except ImportError:
        pass
    else:
        table['knowledge_publish_cli._write'] = ('record.json', lambda t: kpc._write(t, DOC))
    return table


def _content_addressed():
    """Helpers that choose their own file name (a content digest): name -> call(directory)."""
    from kp_agent_tooling._impl import navigation_search_pages as nsp
    manifest = {'repo_key': 'répo', 'source_revision': 'a' * 40, 'files': ['é.py']}
    return {'navigation_search_pages._save': lambda d: nsp._save(d, {'query': 'é', 'offset': 3}),
            'navigation_search_pages._store_manifest': lambda d: nsp._store_manifest(d, manifest)}


def _snapshot(path):
    info = os.lstat(path)
    return stat.S_IMODE(info.st_mode), hashlib.sha256(path.read_bytes()).hexdigest()


def _through(root, link, target, call):
    """Problems when ``call`` writes through the symlink ``link`` (to ``target``)."""
    victim = target.exists()
    before = _snapshot(target) if victim else None
    recording = Recording()
    with recording.active():
        try:
            call()
        except Exception:
            pass
    problems = []
    if victim and _snapshot(target) != before:
        problems.append(f'the victim {target.name} changed {before} -> {_snapshot(target)}')
    if not victim and os.path.lexists(target):
        problems.append(f'the dangling target {target.name} was created')
    for event in recording.events:
        if event[0] == 'open' and event[1] == str(link):
            flags = event[2]
            writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            if writing and not flags & os.O_NOFOLLOW and not (flags & os.O_EXCL and flags & os.O_CREAT):
                problems.append(f'the link was opened for writing without O_NOFOLLOW (flags {flags:#x})')
    return problems


def _cases_through_links():
    out = {}
    plain = _symlink_targets()
    named = _content_addressed()
    for helper in sorted(plain) + sorted(named):
        for kind in ('existing victim', 'dangling'):
            with scratch('t11b-q3-link-') as root:
                directory = root / 'private'
                directory.mkdir(mode=0o700)
                elsewhere = root / 'elsewhere'
                elsewhere.mkdir(mode=0o700)
                target = elsewhere / 'victim'
                if kind == 'existing victim':
                    target.write_bytes(VICTIM)
                    target.chmod(0o600)
                if helper in plain:
                    name, call = plain[helper]
                    link = directory / name
                    run = lambda call=call, link=link: call(link)  # noqa: E731
                else:
                    with scratch('t11b-q3-name-') as probe:
                        named[helper](probe)
                        name = next(p.name for p in probe.iterdir() if p.suffix == '.json')
                    link = directory / name
                    run = lambda helper=helper: named[helper](directory)  # noqa: E731
                link.symlink_to(target)
                out[f'{helper} | {kind}'] = _through(root, link, target, run)
    return out


def test_q3_no_write_follows_a_symlinked_final_component():
    """GREEN-IF, for every write, publish and lock helper whose target is a symlink (to an existing victim, or
    dangling), the victim keeps its bytes and mode, no dangling target is created, and the link path is never
    opened for writing without O_NOFOLLOW (O_CREAT|O_EXCL, which never follows, excepted)."""
    found = [f'{case}: {problem}' for case, problems in _cases_through_links().items() for problem in problems]
    assert not found, f'{len(found)} writes through a symlinked final component:\n' + '\n'.join(found)


def test_q3_no_chmod_after_creation_follows_a_symlink_swapped_into_the_window():
    """GREEN-IF, for every helper case and flow, when the race instrument replaces a file the call created by a
    symlink to a 0644 victim at the call's first chmod BY PATH of that file, the victim's mode and bytes stay as
    they were (the call created the file with its mode and never chmods it by path, or never follows the link)."""
    found = []
    for (helper, label), setup in sorted(all_cases().items()):
        with scratch('t11b-q3-race-') as root:
            umask = os.umask(0o022)
            try:
                call = setup(root)
            finally:
                os.umask(umask)
            elsewhere = root / 't11b-elsewhere'
            elsewhere.mkdir(mode=0o700)
            victim = elsewhere / 'victim'
            victim.write_bytes(VICTIM)
            victim.chmod(0o644)
            before = _snapshot(victim)
            recording = Recording(swap_to=victim)
            with recording.active():
                try:
                    call()
                except Exception:
                    pass
            after = _snapshot(victim)
            if after != before:
                shown = [p.replace(str(root), '<case>') for p in recording.swapped]
                found.append(f'{helper} | {label}: {shown} swapped for a link; the victim went {oct(before[0])} -> '
                             f'{oct(after[0])}' + (' (bytes changed)' if after[1] != before[1] else ''))
    assert not found, f'{len(found)} chmods after creation followed a swapped-in symlink:\n' + '\n'.join(found)


# ------------------------------------------------------------------------ the wider P3 primitive set

def test_q3_wider_p3_primitive_set_is_zero_outside_the_leaf_and_allowlist():
    """GREEN-IF T11a's guard reads zero for "the rest of the census set outside the allowlist" (chmod, 0700 mkdirs,
    renames, fsync, st_mode & 0o077): every such primitive is in the leaf or in the guard's allowlist."""
    import test_t11_leaf_single_home as guard
    rest = [s for s in guard.private_primitive_violations() if s.kind not in guard.ORDER_P3_PRIMITIVES]
    kinds = dict(sorted(Counter(s.kind for s in rest).items()))
    assert not rest, (f'{len(rest)} wider-set P3 primitives outside the leaf and the allowlist {kinds}:\n'
                      + guard.fmt(rest))
