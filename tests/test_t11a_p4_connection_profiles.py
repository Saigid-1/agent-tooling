"""T11a P4: one SQLite open, profiles unchanged (T11a order, P4; Verification M2).

The base profiles are the goldens tests/fixtures/t11a/connection_profiles.json (and _ops.json for
the two stores in extensions/ops), generated at
base by ``python tests/fixtures/t11a/generate_connection_profiles.py``. The generator's
recorder wraps ``sqlite3.connect`` and records each call's raw ``database`` argument (type and
text, scratch paths normalised, a symlinked state directory kept distinguishable from its
target) and its effective ``uri``, ``timeout``, ``isolation_level``, ``check_same_thread``,
``detect_types`` and ``autocommit``. It drives every one of the census's 36 module connect
sites through public entry points (tests/fixtures/t11a/connection_profile_sites.txt lists
which step reached which site at base). This test replays the same steps in this tree.

GREEN-IF: every step opens the same number of connections, in the same order, each with a
byte-identical profile and the same first statement (each step also runs under the T10 recorder,
so a statement the open itself runs reads as a difference).

At T11b's head (T11b Amendment 1, meet) this golden is T11b's regression set: an entry may differ from base
only where tests/test_t11b_regression_set.py RULINGS names it, and every other entry stays byte-identical.
"""
from __future__ import annotations

import pytest

from t11a_golden import load_generator
from test_t11b_regression_set import assert_matches_base_except_ruled


def test_p4_connection_profiles_match_base_except_t11b_rulings():
    generator = load_generator('generate_connection_profiles')
    text, _sites = generator.build('core')
    assert_matches_base_except_ruled(text, generator.GOLDENS['core'])


def test_p4_ops_connection_profiles_match_base_except_t11b_rulings():
    pytest.importorskip('kp_agent_tooling_ops', reason='the knowledge lifecycle and reference manifest stores '
                        'need extensions/ops installed')
    generator = load_generator('generate_connection_profiles')
    text, _sites = generator.build('ops')
    assert_matches_base_except_ruled(text, generator.GOLDENS['ops'])


def _sqlite_connect_profiles(parameters):
    """Every profile combination the leaf's sqlite_connect supports, from its own signature."""
    import itertools
    domains = {'mode': [None, 'ro', 'rw', 'rwc'], 'resolve': [False, True], 'timeout': [None, 10, 30.0],
               'isolation_level': ['<omitted>', None, 'DEFERRED'], 'check_same_thread': [True, False]}
    names = [name for name in domains if name in parameters]
    for values in itertools.product(*(domains[name] for name in names)):
        profile = {name: value for name, value in zip(names, values) if value != '<omitted>'}
        if profile.get('resolve') and profile.get('mode') is None:
            continue  # resolve applies only to a URI open
        yield profile


def test_p4_leaf_sqlite_connect_runs_no_statement_before_the_callers_first(tmp_path):
    """Verification U2: under the T10 recorder, a fresh leaf.sqlite_connect, in every profile the
    leaf supports, traces ZERO statements before the caller's first (an implicit pragma would
    show). GREEN-IF: for each profile, the connection's first traced statement is the caller's.
    Skips at base, which has no leaf; the per-site form is the golden's first_statement field."""
    import inspect
    import sqlite3
    from contextlib import closing
    leaf = pytest.importorskip('kp_agent_tooling._impl.leaf', reason='no leaf module at base')
    if not hasattr(leaf, 'sqlite_connect'):
        pytest.skip('the leaf names no sqlite_connect')
    from t10_instruments import recording
    database = tmp_path / 'store.sqlite3'
    with closing(sqlite3.connect(database)) as db:
        db.execute('CREATE TABLE t (x)')
        db.commit()
    parameters = inspect.signature(leaf.sqlite_connect).parameters
    profiles = list(_sqlite_connect_profiles(parameters))
    assert profiles, 'no profile to exercise'
    for profile in profiles:
        targets = [database] if profile.get('mode') else [database, ':memory:']
        for target in targets:
            with recording() as traced:
                connection = leaf.sqlite_connect(target, **profile)
                try:
                    before = [s.sql for s in traced.statements if s.conn == id(connection)]
                    connection.execute('SELECT 1').fetchall()
                    after = [s.sql for s in traced.statements if s.conn == id(connection) and not s.nested]
                finally:
                    connection.close()
            assert before == [], f'sqlite_connect({target!r}, {profile}) ran {before} before the caller'
            assert after[:1] == ['SELECT 1'], f'sqlite_connect({target!r}, {profile}): first statement {after[:1]}'
