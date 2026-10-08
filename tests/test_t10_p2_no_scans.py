"""T10 P2: no scan of slow-moving tables, and payloads only as needed.

Order: docs/work/orders/T10-read-path-projection.md (P2). Every memory tool
(and the filtered/view search variants), small and large, measured from a
fresh tools object including its construction (``t10_measure``):

* no traced statement has a plan line ``SCAN <t>`` for a protected table
  (``episodes``, ``source_episodes``, ``session_episodes``, ``session_claims``,
  ``source_sessions``, ``source_projects``, ``desk_session_contexts``,
  ``indexed_episodes``), including ``USING COVERING INDEX``;
* statements select named columns (no ``*``);
* sealed ``payload`` is read only by identity (primary key or rowid lookup);
* sealed payloads are returned only for records the call returns or cites,
  and never twice;
* no Python-level file read under the state root (the SQLite files are opened
  by SQLite itself), the imported catalog at most once per call;
* FTS-vs-regex disagreements (``foo_bar`` for "foo bar", ``café bar`` for
  "cafe bar") ranked above the valid match are rejected without reading
  their payloads, and the valid match is returned.
"""
import sqlite3

import pytest

import t10_measure as m
from t10_measure import Counts, CaseRunner

CASES = ['connection_status', 'search-desk', 'search-topic', 'search-explicit-binding',
         'search-filter-attribution', 'search-filter-claims', 'search-filter-session', 'search-view-agent',
         'search-view-repo', 'session', 'bindings', 'resume-own', 'resume-cross', 'handoff_page-cross',
         'status', 'list-episodes', 'list-capsules', 'list-cross', 'handoff', 'handoff-cross',
         'evidence_directory', 'episode_directory', 'episode_directory-topic', 'read_event',
         'read_event-topic', 'propose']
SIZES = ['small', 'large']


@pytest.fixture(scope='module')
def runner(tmp_path_factory):
    counts = Counts(tmp_path_factory.mktemp('t10-p2') / 'world').build()
    return CaseRunner(counts, tmp_path_factory.mktemp('t10-p2-snapshots'))


def _measured(runner, case, size):
    measured = runner.get(case, size)
    assert measured.error is None, f'{case}: refused {measured.failure()} ({measured.error!r})'
    problems = m.expected_reads_present(runner.counts, measured)
    assert not problems, problems
    return measured


@pytest.mark.parametrize('size', SIZES)
@pytest.mark.parametrize('case', CASES)
def test_p2_no_scan_of_slow_moving_tables(runner, case, size):
    recorder = _measured(runner, case, size).recorder
    assert not recorder.plan_errors(), f'instrument: plans unavailable {recorder.plan_errors()}'
    planned = [s for s in recorder.top() if s.plan]
    assert planned, 'positive control: no query plan was taken'
    scans = recorder.scans()
    assert not scans, f'{case}/{size}: {len(scans)} scan(s): ' + '\n'.join(map(str, scans[:12]))


@pytest.mark.parametrize('size', SIZES)
@pytest.mark.parametrize('case', CASES)
def test_p2_statements_select_named_columns(runner, case, size):
    recorder = _measured(runner, case, size).recorder
    stars = recorder.star_selects()
    assert not stars, f'{case}/{size}: ' + '\n'.join(stars[:5])


@pytest.mark.parametrize('size', SIZES)
@pytest.mark.parametrize('case', CASES)
def test_p2_payload_selected_only_by_identity(runner, case, size):
    recorder = _measured(runner, case, size).recorder
    violations = recorder.payload_access_violations()
    assert not violations, f'{case}/{size}: ' + '\n'.join(map(str, violations[:6]))


@pytest.mark.parametrize('size', SIZES)
@pytest.mark.parametrize('case', CASES)
def test_p2_payload_only_for_returned_or_cited_records_once(runner, case, size):
    measured = _measured(runner, case, size)
    episodes, capsules = m.returned_ids(runner.counts, measured)
    problems = []
    for kind, allowed in (('episode', episodes), ('episode-capsule', capsules)):
        read = measured.recorder.payloads(kind)
        extra = set(read) - set(allowed)
        twice = {i for i, n in read.items() if n > 1}
        if extra:
            problems.append(f'{len(extra)} {kind} payload(s) read for records neither returned nor cited')
        if twice:
            problems.append(f'{len(twice)} {kind} payload(s) read more than once')
    assert not problems, f'{case}/{size}: ' + '; '.join(problems)


@pytest.mark.parametrize('size', SIZES)
@pytest.mark.parametrize('case', CASES)
def test_p2_no_file_reads_outside_sqlite(runner, case, size):
    measured = _measured(runner, case, size)
    world = runner.counts.world
    recorder = measured.recorder
    assert recorder.connected(world.ledger_path) >= 1, 'positive control: ledger connection not audited'
    under = recorder.opened_under(world.state)
    assert not under, f'{case}/{size}: files opened under the state root: {under[:5]}'
    catalog = recorder.opened(world.catalog)
    assert catalog <= 1, f'{case}/{size}: imported catalog opened {catalog}x in one call'


def _fts_premise(texts, phrase):
    """In-memory check of the fixture: unicode61 ranks the rejected texts first."""
    with sqlite3.connect(':memory:') as db:
        db.execute("CREATE VIRTUAL TABLE t USING fts5(name UNINDEXED, text, tokenize='unicode61')")
        db.executemany('INSERT INTO t VALUES (?,?)', texts)
        return [row[0] for row in db.execute('SELECT name FROM t WHERE t MATCH ? ORDER BY rank', (phrase,))]


@pytest.mark.parametrize('query,rejected,valid', [
    ('foo bar', 'foo_bar', 'foo_valid'),
    ('cafe bar', 'cafe', 'cafe_valid'),
], ids=['foo_bar', 'cafe'])
def test_p2_regex_rejections_never_read_payload(runner, query, rejected, valid):
    counts = runner.counts
    ids = counts.ids
    template = m.VALID_TEXT.format(phrase=query)
    texts = [(f'rejected-{i}', (f'foo_bar {i}' if rejected == 'foo_bar' else f'café bar {i}')) for i in range(4)]
    ranked = _fts_premise(texts + [('valid', template)], '"' + query + '"')
    assert ranked[-1] == 'valid' and len(ranked) == 5, f'fixture premise: FTS ranking {ranked}'
    measured = m.measure(counts.world, m.A, 'memory.search', {'query': query, 'limit': 1})
    assert measured.error is None, measured.failure()
    returned = [r['episode_id'] for r in measured.output['results']]
    assert returned == [ids[valid]], 'the valid literal match must be the result'
    read = measured.recorder.payloads('episode')
    assert ids[valid] in read, 'positive control: the returned record is reopened from its sealed row'
    rejected_read = set(read) & set(ids[rejected])
    assert not rejected_read, f'{len(rejected_read)} rejected candidate payload(s) read'
    assert set(read) == {ids[valid]} and read[ids[valid]] == 1
