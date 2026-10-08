"""Measured tool calls (fresh tools object, construction included) and the
synthetic stores the P1/P2/P3/P4b tests measure on."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import t10_world as w
from t10_instruments import recording

ALPHA = w.binding_key(w.TENANT_ONE, 'alpha')
BETA = w.binding_key(w.TENANT_ONE, 'beta')
GAMMA = w.binding_key(w.TENANT_ONE, 'gamma')
OMEGA = w.binding_key(w.TENANT_TWO, 'omega')
A, A2, B, C, O = 'sess-alpha', 'sess-alpha-2', 'sess-beta', 'sess-gamma', 'sess-omega'
FILLERS = [(w.TENANT_ONE, f'filler-{i:02d}') for i in range(22)]
DESKS = [(w.TENANT_ONE, 'alpha'), (w.TENANT_ONE, 'beta'), (w.TENANT_ONE, 'gamma'), *FILLERS,
         (w.TENANT_TWO, 'omega')]
SESSIONS = {A: (w.TENANT_ONE, 'alpha'), A2: (w.TENANT_ONE, 'alpha'), B: (w.TENANT_ONE, 'beta'),
            C: (w.TENANT_ONE, 'gamma'), O: (w.TENANT_TWO, 'omega')}
PROFILE_DESK = 'desk:0f6c1a2b-3d4e-4f50-8a1b-2c3d4e5f6a7b'


@dataclass
class Measured:
    recorder: object
    output: object
    error: BaseException | None
    name: str
    arguments: dict

    @property
    def count(self):
        return self.recorder.count()

    @property
    def steps(self):
        return self.recorder.vm_steps()

    def failure(self):
        return None if self.error is None else w.failure(self.name, self.error)


def measure(world, session, name, arguments, *, step_cap=None):
    """Construct a fresh tools object and make one call, recording both."""
    from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools
    store = world.store(session)
    output = error = None
    with recording(step_cap=step_cap) as recorder:
        recorder.phase = 'construct'
        try:
            tools = EpisodicMemoryTools(store, session)
            recorder.phase = 'call'
            output = tools.call(name, arguments)
        except Exception as caught:  # noqa: BLE001 - the measurement records it
            error = caught
    return Measured(recorder, output, error, name, arguments)


class Counts:
    """The P1/P2 store: every tool has a small and a large argument set."""

    def __init__(self, root: Path):
        self.world = w.World(root, DESKS, SESSIONS)
        self.ids = {}
        self.capsule_sources = {}

    def build(self):
        world = self.world
        with w.frozen_clock():
            world.initialize()
            store, sources = world.store(), world.sources()
            ids = self.ids
            ids['S_LUM'] = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='lumen')
            sources.claim(**w.claim_args(ids['S_LUM'], ALPHA))
            lumen = []
            for i in range(32):
                receipt = sources.import_episode(
                    session_id=ids['S_LUM'], source_ref=f'lumen:{i:02d}',
                    events=w.events(f'Lumen marker {i:02d} records the alpha decision.', f'Detail {i} only.'),
                    provenance=w.provenance(f'lumen-{i}', coordinates={'path': '/t10/native/lumen.jsonl',
                                                                       'start': i * 100, 'end': i * 100 + 90}))
                lumen.append(receipt['episode_id'])
            ids['lumen'] = lumen
            ids['BIG'] = sources.import_episode(
                session_id=ids['S_LUM'], source_ref='lumen:big',
                events=w.events(*[f'Big episode event {i} ' + 'x' * 40 for i in range(25)]),
                provenance=w.provenance('lumen-big'))['episode_id']
            ids['legacy_alpha'] = [store.capture(A, source_ref=f'alpha:{i:02d}', events=w.events(
                f'Lumen marker legacy {i:02d} kept by alpha.'))['episode_id'] for i in range(32)]
            ids['legacy_alpha2'] = store.capture(A2, source_ref='alpha2:0', events=w.events(
                'Second alpha session lumen marker note.'))['episode_id']
            ids['S_BETA'] = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='beta-notes')
            sources.claim(**w.claim_args(ids['S_BETA'], BETA))
            ids['beta'] = [sources.import_episode(
                session_id=ids['S_BETA'], source_ref=f'beta:{i}', events=w.events(f'Beta notes {i}; lumen marker too.'),
                provenance=w.provenance(f'beta-{i}'))['episode_id'] for i in range(10)]
            ids['legacy_beta'] = [store.capture(B, source_ref=f'beta:{i}', events=w.events(
                f'Beta legacy {i}.'))['episode_id'] for i in range(3)]
            ids['legacy_gamma'] = store.import_operator_episode(
                tenant_id=w.TENANT_ONE, role='gamma', repo_key=w.REPO, source_ref='legacy:gamma',
                events=w.imported_events('Gamma imported lumen marker.'),
                source_provenance=w.legacy_provenance('gamma-0'))['episode_id']
            ids['S_UNRES'] = sources.register(tenant_id=w.TENANT_ONE, runtime='codex', native_id='unresolved')
            ids['unresolved'] = [sources.import_episode(
                session_id=ids['S_UNRES'], source_ref=f'unres:{i}', events=w.events(f'Unresolved lumen marker {i}.'),
                provenance=w.provenance(f'unres-{i}'))['episode_id'] for i in range(5)]
            ids['S_CONF'] = sources.register(tenant_id=w.TENANT_ONE, runtime='codex', native_id='conflict')
            sources.claim(**w.claim_args(ids['S_CONF'], ALPHA))
            sources.claim(**w.claim_args(ids['S_CONF'], BETA))
            ids['conflicting'] = [sources.import_episode(
                session_id=ids['S_CONF'], source_ref=f'conf:{i}', events=w.events(f'Conflicting lumen marker {i}.'),
                provenance=w.provenance(f'conf-{i}'))['episode_id'] for i in range(3)]
            ids['S_OMEGA'] = sources.register(tenant_id=w.TENANT_TWO, runtime='claude', native_id='omega')
            sources.claim(**w.claim_args(ids['S_OMEGA'], OMEGA))
            ids['omega'] = [sources.import_episode(
                session_id=ids['S_OMEGA'], source_ref=f'omega:{i}', events=w.events(f'Omega lumen marker {i}.'),
                provenance=w.provenance(f'omega-{i}'))['episode_id'] for i in range(5)]
            store.capture(O, source_ref='omega:legacy', events=w.events('Omega legacy lumen marker.'))
            ids['S_CLAIMS'] = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='claims')
            sources.claim(**w.claim_args(ids['S_CLAIMS'], ALPHA))
            for i in range(24):
                sources.claim(**w.claim_args(ids['S_CLAIMS'], {'kind': 'tag', 'id': f'tag-{i:02d}'},
                                             predicate='session.tag'))
            sources.claim(**w.claim_args(ids['S_LUM'], {'kind': 'actor', 'id': 'person:ana'},
                                         predicate='session.contributor'))
            # FTS-vs-regex disagreements ranked above the valid matches (desk alpha).
            ids['S_FTS'] = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='fts')
            sources.claim(**w.claim_args(ids['S_FTS'], ALPHA))
            ids['foo_bar'] = [sources.import_episode(session_id=ids['S_FTS'], source_ref=f'fu:{i}',
                              events=w.events(f'foo_bar {i}'), provenance=w.provenance(f'fu-{i}'))['episode_id']
                              for i in range(4)]
            ids['cafe'] = [sources.import_episode(session_id=ids['S_FTS'], source_ref=f'ca:{i}',
                           events=w.events(f'café bar {i}'), provenance=w.provenance(f'ca-{i}'))['episode_id']
                           for i in range(4)]
            ids['foo_valid'] = sources.import_episode(
                session_id=ids['S_FTS'], source_ref='fu:valid', provenance=w.provenance('fu-valid'),
                events=w.events(VALID_TEXT.format(phrase='foo bar')))['episode_id']
            ids['cafe_valid'] = sources.import_episode(
                session_id=ids['S_FTS'], source_ref='ca:valid', provenance=w.provenance('ca-valid'),
                events=w.events(VALID_TEXT.format(phrase='cafe bar')))['episode_id']
            # Profile views over the lumen session.
            from kp_agent_tooling._impl.service.desk_profiles import DeskProfiles
            profiles = DeskProfiles(store, A)
            profiles.initialize()
            profiles.save(dict(desk_id=PROFILE_DESK, name='Lumen', description='Lumen work.', role='Coordinator',
                               expected_version=0))
            profiles.annotate(dict(source_session_id=ids['S_LUM'], desk_id=PROFILE_DESK, repos=['repo-lumen'],
                                   adrs=[], cards=[], account_ref=None, provider='fixture', model=None,
                                   expected_version=0, source_ref='fixture:t10'))
            world.index().rebuild()
            # Capsules: one citing 1 episode, one citing 32, and 20 small ones.
            cited = ids['legacy_alpha']
            self.capsule('K1', store, cited[:1])
            self.capsule('K32', store, cited)
            for i in range(20):
                self.capsule(f'K_small_{i:02d}', store, cited[i:i + 2], label=str(i))
        return self

    def capsule(self, name, store, episodes, label=''):
        """Seal a capsule the way ``memory.propose`` does (operator-side, no tool call)."""
        arguments = propose_arguments(episodes, label)
        result = store.consolidate(A, episode_ids=arguments['episode_ids'], items=arguments['items'],
                                   unresolved_questions=arguments['unresolved_questions'],
                                   budget_bytes=arguments['budget_bytes'], qualifier_scope='cited_events')
        self.ids[name] = result['capsule_id']
        self.capsule_sources[result['capsule_id']] = list(episodes)
        return result


VALID_TEXT = ('Long alpha record listing many unrelated planning words, budgets, reviews, schedules, owners, '
              'risks and dependencies before it finally states {phrase} as a plain literal phrase here.')


def propose_arguments(episodes, label=''):
    """A proposal whose handoff cites ``episodes`` (its ``source_episode_ids``).

    One item quotes the first episode; the other source episodes stay in the
    capsule's evidence directory, so 32 sources fit the handoff byte budget.
    """
    episodes = list(episodes)
    return {'episode_ids': episodes,
            'items': [{'kind': 'observation', 'text': f'Lumen {label}',
                       'citations': [{'episode_id': episodes[0], 'event_id': 'e0', 'start': 0, 'end': 5,
                                      'quote': 'Lumen'}]}],
            'unresolved_questions': [], 'budget_bytes': 24000}


def returned_ids(counts, measured):
    """Episode IDs a call returns or cites; capsule IDs it returns."""
    name, arguments, output = measured.name, measured.arguments, measured.output
    episodes, capsules = set(), set()
    if output is None:
        return episodes, capsules

    def capsule_rows(rows):
        for entry in rows:
            capsules.add(entry['capsule_id'])
            episodes.update(entry.get('source_episode_ids', []))

    if name == 'memory.search':
        episodes.update(r['episode_id'] for r in output['results'])
    elif name == 'memory.list':
        if arguments['kind'] == 'episodes':
            episodes.update(e['episode_id'] for e in output['entries'])
        else:
            capsule_rows(output['entries'])
    elif name == 'memory.status':
        episodes.update(e['episode_id'] for e in output['episodes']['entries'])
        capsule_rows(output['capsules']['entries'])
    elif name in ('memory.read_event', 'memory.episode_directory'):
        episodes.add(arguments['episode_id'])
    elif name in ('memory.handoff', 'memory.evidence_directory', 'memory.resume', 'memory.handoff_page'):
        capsules.add(arguments['capsule_id'])
        episodes.update(counts.capsule_sources.get(arguments['capsule_id'], []))
    elif name == 'memory.propose':
        episodes.update(arguments['episode_ids'])
        capsules.add(output['capsule_id'])
    return episodes, capsules


def descriptor_world(root: Path):
    """A small ``agent-tooling.desk-registry.v1`` store: descriptor-based registry."""
    root.mkdir(parents=True, exist_ok=True)
    state = root / 'state'
    state.mkdir(mode=0o700, exist_ok=True)
    descriptor = w.private_write(root / 'registry.json', {
        'schema_version': 'agent-tooling.desk-registry.v1', 'tenant_id': 't10-registry',
        'roster_path': str(root / 'roster.json')})
    config = w.private_write(root / 'registry-session.json', {
        'schema_version': 'ops.desk-memory.local.v1', 'state_root': str(state), 'catalog_path': str(descriptor),
        'workspace_root': str(root), 'provider_instance': w.INSTANCE, 'provider_session_id': 'registry-session'})
    from kp_agent_tooling._impl.service.desk_memory_runtime import admit, components, initialize
    from kp_agent_tooling._impl.service.desk_profiles import DeskProfiles
    with w.frozen_clock():
        initialize(config)
        store = components(config)[3]
        profiles = DeskProfiles(store, 'registry-session')
        profiles.initialize()
        desk = profiles.save(dict(desk_id='desk:1a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d', name='Registry desk',
                                  description='Registry mode desk.', role='general', expected_version=0))
        admit(config, desk_id=desk['desk_id'], provider_id='fixture', model_id='t10-model')
        store = components(config)[3]
        for i in range(6):
            store.capture('registry-session', source_ref=f'registry:{i}',
                          events=w.events(f'Registry lumen marker {i}.'))
        from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
        EpisodicSearchIndex(store.path.with_name(w.INDEX_NAME), episode_store=store).rebuild()
    return config, descriptor, state


def registry_measure(config, name, arguments):
    from kp_agent_tooling._impl.service.desk_memory_runtime import components
    from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools
    store = components(config)[3]
    session = json.loads(Path(config).read_text())['provider_session_id']
    output = error = None
    with recording() as recorder:
        recorder.phase = 'construct'
        try:
            tools = EpisodicMemoryTools(store, session)
            recorder.phase = 'call'
            output = tools.call(name, arguments)
        except Exception as caught:  # noqa: BLE001
            error = caught
    return Measured(recorder, output, error, name, arguments)


# --- the P1/P2 case table -----------------------------------------------------------

LUMEN = 'lumen marker'


def counts_cases(counts):
    """``{case: (session, tool, small_arguments, large_arguments, large_control)}``.

    ``large_control(output)`` is the positive control that the large case is
    really large (limit 20 returns 20, the handoff cites 32 episodes, ...).
    """
    ids = counts.ids
    k1, k32 = ids['K1'], ids['K32']
    cited = ids['legacy_alpha']

    def results(n):
        return lambda out: len(out['results']) == n

    def entries(n):
        return lambda out: len(out['entries']) == n

    def cites_32(out):
        return len(counts.capsule_sources[k32]) == 32

    search = lambda **extra: ({'query': LUMEN, 'limit': 1, **extra}, {'query': LUMEN, 'limit': 20, **extra})
    cases = {
        'connection_status': (A, 'memory.connection_status', {}, {}, lambda out: out['status'] == 'ready'),
        'search-desk': (A, 'memory.search', *search(), results(20)),
        'search-topic': (A, 'memory.search', *search(scope='topic'), results(20)),
        'search-explicit-binding': (B, 'memory.search', *search(binding_key=ALPHA), results(20)),
        'search-filter-attribution': (A, 'memory.search', *search(scope='topic', attribution='resolved'), results(20)),
        'search-filter-claims': (A, 'memory.search', *search(scope='topic', claims=[
            {'predicate': 'session.contributor', 'object': {'kind': 'actor', 'id': 'person:ana'}}]), results(20)),
        'search-filter-session': (A, 'memory.search', *search(scope='topic', session_id=ids['S_LUM']), results(20)),
        'search-view-agent': (A, 'memory.search', *search(view={'kind': 'agent', 'value': PROFILE_DESK}), results(20)),
        'search-view-repo': (A, 'memory.search', *search(view={'kind': 'repo', 'value': 'repo-lumen'}), results(20)),
        'session': (A, 'memory.session', {'session_id': ids['S_CLAIMS'], 'limit': 1},
                    {'session_id': ids['S_CLAIMS'], 'limit': 20}, lambda out: len(out['claims']) == 20),
        'bindings': (A, 'memory.bindings', {'limit': 1}, {'limit': 20}, lambda out: len(out['bindings']) == 20),
        'resume-own': (A, 'memory.resume', {'capsule_id': k1, 'budget_bytes': 24000},
                       {'capsule_id': k32, 'budget_bytes': 24000}, cites_32),
        'resume-cross': (B, 'memory.resume', {'capsule_id': k1, 'binding_key': ALPHA, 'budget_bytes': 24000},
                         {'capsule_id': k32, 'binding_key': ALPHA, 'budget_bytes': 24000}, cites_32),
        'handoff_page-cross': (B, 'memory.handoff_page', {'capsule_id': k1, 'binding_key': ALPHA},
                               {'capsule_id': k32, 'binding_key': ALPHA}, cites_32),
        'status': (A, 'memory.status', {'limit': 1}, {'limit': 20},
                   lambda out: len(out['episodes']['entries']) == 20 and len(out['capsules']['entries']) == 20),
        'list-episodes': (A, 'memory.list', {'kind': 'episodes', 'limit': 1}, {'kind': 'episodes', 'limit': 20},
                          entries(20)),
        'list-capsules': (A, 'memory.list', {'kind': 'capsules', 'limit': 1}, {'kind': 'capsules', 'limit': 20},
                          entries(20)),
        'list-cross': (B, 'memory.list', {'kind': 'episodes', 'binding_key': ALPHA, 'limit': 1},
                       {'kind': 'episodes', 'binding_key': ALPHA, 'limit': 20}, entries(20)),
        'handoff': (A, 'memory.handoff', {'capsule_id': k1}, {'capsule_id': k32},
                    lambda out: len(out['source_episode_ids']) == 32),
        'handoff-cross': (B, 'memory.handoff', {'capsule_id': k1, 'binding_key': ALPHA},
                          {'capsule_id': k32, 'binding_key': ALPHA}, lambda out: len(out['source_episode_ids']) == 32),
        'evidence_directory': (A, 'memory.evidence_directory', {'capsule_id': k1, 'limit': 1},
                               {'capsule_id': k32, 'limit': 20}, lambda out: len(out['entries']) == 20 and cites_32(out)),
        'episode_directory': (A, 'memory.episode_directory', {'episode_id': ids['BIG'], 'limit': 1},
                              {'episode_id': ids['BIG'], 'limit': 20}, entries(20)),
        'episode_directory-topic': (B, 'memory.episode_directory', {'episode_id': ids['BIG'], 'scope': 'topic', 'limit': 1},
                                    {'episode_id': ids['BIG'], 'scope': 'topic', 'limit': 20}, entries(20)),
        'read_event': (A, 'memory.read_event', {'episode_id': ids['BIG'], 'event_id': 'e0', 'length': 1},
                       {'episode_id': ids['BIG'], 'event_id': 'e0', 'length': 8000},
                       lambda out: len(out['text']) > 1),
        'read_event-topic': (B, 'memory.read_event', {'episode_id': ids['BIG'], 'event_id': 'e0', 'scope': 'topic',
                                                      'length': 1},
                             {'episode_id': ids['BIG'], 'event_id': 'e0', 'scope': 'topic', 'length': 8000},
                             lambda out: len(out['text']) > 1),
        'propose': (A, 'memory.propose', propose_arguments(cited[:1], 'small'), propose_arguments(cited, 'large'),
                    lambda out: len(out['handoff']['source_episode_ids']) == 32),
    }
    return cases


WRITING_TOOLS = ('memory.propose',)


class CaseRunner:
    """Measure each (case, size) once on a Counts store; writes are rolled back."""

    def __init__(self, counts, scratch: Path):
        self.counts = counts
        self.cases = counts_cases(counts)
        self.scratch = scratch
        self.cache = {}

    def get(self, case, size):
        key = (case, size)
        if key not in self.cache:
            session, tool, small, large, _ = self.cases[case]
            arguments = small if size == 'small' else large
            saved = None
            if tool in WRITING_TOOLS:
                saved = self.scratch / f'snapshot-{case}-{size}'
                self.counts.world.snapshot(saved)
            try:
                self.cache[key] = measure(self.counts.world, session, tool, arguments)
            finally:
                if saved is not None:
                    self.counts.world.restore(saved)
        return self.cache[key]


def expected_reads_present(counts, measured, world=None):
    """Positive control: the trace is non-empty and holds the call's expected reads."""
    recorder = measured.recorder
    problems = []
    if not recorder.top(('call',)):
        problems.append('no statement traced during the call')
    ledger = (world or counts.world).ledger_path
    if not recorder.matching(r'\bdesk_session_launches\b', phases=('construct', 'call'), db=ledger):
        problems.append('admission read of the ledger is not in the trace')
    episodes, capsules = returned_ids(counts, measured)
    if measured.name in ('memory.search', 'memory.list', 'memory.status', 'memory.read_event',
                         'memory.episode_directory'):
        returned = set(episodes) if measured.name != 'memory.list' or measured.arguments['kind'] == 'episodes' else set()
        if measured.name == 'memory.status':
            returned = {e['episode_id'] for e in measured.output['episodes']['entries']}
        missing = returned - set(recorder.payloads('episode'))
        if missing:
            problems.append(f'{len(missing)} returned record(s) not reopened from their sealed rows in the trace')
    if capsules and measured.name != 'memory.propose':
        if not set(recorder.payloads('episode-capsule')) & capsules:
            problems.append('capsule payload read not in the trace')
    return problems


# --- P3: a base store of >= 1,000 episodes and its tenfold growths ---------------------

def _fill(world, *, tenant, owner, sessions, per_session, prefix, start=0):
    """``sessions`` source sessions of ``per_session`` episodes; no query text."""
    sources = world.sources()
    made = 0
    for s in range(start, start + sessions):
        sid = sources.register(tenant_id=tenant, runtime='codex', native_id=f'{prefix}-{s:05d}')
        if owner is not None:
            sources.claim(**w.claim_args(sid, owner))
        for j in range(per_session):
            sources.import_episode(session_id=sid, source_ref=f'{prefix}:{s:05d}:{j}',
                                   events=w.events(f'Routine {prefix} note {s}-{j} about schedules.'),
                                   provenance=w.provenance(f'{prefix}-{s}-{j}',
                                                           coordinates={'path': f'/t10/native/{prefix}-{s}.jsonl',
                                                                        'start': j * 10, 'end': j * 10 + 9}))
            made += 1
    return made


def _captures(world, session, count, prefix, start=0):
    store = world.store(session)
    for i in range(start, start + count):
        store.capture(session, source_ref=f'{prefix}:{i:05d}', events=w.events(f'Routine {prefix} capture {i}.'))
    return count


def census(world):
    """Episode populations of a world, from the base ``select`` oracle and plain SQL."""
    import sqlite3
    from contextlib import closing
    import t10_oracle
    with closing(sqlite3.connect(world.store_path)) as db:
        total = db.execute('SELECT (SELECT count(*) FROM episodes)+(SELECT count(*) FROM source_episodes)').fetchone()[0]
        sessions = db.execute('SELECT count(*) FROM source_sessions').fetchone()[0]
        other_tenant = db.execute('SELECT count(*) FROM source_episodes WHERE tenant=?', (w.TENANT_TWO,)).fetchone()[0]
        other_tenant += db.execute('SELECT count(*) FROM episodes WHERE binding=?', (OMEGA,)).fetchone()[0]
    own, _ = t10_oracle.select_for(world, A)
    topic, _ = t10_oracle.select_for(world, A, scope='topic')
    return {'total': total, 'sessions': sessions, 'own': len(own), 'unrelated_desk': total - len(own),
            'other_tenant': other_tenant, 'topic': len(topic)}


class History(Counts):
    """Counts plus filler history: >= 1,000 episodes across several desks."""

    def build(self):
        super().build()
        world = self.world
        with w.frozen_clock():
            _fill(world, tenant=w.TENANT_ONE, owner=ALPHA, sessions=40, per_session=4, prefix='own')
            _fill(world, tenant=w.TENANT_ONE, owner=BETA, sessions=50, per_session=4, prefix='beta')
            _fill(world, tenant=w.TENANT_ONE, owner=GAMMA, sessions=50, per_session=4, prefix='gamma')
            _captures(world, B, 40, 'beta-capture')
            _fill(world, tenant=w.TENANT_ONE, owner=None, sessions=20, per_session=4, prefix='unowned')
            _fill(world, tenant=w.TENANT_TWO, owner=OMEGA, sessions=75, per_session=4, prefix='omega')
            _captures(world, O, 10, 'omega-capture')
            world.index().rebuild()
        self.census = census(world)
        return self

    def grow(self, label, root: Path, snapshot: Path):
        """A copy of the base store grown tenfold in one population (label: a-desk, a-topic, b)."""
        grown = w.World(root, DESKS, SESSIONS)
        grown.restore(snapshot)
        base = self.census
        with w.frozen_clock():
            if label == 'a-desk':
                extra = 9 * base['unrelated_desk']
                quarter = extra // 4
                made = _fill(grown, tenant=w.TENANT_ONE, owner=BETA, sessions=quarter // 4, per_session=4,
                             prefix='grow-beta')
                made += _fill(grown, tenant=w.TENANT_ONE, owner=GAMMA, sessions=quarter // 4, per_session=4,
                              prefix='grow-gamma')
                made += _fill(grown, tenant=w.TENANT_ONE, owner=None, sessions=quarter // 4, per_session=4,
                              prefix='grow-unowned')
                made += _captures(grown, B, 40, 'grow-beta-capture')
                rest = extra - made
                made += _fill(grown, tenant=w.TENANT_TWO, owner=OMEGA, sessions=rest // 4, per_session=4,
                              prefix='grow-omega')
                made += _captures(grown, O, extra - made, 'grow-omega-capture')
            elif label == 'a-topic':
                extra = 9 * base['other_tenant']
                made = _fill(grown, tenant=w.TENANT_TWO, owner=OMEGA, sessions=extra // 4, per_session=4,
                             prefix='grow-omega')
                made += _captures(grown, O, extra - made, 'grow-omega-capture')
            elif label == 'b':
                extra = 9 * base['own']
                made = _captures(grown, A, 9 * 32, 'grow-alpha-capture')
                made += _fill(grown, tenant=w.TENANT_ONE, owner=ALPHA, sessions=(extra - made) // 4,
                              per_session=4, prefix='grow-own')
                made += _fill(grown, tenant=w.TENANT_ONE, owner=ALPHA, sessions=1, per_session=extra - made,
                              prefix='grow-own-rest') if extra - made else 0
            else:
                raise ValueError(label)
            grown.index().rebuild()
        return grown
