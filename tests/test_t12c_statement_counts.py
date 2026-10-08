"""T12c: the desk and topic search statement counts, pinned absolutely (new; census E11).

Order: docs/work/orders/T12c-per-desk-postings.md, Falsifiers: "Desk and topic search statement counts. The census
measured 11 for each (A2). T12c pins its own absolute counts in a new test, and any difference from 11 is stated in
the meet record." T10 P1 pins small == large only; this pins the number.

GREEN-IF one `memory.search` (query 'lumen marker', limit 20) as `sess-alpha` in the T10 P1 world, through a fresh
`EpisodicMemoryTools`, construction included (the T10 P1 count: top-level traced statements, t10_instruments),
runs exactly PINNED[scope] statements, desk and topic. The pin is the census's base number; a head that runs
another number fails here, and the meet records the head's number (the failure message prints it and the trace).
"""
from __future__ import annotations

import pytest

import t10_measure as m

PINNED = {'desk': 11, 'topic': 11}


@pytest.fixture(scope='module')
def p1(tmp_path_factory):
    return m.Counts(tmp_path_factory.mktemp('t12c-statements') / 'world').build()


@pytest.mark.parametrize('scope', ['desk', 'topic'])
def test_t12c_search_statement_count_is_pinned(p1, scope):
    arguments = {'query': m.LUMEN, 'limit': 20, **({'scope': 'topic'} if scope == 'topic' else {})}
    measured = m.measure(p1.world, m.A, 'memory.search', arguments)
    assert measured.error is None, f'{scope} search refused: {measured.failure()}'
    assert len(measured.output['results']) == 20, 'positive control: the search returns 20 results'
    problems = m.expected_reads_present(p1, measured)
    assert not problems, problems
    construct = measured.recorder.count(('construct',))
    call = measured.recorder.count(('call',))
    assert measured.count == PINNED[scope], (
        f'{scope} search: {measured.count} statements (construct {construct} + call {call}), pinned '
        f'{PINNED[scope]}\n{measured.recorder.describe()}')
