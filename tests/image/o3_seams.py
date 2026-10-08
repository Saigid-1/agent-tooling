"""The seams the O3 image tests assume, named once (order docs/work/orders/O3-opencode-optional-image.md).

The TEST arm writes these blind to FEATURE; the meet reconciles each with FEATURE's names by editing
this file only:

- `OPENCODE_IMAGE`: the environment variable naming the new target's image. The target itself is
  assumed to be called `opencode` (the order's example name).
- `PIN_MODULE`.`PIN_NAME`: the one constant the tests import (Q4), `OPENCODE_VERSION` in
  tests/image/opencode_pin.py (reconciled at the meet; the TEST arm had assumed tests/image/image_harness.py).
- `VERSION_LABEL`: the image label carrying the pin (Q4), assumed `agent-tooling.opencode.version`,
  the agents target's `agent-tooling.<cli>.version` pattern.
- `LICENSE_PATH`: where the target ships the upstream LICENSE text. This one is the Coordinator's
  specification (2026-10-06), not an assumption.

The default image is the product image the S2 suite already names (`image_harness.PRODUCT`).
"""
from __future__ import annotations

import importlib
import re

import pytest

OPENCODE_IMAGE = "AGENT_TOOLING_TEST_IMAGE_OPENCODE"
PIN_MODULE, PIN_NAME = "opencode_pin", "OPENCODE_VERSION"
VERSION_LABEL = "agent-tooling.opencode.version"
LICENSE_PATH = "/usr/local/share/doc/opencode/LICENSE"

VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")


def pinned_version() -> str:
    """The pin, from the constant FEATURE exposes; FAILS (never skips) when it is missing."""
    value = getattr(importlib.import_module(PIN_MODULE), PIN_NAME, None)
    if not (isinstance(value, str) and VERSION.fullmatch(value)):
        pytest.fail(f"the OpenCode pin constant {PIN_MODULE}.{PIN_NAME} is {value!r}, not a version: the "
                    "order's Q4 constant is missing (a seam this test assumes; the meet reconciles it)",
                    pytrace=False)
    return value


def prints_version(output: str, version: str) -> bool:
    """`version` appears in `output` as a whole token (the agents-CLI image test's rule)."""
    return re.search(rf"(?<![\w.]){re.escape(version)}(?![\w.])", output) is not None
