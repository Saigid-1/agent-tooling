"""T12c C2: a reassignment is visible to the next read, before and after the indexer applies it.

Order: docs/work/orders/T12c-per-desk-postings.md, C2. Falsifiers: "A reassignment (claim A->B, a link) not visible
to the next read with the indexer stopped, or not visible after the drain."; "After the drain, a posting places a
session in a desk it left. A `desks_changed` row is applied twice."; the mutants "the drain skips `desks_changed`
rows", "the read ignores unapplied rows", "the read treats every present row as unapplied", "the postings are
written outside the watermark's transaction".

The world (tests/t10_corpus.py desks and sessions): session S_MOVE owned by desk alpha holds the moved episodes;
S_OWN (alpha) holds alpha's own; a legacy episode L stored by desk gamma, unlinked; S_LINK owned by beta. Every
row is drained (the index current). Then S_MOVE is claimed by beta (superseding alpha's claim) and L is linked to
S_LINK, with the indexer stopped. Reads go through fresh `EpisodicMemoryTools` (`memory.search`).

- test_c2_reassignment_is_visible_to_the_next_read_with_the_indexer_stopped: GREEN-IF, with both rows waiting
  above the watermark, beta finds exactly S_MOVE's episodes (binding beta) and L, alpha finds only its own, gamma no
  longer finds L, and every search reports total == covered == the base `select` oracle's count.
- test_c2_reassignment_is_still_correct_after_the_drain: GREEN-IF the same holds after the drain applied the rows
  (outbox empty, watermark advanced), and a first capture drained with them is found by its desk.
- test_c2_after_the_drain_no_posting_places_the_session_in_a_desk_it_left: GREEN-IF, after the drain, alpha's
  search costs (VM steps, T10 instrument) at most 1.5x what it costs in a control world with the same content where
  S_MOVE (2,000 matching episodes) never belonged to alpha. A posting left in alpha makes alpha's search visit them.
- test_c2_an_applied_row_still_present_is_not_applied_again: GREEN-IF, with every applied `desks_changed` row put
  back unchanged at or below the watermark (present before retention, census B3), alpha's and beta's searches cost
  at most 1.5x what they cost without them and return the same results, and the next drain applies none of them
  (applied_rows 0, watermark unchanged) and retains them.
- test_c2_postings_are_written_in_the_watermarks_transaction: GREEN-IF, in a drain applying a seal, a claim and a
  link, every data write on the index file is in a transaction that also writes `outbox_watermark`.

At base every C2 test is a guard (green): the base index carries no desk, so reads are always live. They are the
falsifiers of a postings implementation; the mutation runs show which goes red for which mutant.
"""
from __future__ import annotations

import re

import pytest

import t10_corpus as corpus
import t10_measure as m
import t10_oracle
import t10_world as w
import t12b_seams as t12b
import t12c_seams as seams
from t12c_seams import ids, search

A, B, C, A2 = corpus.A, corpus.B, corpus.C, corpus.A2
ALPHA, BETA, GAMMA = corpus.ALPHA, corpus.BETA, corpus.GAMMA
MOVED = 'cobalt reassignment marker'
LINKED = 'cobalt link marker'
BIG = 2000
STEP_RATIO = 1.5
# A posting left in alpha makes alpha's search visit S_MOVE's BIG matching episodes: tens of times the control's
# cost. The bound leaves room for a delete's physical residue in an index structure (for example FTS5 delete
# markers until a merge), which is not a posting (see the report's AMBIGUITY).
LEFT_RATIO = 3.0


def build(root, *, moved=3, start=ALPHA):
    world = w.World(root, corpus.DESKS, corpus.SESSIONS)
    world.initialize()
    store, sources = world.store(), world.sources()
    ids_ = {}
    with w.frozen_clock():
        ids_['S_OWN'] = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='t12c-c2-own')
        sources.claim(**w.claim_args(ids_['S_OWN'], ALPHA))
        ids_['own'] = [sources.import_episode(session_id=ids_['S_OWN'], source_ref=f't12c:c2:own:{i}',
                                              events=w.events(f'Cobalt reassignment marker own {i}.'),
                                              provenance=w.provenance(f't12c-c2-own-{i}'))['episode_id']
                       for i in range(2)]
        ids_['S_MOVE'] = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='t12c-c2-move')
        ids_['c_move'] = sources.claim(**w.claim_args(ids_['S_MOVE'], start))
        ids_['moved'] = [sources.import_episode(session_id=ids_['S_MOVE'], source_ref=f't12c:c2:move:{i:05d}',
                                                events=w.events(f'Cobalt reassignment marker moved {i:05d}.'),
                                                provenance=w.provenance(f't12c-c2-move-{i}'))['episode_id']
                         for i in range(moved)]
        ids_['L'] = store.import_operator_episode(
            tenant_id=w.TENANT_ONE, role='gamma', repo_key=w.REPO, source_ref='t12c:c2:legacy',
            events=w.imported_events('Cobalt link marker legacy note.'),
            source_provenance=w.legacy_provenance('t12c-c2-legacy'))['episode_id']
        ids_['S_LINK'] = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='t12c-c2-link')
        sources.claim(**w.claim_args(ids_['S_LINK'], BETA))
        ids_['link_own'] = sources.import_episode(session_id=ids_['S_LINK'], source_ref='t12c:c2:link',
                                                  events=w.events('Cobalt link marker session note.'),
                                                  provenance=w.provenance('t12c-c2-link'))['episode_id']
    world.ids = ids_
    return world


def reassign(world):
    """Claim S_MOVE alpha -> beta and link L to S_LINK (beta): two `desks_changed` rows, no drain."""
    with w.frozen_clock():
        world.sources().claim(**w.claim_args(world.ids['S_MOVE'], BETA, supersedes=world.ids['c_move'],
                                             recorded_at='2026-09-02T00:00:00Z'))
        world.sources().link(episode_id=world.ids['L'], session_id=world.ids['S_LINK'])


def _oracle_total(world, session):
    return len(t10_oracle.select_for(world, session)[0])


def _scope_problems(world, session, query, expected, binding=None, label=''):
    found = search(world, session, query)
    if 'error' in found:
        return [f'{label}{session} {query!r}: refused {found["error"]}']
    problems = []
    if set(ids(found)) != set(expected):
        problems.append(f'{label}{session} {query!r}: found {sorted(ids(found))}, expected {sorted(expected)}')
    if binding is not None and any(r['binding_key'] != binding for r in found['results']):
        problems.append(f'{label}{session} {query!r}: result bindings {[r["binding_key"] for r in found["results"]]}')
    total = _oracle_total(world, session)
    if not found['total_episodes'] == found['covered_episodes'] == total:
        problems.append(f'{label}{session}: total {found["total_episodes"]}, covered {found["covered_episodes"]}, '
                        f'oracle {total}')
    return problems


def visibility(world, label=''):
    i = world.ids
    return (_scope_problems(world, B, MOVED, i['moved'], BETA, label)
            + _scope_problems(world, A, MOVED, i['own'], ALPHA, label)
            + _scope_problems(world, B, LINKED, [i['L'], i['link_own']], BETA, label)
            + _scope_problems(world, C, LINKED, [], None, label))


def _drained(world):
    t12b.drain(world.store())
    assert t12b.outbox_count(world.store_path) == 0, 'precondition: the drain applied every row'


def _before(world):
    i = world.ids
    problems = (_scope_problems(world, A, MOVED, i['own'] + i['moved'], ALPHA, 'before: ')
                + _scope_problems(world, C, LINKED, [i['L']], GAMMA, 'before: ')
                + _scope_problems(world, B, MOVED, [], None, 'before: '))
    assert not problems, 'precondition, before the reassignment:\n' + '\n'.join(problems)


def test_c2_reassignment_is_visible_to_the_next_read_with_the_indexer_stopped(tmp_path):
    world = build(tmp_path / 'world')
    _drained(world)
    _before(world)
    applied = seams.watermark(world.store())
    reassign(world)
    waiting = t12b.outbox_rows(world.store_path)
    assert [r['reason'] for r in waiting] == ['desks_changed', 'desks_changed'], f'precondition: {waiting}'
    assert all(r['seq'] > applied for r in waiting) and seams.watermark(world.store()) == applied, \
        'precondition: the two rows wait above the watermark (indexer stopped)'
    problems = visibility(world, 'indexer stopped: ')
    assert not problems, 'the reassignment is not visible to the next read:\n' + '\n'.join(problems)


def test_c2_reassignment_is_still_correct_after_the_drain(tmp_path):
    world = build(tmp_path / 'world')
    _drained(world)
    _before(world)
    reassign(world)
    with w.frozen_clock():
        captured = world.store().capture(A2, source_ref='t12c:c2:first-capture',
                                         events=w.events('Cobalt reassignment marker captured first.'))['episode_id']
    before = seams.watermark(world.store())
    _drained(world)
    assert seams.watermark(world.store()) > before, 'precondition: the drain advanced the watermark'
    world.ids['own'] = world.ids['own'] + [captured]
    problems = visibility(world, 'after the drain: ')
    assert not problems, 'the reassignment is not correct after the drain:\n' + '\n'.join(problems)


def _steps(world, session, query):
    measured = m.measure(world, session, 'memory.search', {'query': query, 'limit': 20})
    assert measured.error is None, f'{session}: {measured.failure()}'
    return measured.steps, ids(measured.output)


def test_c2_after_the_drain_no_posting_places_the_session_in_a_desk_it_left(tmp_path):
    moved = build(tmp_path / 'moved', moved=BIG, start=ALPHA)
    _drained(moved)
    found = search(moved, A, MOVED)
    assert len(found['results']) == 20 and found['total_episodes'] == BIG + 2, \
        f'precondition: alpha holds S_MOVE before the reassignment: {found.get("total_episodes")}'
    reassign(moved)
    _drained(moved)
    control = build(tmp_path / 'control', moved=BIG, start=BETA)
    _drained(control)
    moved_total = search(moved, B, MOVED).get('total_episodes')
    assert moved_total == BIG + 2, f'precondition: beta\'s scope holds S_MOVE, S_LINK and L: {moved_total}'
    steps, result = _steps(moved, A, MOVED)
    control_steps, control_result = _steps(control, A, MOVED)
    print(f'\nalpha after the drain: {steps} VM steps; control {control_steps} ({steps / control_steps:.2f}x)')
    assert set(result) == set(moved.ids['own']) and set(control_result) == set(control.ids['own']), \
        f'alpha finds only its own: {result} / {control_result}'
    assert steps <= LEFT_RATIO * control_steps, (
        f'after the drain, alpha\'s search costs {steps} VM steps, {steps / control_steps:.2f}x the {control_steps} of '
        f'a world where S_MOVE ({BIG} matching episodes) never belonged to alpha (bound {LEFT_RATIO}x): a posting '
        'still places S_MOVE in alpha')


def test_c2_an_applied_row_still_present_is_not_applied_again(tmp_path):
    world = build(tmp_path / 'world', moved=BIG, start=ALPHA)
    first = t12b.outbox_rows(world.store_path)
    _drained(world)
    reassign(world)
    second = t12b.outbox_rows(world.store_path)
    _drained(world)
    applied = seams.watermark(world.store())
    rows = [r for r in first + second if r['reason'] == 'desks_changed']
    assert len(rows) >= BIG + 2 and max(r['seq'] for r in rows) <= applied, f'precondition: {len(rows)} rows'
    base = {session: _steps(world, session, MOVED) for session in (A, B)}
    seams.reinsert(world.store_path, rows)
    assert t12b.outbox_count(world.store_path) == len(rows) and seams.watermark(world.store()) == applied, \
        'precondition: the applied rows are present again, at or below the watermark'
    problems = []
    for session in (A, B):
        steps, result = _steps(world, session, MOVED)
        if result != base[session][1]:
            problems.append(f'{session}: results changed with applied rows present: {result} vs {base[session][1]}')
        if steps > STEP_RATIO * base[session][0]:
            problems.append(f'{session}: {steps} VM steps with {len(rows)} applied rows present, {base[session][0]} '
                            f'without ({steps / base[session][0]:.2f}x > {STEP_RATIO}x): the read applies them again')
    report = t12b.drain_once(world.store())
    if report.get('applied_rows') not in (0, None):
        problems.append(f'the next drain applied {report.get("applied_rows")} rows at or below its watermark: {report}')
    if seams.watermark(world.store()) != applied:
        problems.append(f'the watermark moved from {applied} to {seams.watermark(world.store())}')
    if t12b.outbox_count(world.store_path):
        problems.append(f'{t12b.outbox_count(world.store_path)} applied rows were not retained')
    problems += visibility_after_move(world)
    assert not problems, 'an applied desks_changed row is applied again:\n' + '\n'.join(problems)


def visibility_after_move(world):
    i = world.ids
    return (_scope_problems(world, A, MOVED, i['own'], ALPHA, 'after the retention: ')
            + _scope_problems(world, C, LINKED, [], None, 'after the retention: '))


_DML = re.compile(r'\s*(INSERT|REPLACE|UPDATE|DELETE)\b', re.I)
_BEGIN = re.compile(r'\s*BEGIN\b', re.I)
_END = re.compile(r'\s*(COMMIT|END|ROLLBACK)\b', re.I)
_ATTACH = re.compile(r"\s*ATTACH\s+(?:DATABASE\s+)?'([^']+)'\s+AS\s+\"?(\w+)\"?", re.I)
_NESTED = re.compile(r"\s*--|\s*(?:SELECT|INSERT|REPLACE|UPDATE|DELETE)\b.*?'[A-Za-z_]\w*'\.'[A-Za-z_]\w*'", re.I | re.S)


def index_transactions(trace, index_path):
    """[(connection, [statements], [index writes])] of the transactions that write the index file, per connection,
    from the trace: data writes on a connection to the index, and data writes naming an alias under which a
    connection attached the index writable."""
    target = t12b.normalize(index_path)
    by_conn = {}
    for event in trace.events:
        if event.kind == 'sql':
            # A connection is its object id AND its database: an id is reused after a connection is collected.
            by_conn.setdefault((event.conn, event.db), []).append(event)
    found = []
    for (conn, db), events in by_conn.items():
        aliases = set()
        on_index = db == target
        current, open_ = [], False
        for event in events:
            attach = _ATTACH.match(event.sql)
            if attach and t12b.normalize(attach.group(1)) == target and 'mode=ro' not in attach.group(1):
                aliases.add(attach.group(2).lower())
            if _NESTED.match(event.sql):
                if open_:
                    current.append(event.sql)
                continue
            if _BEGIN.match(event.sql):
                current, open_ = [event.sql], True
                continue
            if _END.match(event.sql):
                if open_:
                    current.append(event.sql)
                    found.append((conn, on_index, aliases, current))
                current, open_ = [], False
                continue
            if open_:
                current.append(event.sql)
            elif _DML.match(event.sql):
                found.append((conn, on_index, aliases, [event.sql]))  # an autocommit write
    writes = []
    for conn, on_index, aliases, statements in found:
        def writes_index(sql):
            if not _DML.match(sql):
                return False
            if on_index:
                return True
            return any(re.search(r'\b' + alias + r'\s*\.', sql, re.I) for alias in aliases)
        mine = [sql for sql in statements if writes_index(sql)]
        if mine:
            writes.append((conn, statements, mine))
    return writes


def test_c2_postings_are_written_in_the_watermarks_transaction(tmp_path):
    world = build(tmp_path / 'world')
    _drained(world)
    reassign(world)
    with w.frozen_clock():
        session = world.sources().register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='t12c-c2-new')
        world.sources().claim(**w.claim_args(session, ALPHA))
        world.sources().import_episode(session_id=session, source_ref='t12c:c2:new', events=w.events('Cobalt new.'),
                                       provenance=w.provenance('t12c-c2-new'))
    reasons = sorted({r['reason'] for r in t12b.outbox_rows(world.store_path)})
    assert reasons == ['desks_changed', 'seal'], f'precondition: seal and desks_changed rows wait: {reasons}'
    with t12b.traced() as trace:
        t12b.drain_once(world.store())
    assert t12b.outbox_count(world.store_path) == 0, 'precondition: one drain applied every row'
    transactions = index_transactions(trace, world.index_path)
    watermarked = [t for t in transactions if any(seams.WATERMARK_TABLE in sql for sql in t[2])]
    assert watermarked, 'positive control: no index transaction of the drain wrote outbox_watermark'
    assert any(len(t[2]) > 1 for t in watermarked), \
        'positive control: the watermark transaction wrote nothing else (no posting traced)'
    outside = [t for t in transactions if not any(seams.WATERMARK_TABLE in sql for sql in t[2])]
    assert not outside, ('index writes outside the watermark\'s transaction:\n' +
                         '\n'.join(f'{len(t[2])} write(s), e.g. {t[2][0][:200]}' for t in outside[:5]))
    problems = visibility(world, 'after the traced drain: ')
    assert not problems, '\n'.join(problems)
