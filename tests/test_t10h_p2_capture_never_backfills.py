"""T10h P2: capture never backfills and never runs the whole-store check (in-process).

Order: docs/work/orders/T10h-projection-hotfix.md (P2, the Verification amendment), frozen
at its merge commit. Instrument: T10's in-process statement trace (tests/t10_instruments.py): every
``sqlite3.connect`` is traced, and a *scan* is any plan line ``SCAN <protected table>`` (T10 P2).

Captures are driven through the public ``WorkspaceCapture`` service, constructed fresh for each
pass as ``kp-agent-workspace-capture ... once`` does, with the synthetic Claude transcripts, Git
checkout and capture policy of the existing capture tests (test_workspace_capture_*). The store is
the portable fixture store (test_portable_desk_memory.episode_store), populated through public
writes before any pass: legacy captures for two desks and a claimed source session.

* P2(i): two passes on a store whose projection is complete trace no ``PRAGMA quick_check``
  (nor ``integrity_check``) and no scan. Guard: reads stay served after them.
* P2(i), early return: ``SessionSources.upgrade()`` and ``backfill()`` on a complete projection
  with nothing behind trace no whole-store check and no scan, and change nothing.
* P2(ii): one pass on a store whose projection is incomplete (installed with its marks behind,
  or not installed) traces no whole-store check and projects no row for an episode sealed before
  the pass; capture itself succeeds (status ok, the new rows sealed).
* P2(iii): after that pass, reads refuse with ``projection_incomplete`` until
  ``kp-agent-desk ... upgrade-sources`` runs; then they answer.

"Projects no row for an existing episode": the projection is discovered as in T10
(t10_tamper: every table, and base-table column, the base writer does not create); every
projection row whose values name an episode sealed before the pass is identical before and
after the pass. A pass may project its own new rows.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import t10_tamper as tamper
import t10_world as w
from t10_instruments import PROTECTED, recording, strip_literals
from test_portable_desk_memory import config, episode_store
from test_workspace_capture_adversarial import _line, _repo, _worker
from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools
from kp_agent_tooling._impl.service.session_sources import SessionSources
from kp_agent_tooling._impl.service.workspace_capture import WorkspaceCapture

SESSION = 'session-1'
NATIVE = 'b10a8c3e-5d2f-4e61-9a7b-0c1d2e3f4a5b'
NATIVE_TWO = 'c10a8c3e-5d2f-4e61-9a7b-0c1d2e3f4a5b'
WHOLE_STORE_CHECK = r'^\s*PRAGMA\s+(?:\w+\.)?(?:quick_check|integrity_check)\b'
INCOMPLETE = ['marks_behind', 'not_installed']


# ---- world -------------------------------------------------------------------------------------

class CaptureWorld:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.store = episode_store(tmp_path)
        self.repo = _repo(tmp_path / 'repo')
        codex = tmp_path / 'codex'
        codex.mkdir()
        self.policy = _worker(tmp_path, self.store, self.repo, codex).policy
        self.config = config(tmp_path, SESSION, 'fixture')
        admitted = self.store.sessions.resolve(SESSION, self.store.registry)
        self.tenant, self.binding = admitted.tenant_id, admitted.binding_key

    @property
    def path(self):
        return self.store.path

    def transcript(self, native, *texts):
        """Append visible Claude rows of session ``native`` (cwd: the approved checkout)."""
        folder = Path(self.policy['native_roots']['claude']) / 'project-folder'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'{native}.jsonl'
        with path.open('ab') as stream:
            for i, text in enumerate(texts):
                role = 'user' if i % 2 == 0 else 'assistant'
                stream.write(_line({'type': role, 'sessionId': native, 'cwd': str(self.repo),
                                    'message': {'role': role, 'content': text}}))
        return path

    def populate(self):
        """Episodes sealed through public writes: two desks' captures and a claimed source session."""
        store, sources = self.store, SessionSources(self.store)
        for i in range(4):
            store.capture(SESSION, source_ref=f't10h:own:{i}', events=w.events(f'Existing cedar note {i}.'))
        for i in range(2):
            store.capture('session-3', source_ref=f't10h:other:{i}', events=w.events(f'Other desk cedar {i}.'))
        session = sources.register(tenant_id=self.tenant, runtime='claude', native_id='t10h-existing')
        for i in range(3):
            sources.import_episode(session_id=session, source_ref=f't10h:source:{i}',
                                   events=w.events(f'Imported cedar row {i}.'), provenance=w.provenance(f't10h-{i}'))
        sources.claim(**w.claim_args(session, self.binding, recorded_at='2026-10-03T00:00:00Z'))
        return self

    def older_writer_episode(self):
        """One ``episodes`` row inserted as the base writer's SQL inserts it: sealed, no projection."""
        payload = {'schema_version': 'ops.episode.v1', 'binding_key': self.binding,
                   'provider_instance': 'operator-import', 'session': 'legacy-import', 'source_ref': 't10h:older',
                   'events': w.imported_events('Older writer cedar.'),
                   'source_provenance': w.legacy_provenance('t10h-older'),
                   'import_recorded_at': '2026-10-01T00:00:00+00:00',
                   'evidence_boundary': 'legacy graph assertion; original actor and observation time unverified'}
        identity = w.content_id('episode', payload)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('INSERT INTO episodes(id,binding,session,source_ref,payload) VALUES (?,?,?,?,?)',
                       (identity, self.binding, 'legacy-import', 't10h:older', w.canonical(payload)))
            db.commit()
        return identity

    def strip_projection(self):
        """Reduce the store to the base writer's schema: the same sealed rows, no projection."""
        stripped = self.path.with_name('t10h-stripped.sqlite3')
        w.strip_to_older_writer(self.path, stripped, tamper.base_schema())
        os.replace(stripped, self.path)
        self.path.chmod(0o600)
        assert tamper.projection_dump(self.path) == {}, 'fixture: the stripped store still has a projection'

    def capture(self):
        """One ``once`` pass from a freshly constructed worker, traced."""
        with recording() as recorder:
            recorder.phase = 'call'
            result = WorkspaceCapture(self.store, self.policy).once()
        return result, recorder

    def tools(self):
        return EpisodicMemoryTools(self.store, SESSION)

    def upgrade_sources(self):
        from kp_agent_tooling import desk_cli
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = desk_cli.main(['--config', str(self.config), 'upgrade-sources'])
        assert code == 0, f'upgrade-sources exit {code}: {output.getvalue()}'
        return json.loads(output.getvalue())


def sealed_ids(path):
    with closing(sqlite3.connect(path)) as db:
        return {row[0] for row in db.execute('SELECT id FROM episodes UNION ALL SELECT id FROM source_episodes')}


def rows_naming(path, identities, *, mask=(), skip=()):
    """Projection rows (discovered, T10 t10_tamper) whose values name any of ``identities``. T12b: ``mask`` blanks
    the named columns and ``skip`` leaves out tables (the host case's coverage marks and outbox)."""
    found = set()
    for table, content in tamper.projection_dump(path).items():
        if table in skip:
            continue
        masked = [i for i, column in enumerate(content['columns']) if column in mask]
        for row in content['rows']:
            row = [None if i - (len(row) - len(content['columns'])) in masked else v for i, v in enumerate(row)]
            text = json.dumps(row)
            if any(identity in text for identity in identities):
                found.add((table, text))
    return found


def whole_store_checks(recorder):
    return [s.sql for s in recorder.top() if re.search(WHOLE_STORE_CHECK, s.sql, re.I)]


def store_inserts(recorder, path, table):
    target = os.path.realpath(path)
    return [s for s in recorder.top()
            if s.db == target and re.match(r'\s*INSERT(?:\s+OR\s+\w+)?\s+INTO\s+["`\[]?%s\b' % table, s.sql, re.I)]


def incomplete(value):
    return 'projection_incomplete' in json.dumps(value, default=str)


def call(tools, name, arguments):
    return w.call(tools, name, arguments)


def assert_ok(result, *, imported):
    assert result.get('status') == 'ok', f'the capture pass did not report ok: {str(result)[:600]}'
    files = [entry for entry in result.get('files', []) if isinstance(entry, dict)]
    got = sum(entry.get('imported', 0) for entry in files)
    if imported:
        assert got >= 1, f'positive control: the pass imported nothing: {str(result)[:600]}'
    return got


# ---- P2(i) -------------------------------------------------------------------------------------

def test_t10h_p2_capture_passes_on_complete_projection_run_no_whole_store_check_or_scan(tmp_path):
    """GREEN-IF two `once` passes (fresh workers) on a populated store whose projection is complete
    each report ok, trace no PRAGMA quick_check/integrity_check and no scan of a protected table
    (T10 P2), the first pass's trace inserting the captured row (positive control), and reads stay
    served afterwards (guard: capture projects its own rows)."""
    world = CaptureWorld(tmp_path).populate()
    tools = world.tools()
    assert call(tools, 'memory.connection_status', {}).get('status') == 'ready', 'fixture: projection complete'
    sealed = sealed_ids(world.path)
    assert len(sealed) >= 9, f'fixture: the store is populated: {len(sealed)}'

    world.transcript(NATIVE, 'First pass cedar question.', 'First pass cedar answer.')
    first, first_trace = world.capture()
    assert_ok(first, imported=True)
    assert store_inserts(first_trace, world.path, 'source_episodes'), (
        'positive control: the traced pass did not insert the captured source episode')
    world.transcript(NATIVE, 'Second pass cedar question.', 'Second pass cedar answer.')
    second, second_trace = world.capture()
    assert_ok(second, imported=False)

    for label, recorder in (('first', first_trace), ('second', second_trace)):
        assert recorder.top(), f'positive control: the {label} pass traced no statement'
        checks = whole_store_checks(recorder)
        assert not checks, f'the {label} capture pass ran the whole-store check: {checks}'
        scans = recorder.scans(phases=('call',))
        assert not scans, f'the {label} capture pass scanned a protected table {PROTECTED}: {scans[:5]}'
        errors = recorder.plan_errors(phases=('call',))
        assert not errors, f'instrument: no plan for traced statements of the {label} pass: {errors[:3]}'

    status = call(world.tools(), 'memory.connection_status', {})
    assert not incomplete(status) and status.get('status') == 'ready', f'reads refuse after capture: {status}'


@pytest.mark.parametrize('entry', ['upgrade', 'backfill'])
def test_t10h_p2_upgrade_and_backfill_return_early_on_complete_projection(tmp_path, entry):
    """GREEN-IF SessionSources.upgrade() and .backfill() on a populated store whose projection is
    complete, with nothing behind, trace no PRAGMA quick_check/integrity_check and no scan of a
    protected table, and leave the store unchanged (iterdump); the trace reads the projection's
    state (positive control)."""
    world = CaptureWorld(tmp_path).populate()
    assert call(world.tools(), 'memory.connection_status', {}).get('status') == 'ready', 'fixture: complete'
    with closing(sqlite3.connect(world.path)) as db:
        before = list(db.iterdump())
    with recording() as recorder:
        recorder.phase = 'call'
        report = getattr(SessionSources(world.store), entry)()
    assert isinstance(report, dict), report
    assert recorder.top(), 'positive control: nothing was traced'
    checks = whole_store_checks(recorder)
    assert not checks, f'{entry}() ran the whole-store check on a complete projection: {checks}'
    scans = recorder.scans(phases=('call',))
    assert not scans, f'{entry}() scanned a protected table on a complete projection: {scans[:5]}'
    with closing(sqlite3.connect(world.path)) as db:
        assert list(db.iterdump()) == before, f'{entry}() changed a complete store'


# ---- P2(ii), P2(iii) -----------------------------------------------------------------------------

def _incomplete_world(tmp_path, state):
    world = CaptureWorld(tmp_path).populate()
    assert call(world.tools(), 'memory.connection_status', {}).get('status') == 'ready', 'fixture: complete'
    if state == 'marks_behind':
        world.older = world.older_writer_episode()
    else:
        world.older = None
        world.strip_projection()
    status = call(world.tools(), 'memory.connection_status', {})
    assert incomplete(status), f'fixture: the store is not incomplete: {status}'
    return world


@pytest.mark.parametrize('state', INCOMPLETE)
def test_t10h_p2_capture_on_incomplete_projection_projects_no_existing_row(tmp_path, state):
    """GREEN-IF one `once` pass on a populated store whose projection is incomplete (installed with
    marks behind; not installed) reports ok, seals the new transcript's rows, traces no PRAGMA
    quick_check/integrity_check, and leaves every projection row naming an episode sealed before the
    pass exactly as it was (none created, none changed, none removed).

    T12b (ruled exception, Verification, 2026-10-04): with marks behind, the pass runs as the capture role
    does, under the Compose marker (tests/t12b_seams.py `compose_marker`: AGENT_MEMORY_VOLUME set, the leaf's
    volume root at this test's root so T11b's store check passes), where a sealer never drains; the
    assertion stands as written. The host case is
    test_t10h_p2_host_capture_on_marks_behind_drains_what_it_finds."""
    world = _incomplete_world(tmp_path, state)
    existing = sealed_ids(world.path)
    assert len(existing) >= 9, f'fixture: populated: {len(existing)}'
    if state == 'marks_behind':
        assert world.older in existing
    before = rows_naming(world.path, existing)

    world.transcript(NATIVE_TWO, 'Incomplete store cedar question.', 'Incomplete store cedar answer.')
    if state == 'marks_behind':
        from t12b_seams import compose_marker
        with compose_marker(tmp_path):
            result, recorder = world.capture()
    else:
        result, recorder = world.capture()

    imported = assert_ok(result, imported=True)
    added = sealed_ids(world.path) - existing
    assert len(added) == imported, f'the pass reported {imported} imported rows but sealed {len(added)}'
    checks = whole_store_checks(recorder)
    assert not checks, f'a capture pass on an incomplete projection ran the whole-store check: {checks}'
    after = rows_naming(world.path, existing)
    created = sorted(after - before)
    removed = sorted(before - after)
    assert not created and not removed, (
        f'a capture pass changed the projection of episodes sealed before it ({state}): '
        f'{len(created)} rows created or changed, e.g. {created[:3]}; {len(removed)} removed, e.g. {removed[:3]}')


def test_t10h_p2_host_capture_on_marks_behind_drains_what_it_finds(tmp_path):
    """T12b (ruled exception, Verification, 2026-10-04), the host case of marks behind: a host install drains once
    after its seal, so a host `once` pass drains what it finds. GREEN-IF the pass reports ok, seals the new rows and
    traces no PRAGMA quick_check/integrity_check; outbox rows were waiting before it (positive control) and none after
    it; every sealed episode, the ones sealed before the pass included, is indexed after it; and every projection row
    naming an episode sealed before the pass is unchanged except its coverage mark (`covered`), the outbox rows the
    drain applied being gone."""
    from t12b_seams import outbox_count
    world = _incomplete_world(tmp_path, 'marks_behind')
    existing = sealed_ids(world.path)
    waiting = outbox_count(world.path)
    assert waiting >= len(existing), f'positive control: the seals before the pass wait in the outbox: {waiting}'
    before = rows_naming(world.path, existing, mask=('covered',), skip=('index_outbox',))

    world.transcript(NATIVE_TWO, 'Host store cedar question.', 'Host store cedar answer.')
    result, recorder = world.capture()

    imported = assert_ok(result, imported=True)
    added = sealed_ids(world.path) - existing
    assert len(added) == imported, f'the pass reported {imported} imported rows but sealed {len(added)}'
    assert not whole_store_checks(recorder), 'a host capture pass ran the whole-store check'
    assert outbox_count(world.path) == 0, f'a host seal left {outbox_count(world.path)} outbox rows undrained'
    with closing(sqlite3.connect(world.path.with_name('episode-search.sqlite3'))) as db:
        indexed = {row[0] for row in db.execute('SELECT episode_id FROM indexed_episodes')}
    missing = sorted((existing | added) - indexed)
    assert not missing, f'after a host pass {len(missing)} sealed episodes are not indexed: {missing[:3]}'
    after = rows_naming(world.path, existing, mask=('covered',), skip=('index_outbox',))
    assert after == before, (f'a host capture pass changed the projection of episodes sealed before it beyond their '
                             f'coverage: {sorted(after ^ before)[:3]}')


@pytest.mark.parametrize('state', INCOMPLETE)
def test_t10h_p2_reads_refuse_after_capture_until_upgrade_sources(tmp_path, state):
    """GREEN-IF after one `once` pass on an incomplete projection, memory.connection_status reports
    projection_incomplete and memory.search / memory.list refuse with category projection_incomplete
    and guidance; after `kp-agent-desk ... upgrade-sources` reports complete, they answer, and the
    episode the older writer sealed is listed (marks_behind)."""
    world = _incomplete_world(tmp_path, state)
    world.transcript(NATIVE_TWO, 'Incomplete store cedar question.', 'Incomplete store cedar answer.')
    result, _ = world.capture()
    assert_ok(result, imported=True)

    tools = world.tools()
    status = call(tools, 'memory.connection_status', {})
    assert incomplete(status), f'connection_status does not report projection_incomplete after capture: {status}'
    for name, arguments in (('memory.search', {'query': 'cedar'}), ('memory.list', {'kind': 'episodes'})):
        refused = call(tools, name, arguments)
        assert 'error' in refused, f'{name} answered after capture on an incomplete projection: {str(refused)[:400]}'
        assert refused['error'].get('category') == 'projection_incomplete', refused
        assert refused['error'].get('guidance'), refused

    report = world.upgrade_sources()
    assert report.get('status') == 'complete', report
    tools = world.tools()
    status = call(tools, 'memory.connection_status', {})
    assert not incomplete(status) and status.get('status') == 'ready', status
    listed = call(tools, 'memory.list', {'kind': 'episodes', 'limit': 50})
    assert 'error' not in listed, listed
    found = call(tools, 'memory.search', {'query': 'cedar'})
    assert 'error' not in found, found
    if world.older is not None:
        assert world.older in {e['episode_id'] for e in listed['entries']}, 'the older-writer episode is not listed'


# ---- guard: the operator's step keeps its whole-store check -----------------------------------------

@pytest.mark.parametrize('state', INCOMPLETE)
def test_t10h_p2_guard_operator_upgrade_checks_the_store_before_projecting(tmp_path, state):
    """GUARD (passes at base by design; P2 "the operator's CLI keeps quick_check before any projection
    it does"). GREEN-IF `kp-agent-desk ... upgrade-sources` on an incomplete projection reports complete
    and traces a PRAGMA quick_check on the episode store before its first read of a sealed payload
    (column `payload` of `episodes` or `source_episodes`, per the trace's authorizer): a projection of
    sealed rows is derived from, and verified against, their payloads (T10 P6), so it cannot precede that
    read. Positive control: such a read is traced. Red for a fix that drops the whole-store check from
    the operator's step, or moves it after the projection. A rebuild of episode_desks (P1) reads no
    payload, so it may precede the check."""
    from kp_agent_tooling import desk_cli
    world = _incomplete_world(tmp_path, state)
    target = os.path.realpath(world.path)
    output = io.StringIO()
    with contextlib.redirect_stdout(output), recording() as recorder:
        recorder.phase = 'call'
        code = desk_cli.main(['--config', str(world.config), 'upgrade-sources'])
    assert code == 0 and json.loads(output.getvalue()).get('status') == 'complete', output.getvalue()
    statements = [s for s in recorder.top() if s.db == target]
    check = next((i for i, s in enumerate(statements) if re.search(WHOLE_STORE_CHECK, s.sql, re.I)), None)
    sealed = [i for i, s in enumerate(statements)
              if {('episodes', 'payload'), ('source_episodes', 'payload')} & set(s.reads)]
    assert sealed, 'positive control: upgrade-sources read no sealed payload on an incomplete store'
    assert check is not None, 'upgrade-sources projected an incomplete store without the whole-store check'
    assert check < sealed[0], (
        f'upgrade-sources read sealed payloads to project before its whole-store check: '
        f'{statements[sealed[0]].sql[:160]!r} precedes {statements[check].sql!r}')
