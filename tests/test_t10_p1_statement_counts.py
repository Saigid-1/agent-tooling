"""T10 P1: a call's statement count does not depend on its result count.

Order: docs/work/orders/T10-read-path-projection.md (P1), instruments per its
Instruments section (``t10_instruments``). Every count starts from a freshly
constructed ``EpisodicMemoryTools`` and includes its construction.

* ``test_p1_statement_count_independent_of_result_count``: every tool (and the
  filtered/view search variants), small vs large arguments (limit 1 vs 20 or
  the tool maximum; a handoff citing 1 vs 32 episodes): identical statement
  counts, with positive controls that the large case is large and that the
  trace holds the call's expected reads.
* ``test_p1_once_per_call``: within one call, ``PRAGMA quick_check`` at most
  once per database, the ledger admission select at most once, the write-scope
  select at most once, the imported catalog at most once, and no sealed
  payload (episode or capsule) read twice.
* ``test_p1_registry_descriptor_once_per_call``: with an
  ``agent-tooling.desk-registry.v1`` registry, the descriptor file and the
  registry's ``desk_profiles`` select at most once per call.
"""
import pytest

import t10_measure as m
from t10_measure import Counts, CaseRunner


@pytest.fixture(scope='module')
def runner(tmp_path_factory):
    counts = Counts(tmp_path_factory.mktemp('t10-p1') / 'world').build()
    runner = CaseRunner(counts, tmp_path_factory.mktemp('t10-p1-snapshots'))
    from kp_agent_tooling._impl.service.episodic_memory_tools import TOOLS
    covered = {runner.cases[case][1] for case in CASES}
    assert {tool['name'] for tool in TOOLS} <= covered, 'fixture: every tool in TOOLS has a case'
    return runner


CASES = ['connection_status', 'search-desk', 'search-topic', 'search-explicit-binding',
         'search-filter-attribution', 'search-filter-claims', 'search-filter-session', 'search-view-agent',
         'search-view-repo', 'session', 'bindings', 'resume-own', 'resume-cross', 'handoff_page-cross',
         'status', 'list-episodes', 'list-capsules', 'list-cross', 'handoff', 'handoff-cross',
         'evidence_directory', 'episode_directory', 'episode_directory-topic', 'read_event',
         'read_event-topic', 'propose']


def _ok(runner, measured):
    assert measured.error is None, f'{measured.name} refused: {measured.failure()} ({measured.error!r})'
    problems = m.expected_reads_present(runner.counts, measured)
    assert not problems, problems


@pytest.mark.parametrize('case', CASES)
def test_p1_statement_count_independent_of_result_count(runner, case):
    small, large = runner.get(case, 'small'), runner.get(case, 'large')
    _ok(runner, small)
    _ok(runner, large)
    control = runner.cases[case][4]
    assert control(large.output), f'positive control: the large {case} call is not large'
    assert large.count == small.count, (
        f'{case}: {small.count} statements for the small call, {large.count} for the large one\n'
        f'--- large ---\n{large.recorder.describe()}')


def _once_problems(runner, measured):
    world = runner.counts.world
    recorder = measured.recorder
    problems = []
    for db in {s.db for s in recorder.top(('call',))}:
        checks = [s for s in recorder.top(('call',)) if s.db == db and 'quick_check' in s.sql.lower()]
        if len(checks) > 1:
            problems.append(f'PRAGMA quick_check {len(checks)}x on {db}')
    launches = recorder.matching(r'^\s*SELECT\b.*\bdesk_session_launches\b', db=world.ledger_path)
    if len(launches) > 1:
        problems.append(f'ledger admission select {len(launches)}x')
    scopes = recorder.matching(r'^\s*SELECT\b.*\bdesk_write_scopes\b', db=world.ledger_path)
    if len(scopes) > 1:
        problems.append(f'write-scope select {len(scopes)}x')
    catalog = recorder.opened(world.catalog)
    if catalog > 1:
        problems.append(f'imported catalog opened {catalog}x')
    for kind in ('episode', 'episode-capsule'):
        twice = {i: n for i, n in recorder.payloads(kind, phases=('call',)).items() if n > 1}
        if twice:
            problems.append(f'{len(twice)} {kind} payload(s) read more than once')
    return problems


@pytest.mark.parametrize('size', ['small', 'large'])
@pytest.mark.parametrize('case', CASES)
def test_p1_once_per_call(runner, case, size):
    measured = runner.get(case, size)
    _ok(runner, measured)
    problems = _once_problems(runner, measured)
    assert not problems, f'{case}/{size}: ' + '; '.join(problems)


@pytest.fixture(scope='module')
def registry(tmp_path_factory):
    return m.descriptor_world(tmp_path_factory.mktemp('t10-p1-registry'))


@pytest.mark.parametrize('tool,arguments', [
    ('memory.connection_status', {}),
    ('memory.list', {'kind': 'episodes', 'limit': 5}),
    ('memory.search', {'query': 'lumen marker', 'limit': 5}),
    ('memory.status', {'limit': 5}),
    ('memory.bindings', {}),
], ids=['connection_status', 'list', 'search', 'status', 'bindings'])
def test_p1_registry_descriptor_once_per_call(registry, tool, arguments):
    config, descriptor, state = registry
    measured = m.registry_measure(config, tool, arguments)
    assert measured.error is None, measured.failure()
    recorder = measured.recorder
    assert recorder.opened(descriptor, phases=('construct', 'call')) >= 1, \
        'positive control: the registry descriptor read is not in the audit'
    assert measured.output, 'positive control: empty output'
    if tool == 'memory.connection_status':
        assert measured.output.get('status') == 'ready', measured.output
    # Meet (Coordinator): the order bounds admission to at most once per construction-plus-call;
    # it does not require the read to fall in the call phase. A first call may reuse the
    # immutable ledger row its construction just read.
    assert recorder.matching(r'\bdesk_session_launches\b', phases=('construct', 'call')), \
        'positive control: construction or the call checks admission'
    descriptor_reads = recorder.opened(descriptor)
    profile_reads = recorder.matching(r'^\s*SELECT\b.*\bFROM\s+desk_profiles\b')
    problems = []
    if descriptor_reads > 1:
        problems.append(f'registry descriptor opened {descriptor_reads}x')
    if len(profile_reads) > 1:
        problems.append(f'registry desk_profiles select {len(profile_reads)}x')
    quick = [s for s in recorder.top(('call',)) if 'quick_check' in s.sql.lower()]
    by_db = {}
    for s in quick:
        by_db[s.db] = by_db.get(s.db, 0) + 1
    problems += [f'PRAGMA quick_check {n}x on {db}' for db, n in by_db.items() if n > 1]
    assert not problems, f'{tool}: ' + '; '.join(problems)
