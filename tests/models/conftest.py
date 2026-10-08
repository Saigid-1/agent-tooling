"""Fixtures for the M1 model-gateway tests (docs/work/orders/M1-model-gateway.md).

`world` gives each test its own scratch tree under pytest's basetemp (which follows
TMPDIR): key files, artifact_root, a HOME, a TMPDIR and logs for the gateway, and the
loopback provider stubs it starts. Everything it started is stopped at teardown.

Nothing here skips: when the console script is absent, each test FAILS when it first
needs it (m1_harness.require_gateway).
"""
from __future__ import annotations

import pytest

from m1_harness import World


@pytest.fixture
def world(tmp_path):
    scratch = World(tmp_path / "m1")
    try:
        yield scratch
    finally:
        scratch.close()
