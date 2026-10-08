"""T11a P3: one home for private files, guarantees unchanged (T11a order, P3).

Instrument (the order's): ``sys.addaudithook`` on ``open``, ``os.chmod``, ``os.rename``,
``os.link`` (plus ``os.mkdir`` and ``os.remove``), and the ``os.fsync``/``fcntl.flock`` calls,
comparing base and head per helper on the same inputs. The base sequences are the golden
tests/fixtures/t11a/private_file_sequences.json (and _ops.json for knowledge_publish_cli._write,
which needs extensions/ops), generated at base by
``python tests/fixtures/t11a/generate_private_file_sequences.py``; this test replays the
same inputs in this tree and compares byte for byte.

GREEN-IF: for each of the census's 30 private-file helpers, called through its old name
(P9) on every fixed input, the ordered event sequence (paths, open flags, modes), the
outcome (value or exception type and message) and the files left behind (type, mode, size,
content digest) are identical to base: create-then-chmod stays create-then-chmod,
create-with-mode stays create-with-mode, NOFOLLOW, EXCL and fsync stay where they were.

At T11b's head (T11b Amendment 1, meet) this golden is T11b's regression set: an entry may differ from base
only where tests/test_t11b_regression_set.py RULINGS names it, and every other entry stays byte-identical.
"""
from __future__ import annotations

import pytest

from t11a_golden import load_generator
from test_t11b_regression_set import assert_matches_base_except_ruled


def test_p3_private_file_helper_sequences_match_base_except_t11b_rulings():
    generator = load_generator('generate_private_file_sequences')
    assert_matches_base_except_ruled(generator.build('core'), generator.GOLDENS['core'])


def test_p3_ops_private_file_helper_sequences_match_base_except_t11b_rulings():
    pytest.importorskip('kp_agent_tooling_ops', reason='knowledge_publish_cli._write needs extensions/ops installed')
    generator = load_generator('generate_private_file_sequences')
    assert_matches_base_except_ruled(generator.build('ops'), generator.GOLDENS['ops'])
