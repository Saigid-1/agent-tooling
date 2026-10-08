"""T12b B3: build-and-swap reindex (docs/work/orders/T12b-one-indexer-outbox.md, B3, frozen r5), in-process.

Falsifiers (the order's words) and the test for each:
- "`DELETE FROM event_search` on any reindex": test_b3_no_reindex_deletes_from_event_search (every caller that
  requests a reindex: `index-history`, `import-native-history --apply` and `upgrade-sources` on an older-writer store;
  each followed by the drain that performs it; a host card requests none, by the meet's ruling);
- "covered 0 reported during or after a swap on a store whose marks were complete":
  test_b3_covered_is_never_zero_through_a_swap_on_a_complete_store;
- "a seal during a build that is not indexed after the swap": test_b3_a_seal_during_a_build_is_indexed_after_the_swap;
- "a reader error during the swap": test_b3_no_reader_error_during_the_swap;
- the rename-into-place primitive's own falsifier (r5): "fsync of the built file, then the `rename`, then fsync of
  the parent directory; both names are regular files in the same directory; no symlink at either name (`lstat`
  before the rename)", by the T11b N4 fd-identity instrument (tests/test_t11b_q3_private_writes.py `Recording`):
  test_b3_the_swap_fsyncs_the_file_renames_then_fsyncs_the_parent (observed at the swap of a real reindex) and
  test_b3_rename_into_place_refuses_a_symlink_at_either_name_and_another_directory (the leaf primitive, provoked);
- W11 never calls `rebuild()`: extensions/ops/tests/test_t12b_w11_reconcile.py (it needs the ops extension).

Instruments: tests/t12b_seams.py `traced` (statements, with the indexer on the stack) and `audit_handler` (the
`os.rename` audit event, raised by os.rename and os.replace before the rename happens). Probes are
`memory.search` calls through a fresh EpisodicMemoryTools: at the build's first statement on the build file, at
every 5th top-level build statement (at most 6), at the rename onto the index (just before it), at the indexer's first
statement after it, and after the drain; and, concurrently, from a reader thread looping searches.

Readings (repeated in the report under AMBIGUITY): the build is observed by statements on the build file (a file
of the index family other than the index); "covered" is the search response's `covered_episodes`; a reader error is
any exception a search raises, or an `index_unavailable` status, while the store's marks were complete.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import stat
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

import t10_corpus as corpus
import t10_world as w
import t12b_seams as seams
from t12b_seams import traced

A = corpus.A
QUERY = {'query': 'juniper', 'limit': 20}
# Statements SQLite runs on its own behalf (FTS5 shadow tables), as tests/t10_instruments.py tells them apart: they
# are not the build's statements.
_NESTED = re.compile(r"\s*--|\s*(?:SELECT|INSERT|REPLACE|UPDATE|DELETE)\b.*?'[A-Za-z_]\w*'\.'[A-Za-z_]\w*'", re.I | re.S)


def _world(root: Path) -> w.World:
    world = w.World(root, corpus.DESKS, corpus.SESSIONS)
    world.initialize()
    return world


def _desk_cli(config, *argv):
    from kp_agent_tooling import desk_cli
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
        code = desk_cli.main(['--config', str(config), *argv])
    assert code == 0, f'{argv}: exit {code}: {out.getvalue()} {err.getvalue()}'
    return json.loads(out.getvalue())


def _populate(world, count=5):
    store, sources = world.store(), world.sources()
    ids = [store.capture(A, source_ref=f't12b:b3:{i}', events=w.events(f'Juniper build text {i}.'))['episode_id']
           for i in range(count)]
    session = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='t12b-b3')
    sources.claim(**w.claim_args(session, corpus.ALPHA))
    for i in range(count):
        ids.append(sources.import_episode(session_id=session, source_ref=f't12b:b3:import:{i}',
                                          events=w.events(f'Juniper imported build text {i}.'),
                                          provenance=w.provenance(f't12b-b3-{i}'))['episode_id'])
    return ids


def _search(world):
    return world.tools(A).call('memory.search', dict(QUERY))


def _complete_world(root):
    """A store whose sealed episodes are all indexed and covered (search: covered == total > 0)."""
    world = _world(root)
    _populate(world)
    seams.setup_index(world.store())
    found = _search(world)
    assert found['total_episodes'] > 0 and found['covered_episodes'] == found['total_episodes'], (
        f'precondition: the store is complete before the reindex: {found}')
    return world


class SwapRun:
    """One reindex (requested through the request seam, which only writes the row; performed by the drain this
    run wraps) under probes."""

    def __init__(self, world, *, seal_during_build=False):
        self.world, self.seal_during_build = world, seal_during_build
        self.index = os.path.realpath(world.state / seams.INDEX_NAME)
        self.probes, self.reader, self.renames = [], [], []
        self.build_statements = 0
        self.build_started = self.renamed = self.after = False
        self.busy = threading.local()
        self.seals = []  # one per seal: {'thread', 'text', 'episode', 'done_at', 'error'}
        self.renamed_at = None

    def probe(self, moment):
        if getattr(self.busy, 'on', False):
            return
        self.busy.on = True
        try:
            found = _search(self.world)
            self.probes.append((moment, found['covered_episodes'], found['total_episodes'], found['index_status'], None))
        except Exception as error:  # noqa: BLE001 - a reader error is the observation
            self.probes.append((moment, None, None, None, repr(error)))
        finally:
            self.busy.on = False

    def _seal(self, seal):
        try:
            seal['episode'] = self.world.store().capture(
                A, source_ref=f't12b:b3:during-build:{seal["number"]}', events=w.events(seal['text']))['episode_id']
        except Exception as error:  # noqa: BLE001
            seal['error'] = repr(error)
        seal['done_at'] = time.monotonic()

    def _start_seal(self):
        number = len(self.seals)
        seal = {'number': number, 'text': f'Cobalt sealed during the build number{number}x.', 'episode': None,
                'done_at': None, 'error': None}
        seal['thread'] = threading.Thread(target=self._seal, args=(seal,))
        self.seals.append(seal)
        seal['thread'].start()
        seal['thread'].join(3)

    def observe(self, trace):
        if getattr(self.busy, 'on', False):
            return
        me = threading.get_ident()
        last = next((e for e in reversed(trace.events) if e.thread == me), None)
        if last is None or not last.drain:
            return
        if self.renamed and not self.after:
            self.after = True
            self.probe('the indexer\'s first statement after the swap')
            self._let_the_reader_run()
        if seams.index_family(last.db) and last.db != self.index and not _NESTED.match(last.sql):
            self.build_statements += 1
            if not self.build_started:
                self.build_started = True
                self.probe('build started')
                if self.seal_during_build:
                    self._start_seal()
            else:
                if self.build_statements % 5 == 0 and self.build_statements <= 30:
                    self.probe(f'building (statement {self.build_statements})')
                if self.seal_during_build and self.build_statements % 7 == 0 and len(self.seals) < 20:
                    self._start_seal()

    def on_audit(self, event, args):
        if event != 'os.rename' or getattr(self.busy, 'on', False):
            return
        try:
            destination = os.path.realpath(os.fsdecode(os.fspath(args[1])))
        except (TypeError, ValueError):
            return
        if destination != self.index:
            return
        for seal in self.seals:
            seal['thread'].join(15)
        self.probe('just before the rename onto the index')
        self._let_the_reader_run()
        self.renames.append(tuple(args[:2]))
        self.renamed = True
        self.renamed_at = time.monotonic()

    def _let_the_reader_run(self, searches=3, seconds=5.0):
        """Hold the indexer (at the rename, and just after it) until the concurrent reader has run `searches` more
        searches, so its reads overlap the swap."""
        target, deadline = len(self.reader) + searches, time.monotonic() + seconds
        while len(self.reader) < target and time.monotonic() < deadline:
            time.sleep(0.005)

    def read_loop(self, stop):
        while not stop.is_set():
            try:
                found = _search(self.world)
                self.reader.append((found['covered_episodes'], found['total_episodes'], found['index_status'], None))
            except Exception as error:  # noqa: BLE001
                self.reader.append((None, None, None, repr(error)))
            time.sleep(0.005)

    def run(self):
        self.before_identity = os.stat(self.index).st_ino
        # The request only writes its row (meet, 2026-10-04): on a host `index-history` drains it at once, before
        # these probes could wrap the drain.
        seams.request_reindex(self.world.store())
        stop = threading.Event()
        reader = threading.Thread(target=self.read_loop, args=(stop,))
        reader.start()
        self.drain_error = None
        try:
            with seams.audit_handler(self.on_audit), traced(self.observe) as self.trace:
                try:
                    seams.drain(self.world.store())
                except BaseException as error:  # noqa: BLE001 - reported by preconditions()
                    self.drain_error = error
                finally:
                    stop.set()
                    reader.join(30)  # the reader ends inside the recording, never racing its end
        finally:
            stop.set()
            reader.join(30)
        self.probe('after the drain')
        self.after_identity = os.stat(self.index).st_ino
        return self

    def preconditions(self):
        problems = [f'the drain failed: {self.drain_error}'] if self.drain_error is not None else []
        if not self.build_started:
            problems.append('no statement on a build file was observed (no build beside the index)')
        if not self.renames:
            problems.append('no rename onto the index was observed (no swap)')
        if self.after_identity == self.before_identity:
            problems.append('the index file is the same file after the reindex (no new file swapped in)')
        return problems


def test_b3_covered_is_never_zero_through_a_swap_on_a_complete_store(tmp_path):
    """GREEN-IF, on a store whose marks are complete, every search during the build, at the rename, after it and
    after the drain (deterministic probes and a concurrent reader) reports covered_episodes > 0, and after the drain
    covered == total."""
    run = SwapRun(_complete_world(tmp_path / 'world')).run()
    assert not run.preconditions(), 'precondition: ' + '; '.join(run.preconditions())
    zero = [p for p in run.probes if p[1] == 0] + [('reader', *r) for r in run.reader if r[0] == 0]
    final = run.probes[-1]
    problems = [f'covered 0 at {p[0]}: {p}' for p in zero[:10]]
    if final[1] != final[2]:
        problems.append(f'after the drain: covered {final[1]} of {final[2]}')
    assert not problems, 'coverage through the swap:\n' + '\n'.join(problems)


def test_b3_no_reader_error_during_the_swap(tmp_path):
    """GREEN-IF no search during the build, at the rename, after it or from the concurrent reader raises or reports
    `index_unavailable` (the reader thread ran at least 5 searches)."""
    run = SwapRun(_complete_world(tmp_path / 'world')).run()
    assert not run.preconditions(), 'precondition: ' + '; '.join(run.preconditions())
    errors = [p for p in run.probes if p[4] is not None or p[3] == 'index_unavailable']
    errors += [('reader', *r) for r in run.reader if r[3] is not None or r[2] == 'index_unavailable']
    assert len(run.reader) >= 5, f'instrument: the reader thread ran {len(run.reader)} searches'
    assert not errors, 'reader errors during the swap:\n' + '\n'.join(str(e)[:300] for e in errors[:10])


def test_b3_a_seal_during_a_build_is_indexed_after_the_swap(tmp_path):
    """GREEN-IF every episode sealed (committed) while the reindex builds, before its swap (seals at the build's
    first statement on the build file and at every 7th top-level one after it, up to 20, spread over the whole build), is
    indexed and found by a search once the drains after the swap are done."""
    world = _complete_world(tmp_path / 'world')
    run = SwapRun(world, seal_during_build=True).run()
    assert not run.preconditions(), 'precondition: ' + '; '.join(run.preconditions())
    during = [s for s in run.seals if s['episode'] and s['error'] is None and s['done_at'] is not None
              and run.renamed_at is not None and s['done_at'] < run.renamed_at]
    assert during, f'precondition: no seal committed during the build: {[(s["error"], s["done_at"]) for s in run.seals]}'
    seams.drain(world.store())
    with seams.ro(world.state / seams.INDEX_NAME) as db:
        indexed = {row[0] for row in db.execute('SELECT episode_id FROM indexed_episodes')}
    tools = world.tools(A)
    missing = []
    for seal in during:
        found = tools.call('memory.search', {'query': seal['text'].rstrip('.')})
        if seal['episode'] not in indexed or [r['episode_id'] for r in found['results']] != [seal['episode']]:
            missing.append(f'seal {seal["number"]}: indexed {seal["episode"] in indexed}, search '
                           f'{[r["episode_id"] for r in found["results"]]} ({found["index_status"]})')
    assert not missing, (f'{len(missing)} of {len(during)} episodes sealed during the build are not indexed after the '
                         'swap:\n' + '\n'.join(missing))


# ---------------------------------------------------------------------------------- DELETE on any reindex

def _request_index_history(root):
    world = _world(root)
    _populate(world)
    seams.setup_index(world.store())
    return world.store(), lambda: _desk_cli(world.configs[A], 'index-history')


def _request_native_history(root):
    from test_native_history_import import claude_row, line
    world = _world(root)
    _populate(world)
    seams.setup_index(world.store())
    source = root / '12345678-1234-1234-1234-123456789abc.jsonl'
    source.write_bytes(line(claude_row('Juniper visible native history')))
    return world.store(), lambda: _desk_cli(world.configs[A], 'import-native-history', '--runtime', 'claude',
                                            '--tenant-id', w.TENANT_ONE, '--source-file', str(source), '--apply')


def _request_upgrade_older_writer(root):
    from test_t10_p6_upgrade import _older_writer_copy, _populate as populate, _world as p6_world
    fresh = p6_world(root / 'fresh')
    fresh.ids = populate(fresh)
    older = _older_writer_copy(fresh, root / 'older')
    store = older.store()
    import t12b_flows as flows
    flows.ensure_index(store)
    return store, lambda: _desk_cli(older.configs[A], 'upgrade-sources')


# A host card event is no reindex caller (meet ruling, 2026-10-04: `attach_card` writes no `reindex` row); its
# no-request, no-inline-rebuild property is tests/test_host_card_bridge.py's T12b R3 replacement.
REQUESTS = {'index-history': _request_index_history, 'import-native-history --apply': _request_native_history,
            'upgrade-sources (older-writer store)': _request_upgrade_older_writer}


@pytest.mark.parametrize('name', sorted(REQUESTS))
def test_b3_no_reindex_deletes_from_event_search(tmp_path, name):
    """GREEN-IF the reindex this caller requests, performed by the drains that follow it, runs no `DELETE FROM
    event_search` on any database, and replaces the index file by a new one (a build swapped in) that indexes every
    sealed episode."""
    store, request = REQUESTS[name](tmp_path / 'root')
    index = seams.index_path_of(store)
    before = os.stat(index).st_ino if index.exists() else None
    failed = None
    with traced() as trace:
        request()
        try:
            seams.drain(store)
        except BaseException as error:  # noqa: BLE001 - reported after the property
            failed = error
    deletes = trace.statements(pattern=r'^\s*DELETE\s+FROM\s+(\w+\.)?"?event_search\b')
    assert not deletes, f'{name}: the reindex deleted in place: {[(Path(e.db).name, e.sql[:120]) for e in deletes[:5]]}'
    assert failed is None, f'the drain after {name} failed: {failed}'
    with seams.ro(store.path) as db:
        sealed = {row[0] for row in db.execute('SELECT id FROM episodes UNION SELECT id FROM source_episodes')}
    with seams.ro(index) as db:
        indexed = {row[0] for row in db.execute('SELECT episode_id FROM indexed_episodes')}
    assert os.stat(index).st_ino != before, f'positive control: {name} did not swap a new index file in'
    assert sealed <= indexed, f'positive control: after {name}, {len(sealed - indexed)} sealed episodes are not indexed'


# ---------------------------------------------------------------------------------- the rename primitive

class SwapRecording:
    """The T11b N4 fd-identity instrument (tests/test_t11b_q3_private_writes.py `Recording`), plus, at each rename
    event (before the rename runs), the lstat type of both names and whether a hot journal sits beside the target."""

    def __init__(self):
        from test_t11b_q3_private_writes import Recording
        self.recording = Recording()
        self.renames = []

    def on_audit(self, event, args):
        if event != 'os.rename':
            return
        source, destination = (os.path.abspath(os.fsdecode(os.fspath(a))) for a in args[:2])

        def kind(path):
            try:
                mode = os.lstat(path).st_mode
            except OSError:
                return 'absent'
            return 'symlink' if stat.S_ISLNK(mode) else 'regular' if stat.S_ISREG(mode) else 'other'
        self.renames.append({'source': source, 'destination': destination,
                             'source_kind': kind(source), 'destination_kind': kind(destination),
                             'journal': os.path.lexists(destination + '-journal')})

    @contextlib.contextmanager
    def active(self):
        with self.recording.active(), seams.audit_handler(self.on_audit):
            yield self

    def problems(self, destination):
        """The order's checks for every rename onto `destination`."""
        events = self.recording.events
        fsyncs = [(i, e[1]) for i, e in enumerate(events) if e[0] == 'os.fsync']
        target_real = os.path.realpath(destination)
        renames = [i for i, e in enumerate(events) if e[0] == 'os.rename' and os.path.realpath(e[2]) == target_real]
        mine = [r for r in self.renames if os.path.realpath(r['destination']) == target_real]
        if not renames or not mine:
            return ['no rename onto the target was observed']
        target = Path(destination)
        published = (os.lstat(target).st_dev, os.lstat(target).st_ino)
        parent = (os.stat(target.parent).st_dev, os.stat(target.parent).st_ino)
        found = []
        for at, rename in zip(renames, mine):
            if os.path.dirname(rename['source']) != os.path.dirname(rename['destination']):
                found.append(f'the source {rename["source"]} is not in the target\'s directory')
            if rename['source_kind'] != 'regular' or rename['destination_kind'] not in ('regular', 'absent'):
                found.append(f'at the rename: source {rename["source_kind"]}, target {rename["destination_kind"]}')
            if rename['journal']:
                found.append('a journal beside the old index at the swap')
        last = renames[-1]
        if not any(identity == published and i < last for i, identity in fsyncs):
            found.append('the built file (the published identity) was not fsynced before the rename')
        if not any(identity == parent and i > last for i, identity in fsyncs):
            found.append('the parent directory was not fsynced after the rename')
        return found


def test_b3_the_swap_fsyncs_the_file_renames_then_fsyncs_the_parent(tmp_path):
    """GREEN-IF the reindex's swap onto the index is a rename of a regular file from the index's own directory,
    with no symlink at either name and no journal beside the old index, preceded by an fsync of the built file
    (its st_dev, st_ino) and followed by an fsync of the parent directory."""
    world = _complete_world(tmp_path / 'world')
    recording = SwapRecording()
    failed = None
    with recording.active():
        seams.request_reindex(world.store())
        try:
            seams.drain(world.store())
        except BaseException as error:  # noqa: BLE001 - reported with the property
            failed = error
    problems = recording.problems(world.state / seams.INDEX_NAME)
    if failed is not None:
        problems.append(f'the drain failed: {failed}')
    assert not problems, 'the swap:\n' + '\n'.join(problems)


def test_b3_rename_into_place_refuses_a_symlink_at_either_name_and_another_directory(tmp_path):
    """GREEN-IF the leaf's rename-into-place primitive (seam: leaf.rename_into_place(source, destination)) moves a
    regular file over a regular file in the same directory with fsync file / rename / fsync parent, and refuses,
    leaving every name and target unchanged: a symlink at the destination, a symlink at the source, and a source in
    another directory."""
    rename = seams.rename_primitive()
    problems = []
    directory = tmp_path / 'dir'
    directory.mkdir(mode=0o700)
    other = tmp_path / 'other'
    other.mkdir(mode=0o700)
    victim = tmp_path / 'victim'
    victim.write_bytes(b'victim')

    def fresh(name, data):
        path = directory / name
        path.write_bytes(data)
        path.chmod(0o600)
        return path

    built, target = fresh('built.sqlite3', b'new'), fresh('index.sqlite3', b'old')
    recording = SwapRecording()
    with recording.active():
        try:
            rename(built, target)
        except Exception as error:  # noqa: BLE001
            problems.append(f'the regular case refused: {error!r}')
    if target.read_bytes() != b'new' or built.exists():
        problems.append('the regular case did not move the built file into place')
    problems += [f'regular case: {p}' for p in recording.problems(target)]
    cases = []
    link_target = directory / 'linked.sqlite3'
    link_target.symlink_to(victim)
    cases.append(('a symlink at the destination', fresh('built2.sqlite3', b'new2'), link_target))
    link_source = directory / 'built3.sqlite3'
    link_source.symlink_to(victim)
    cases.append(('a symlink at the source', link_source, fresh('index3.sqlite3', b'old3')))
    elsewhere = other / 'built4.sqlite3'
    elsewhere.write_bytes(b'new4')
    cases.append(('a source in another directory', elsewhere, fresh('index4.sqlite3', b'old4')))
    for label, source, destination in cases:
        before = {p: (os.path.islink(p), os.readlink(p) if os.path.islink(p) else Path(p).read_bytes())
                  for p in (source, destination)}
        refused = False
        try:
            rename(source, destination)
        except Exception:  # noqa: BLE001 - a refusal of any kind
            refused = True
        after = {p: (os.path.islink(p), os.readlink(p) if os.path.islink(p) else Path(p).read_bytes())
                 for p in (source, destination) if os.path.lexists(p)}
        if not refused or after != before or victim.read_bytes() != b'victim':
            problems.append(f'{label}: refused={refused}, names changed={after != before}')
    assert not problems, 'rename into place:\n' + '\n'.join(problems)
