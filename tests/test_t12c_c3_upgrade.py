"""T12c C3: the upgrade. An index built before T12c is rebuilt by T12b's build-and-swap, requested by the indexer.

Order: docs/work/orders/T12c-per-desk-postings.md, C3 and A1 (Verification): "A schema-version mismatch is the one
case in which the indexer requests its own reindex. It requests no reindex for anything else, an empty or absent
index included"; "the old file keeps answering searches, with today's cost, until the rename". Falsifiers: "During
C3's build, a search that fails or returns nothing."; the mutant "the indexer requests a reindex on anything other
than a schema-version mismatch, for example on an empty index (A1)".

An index built before T12c (tests/t12c_seams.py): the base schema written by the frozen base writer over a store
the indexer has fully drained (`frozen-base`), or a committed T12b base-store fixture (`t10_corpus`,
`t10h_upgraded`). The indexer is the role's loop, `watch`, over the world's root. Reindex rows are counted by the
outbox log instrument (every insert, kept after retention).

- test_c3_an_index_built_before_t12c_gets_exactly_one_reindex_row_from_the_indexer: GREEN-IF, over 3 passes of
  the indexer, exactly one `reindex` row is inserted, the index file is replaced (a new file at the path), the
  outbox is empty afterwards, and a search returns the episodes it returned on the old file.
- test_c3_an_empty_or_absent_index_gets_no_reindex_row_from_the_indexer (a guard at base; the A1 mutant's
  falsifier): GREEN-IF, for an absent index and an empty index of the current schema, with seal rows waiting or with
  none, 3 passes of the indexer insert no `reindex` row.
- test_c3_the_old_index_answers_before_the_indexer_runs (a guard at base): GREEN-IF a search on the index built
  before T12c returns the episodes the product's own index returned, with no error and not `index_unavailable`.
- test_c3_a_search_during_the_build_answers: GREEN-IF the indexer builds (statements on a build file, then a rename
  onto the index), and a search at the build's first statement and one just before the rename each return the
  episodes the old file returned, with no error and not `index_unavailable`, while the old file is still in place.
- test_c3_deploy_record_expectations (the order's A1 post-deploy expectations, not a falsifier): GREEN-IF
  `index_lag` reads 1 at the build's first statement and at the rename and 0 after the pass, the pass's line is not
  `idle` and names the reindex, and the role's health reads healthy.
- test_c3_upgrade_sources_does_not_request_the_reindex (a guard at base): GREEN-IF `upgrade-sources` on a store
  whose index was built before T12c reports `reindex_requested: false` and inserts no `reindex` row.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
from pathlib import Path

import pytest

import t10_corpus as corpus
import t10_world as w
import t12b_seams as t12b
import t12c_seams as seams
from t12c_seams import ids, search

A = corpus.A
QUERY = 'juniper marker'
FIXTURE_STORES = ('t10_corpus', 't10h_upgraded')
FIXTURE_QUERY = 'cedar'
PASSES = 3


def populate(world):
    store, sources = world.store(), world.sources()
    with w.frozen_clock():
        for i in range(4):
            store.capture(A, source_ref=f't12c:c3:capture:{i}', events=w.events(f'Juniper marker capture {i}.'))
        session = sources.register(tenant_id=w.TENANT_ONE, runtime='claude', native_id='t12c-c3')
        sources.claim(**w.claim_args(session, corpus.ALPHA))
        for i in range(4):
            sources.import_episode(session_id=session, source_ref=f't12c:c3:import:{i}',
                                   events=w.events(f'Juniper marker import {i}.'), provenance=w.provenance(f't12c-c3-{i}'))
        store.import_operator_episode(tenant_id=w.TENANT_ONE, role='alpha', repo_key=w.REPO, source_ref='t12c:c3:legacy',
                                      events=w.imported_events('Juniper marker legacy.'),
                                      source_provenance=w.legacy_provenance('t12c-c3-legacy'))


def frozen_base_world(root):
    """A store fully drained by the indexer in this tree, its index replaced by the one the base indexer leaves."""
    world = w.World(root, corpus.DESKS, corpus.SESSIONS)
    world.initialize()
    populate(world)
    t12b.drain(world.store())
    assert t12b.outbox_count(world.store_path) == 0, 'precondition: drained'
    product = search(world, A, QUERY)
    assert len(ids(product)) == 9 and product['covered_episodes'] == product['total_episodes'], \
        f'precondition: the product index answers: {product}'
    seams.write_base_index(world.store_path, world.index_path)
    seams.install_outbox_log(world.store_path)
    return world, QUERY, set(ids(product))


def fixture_world(root, name):
    """A committed T12b base-store fixture (store and index written at T12b's base), its outbox installed by a
    public write that seals nothing."""
    from test_t12b_b1_outbox import base_world
    world, _ = base_world(root, name)
    world.sources().register(tenant_id=w.TENANT_ONE, runtime='claude', native_id=f't12c-c3-{name}')
    assert t12b.outbox_count(world.store_path) == 0, 'precondition: an empty outbox'
    found = search(world, A, FIXTURE_QUERY)
    assert 'error' not in found and ids(found), f'precondition: the fixture index answers {FIXTURE_QUERY!r}: {found}'
    seams.install_outbox_log(world.store_path)
    return world, FIXTURE_QUERY, set(ids(found))


def old_world(root, name):
    return frozen_base_world(root) if name == 'frozen-base' else fixture_world(root, name)


def _answer_problems(found, expected, moment):
    if 'error' in found:
        return [f'{moment}: the search failed: {found["error"]}']
    problems = []
    if found.get('index_status') == 'index_unavailable':
        problems.append(f'{moment}: index_unavailable')
    if not ids(found):
        problems.append(f'{moment}: the search returned nothing ({found.get("index_status")})')
    elif set(ids(found)) != expected:
        problems.append(f'{moment}: returned {sorted(ids(found))}, the old file returned {sorted(expected)}')
    return problems


@pytest.mark.parametrize('name', ('frozen-base',) + FIXTURE_STORES)
def test_c3_an_index_built_before_t12c_gets_exactly_one_reindex_row_from_the_indexer(tmp_path, name):
    world, query, expected = old_world(tmp_path / 'world', name)
    version, _ = seams.base_index_shape(world.index_path)
    assert version == 1, 'precondition: the index is in the base schema'
    before = os.stat(world.index_path).st_ino
    code, passes = seams.run_indexer(world.root, max_passes=PASSES)
    requested = seams.logged(world.store_path, 'reindex')
    problems = []
    if len(requested) != 1:
        problems.append(f'{len(requested)} reindex rows inserted over {PASSES} passes, not 1: {requested}; '
                        f'lines {[[(l.get("status"), l.get("reindex")) for l in p] for p in passes]}')
    if os.stat(world.index_path).st_ino == before:
        problems.append('the index file was not replaced (no build-and-swap)')
    if t12b.outbox_count(world.store_path):
        problems.append(f'{t12b.outbox_count(world.store_path)} outbox rows remain after {PASSES} passes')
    if code != 0:
        problems.append(f'the indexer exited {code}: {passes}')
    problems += _answer_problems(search(world, A, query), expected, 'after the passes')
    assert not problems, f'{name}:\n' + '\n'.join(problems)


def _absent_pending(root):
    world = w.World(root, corpus.DESKS, corpus.SESSIONS)
    world.initialize()
    populate(world)
    assert not world.index_path.exists() and t12b.outbox_count(world.store_path), 'precondition: rows wait, no index'
    return world, True


def _absent_drained(root):
    world, _ = _absent_pending(root)
    t12b.drain(world.store())
    world.index_path.unlink()
    assert t12b.outbox_count(world.store_path) == 0, 'precondition: drained, then the index removed'
    return world, False


def _empty_pending(root):
    world, _ = _absent_pending(root)
    world.index().initialize()
    return world, True


def _empty_drained(root):
    world, _ = _absent_drained(root)
    world.index().initialize()
    return world, False


EMPTY_OR_ABSENT = {'absent-with-rows-waiting': _absent_pending, 'absent-drained': _absent_drained,
                   'empty-with-rows-waiting': _empty_pending, 'empty-drained': _empty_drained}


@pytest.mark.parametrize('case', sorted(EMPTY_OR_ABSENT))
def test_c3_an_empty_or_absent_index_gets_no_reindex_row_from_the_indexer(tmp_path, case):
    world, waiting = EMPTY_OR_ABSENT[case](tmp_path / 'world')
    seams.install_outbox_log(world.store_path)
    code, passes = seams.run_indexer(world.root, max_passes=PASSES)
    lines = [line for lines in passes for line in lines if line.get('store')]
    assert len(lines) == PASSES and all(line.get('status') != 'error' for line in lines), \
        f'positive control: the indexer drained the store on every pass: {passes}'
    requested = seams.logged(world.store_path, 'reindex')
    assert requested == [], f'{case}: the indexer inserted {len(requested)} reindex row(s): {requested}'
    if waiting:
        found = search(world, A, QUERY)
        assert len(ids(found)) == 9 and found['covered_episodes'] == found['total_episodes'], \
            f'positive control: the waiting rows were indexed without a reindex: {found}'


@pytest.mark.parametrize('name', ('frozen-base',) + FIXTURE_STORES)
def test_c3_the_old_index_answers_before_the_indexer_runs(tmp_path, name):
    world, query, expected = old_world(tmp_path / 'world', name)
    problems = _answer_problems(search(world, A, query), expected, 'on the old index')
    assert not problems, '\n'.join(problems)


class BuildProbe:
    """Searches at the build's first statement on a build file and just before the rename onto the index."""

    def __init__(self, world, query):
        self.world, self.query = world, query
        self.index = os.path.realpath(world.index_path)
        self.started = self.renamed = False
        self.probes = []   # (moment, output, index_lag, old file in place)
        self.busy = False

    def probe(self, moment):
        self.busy = True
        try:
            version, _ = seams.base_index_shape(self.world.index_path)
            self.probes.append((moment, search(self.world, A, self.query), t12b.outbox_count(self.world.store_path),
                                version == 1))
        finally:
            self.busy = False

    def observe(self, trace):
        if self.busy or self.started:
            return
        last = next((e for e in reversed(trace.events) if e.kind == 'sql'), None)
        # The build: a statement on an index-family file other than the index (tests/t12b_seams.py index_family),
        # whatever indexer function runs it.
        if last is not None and t12b.index_family(last.db) and last.db != self.index:
            self.started = True
            self.probe('at the build\'s first statement')

    def on_audit(self, event, args):
        if event != 'os.rename' or self.busy:
            return
        try:
            destination = os.path.realpath(os.fsdecode(os.fspath(args[1])))
        except (TypeError, ValueError):
            return
        if destination == self.index:
            self.renamed = True
            self.probe('just before the rename onto the index')

    def run(self):
        with t12b.audit_handler(self.on_audit), t12b.traced(self.observe):
            self.code, self.passes = seams.run_indexer(self.world.root, max_passes=1)
        return self


def test_c3_a_search_during_the_build_answers(tmp_path):
    world, query, expected = frozen_base_world(tmp_path / 'world')
    run = BuildProbe(world, query).run()
    assert run.started and run.renamed, (f'precondition: the indexer built a new index (build statements '
                                         f'{run.started}, rename onto the index {run.renamed}): {run.passes}')
    problems = []
    for moment, found, _, old in run.probes:
        if not old:
            problems.append(f'{moment}: the old file is no longer in place')
        problems += _answer_problems(found, expected, moment)
    assert not problems, 'a search during the build:\n' + '\n'.join(problems)


def test_c3_deploy_record_expectations(tmp_path):
    world, query, _ = frozen_base_world(tmp_path / 'world')
    run = BuildProbe(world, query).run()
    assert run.started and run.renamed, f'precondition: the indexer built a new index: {run.passes}'
    problems = [f'{moment}: index_lag {lag}, not 1' for moment, _, lag, _ in run.probes if lag != 1]
    lines = [line for lines in run.passes for line in lines if line.get('store')]
    if len(lines) != 1:
        problems.append(f'{len(lines)} store lines in the pass: {run.passes}')
    else:
        line = lines[0]
        if line.get('status') == 'idle' or not (line.get('reindex') or line.get('status') == 'reindex'):
            problems.append(f'the pass\'s line does not read reindex: {line}')
        if line.get('index_lag') != 0:
            problems.append(f'index_lag {line.get("index_lag")} after the pass, not 0')
        if line.get('health') != 'healthy':
            problems.append(f'the line\'s health: {line.get("health")}')
    code, record = t12b.indexer_function(t12b.HEALTH_NAME)(world.root)
    if code != 0 or record.get('status') != 'healthy':
        problems.append(f'the role health: exit {code}, {record}')
    assert not problems, 'A1 post-deploy expectations:\n' + '\n'.join(problems)


def test_c3_upgrade_sources_does_not_request_the_reindex(tmp_path):
    world, query, expected = frozen_base_world(tmp_path / 'world')
    from kp_agent_tooling import desk_cli
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
        code = desk_cli.main(['--config', str(world.configs[A]), 'upgrade-sources'])
    assert code == 0, f'upgrade-sources exited {code}: {out.getvalue()} {err.getvalue()}'
    report = json.loads(out.getvalue())
    coverage = report.get('coverage', {})
    assert coverage.get('reindex_requested') is False, f'upgrade-sources: {coverage}'
    assert seams.logged(world.store_path, 'reindex') == [], 'upgrade-sources inserted a reindex row'
    problems = _answer_problems(search(world, A, query), expected, 'after upgrade-sources')
    assert not problems, '\n'.join(problems)
