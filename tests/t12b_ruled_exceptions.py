"""T12b's named regression-set exceptions, ruled at the meet (Verification, 2026-10-04).

This is a record, not RULINGS (tests/test_t11b_regression_set.py, which the Coordinator fills): each entry names a
test, the old assertions it no longer makes, and the replacement, which keeps the test's subject and is not weaker
(each replacement is red under a stub that reintroduces the old inline path or drops the outbox row; the meet report
lists the runs). tests/test_t12b_ruled_exceptions.py checks that every named test exists and cites its ruling.

R2, T10 P5 `status`: the only allowed difference from the golden is an added integer `index_lag`, pinned in the test
(0 wherever the corpus helper drained at that step); every other T10 golden stays byte-identical.

R3, six inline-failure tests, each replaced. Also ruled with them: T10h P2 `marks_behind` in both modes (the Compose
case under the marker with its assertion as written; the host case asserting that a host seal drains what it finds).

R4, T10 P6 (Verification, meet): `index_outbox` is a STORE table in t10_tamper's discovery (`STORE_TABLES`), not a
projection. A fresh store holds outbox rows for its inserts and an upgraded store's outbox starts empty, so the rows
cannot match; comparing after a drain was rejected (it binds P6 to retention). P6 compares the outbox's schema exactly
and excludes only its rows. The defect the exclusion could hide, an upgraded store whose triggers do not fire, is
carried by the B1 test named in the entry's `rows`.
"""
from __future__ import annotations

R2 = {
    'tests/test_t10_p5_identical_results.py::test_p5_outputs_equal_goldens': {
        'old': "every read's digest equals the golden's, `status` included",
        'new': "`status` equals the golden plus an integer `index_lag` pinned per step (0 where the corpus helper "
               "drained; the rows that wait at the one step that leaves its seal for the indexer); every other read "
               "and every write receipt byte-identical",
    },
}

R3 = {
    'tests/test_workspace_capture_adversarial.py::test_large_visible_row_is_reconstructable_and_index_failure_repairs': {
        'old': "with upsert_episodes raising, `failed['files'][0]['status'] == 'error'`; after a repair pass "
               "`index_pending == []`",
        'new': "with the index out during the pass's drain the pass reports no error, the row is sealed once and "
               "reconstructs, its seal waits in the outbox, `index_pending` stays []; the next drain indexes it and a "
               "search finds it",
    },
    'extensions/ops/tests/test_claude_memory_hook.py::test_mapped_large_attachment_does_not_block_short_stop': {
        'old': "with a FailedIndex, `pytest.raises(RuntimeError, match='index outage')` from handle_hook",
        'new': "the short stop still flushes (pending 0, one job, two events, one omission) with the capture's index "
               "configured under the Compose marker, the T10 P4 honesty instrument counts no index connection (and "
               "one store connection), and the next drain indexes the turn",
    },
    'tests/test_native_history_import.py::test_replay_rebuilds_missing_projection_and_bounds_file': {
        'old': "after the replay, `counts['indexed_episodes'] == 1` and the index exists, rebuilt inline by the import",
        'new': "the replay seals exactly once, every index write during it has the indexer (`drain`) on its stack, "
               "`counts['indexed_episodes'] == 1` from that one drain, and the index holds the one episode",
    },
    'tests/test_claude_episode_capture.py::test_index_failure_replays_without_duplicate_capture': {
        'old': "with a failing index, the capture raises and leaves its batch pending; the replay upserts "
               "`{episode_id}` into the index and `len(store.episodes) == len(queue.jobs) == 1`",
        'new': "with the batch left pending by a queue failure after the seal, the replay (index configured, under "
               "the Compose marker) captures the same episode, seals once, queues one job, never connects to the "
               "index, leaves exactly one `seal` outbox row, and the next drain indexes exactly that episode",
    },
    'tests/test_session_import_job.py::test_more_than_eight_megabytes_is_bounded_and_index_repair_is_durable': {
        'old': "with upsert_episodes raising, `advance` raises `index_failed`, the job is `index_error` with "
               "`index_pending_episodes == 1`, a repair advance empties it, and `counts['indexed_episodes'] == 2`",
        'new': "the job completes with the old bounds and `index_pending_episodes == 0` under the Compose marker; a "
               "drainer process killed at its first index write applies nothing and both seals still wait; the next "
               "drain applies both",
    },
    'tests/test_host_card_bridge.py::test_card_capture_refreshes_search_and_retry_repairs_projection': {
        'old': "with rebuild raising, attach_card raises OSError('projection unavailable'); the retry rebuilds and "
               "the card is found",
        'new': "(W7 ruling) with the drainer failing, attach_card attaches, writes no `reindex` row and never "
               "rebuilds (a rebuild or reindex call fails the test), the drainer's result reports the failure, the "
               "receipt shows the card not indexed and its seal waits; with the drainer back a replay drains, the card "
               "is found with full coverage, and a second replay returns the same receipt",
    },
}

R4 = {
    'tests/test_t10_p6_upgrade.py::test_p6_older_writer_store_refuses_then_upgrades_to_the_fresh_projection': {
        'old': "`projection_dump(older) == projection_dump(fresh)` with every table outside the base schema, "
               "`index_outbox` and its rows included, counted as projection",
        'new': "the same equality over the projection with `index_outbox` excluded as a store table, plus "
               "`store_table_schema(older) == store_table_schema(fresh)`: the outbox DDL, the four watched tables' "
               "triggers and `user_version` 1, exactly",
        'rows': 'tests/test_t12b_b1_outbox.py::test_b1_a_store_created_at_base_gets_one_row_per_insert',
        # R4b (meet, after fix b: the first upgrade-sources of an index-less older store requests the reindex
        # and, on a host, drains it): the older copy is taken from the fresh store as sealed, then the fresh reference
        # is drained (what a running indexer leaves); the reads must be equal and answer from the index; each store's
        # coverage token must be its own index's token, and the projections are compared with the token's random
        # value set aside.
        'r4b': "reads compared with a DRAINED fresh store (all answering `results`); "
               "`_store_token(w) == _index_token(w)` for both; projection equality with `scope_state.coverage_token` "
               "set aside",
    },
}

ALSO_RULED = {
    'tests/test_t10h_p2_capture_never_backfills.py::test_t10h_p2_capture_on_incomplete_projection_projects_no_existing_row': (
        'marks_behind runs under the Compose marker; the assertion stands as written'),
    'tests/test_t10h_p2_capture_never_backfills.py::test_t10h_p2_host_capture_on_marks_behind_drains_what_it_finds': (
        'the host case: a host seal drains what it finds'),
}
