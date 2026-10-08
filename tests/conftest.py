"""Hooks for every suite under tests/: D0b R6, the image suites on pushed digests beside a local build.

See tests/d0b_r6_harness.py. Outside pushed-digest mode (no published target's variable names a
registry digest) these hooks change nothing.
"""
from __future__ import annotations

import os

import pytest

import d0b_r6_harness as r6


def pytest_report_header(config):
    return r6.header(os.environ)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    if item.get_closest_marker("image") is None:
        return
    reason = r6.failure(os.environ)
    if reason:
        pytest.fail(reason, pytrace=False)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    hidden = {}
    if item.get_closest_marker("image") is not None:
        for name in r6.withheld(os.environ, r6.node_id(item.path, item.name)):
            hidden[name] = os.environ.pop(name)
        if hidden:
            item.add_report_section("setup", "d0b-r6", "withheld (not in the order's R6 list of local-build tests): "
                                    + ", ".join(sorted(hidden)))
    try:
        return (yield)
    finally:
        os.environ.update(hidden)
