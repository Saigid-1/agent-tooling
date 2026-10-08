"""T11a P5: one store-path rule, behaviour unchanged (T11a order, P5; Verification U3).

The golden tests/fixtures/t11a/store_path_decisions.json was generated at base by
``python tests/fixtures/t11a/generate_store_path_decisions.py``. It records the decisions and
texts of BOTH consumers of the rule, one operator file per case: deploy/image/container.py's
``operator_store_paths`` (imported by path from this tree) and runtime_install's
``operator_store_paths``. The cases include control characters, where the consumers differ at
base: runtime_install rejects them, container.py does not.

GREEN-IF: every decision and every text of both consumers is byte-identical to base.

At T11b's head (T11b Amendment 1, meet) this golden is T11b's regression set: an entry may differ from base
only where tests/test_t11b_regression_set.py RULINGS names it, and every other entry stays byte-identical.
"""
from __future__ import annotations

from t11a_golden import load_generator
from test_t11b_regression_set import assert_matches_base_except_ruled


def test_p5_store_path_decisions_of_both_consumers_match_base_except_t11b_rulings():
    generator = load_generator('generate_store_path_decisions')
    assert_matches_base_except_ruled(generator.build(), generator.GOLDEN)
