"""Fixtures for the S3 installer contract tests (tests/install/ only).

The `image` marker is registered here rather than in pytest.ini because the
order confines the TEST arm to tests/install/. Image-marked tests in this
directory are deselected unless a `-m` expression names `image`; when it does,
they run and FAIL (never skip) if AGENT_TOOLING_TEST_IMAGE is unset.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from s3_harness import INSTALLER, World

HERE = Path(__file__).resolve().parent


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "image: needs a built image named by AGENT_TOOLING_TEST_IMAGE; "
        "runs only when -m selects it, and then fails if the variable is unset")


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


@pytest.fixture(scope="session")
def installer() -> Path:
    if not (INSTALLER.is_file() and os.access(INSTALLER, os.X_OK)):
        pytest.fail(f"console script kp-agent-install is not installed next to the test "
                    f"interpreter: {INSTALLER} (order S3: [project.scripts] entry point)")
    return INSTALLER


@pytest.fixture
def world(tmp_path, installer) -> World:
    return World.create(tmp_path)
