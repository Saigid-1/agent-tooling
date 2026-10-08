"""T12c C1: the measured worlds (the T10 P1 world plus N other matching episodes) and the measurement.

Order: docs/work/orders/T12c-per-desk-postings.md, C1. The fixture, in the order's words: "the T10 P1 world.
The searched desk is held at its 79 episodes. N other episodes match the phrase: for desk scope, half in
another desk of the same tenant and half in another tenant; for topic scope, all in another tenant".

Each family (desk, topic) is built at N = 0, 10,000 and (opted in) 100,000, through public writes only:

- the P1 world (`t10_measure.Counts`, the searched session `sess-alpha`, desk alpha, 79 episodes);
- SESSIONS other source sessions, each claimed by its desk (desk family: the first half beta, tenant one;
  the second half omega, tenant two; topic family: all omega, tenant two). The sessions exist at N = 0 too,
  so N alone changes between the two worlds;
- N episodes whose text holds the phrase, spread round-robin over those sessions;
- one drain (the T12b B4 helper): every outbox row applied, the index current.

Each world is measured at K = 0, then K = 1,000 `desks_changed` rows are written by public claims (one
`session.tag` claim on each of the first K other sessions: no desk changes) with the indexer stopped (no
drain), and measured again (the order's A2 case).

The measurement of one (world, K), in its family's scope (desk family: desk scope; topic family: topic scope):
VM steps and statements of one `memory.search` through a fresh
`EpisodicMemoryTools`, construction included (t10_measure.measure, the T10 P1/P3 instrument), and the wall time
of 7 warm in-process calls on one tools object per world (two calls first, unmeasured; the garbage collector paused
while sampling), as the census probe measured them. The family's worlds are sampled in turn (one call of each world,
seven rounds), so a change in the machine's load during the run reaches every world alike.
"""
from __future__ import annotations

import gc
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import t10_measure as m
import t10_world as w
import t12b_seams as t12b

QUERY = 'lumen marker'
SEARCHED = m.A                  # sess-alpha, desk alpha
OWN_EPISODES = 79               # census A3a: desk alpha in the P1 world
SESSIONS = 1000                 # other sessions, at every N
K = 1000                        # A2: unapplied desks_changed rows
SAMPLES = 7
WARM = 2
STEP_RATIO = 1.5
WALL_RATIO = 2.0


def arguments(scope):
    return {'query': QUERY, 'limit': 20, **({'scope': 'topic'} if scope == 'topic' else {})}


def placement(family, index):
    """(tenant, desk binding, native prefix) of other session `index` in `family`."""
    if family == 'desk' and index < SESSIONS // 2:
        return w.TENANT_ONE, m.BETA, 'other-beta'
    return w.TENANT_TWO, m.OMEGA, 'other-omega'


@dataclass
class Sample:
    steps: int
    statements: int
    output: dict
    wall_ms: list
    error: str | None = None

    @property
    def median_ms(self):
        return statistics.median(self.wall_ms)


@dataclass
class World:
    family: str
    n: int
    counts: object
    sessions: list
    samples: dict = field(default_factory=dict)   # (scope, k) -> Sample
    controls: dict = field(default_factory=dict)  # name -> value
    build_seconds: float = 0.0

    @property
    def world(self):
        return self.counts.world


def steps(world, scope):
    """(Sample without wall samples) of one measured search through a fresh tools object."""
    measured = m.measure(world, SEARCHED, 'memory.search', arguments(scope))
    if measured.error is not None:
        return Sample(measured.steps, measured.count, {}, [], repr(measured.failure()))
    return Sample(measured.steps, measured.count, measured.output, [])


def wall(worlds, scope):
    """{world index: [ms] * SAMPLES}: warm in-process calls, one tools object per world, the worlds in turn."""
    tools = [world.tools(SEARCHED) for world in worlds]
    for each in tools:
        for _ in range(WARM):
            each.call('memory.search', arguments(scope))
    samples = {index: [] for index in range(len(worlds))}
    gc.collect()
    enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(SAMPLES):
            for index, each in enumerate(tools):
                started = time.perf_counter()
                each.call('memory.search', arguments(scope))
                samples[index].append((time.perf_counter() - started) * 1000.0)
    finally:
        if enabled:
            gc.enable()
    return samples


def _other_sessions(world, family):
    sources = world.sources()
    made = []
    for index in range(SESSIONS):
        tenant, desk, prefix = placement(family, index)
        sid = sources.register(tenant_id=tenant, runtime='codex', native_id=f'{prefix}-{index:05d}')
        sources.claim(**w.claim_args(sid, desk))
        made.append(sid)
    return made


def _matching(world, sessions, n):
    sources = world.sources()
    for i in range(n):
        sources.import_episode(session_id=sessions[i % len(sessions)], source_ref=f't12c:c1:{i:06d}',
                               events=w.events(f'Other lumen marker {i:06d} recorded outside the searched scope.'),
                               provenance=w.provenance(f't12c-c1-{i}'))


def _tag_claims(world, sessions, k):
    sources = world.sources()
    for j in range(k):
        sources.claim(**w.claim_args(sessions[j], {'kind': 'tag', 'id': f't12c-k-{j:05d}'}, predicate='session.tag',
                                     recorded_at='2026-09-03T00:00:00Z'))


def _controls(handle, label):
    """Positive controls: the searched desk is the P1 one; at K = 0 the N others are indexed, match and lie outside
    the searched scope; the outbox rows waiting. Plain calls (no recorder); only the family's own scope is read for
    the searched session."""
    world, n, family = handle.world, handle.n, handle.family
    own = w.call(world.tools(SEARCHED), 'memory.search', arguments('desk'))
    handle.controls[f'{label}:own_total'] = own.get('total_episodes')
    if family == 'topic':
        topic = w.call(world.tools(SEARCHED), 'memory.search', arguments('topic'))
        handle.controls[f'{label}:topic_total'] = topic.get('total_episodes')
    if n and label == 'k0':
        omega = w.call(world.tools(m.O), 'memory.search', {'query': QUERY, 'limit': 20, 'scope': 'topic'})
        handle.controls[f'{label}:omega_topic_total'] = omega.get('total_episodes')
        handle.controls[f'{label}:omega_topic_covered'] = omega.get('covered_episodes')
        handle.controls[f'{label}:omega_topic_results'] = len(omega.get('results', []))
        if family == 'desk':
            beta = w.call(world.tools(m.B), 'memory.search', {'query': QUERY, 'limit': 20})
            handle.controls[f'{label}:beta_total'] = beta.get('total_episodes')
            handle.controls[f'{label}:beta_covered'] = beta.get('covered_episodes')
            handle.controls[f'{label}:beta_results'] = len(beta.get('results', []))
    handle.controls[f'{label}:outbox'] = t12b.outbox_count(world.store_path)


def build_world(family, n, root: Path):
    """One world of `family` at `n`: built and drained (K = 0), not yet measured."""
    started = time.monotonic()
    counts = m.Counts(root).build()
    world = counts.world
    with w.frozen_clock():
        sessions = _other_sessions(world, family)
        _matching(world, sessions, n)
    t12b.drain(world.store())
    handle = World(family, n, counts, sessions)
    handle.build_seconds = time.monotonic() - started
    return handle


def _measure(handles, k):
    """The family's own scope only: the desk family is measured in desk scope, the topic family in topic scope."""
    for scope in (handles[0].family,):
        for handle in handles:
            handle.samples[(scope, k)] = steps(handle.world, scope)
        for index, samples in wall([h.world for h in handles], scope).items():
            handles[index].samples[(scope, k)].wall_ms = samples


def build_family(family, sizes, root: Path, *, k=K):
    """The family's worlds at `sizes` (N = 0 first): built and drained, measured at K = 0; then K claims written in
    each (no drain) and measured again."""
    handles = [build_world(family, n, root / f'n{n}' / 'world') for n in sizes]
    for handle in handles:
        _controls(handle, 'k0')
    _measure(handles, 0)
    for handle in handles:
        with w.frozen_clock():
            _tag_claims(handle.world, handle.sessions, k)
        _controls(handle, f'k{k}')
    _measure(handles, k)
    return {handle.n: handle for handle in handles}


def ratio_lines(base: World, grown: World, scope, k):
    b, g = base.samples[(scope, k)], grown.samples[(scope, k)]
    lines = [f'{scope} scope, N={grown.n}, K={k}: VM steps {b.steps} -> {g.steps} '
             f'({g.steps / b.steps:.2f}x, bound {STEP_RATIO}x); median wall {b.median_ms:.2f} -> {g.median_ms:.2f} ms '
             f'({g.median_ms / b.median_ms:.2f}x, bound {WALL_RATIO}x); statements {b.statements} -> {g.statements}',
             f'  raw wall samples N=0 (ms): {", ".join(f"{x:.2f}" for x in b.wall_ms)}',
             f'  raw wall samples N={grown.n} (ms): {", ".join(f"{x:.2f}" for x in g.wall_ms)}']
    if k:
        b0, g0 = base.samples[(scope, 0)], grown.samples[(scope, 0)]
        lines.append(f'  against K=0 at the same N: N=0 {b.steps / b0.steps:.2f}x steps, '
                     f'N={grown.n} {g.steps / g0.steps:.2f}x steps; N={grown.n},K={k} against N=0,K=0: '
                     f'{g.steps / b0.steps:.2f}x steps, {g.median_ms / b0.median_ms:.2f}x wall')
    return lines


def result_view(sample: Sample):
    output = sample.output or {}
    return {'results': [(r['episode_id'], r['event_id']) for r in output.get('results', [])],
            'total': output.get('total_episodes'), 'covered': output.get('covered_episodes'),
            'status': output.get('index_status')}
