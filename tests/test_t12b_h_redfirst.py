"""T12b meet H1–H2 (Verification, read of the round-1 meet head): two red-first properties, RED with the bounded marks;
FEATURE's fixes are meet fix a (H1) and meet fix b (H2).

H1 (B2, the indexer role; T12a's loop rules: a pass's failure never ends the loop): the indexer writes its health to
`<root>/indexer-health.json`. A failed write of that file is reported in that pass's JSON output and the loop goes on;
the next pass's write restores the file. Before fix a, `watch` writes it through `leaf.replace_file` outside any `try`, so
one failed write ends the loop.

H2 (B2, "Upgrade of an existing runtime root"; `upgrade-sources` writes the one-time `reindex` request for an
older-writer store): an older-writer store with sealed episodes and NO index file is requested exactly one reindex by
its FIRST `upgrade-sources` run, and the next drain covers every sealed episode; an empty store requests nothing. Before
fix b, `_establish_coverage` returns `{'index': 'absent'}` and requests nothing when the index file is absent, so such
a store heals only on a second run. The next drain differs by install, so H2 is read on both:
- inside Compose (tests/t12b_seams.py `compose_marker`: sealers never drain), the request is the one `reindex` row left
  waiting in the outbox, and the indexer's drain (`seams.drain`) then covers every sealed episode;
- on a host, `upgrade-sources` drains its own request at once (desk_cli: `drain_after_seal` when the coverage reports a
  request), so the first run itself leaves every sealed episode indexed and covered, no row waiting.
The empty-store guard runs inside Compose, where a request would stay visible as a row (on a host it is drained at once).
Assumption (stated, as the meet asked; read at the meet, not measured): `_establish_coverage` counts `episode_scope`
rows, which exist only once the scope projection is installed, and `upgrade` reaches it (through `_backfill`) only after
that install: either the projection is already complete (session_sources.py:1239-1243) or `_install` and the backfill
run first (:1249-1269). The older-writer store here has no projection before its first run, so its first run installs
and backfills it, then counts the projected rows.

The loop is FEATURE's `episodic_search.watch` (tests/t12b_seams.py WATCH_NAME, HEALTH_FILE), driven with the fake
clock of tests/test_t12b_g_indexer_loop.py. The failed write is a directory at the health file's path for the first
pass, removed before the second (no product function is patched).
"""
from __future__ import annotations

import contextlib
import io
import json
import os

import pytest

import t10_corpus as corpus
import t12b_seams as seams
from test_t12b_g_indexer_loop import _run_watch, _seal, _world


def _failure_report(lines, root):
    """The JSON objects of one pass that report a failure of the health file's write: an object naming the health file
    (or carrying a health-write key other than the per-store `health`) with an error or failure in it. The root's path
    is blanked first, so the test's own directory name ("..._failed_health_...") never matches."""
    found = []
    for line in lines:
        text = json.dumps(line)
        for path in {str(root), os.path.realpath(root)}:
            text = text.replace(json.dumps(path)[1:-1], '<root>')
        text = text.lower()
        names_health = seams.HEALTH_FILE in text or any('health' in str(key).lower() and key != 'health'
                                                       for key in line)
        if names_health and ('error' in text or 'fail' in text):
            found.append(line)
    return found


def test_h1_a_failed_health_file_write_is_reported_in_its_pass_and_the_loop_goes_on(tmp_path):
    """GREEN-IF, with a directory at `<root>/indexer-health.json` during the first pass (removed before the second): the
    loop runs all 3 passes (`max_passes`), the first pass's JSON output reports the failed health write, and after the
    run the health file is a regular file holding the indexer's health (the next pass's write restored it)."""
    root = tmp_path / 'root'
    world = _world(root / 'w')
    _seal(world, 2, 'h1')
    health_file = root / seams.HEALTH_FILE
    health_file.mkdir(parents=True)

    def after_pass_1():
        health_file.rmdir()
    try:
        code, passes, clock = _run_watch(root, max_passes=3, steps={1: after_pass_1})
    except Exception as error:  # noqa: BLE001 - the loop ending is the failure under test
        pytest.fail(f'a failed health-file write ended the indexer loop: {error!r}', pytrace=False)
    problems = []
    if len(passes) != 3 or len(clock.sleeps) != 2:
        problems.append(f'{len(passes)} passes, {len(clock.sleeps)} sleeps (3 and 2 expected)')
    if not _failure_report(passes[0], root):
        problems.append(f'the first pass does not report the failed health write: {passes[0]}')
    if not health_file.is_file():
        problems.append('the health file was not restored by a later pass')
    else:
        record = json.loads(health_file.read_text())
        if record.get('status') not in ('healthy', 'unhealthy'):
            problems.append(f'the restored health file holds {record}')
    assert not problems, 'the indexer\'s health-file write:\n' + '\n'.join(problems)


def _upgrade(config):
    from kp_agent_tooling import desk_cli
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = desk_cli.main(['--config', str(config), 'upgrade-sources'])
    assert code == 0, f'upgrade-sources exit {code}: {out.getvalue()}'
    return json.loads(out.getvalue())


def _reindex_rows(world):
    return [row for row in seams.outbox_rows(world.store_path) if row['reason'] == 'reindex']


def _sealed(world):
    with seams.ro(world.store_path) as db:
        return {row[0] for row in db.execute('SELECT id FROM episodes UNION SELECT id FROM source_episodes')}


def _coverage_gaps(world, sealed):
    """(sealed episodes the index does not hold, sealed episodes the store's marks do not record covered)."""
    index = world.state / seams.INDEX_NAME
    if not index.is_file():
        return sealed, sealed
    with seams.ro(index) as db:
        indexed = {row[0] for row in db.execute('SELECT episode_id FROM indexed_episodes')}
    with seams.ro(world.store_path) as db:
        covered = {row[0] for row in db.execute('SELECT episode_id FROM episode_scope WHERE covered = 1')}
    return sealed - indexed, sealed - covered


def _older_writer(tmp_path):
    from test_t12b_b1_outbox import _older_writer_world
    world, _ = _older_writer_world(tmp_path)
    assert not (world.state / seams.INDEX_NAME).exists(), 'precondition: the store has no index file'
    sealed = _sealed(world)
    assert sealed, 'precondition: the store has sealed episodes'
    return world, sealed


def test_h2_inside_compose_the_first_upgrade_of_an_index_less_older_store_leaves_one_reindex_row(tmp_path):
    """GREEN-IF, inside Compose, an older-writer store (sealed by the base writer's schema: no projection, no outbox)
    with sealed episodes and no index file has exactly one `reindex` outbox row waiting after its first
    `upgrade-sources` run (and no index file: sealers never drain), and the indexer's next drain then indexes every
    sealed episode and the store's marks record every one covered."""
    world, sealed = _older_writer(tmp_path)
    with seams.compose_marker(tmp_path):
        _upgrade(world.configs[corpus.A])
    rows = _reindex_rows(world)
    assert len(rows) == 1, f'the first upgrade-sources left {len(rows)} reindex rows waiting (one expected)'
    assert not (world.state / seams.INDEX_NAME).exists(), 'inside Compose upgrade-sources created the index'
    seams.drain(world.store())
    unindexed, uncovered = _coverage_gaps(world, sealed)
    assert not unindexed and not uncovered, (
        f'after the indexer\'s drain {len(unindexed)} sealed episodes are not indexed and {len(uncovered)} not covered')


def test_h2_on_a_host_the_first_upgrade_of_an_index_less_older_store_indexes_every_sealed_episode(tmp_path):
    """GREEN-IF, on a host (no Compose marker), the first `upgrade-sources` run on an older-writer store with sealed
    episodes and no index file leaves every sealed episode indexed and recorded covered (its one request drained by
    the run's own drain-after), with no `reindex` row left waiting."""
    world, sealed = _older_writer(tmp_path)
    _upgrade(world.configs[corpus.A])
    unindexed, uncovered = _coverage_gaps(world, sealed)
    problems = []
    if unindexed or uncovered:
        problems.append(f'after the first run {len(unindexed)} of {len(sealed)} sealed episodes are not indexed and '
                        f'{len(uncovered)} not covered (index file present: {(world.state / seams.INDEX_NAME).is_file()})')
    if _reindex_rows(world):
        problems.append(f'reindex rows left waiting on a host: {_reindex_rows(world)}')
    assert not problems, 'the first upgrade-sources on a host:\n' + '\n'.join(problems)


def test_h2_the_upgrade_of_an_empty_store_without_an_index_requests_nothing(tmp_path):
    """GREEN-IF an empty store (initialized, nothing sealed) with no index file has no `reindex` row after
    `upgrade-sources` run inside Compose (where a request would stay waiting as a row). A guard: green before fix b too."""
    world = _world(tmp_path / 'w')
    assert not (world.state / seams.INDEX_NAME).exists(), 'precondition: no index file'
    assert not _sealed(world), 'precondition: nothing sealed'
    with seams.compose_marker(tmp_path):
        _upgrade(world.configs[corpus.A])
    assert _reindex_rows(world) == [], f'an empty store requested a reindex: {_reindex_rows(world)}'
