"""Collection rules for the T6a P1 variant tests (tests/image/variants only).

These tests are marked `image`. The marker is registered here as well as wherever
pytest.ini registers it, because the order confines the TEST arm to tests/image/ and
tests/ci/. Image-marked tests in this directory are deselected unless a `-m`
expression names `image`; when it does, they run and FAIL (never skip) if the image
variable they read is unset. This directory has its own conftest so that it never
collides with tests/image/conftest.py.
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "image: needs built images named by AGENT_TOOLING_TEST_IMAGE* variables; runs only "
        "when -m selects it, and then fails if a variable it reads is unset")


def pytest_collection_modifyitems(config, items):
    if "image" in (config.getoption("markexpr") or ""):
        return
    keep, drop = [], []
    for item in items:
        mine = Path(str(item.path)).resolve().is_relative_to(HERE)
        (drop if mine and item.get_closest_marker("image") else keep).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
