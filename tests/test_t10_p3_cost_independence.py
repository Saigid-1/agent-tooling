"""T10 P3: cost does not grow with history.

Order: docs/work/orders/T10-read-path-projection.md (P3; P3(c) moved to T12
and is not tested here). The base store holds at least 1,000 episodes across
several desks and two tenants (``t10_measure.History``). Three copies of it
grow tenfold, through public writes, in one population each, with no
matching text:

* ``a-desk``: episodes and sessions outside the caller's desk scope (other
  desks of the tenant, unresolved sessions, the other tenant);
* ``a-topic``: the other tenant only (topic scope's "unrelated");
* ``b``: the caller's own desk.

For every tool called unfiltered (desk and topic scope), and for the
filtered/view search calls under (a), the statement count is identical and
the VM steps grow at most 1.25x, construction of a fresh tools object
included. Positive controls: limit 20 returns 20, the handoff cites 32
episodes, the trace holds the call's expected reads.
"""
import pytest

import t10_measure as m
from t10_measure import History

RATIO = 1.25
TOPIC = {'search-topic', 'episode_directory-topic', 'read_event-topic', 'search-filter-attribution',
         'search-filter-claims', 'search-filter-session', 'search-view-agent', 'search-view-repo'}
FILTERED = {'search-filter-attribution', 'search-filter-claims', 'search-filter-session', 'search-view-agent',
            'search-view-repo'}
ALL = ['connection_status', 'search-desk', 'search-topic', 'search-explicit-binding',
       'search-filter-attribution', 'search-filter-claims', 'search-filter-session', 'search-view-agent',
       'search-view-repo', 'session', 'bindings', 'resume-own', 'resume-cross', 'handoff_page-cross',
       'status', 'list-episodes', 'list-capsules', 'list-cross', 'handoff', 'handoff-cross',
       'evidence_directory', 'episode_directory', 'episode_directory-topic', 'read_event',
       'read_event-topic', 'propose']
PARAMS = ([('a-desk', c) for c in ALL if c not in TOPIC] +
          [('a-topic', c) for c in ALL] +
          [('b', c) for c in ALL if c not in FILTERED])


class Worlds:
    def __init__(self, factory):
        self.history = History(factory.mktemp('t10-p3') / 'base').build()
        self.snapshot = factory.mktemp('t10-p3-snapshot')
        self.history.world.snapshot(self.snapshot)
        self.factory = factory
        self.worlds = {'base': self.history.world}
        self.cases = m.counts_cases(self.history)
        self.cache = {}
        self.census = {'base': self.history.census}

    def world(self, label):
        if label not in self.worlds:
            self.worlds[label] = self.history.grow(label, self.factory.mktemp(f't10-p3-{label}') / 'world',
                                                   self.snapshot)
            self.census[label] = m.census(self.worlds[label])
        return self.worlds[label]

    def measure(self, label, case, step_cap=None):
        key = (label, case)
        if key not in self.cache:
            world = self.world(label)
            session, tool, _, large, _ = self.cases[case]
            saved = None
            if tool in m.WRITING_TOOLS:
                saved = self.factory.mktemp(f't10-p3-undo-{label}-{case}')
                world.snapshot(saved)
            try:
                self.cache[key] = m.measure(world, session, tool, large, step_cap=step_cap)
            finally:
                if saved is not None:
                    world.restore(saved)
        return self.cache[key]


@pytest.fixture(scope='module')
def worlds(tmp_path_factory):
    worlds = Worlds(tmp_path_factory)
    # Fixture checks: >= 1,000 episodes at base, and each copy grown tenfold in one population only.
    base = worlds.census['base']
    assert base['total'] >= 1000, base
    a_desk, a_topic, b = (m.census(worlds.world(label)) for label in ('a-desk', 'a-topic', 'b'))
    assert a_desk['unrelated_desk'] >= 10 * base['unrelated_desk'] and a_desk['own'] == base['own'], (base, a_desk)
    assert a_desk['sessions'] >= 5 * base['sessions'], (base, a_desk)
    assert a_topic['other_tenant'] >= 10 * base['other_tenant'] and a_topic['topic'] == base['topic'], (base, a_topic)
    assert b['own'] >= 10 * base['own'] and b['total'] - b['own'] == base['total'] - base['own'], (base, b)
    return worlds


def _controls(worlds, case, measured, label):
    assert measured.error is None, f'{label}: {case} refused {measured.failure()} ({measured.error!r})'
    assert worlds.cases[case][4](measured.output), f'{label}: positive control, the {case} call is not large'
    problems = m.expected_reads_present(worlds.history, measured, worlds.world(label))
    assert not problems, f'{label}: {problems}'


@pytest.mark.parametrize('growth,case', PARAMS, ids=[f'{g}-{c}' for g, c in PARAMS])
def test_p3_cost_independent_of_history(worlds, growth, case):
    base = worlds.measure('base', case)
    _controls(worlds, case, base, 'base')
    cap = int(4 * RATIO * base.steps) + 20000
    grown = worlds.measure(growth, case, step_cap=cap)
    assert not grown.recorder.capped, (
        f'{growth}/{case}: VM steps passed {cap} (base {base.steps}); stopped early, far above {RATIO}x')
    _controls(worlds, case, grown, growth)
    assert grown.count == base.count, (
        f'{growth}/{case}: {base.count} statements at base, {grown.count} after tenfold growth\n'
        f'--- grown ---\n{grown.recorder.describe()}')
    assert grown.steps <= RATIO * base.steps, (
        f'{growth}/{case}: VM steps {base.steps} -> {grown.steps} ({grown.steps / base.steps:.2f}x > {RATIO}x)')
