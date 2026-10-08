"""T11a ops goldens in the extension job (docs/work/orders/T11a-leaf-consolidation.md, P1/P3/P4).

The T11a goldens live in the core suite (tests/test_t11a_p*.py, tests/fixtures/t11a/). Their
ops parts need extensions/ops installed, so the CI core job (`pytest tests`, core only) skips
them; the extension job runs only `python -m pytest extensions/ops/tests`. This module collects
those ops parts here so that job runs them. extensions/ops/pytest.ini puts ../../tests on the
path, so the core-suite modules import unchanged: one test, one home.

GREEN-IF (each): the ops part of the golden recomputed in this tree is byte-identical to base (P1); for P3 and
P4, at T11b's head, byte-identical except the entries tests/test_t11b_regression_set.py RULINGS names.
"""
from test_t11a_p1_identity_corpus import test_p1_ops_identity_corpus_is_byte_identical_to_base  # noqa: F401
from test_t11a_p3_private_files import test_p3_ops_private_file_helper_sequences_match_base_except_t11b_rulings  # noqa: F401
from test_t11a_p4_connection_profiles import test_p4_ops_connection_profiles_match_base_except_t11b_rulings  # noqa: F401
# T11b's whole-set check (every differing entry ruled, every ruling naming a differing entry), here where the
# ops goldens are compared too; CI's core-only jobs skip them.
from test_t11b_regression_set import test_every_regression_set_difference_is_listed_and_ruled  # noqa: F401
