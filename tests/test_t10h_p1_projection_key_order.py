"""T10h P1: the projection passes ``quick_check`` on the runtime's SQLite (structure, in-process).

Order: docs/work/orders/T10h-projection-hotfix.md (P1), frozen at its merge commit. D1: on SQLite
3.40.1 (the product image) ``PRAGMA quick_check`` reports ``NULL value in
episode_desks.tenant`` for a populated ``WITHOUT ROWID`` table whose NOT NULL non-key
column is declared between its primary-key columns. The host's SQLite (3.53) reports
``ok`` for both orders, so these in-process tests assert structure; the image test
(tests/install/test_t10h_p3_image_sqlite.py) asserts ``quick_check`` on the image's SQLite.

* P1(i): every projection table of a freshly upgraded store declares its primary-key
  columns before any other column. Projection tables are discovered, never named: every
  table the base writer (the goldens' ``base_schema``) does not create. Key columns come
  from ``PRAGMA table_info`` (its ``pk`` field), never from DDL text.
* P1(ii): a store whose ``episode_desks`` has the base DDL's order
  ``(desk, tenant, kind, seq, episode_id)``, populated, is rebuilt by ONE
  ``kp-agent-desk ... upgrade-sources``: key columns first, the same multiset of
  ``(desk, tenant, kind, seq, episode_id)`` rows, the ``episode_desks_episode`` index on
  ``episode_id``, the rebuild's DDL inside one ``BEGIN IMMEDIATE`` transaction, reads equal
  to the store's reads before it was given the old order, and a second run changes nothing
  (``iterdump``).
* P1(iii) is T10's unchanged suite (``tests/test_t10_*.py``), run as is.

The old-order fixture holds the base writer's DDL text for ``episode_desks``; that text is
the fixture (the store the T10 writer left on the live runtime), not an assertion.
"""
from __future__ import annotations

import re
import sqlite3
from collections import Counter
from contextlib import closing

import pytest

import t10_corpus as corpus
import t10_tamper as tamper
import t10_world as w
from t10_instruments import strip_literals
from test_t10_p6_upgrade import _older_row, _older_writer_copy, _populate, _reads, _upgrade, _world

A = corpus.A
TABLE = 'episode_desks'
INDEX = 'episode_desks_episode'
ROW = ('desk', 'tenant', 'kind', 'seq', 'episode_id')
# The base writer's DDL (at the T10h base, session_sources._PROJECTION_DDL), as SQLite records it.
OLD_TABLE_DDL = ('CREATE TABLE episode_desks (desk TEXT NOT NULL, tenant TEXT NOT NULL,'
                 ' kind INTEGER NOT NULL, seq INTEGER NOT NULL, episode_id TEXT NOT NULL,'
                 ' PRIMARY KEY (desk, kind, seq, episode_id)) WITHOUT ROWID')
OLD_INDEX_DDL = 'CREATE INDEX episode_desks_episode ON episode_desks(episode_id)'


# ---- structure, read through PRAGMAs only -------------------------------------------------

def projection_tables(path):
    """Tables of the store that the base writer does not create (discovered, not named)."""
    base = tamper.base_schema()['tables']
    with closing(sqlite3.connect(path)) as db:
        names = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return [name for name in names if name not in base]


def table_info(db, table):
    """``[(cid, name, pk)]`` from ``PRAGMA table_info``; ``pk`` is the 1-based key position, 0 for none."""
    return [(row[0], row[1], row[5]) for row in db.execute(f'PRAGMA table_info("{table}")')]


def key_order_problem(db, table):
    """Why ``table`` does not declare its primary-key columns first, or None."""
    info = table_info(db, table)
    keys = {name for _, name, pk in info if pk}
    declared = [name for _, name, _ in info]
    if keys and set(declared[:len(keys)]) != keys:
        return (f'{table}: declared columns {declared}, primary key '
                f'{[name for _, name, pk in sorted(info, key=lambda r: r[2]) if pk]}')
    return None


def key_order_problems(path):
    with closing(sqlite3.connect(path)) as db:
        return [p for p in (key_order_problem(db, t) for t in projection_tables(path)) if p]


def index_columns(db, table, index):
    """Columns of ``index`` if ``PRAGMA index_list(table)`` lists it, else None."""
    if index not in {row[1] for row in db.execute(f'PRAGMA index_list("{table}")')}:
        return None
    return [row[2] for row in db.execute(f'PRAGMA index_info("{index}")')]


def desk_rows(path):
    with closing(sqlite3.connect(path)) as db:
        return Counter(db.execute(f'SELECT {", ".join(ROW)} FROM {TABLE}').fetchall())


def dump(path):
    with closing(sqlite3.connect(path)) as db:
        return list(db.iterdump())


def give_old_order(path):
    """Rewrite ``episode_desks`` with the base writer's DDL, rows and index unchanged."""
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute(f'ALTER TABLE {TABLE} RENAME TO t10h_prior_desks')
        db.execute(OLD_TABLE_DDL)
        db.execute(f'INSERT INTO {TABLE} ({", ".join(ROW)}) SELECT {", ".join(ROW)} FROM t10h_prior_desks')
        db.execute('DROP TABLE t10h_prior_desks')  # its index goes with it
        db.execute(OLD_INDEX_DDL)
        db.execute('COMMIT')
    with closing(sqlite3.connect(path)) as db:
        info = table_info(db, TABLE)
        assert [name for _, name, _ in info] == list(ROW), f'fixture: the old order was not written: {info}'
        assert index_columns(db, TABLE, INDEX) == ['episode_id'], 'fixture: the old index was not written'


_REBUILDS = re.compile(r'\s*(?:DROP\s+TABLE|ALTER\s+TABLE)\b', re.I)
_NAMES_TABLE = re.compile(r'\b%s\b' % TABLE, re.I)
_IMMEDIATE = re.compile(r'\s*BEGIN\s+(IMMEDIATE|EXCLUSIVE)\b', re.I)


def rebuild_transactions(recorder, store_path):
    """``[(begin_sql, [statements])]``: each transaction on the store that drops or alters a table
    named ``episode_desks`` (``DROP TABLE episode_desks``, ``ALTER TABLE ... RENAME TO episode_desks``,
    ``ALTER TABLE episode_desks RENAME TO ...``): the destructive steps of a rebuild.

    Such a statement outside any explicit transaction is a transaction of its own, with
    ``begin_sql`` None. Statements are the literal-free top-level trace of the store's
    connections; per connection, BEGIN opens and COMMIT/END/ROLLBACK closes. A
    ``CREATE ... IF NOT EXISTS`` that only ensures the schema is not a rebuild step.
    """
    import os
    target = os.path.realpath(store_path)
    open_ = {}
    found = []
    for statement in recorder.top():
        if statement.db != target:
            continue
        sql = strip_literals(statement.sql)
        verb = statement.verb
        if verb == 'BEGIN':
            open_[statement.conn] = [sql, []]
            continue
        if verb in ('COMMIT', 'END', 'ROLLBACK'):
            current = open_.pop(statement.conn, None)
            if current and current[1]:
                found.append((current[0] if verb != 'ROLLBACK' else 'ROLLED BACK: ' + current[0], current[1]))
            continue
        current = open_.get(statement.conn)
        if current is not None:
            current[1].append(sql)
        else:
            found.append((None, [sql]))
    return [(begin, body) for begin, body in found
            if any(_REBUILDS.match(s) and _NAMES_TABLE.search(s) for s in body)]


# ---- worlds ------------------------------------------------------------------------------------

def _fresh(tmp_path, name='fresh'):
    world = _world(tmp_path / name)
    world.ids = _populate(world)
    return world


# ---- P1(i) -------------------------------------------------------------------------------------

@pytest.mark.parametrize('origin', ['new_store', 'older_writer_upgraded'])
def test_t10h_p1_projection_tables_declare_primary_key_first(tmp_path, origin):
    """GREEN-IF every projection table of a freshly upgraded store (a new-version store, and an
    older-writer store projected by `upgrade-sources`) declares its primary-key columns before any
    other column (PRAGMA table_info), and the store has the projection tables (positive control)."""
    world = _fresh(tmp_path)
    if origin == 'older_writer_upgraded':
        world = _older_writer_copy(world, tmp_path / 'older')
        assert not projection_tables(world.store_path), 'fixture: the older-writer copy has no projection'
    report, _ = _upgrade(world)
    assert report.get('status') == 'complete', report
    tables = projection_tables(world.store_path)
    assert TABLE in tables and len(tables) >= 2, f'positive control: projection tables discovered: {tables}'
    with closing(sqlite3.connect(world.store_path)) as db:
        assert sum(1 for _, _, pk in table_info(db, TABLE) if pk) == 4, table_info(db, TABLE)
        assert db.execute(f'SELECT count(*) FROM {TABLE}').fetchone()[0] > 0, 'positive control: populated'
    problems = key_order_problems(world.store_path)
    assert not problems, 'projection tables declare a non-key column before a key column:\n' + '\n'.join(problems)


# ---- P1(ii) ------------------------------------------------------------------------------------

@pytest.mark.parametrize('projection', ['complete', 'marks_behind'])
def test_t10h_p1_old_order_episode_desks_is_rebuilt_by_one_upgrade(tmp_path, projection):
    """GREEN-IF one `upgrade-sources` on a populated store whose episode_desks has the base order
    (tenant at cid 1) leaves episode_desks with its key columns first, every prior
    (desk, tenant, kind, seq, episode_id) row (the same multiset when nothing was behind), the
    episode_desks_episode index on (episode_id), the rebuild's DDL in one BEGIN IMMEDIATE
    transaction and reads equal to the store's reads before; and a second run changes nothing."""
    world = _fresh(tmp_path)
    expected_reads = _reads(world)
    assert all('error' not in r for r in expected_reads), 'positive control: the store answers'
    give_old_order(world.store_path)
    before = desk_rows(world.store_path)
    assert sum(before.values()) >= 5 and len({row[0] for row in before}) >= 2, (
        f'fixture: episode_desks is populated across desks: {before}')
    with closing(sqlite3.connect(world.store_path)) as db:
        assert key_order_problem(db, TABLE), 'fixture: the old order is detected by the structural check'
    if projection == 'marks_behind':
        new = _older_row(world, 'episodes')
        assert new

    report, recorder = _upgrade(world, trace=True)

    assert report.get('status') == 'complete', report
    with closing(sqlite3.connect(world.store_path)) as db:
        problem = key_order_problem(db, TABLE)
        assert problem is None, f'upgrade-sources left the old order: {problem}'
        assert {name for _, name, _ in table_info(db, TABLE)} == set(ROW), table_info(db, TABLE)
        assert index_columns(db, TABLE, INDEX) == ['episode_id'], (
            f'{INDEX} is missing after the rebuild: {db.execute(f"PRAGMA index_list({TABLE})").fetchall()}')
    after = desk_rows(world.store_path)
    if projection == 'complete':
        assert after == before, f'the rebuild changed the row multiset: lost {before - after}, gained {after - before}'
    else:
        assert not (before - after), f'the rebuild lost rows: {before - after}'
        assert any(row[4] == new for row in after), 'the older-writer row was not projected after the rebuild'

    transactions = rebuild_transactions(recorder, world.store_path)
    assert transactions, 'no DDL naming episode_desks was traced: nothing rebuilt the table'
    assert len(transactions) == 1, (
        'the rebuild ran in more than one transaction:\n' + '\n'.join(f'{b}: {s[:4]}' for b, s in transactions))
    begin, body = transactions[0]
    assert begin is not None and _IMMEDIATE.match(begin), f'the rebuild did not run under BEGIN IMMEDIATE: {begin}'
    verbs = ' | '.join(body)
    for step in (r'\bCREATE\s+TABLE\b', r'\bINSERT\b.*\bSELECT\b', r'\bDROP\s+TABLE\b', r'\bCREATE\s+INDEX\b'):
        assert re.search(step, verbs, re.I | re.S), f'the rebuild transaction has no {step}: {body}'

    if projection == 'complete':
        assert _reads(world) == expected_reads, 'reads after the rebuild differ from reads before'
    else:
        assert all('error' not in r for r in _reads(world)), 'reads refuse after upgrade-sources'
    first = dump(world.store_path)
    _upgrade(world)
    assert dump(world.store_path) == first, 'a second upgrade-sources changed the store'
