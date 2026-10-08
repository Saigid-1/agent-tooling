"""The T10 P5 corpus: public writes, each followed by a battery of reads.

The order's P5 corpus list, step by step (see ``STEPS``). After every write
step the same battery of memory-tool reads runs (``battery``), and the
oracle comparisons (``oracle_mismatches``) are taken. The golden generator records
the battery outputs at the base SHA; the P5 test replays the same steps on
the code under test and compares.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path

import t10_world as w

A, A2, B, C, O = 'sess-alpha', 'sess-alpha-2', 'sess-beta', 'sess-gamma', 'sess-omega'
DESKS = [(w.TENANT_ONE, 'alpha'), (w.TENANT_ONE, 'beta'), (w.TENANT_ONE, 'gamma'),
         (w.TENANT_ONE, 'delta'), (w.TENANT_TWO, 'omega')]
SESSIONS = {A: (w.TENANT_ONE, 'alpha'), A2: (w.TENANT_ONE, 'alpha'), B: (w.TENANT_ONE, 'beta'),
            C: (w.TENANT_ONE, 'gamma'), O: (w.TENANT_TWO, 'omega')}
ALPHA, BETA, GAMMA, DELTA, OMEGA = (w.binding_key(t, r) for t, r in DESKS)
PROFILE_DESK = 'desk:5b0c6d2e-7f1a-4c3b-9d8e-0a1b2c3d4e5f'
FILE_A = '/t10/native/s1-a.jsonl'
FILE_B = '/t10/native/s1-b.jsonl'


# T12b R2: the one step that seals and deliberately leaves its seal for the indexer (`index=False`, no later
# upsert in the step); every other step is drained by the helper before its reads, so `index_lag` reads 0 there.
UNDRAINED_STEPS = ('capture-alpha-unindexed',)


def _drains():
    from t12b_seams import has_drain
    return has_drain()


def t12b_drain(store):
    from t12b_seams import drain
    return drain(store)


class Context:
    def __init__(self, root):
        self.world = w.World(Path(root), DESKS, SESSIONS)
        self.ids = {}        # name -> episode/capsule/session/claim id
        self.events = {}     # episode name -> first event id
        self.sentinels = []  # episode names read individually after every step

    def store(self):
        return self.world.store()

    def sources(self):
        return self.world.sources()

    def index(self):
        return self.world.index()

    def upsert(self, *names):
        # T12b R2 (ruled exception, Verification, 2026-10-04): where the product has the one indexer, the corpus's
        # index helper drains the outbox (tests/t12b_seams.py, the B4 helper) instead of writing the index
        # itself; at base (the golden generator) it upserts as it always did.
        if _drains():
            return t12b_drain(self.store())
        return self.index().upsert_episodes([self.ids[n] for n in names])


# --- write steps -------------------------------------------------------------------

def _capture(ctx, name, session, *texts, index=True):
    receipt = ctx.store().capture(session, source_ref='visible:' + name, events=w.events(*texts))
    ctx.ids[name] = receipt['episode_id']
    ctx.events[name] = 'e0'
    if index:
        ctx.upsert(name)
    return receipt


def _legacy(ctx, name, role, *texts):
    receipt = ctx.store().import_operator_episode(
        tenant_id=w.TENANT_ONE, role=role, repo_key=w.REPO, source_ref='legacy:' + name,
        events=w.imported_events(*texts), source_provenance=w.legacy_provenance('row-' + name))
    ctx.ids[name] = receipt['episode_id']
    ctx.events[name] = 'import-000'
    ctx.upsert(name)
    return receipt


def _register(ctx, name, native, tenant=w.TENANT_ONE):
    ctx.ids[name] = ctx.sources().register(tenant_id=tenant, runtime='claude', native_id=native)
    return {'session_id': ctx.ids[name]}


def _import(ctx, name, session, *texts, coordinates=None, include=True, index=True):
    receipt = ctx.sources().import_episode(
        session_id=ctx.ids[session], source_ref='source:' + name, events=w.events(*texts),
        provenance=w.provenance('row-' + name, coordinates=coordinates, include_coordinates=include))
    ctx.ids[name] = receipt['episode_id']
    ctx.events[name] = 'e0'
    if index:
        ctx.upsert(name)
    return receipt


def _claim(ctx, name, session, desk_or_object, **extra):
    if 'supersedes' in extra and extra['supersedes'] in ctx.ids:
        extra['supersedes'] = ctx.ids[extra['supersedes']]
    ctx.ids[name] = ctx.sources().claim(**w.claim_args(ctx.ids[session], desk_or_object, **extra))
    return {'claim_id': ctx.ids[name]}


def _admission_claim(ctx, episode):
    session = w.linked_session(ctx.world.store_path, ctx.ids[episode])
    return session, w.owner_claims(ctx.world.store_path, session)[0][0]


def s_retract_admission(ctx):
    session, claim = _admission_claim(ctx, 'L5')
    ctx.ids['SS_A2'] = session
    ctx.ids['L5_owner'] = claim
    return _claim(ctx, 'L5_retraction', 'SS_A2', ALPHA, supersedes=claim, retracted=True,
                  recorded_at='2026-12-01T00:00:00Z')


def s_link(ctx, episode, session):
    ctx.sources().link(episode_id=ctx.ids[episode], session_id=ctx.ids[session])
    return {'linked': episode}


def s_quartz(ctx):
    _register(ctx, 'S7', 'native-s7')
    _claim(ctx, 'S7_owner', 'S7', BETA)
    names = []
    for i in range(12):
        _import(ctx, f'Q{i:02d}', 'S7', f'quartz signal {i}', index=False)
        names.append(f'Q{i:02d}')
    _register(ctx, 'S8', 'native-s8')
    _claim(ctx, 'S8_owner', 'S8', ALPHA)
    _import(ctx, 'QA', 'S8', 'A much longer alpha record that eventually mentions the quartz signal once, '
            'after a great many unrelated words about planning, review, budgets and timelines.', index=False)
    ctx.upsert(*names, 'QA')
    return {'imported': len(names) + 1}


def s_fts_regex(ctx):
    _register(ctx, 'S9', 'native-s9')
    _claim(ctx, 'S9_owner', 'S9', ALPHA)
    names = []
    for i in range(3):
        _import(ctx, f'FU{i}', 'S9', f'foo_bar {i}', index=False)
        _import(ctx, f'CA{i}', 'S9', f'café bar {i}', index=False)
        names += [f'FU{i}', f'CA{i}']
    _import(ctx, 'FV', 'S9', 'This long alpha note lists several unrelated items before it finally says foo bar '
            'as a plain phrase, which is the only valid literal match.', index=False)
    _import(ctx, 'CV', 'S9', 'This long alpha note lists several unrelated items before it finally says cafe bar '
            'as a plain phrase, which is the only valid literal match.', index=False)
    ctx.upsert(*names, 'FV', 'CV')
    return {'imported': len(names) + 2}


def s_profiles_init(ctx):
    from kp_agent_tooling._impl.service.desk_profiles import DeskProfiles
    profiles = DeskProfiles(ctx.store(), A)
    profiles.initialize()
    return profiles.save(dict(desk_id=PROFILE_DESK, name='Planner', description='Plans cedar work.',
                              role='Coordinator', expected_version=0))


def s_annotate(ctx):
    from kp_agent_tooling._impl.service.desk_profiles import DeskProfiles
    return DeskProfiles(ctx.store(), A).annotate(dict(
        source_session_id=ctx.ids['S1'], desk_id=PROFILE_DESK, repos=['repo-x'], adrs=[], cards=[],
        account_ref=None, provider='fixture', model=None, expected_version=0, source_ref='fixture:t10'))


def s_project(ctx):
    return ctx.sources().record_project(session_id=ctx.ids['S3'], tenant_id=w.TENANT_ONE, repo_key='repo-y',
                                        evidence={'cwd': '/t10/work/repo-y', 'git_common_dir': '/t10/work/repo-y/.git',
                                                  'source_file': '/t10/native/s3.jsonl'})


def _citation(ctx, name, start=0, length=None):
    text = _text_of(ctx, name)
    end = len(text) if length is None else start + length
    return {'episode_id': ctx.ids[name], 'event_id': ctx.events[name], 'start': start, 'end': end,
            'quote': text[start:end]}


def _text_of(ctx, name):
    return TEXTS[name]


def s_propose(ctx, capsule, session, names, *, expect_refusal=False):
    tools = ctx.world.tools(session)
    arguments = {'episode_ids': [ctx.ids[n] for n in names],
                 'items': [{'kind': 'observation', 'text': 'Recorded ' + n,
                            'citations': [_citation(ctx, n, 0, 5)]} for n in names],
                 'unresolved_questions': ['Is the cedar plan final?']}
    result = w.call(tools, 'memory.propose', arguments)
    if 'error' not in result:
        ctx.ids[capsule] = result['capsule_id']
    return result


def s_remove_delta(ctx):
    ctx.world.write_catalog([d for d in DESKS if d[1] != 'delta'])
    return {'catalog': 'delta removed'}


def s_reindex(ctx):
    return ctx.index().rebuild()


TEXTS = {
    'L1': 'Alpha decided the cedar plan at noon.',
    'L2': 'Beta reviewed the cedar plan quietly.',
    'L3': 'Gamma imported ledger note about cedar.',
    'L4': 'Delta archived cedar willow note.',
    'E1': 'Unresolved cedar musing in session one.',
    'E2': 'Second file cedar continuation at the range start.',
    'E3': 'String start cedar coordinate.',
    'E4': 'Float start cedar coordinate.',
    'E5': 'Bool start cedar coordinate.',
    'E6': 'List coordinates cedar record.',
    'E7': 'No coordinates cedar record.',
    'E8': 'Before the range cedar record.',
    'E8b': 'After the range cedar record.',
    'E9': 'Supersession cedar chain record.',
    'E10': 'Contributor cedar note with tags.',
    'E11': 'Reassigned cedar decision text.',
    'L5': 'Legacy linked cedar session.',
    'E12': 'Foreign tenant cedar secret.',
    'L6': 'Omega legacy cedar.',
    'L7': 'Fresh cedar capture not yet indexed.',
}


def _steps():
    t = TEXTS
    return [
        ('capture-alpha', lambda c: _capture(c, 'L1', A, t['L1'], 'Follow up later.')),
        ('capture-beta', lambda c: _capture(c, 'L2', B, t['L2'])),
        ('legacy-import-gamma', lambda c: _legacy(c, 'L3', 'gamma', t['L3'])),
        ('legacy-import-delta', lambda c: _legacy(c, 'L4', 'delta', t['L4'])),
        ('register-s1', lambda c: _register(c, 'S1', 'native-s1')),
        ('import-e1-unresolved', lambda c: _import(c, 'E1', 'S1', t['E1'],
                                                   coordinates={'path': FILE_A, 'start': 0, 'end': 40})),
        ('claim-s1-owner-alpha', lambda c: _claim(c, 'S1_alpha', 'S1', ALPHA)),
        ('import-e2-second-file', lambda c: _import(c, 'E2', 'S1', t['E2'],
                                                    coordinates={'path': FILE_B, 'start': 50, 'end': 99})),
        ('claim-s1-ranged-beta', lambda c: _claim(c, 'S1_beta_range', 'S1', BETA, recorded_at='2026-09-02T00:00:00Z',
                                                  source_range={'native_id': 'native-s1', 'source_file': FILE_B,
                                                                'start_offset': 50})),
        ('import-e3-string-start', lambda c: _import(c, 'E3', 'S1', t['E3'], coordinates={'path': FILE_B, 'start': '60'})),
        ('import-e4-float-start', lambda c: _import(c, 'E4', 'S1', t['E4'], coordinates={'path': FILE_B, 'start': 60.0})),
        ('import-e5-bool-start', lambda c: _import(c, 'E5', 'S1', t['E5'], coordinates={'path': FILE_B, 'start': True})),
        ('import-e6-list-coordinates', lambda c: _import(c, 'E6', 'S1', t['E6'], coordinates=[FILE_B, 60])),
        ('import-e7-no-coordinates', lambda c: _import(c, 'E7', 'S1', t['E7'], include=False)),
        ('import-e8-before-range', lambda c: _import(c, 'E8', 'S1', t['E8'], coordinates={'path': FILE_B, 'start': 49})),
        ('import-e8b-after-range', lambda c: _import(c, 'E8b', 'S1', t['E8b'], coordinates={'path': FILE_B, 'start': 51})),
        ('register-s2', lambda c: _register(c, 'S2', 'native-s2')),
        ('import-e9', lambda c: _import(c, 'E9', 'S2', t['E9'])),
        ('claim-s2-c1-gamma', lambda c: _claim(c, 'c1', 'S2', GAMMA, recorded_at='2026-09-03T00:00:00Z')),
        ('claim-s2-c2-alpha-supersedes-c1', lambda c: _claim(c, 'c2', 'S2', ALPHA, supersedes='c1',
                                                             recorded_at='2026-09-04T00:00:00Z')),
        ('claim-s2-c3-beta-supersedes-c2', lambda c: _claim(c, 'c3', 'S2', BETA, supersedes='c2',
                                                            recorded_at='2026-09-05T00:00:00Z')),
        ('claim-s2-c4-retracts-c3', lambda c: _claim(c, 'c4', 'S2', BETA, supersedes='c3', retracted=True,
                                                     recorded_at='2026-09-06T00:00:00Z')),
        ('register-s3', lambda c: _register(c, 'S3', 'native-s3')),
        ('import-e10', lambda c: _import(c, 'E10', 'S3', t['E10'])),
        ('claim-s3-owner-beta-interval', lambda c: _claim(c, 'S3_beta', 'S3', BETA,
                                                          valid_from='2026-01-01T00:00:00Z',
                                                          valid_until='2026-06-01T00:00:00Z')),
        ('claim-s3-contributor', lambda c: _claim(c, 'S3_ana', 'S3', {'kind': 'actor', 'id': 'person:ana'},
                                                  predicate='session.contributor')),
        ('claim-s3-tag', lambda c: _claim(c, 'S3_tag', 'S3', {'kind': 'tag', 'id': 'release'}, predicate='session.tag')),
        ('register-s4', lambda c: _register(c, 'S4', 'native-s4')),
        ('import-e11', lambda c: _import(c, 'E11', 'S4', t['E11'])),
        ('claim-s4-owner-alpha', lambda c: _claim(c, 'o1', 'S4', ALPHA)),
        ('claim-s4-reassign-gamma', lambda c: _claim(c, 'o2', 'S4', GAMMA, supersedes='o1',
                                                     recorded_at='2026-09-07T00:00:00Z')),
        ('capture-alpha-2-linked', lambda c: _capture(c, 'L5', A2, t['L5'])),
        ('retract-l5-admission-owner', s_retract_admission),
        ('register-s5', lambda c: _register(c, 'S5', 'native-s5')),
        ('claim-s5-owner-beta', lambda c: _claim(c, 'S5_beta', 'S5', BETA)),
        ('link-l3-to-s5', lambda c: s_link(c, 'L3', 'S5')),
        ('register-s6-other-tenant', lambda c: _register(c, 'S6', 'native-s6', tenant=w.TENANT_TWO)),
        ('import-e12-other-tenant', lambda c: _import(c, 'E12', 'S6', t['E12'])),
        ('claim-s6-owner-omega', lambda c: _claim(c, 'S6_omega', 'S6', OMEGA)),
        ('capture-omega', lambda c: _capture(c, 'L6', O, t['L6'])),
        ('link-l1-to-other-tenant-refused', lambda c: s_link(c, 'L1', 'S6')),
        ('quartz-candidate-limit', s_quartz),
        ('fts-regex-disagreement', s_fts_regex),
        ('capture-alpha-unindexed', lambda c: _capture(c, 'L7', A, t['L7'], index=False)),
        ('reindex', s_reindex),
        ('profile-desk', s_profiles_init),
        ('profile-annotate-s1', s_annotate),
        ('project-s3-repo-y', s_project),
        ('propose-alpha-k1', lambda c: s_propose(c, 'K1', A, ['L1', 'E1'])),
        ('propose-alpha-conflicting-refused', lambda c: s_propose(c, 'K_refused', A, ['E2'])),
        ('propose-beta-k2', lambda c: s_propose(c, 'K2', B, ['L2'])),
        ('catalog-remove-delta', s_remove_delta),
    ]


STEPS = _steps()
SENTINELS = ['L1', 'L3', 'L4', 'E1', 'E2', 'E3', 'E5', 'E8', 'E8b', 'E9', 'E10', 'E11', 'L5', 'E12', 'L6', 'FV']
SOURCE_SESSIONS = ['S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'SS_A2']


def battery(ctx):
    """``[(key, session, tool, arguments)]`` for the current state."""
    ids = ctx.ids
    reads = [
        ('status', A, 'memory.connection_status', {}),
        ('bindings', A, 'memory.bindings', {}),
        ('bindings-omega', O, 'memory.bindings', {}),
    ]
    for session in (A, B, C, O):
        reads.append((f'list-{session}', session, 'memory.list', {'kind': 'episodes', 'limit': 50}))
    reads += [
        ('list-alpha-reads-gamma', A, 'memory.list', {'kind': 'episodes', 'binding_key': GAMMA, 'limit': 50}),
        ('list-alpha-page-2', A, 'memory.list', {'kind': 'episodes', 'offset': 2, 'limit': 3}),
        ('memory-status-alpha', A, 'memory.status', {'limit': 3}),
    ]
    for session in (A, B, C):
        reads.append((f'search-cedar-{session}', session, 'memory.search', {'query': 'cedar', 'limit': 8}))
    reads += [
        ('search-cedar-topic-alpha', A, 'memory.search', {'query': 'cedar', 'scope': 'topic', 'limit': 20}),
        ('search-cedar-topic-omega', O, 'memory.search', {'query': 'cedar', 'scope': 'topic', 'limit': 5}),
        ('search-cedar-alpha-reads-beta', A, 'memory.search', {'query': 'cedar', 'binding_key': BETA, 'limit': 5}),
        ('search-cedar-desk-conflicting', A, 'memory.search', {'query': 'cedar', 'attribution': 'conflicting', 'limit': 4}),
        ('search-cedar-desk-unresolved', A, 'memory.search', {'query': 'cedar', 'attribution': 'unresolved', 'limit': 4}),
    ]
    for attribution in ('resolved', 'unresolved', 'conflicting'):
        reads.append((f'search-cedar-topic-{attribution}', A, 'memory.search',
                      {'query': 'cedar', 'scope': 'topic', 'attribution': attribution, 'limit': 4}))
    reads += [
        ('search-claim-contributor', A, 'memory.search', {'query': 'cedar', 'scope': 'topic', 'claims': [
            {'predicate': 'session.contributor', 'object': {'kind': 'actor', 'id': 'person:ana'}}]}),
        ('search-claim-tag', A, 'memory.search', {'query': 'cedar', 'scope': 'topic', 'claims': [
            {'predicate': 'session.tag', 'object': {'kind': 'tag', 'id': 'release'}}]}),
        ('search-claim-owner-beta-desk', B, 'memory.search', {'query': 'cedar', 'limit': 4, 'claims': [
            {'predicate': 'session.owner', 'object': {'kind': 'desk', 'id': BETA}}]}),
        ('search-quartz-desk', A, 'memory.search', {'query': 'quartz signal', 'limit': 1}),
        ('search-quartz-topic', A, 'memory.search', {'query': 'quartz signal', 'scope': 'topic', 'limit': 1}),
        ('search-quartz-beta', B, 'memory.search', {'query': 'quartz signal', 'limit': 1}),
        ('search-foo-bar', A, 'memory.search', {'query': 'foo bar', 'limit': 1}),
        ('search-foo-bar-3', A, 'memory.search', {'query': 'foo bar', 'limit': 3}),
        ('search-cafe-bar', A, 'memory.search', {'query': 'cafe bar', 'limit': 1}),
        ('search-unknown-binding', A, 'memory.search', {'query': 'cedar', 'binding_key': 'binding:' + '0' * 64}),
        ('search-topic-with-binding', A, 'memory.search', {'query': 'cedar', 'scope': 'topic', 'binding_key': BETA}),
        ('search-empty-query', A, 'memory.search', {'query': '***'}),
    ]
    if 'S1' in ids:
        reads.append(('search-session-s1', A, 'memory.search',
                      {'query': 'cedar', 'scope': 'topic', 'session_id': ids['S1'], 'limit': 6}))
    for kind, value in (('agent', PROFILE_DESK), ('repo', 'repo-x'), ('repo', 'repo-y')):
        reads.append((f'search-view-{kind}-{value}', A, 'memory.search',
                      {'query': 'cedar', 'view': {'kind': kind, 'value': value}}))
    reads.append(('search-view-global', A, 'memory.search', {'query': 'cedar', 'view': {'kind': 'global'}, 'limit': 6}))
    for name in SENTINELS:
        if name not in ids:
            continue
        episode, event = ids[name], ctx.events[name]
        reads += [
            (f'read-{name}-alpha-desk', A, 'memory.read_event', {'episode_id': episode, 'event_id': event, 'length': 60}),
            (f'read-{name}-alpha-topic', A, 'memory.read_event',
             {'episode_id': episode, 'event_id': event, 'scope': 'topic', 'start': 2, 'length': 40}),
            (f'read-{name}-beta-desk', B, 'memory.read_event', {'episode_id': episode, 'event_id': event}),
            (f'dir-{name}-alpha-topic', A, 'memory.episode_directory', {'episode_id': episode, 'scope': 'topic'}),
            (f'dir-{name}-gamma-desk', C, 'memory.episode_directory', {'episode_id': episode}),
        ]
    for name in SOURCE_SESSIONS:
        if name in ids:
            reads.append((f'session-{name}', A, 'memory.session', {'session_id': ids[name], 'limit': 50}))
    if 'S1' in ids:
        reads.append(('session-s1-page', A, 'memory.session', {'session_id': ids['S1'], 'offset': 1, 'limit': 1}))
    for capsule, owner, other in (('K1', A, B), ('K2', B, A)):
        if capsule not in ids:
            continue
        key = ids[capsule]
        own = ALPHA if owner == A else BETA
        reads += [
            (f'handoff-{capsule}', owner, 'memory.handoff', {'capsule_id': key}),
            (f'resume-{capsule}', owner, 'memory.resume', {'capsule_id': key, 'budget_bytes': 4000}),
            (f'resume-{capsule}-cross', other, 'memory.resume', {'capsule_id': key, 'binding_key': own}),
            (f'page-{capsule}-cross', other, 'memory.handoff_page', {'capsule_id': key, 'binding_key': own,
                                                                    'budget_bytes': 3000}),
            (f'evidence-{capsule}', owner, 'memory.evidence_directory', {'capsule_id': key}),
            (f'handoff-{capsule}-cross', other, 'memory.handoff', {'capsule_id': key, 'binding_key': own}),
        ]
    if 'K1' in ids or 'K2' in ids:
        reads += [('capsules-alpha', A, 'memory.list', {'kind': 'capsules'}),
                  ('capsules-beta', B, 'memory.list', {'kind': 'capsules'})]
    return reads


ORACLE_VARIANTS = [
    ('desk', {}),
    ('desk-resolved', {'attribution': 'resolved'}),
    ('desk-conflicting', {'attribution': 'conflicting'}),
    ('topic', {'scope': 'topic'}),
    ('topic-resolved', {'scope': 'topic', 'attribution': 'resolved'}),
    ('topic-unresolved', {'scope': 'topic', 'attribution': 'unresolved'}),
    ('topic-conflicting', {'scope': 'topic', 'attribution': 'conflicting'}),
    ('topic-contributor', {'scope': 'topic', 'claims': [
        {'predicate': 'session.contributor', 'object': {'kind': 'actor', 'id': 'person:ana'}}]}),
]


def oracle_variants(ctx):
    variants = list(ORACLE_VARIANTS)
    if 'S1' in ctx.ids:
        variants.append(('topic-session-s1', {'scope': 'topic', 'session_id': ctx.ids['S1']}))
    variants.append(('desk-gamma', {'binding_key': GAMMA}))
    return variants


def digest(value):
    return hashlib.sha256(w.canonical(value)).hexdigest()[:32]


def _list_all(tools, arguments):
    entries, offset, total = [], 0, None
    while True:
        page = tools.call('memory.list', dict(arguments, kind='episodes', offset=offset, limit=50))
        total = page['total']
        entries += page['entries']
        if page['next_offset'] is None:
            return entries, total
        offset = page['next_offset']


def oracle_mismatches(ctx, sessions=(A, B, C, O)):
    """Compare the tools' resolved scope with the base ``select`` oracle."""
    import t10_oracle as oracle
    problems = []
    for session in sessions:
        tools = ctx.world.tools(session)
        for label, variant in oracle_variants(ctx):
            where = f'{session}/{label}'
            try:
                selected, report = oracle.select_for(ctx.world, session, **variant)
                refused = None
            except (oracle.OracleUnavailable, ValueError) as error:
                selected, report, refused = None, None, error
            try:
                found = tools.call('memory.search', dict(variant, query='cedar', limit=20))
            except Exception as error:  # noqa: BLE001
                if refused is None:
                    problems.append(f'{where}: tool refused ({type(error).__name__}) where select returns a scope')
                continue
            if refused is not None:
                problems.append(f'{where}: tool returned a scope where select refuses ({refused})')
                continue
            if found['search_scope'] != report:
                problems.append(f'{where}: search_scope {found["search_scope"]} != select {report}')
            if found['total_episodes'] != len(selected):
                problems.append(f'{where}: total_episodes {found["total_episodes"]} != select {len(selected)}')
            for row in found['results']:
                item = selected.get(row['episode_id'])
                if item is None:
                    problems.append(f'{where}: result {row["episode_id"]} outside the select scope')
                    continue
                for key in ('binding_key', 'attribution_status', 'source_session_id'):
                    if row[key] != item[key]:
                        problems.append(f'{where}: {row["episode_id"]} {key} {row[key]} != {item[key]}')
            if label in ('desk', 'desk-gamma'):
                arguments = {'binding_key': variant['binding_key']} if 'binding_key' in variant else {}
                entries, total = _list_all(tools, arguments)
                expected = list(selected)[::-1]
                if [e['episode_id'] for e in entries] != expected or total != len(expected):
                    problems.append(f'{where}: memory.list order {[e["episode_id"][-8:] for e in entries]} '
                                    f'!= select {[i[-8:] for i in expected]}')
                for entry in entries:
                    item = selected[entry['episode_id']]
                    attribution = entry['read_attribution']
                    if attribution.get('attribution_status', 'resolved') != item['attribution_status']:
                        problems.append(f'{where}: list attribution of {entry["episode_id"]} differs')
    return problems


def run(root, *, on_step=None):
    """Apply every step, then the battery. Returns ``[{'step', 'write', 'reads'}]``.

    ``on_step(ctx, name)`` runs after each battery (the oracle comparison hook).
    """
    ctx = Context(root)
    record = []
    with w.frozen_clock():
        ctx.world.initialize()
        for name, write in STEPS:
            try:
                result = w.jsonable(write(ctx))
            except Exception as error:  # noqa: BLE001 - the refusal is the observable
                # Write refusals are recorded through the same public envelope.
                result = {'error': w.failure('memory.propose', error)}
            if _drains() and name not in UNDRAINED_STEPS:
                t12b_drain(ctx.store())  # T12b R2: the helper has drained at this step
            reads = {}
            tools = {}
            for key, session, tool, arguments in battery(ctx):
                if session not in tools:
                    tools[session] = ctx.world.tools(session)
                reads[key] = {'session': session, 'tool': tool, 'arguments': arguments,
                              'output': w.jsonable(w.call(tools[session], tool, arguments))}
            record.append({'step': name, 'write': result, 'reads': reads})
            if on_step is not None:
                on_step(ctx, name)
    return ctx, record
