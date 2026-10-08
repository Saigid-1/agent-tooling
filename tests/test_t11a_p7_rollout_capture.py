"""T11a P7: one ``_page`` (docs/work/orders/T11a-leaf-consolidation.md, P7, L6).

The golden tests/fixtures/t11a/rollout_capture.json was generated at base by
``python tests/fixtures/t11a/generate_rollout_capture.py``. It drives ``RolloutCapture`` and
``ChildRolloutCapture`` as the product does (``initialize()``, then ``capture(...)``) over: a
complete page, a partial trailing line, a prefix change, paging, omissions, and every
``RolloutCaptureConflict`` raise of ``_page`` -- the census's 7 that differ only in the
exception spelling (launch_binding.py:640/644/650/667/676/684/690) and the 2 whose message
differs (:655/:664, "this session metadata" against "this child session metadata").

GREEN-IF: for both classes, every step's result (pages, cursors, receipts, sealed events) or
raised exception (type and message) is byte-identical to base.

The near-duplicate guard of P7 is category (f) of tests/test_t11_leaf_single_home.py: at
base it reports exactly the ``_page`` pair (0.967 under its normalisation), at head none.
"""
from __future__ import annotations

from t11a_golden import assert_matches_golden, load_generator


def test_p7_rollout_and_child_rollout_capture_are_byte_identical_to_base():
    generator = load_generator('generate_rollout_capture')
    assert_matches_golden(generator.build(), generator.GOLDEN)
