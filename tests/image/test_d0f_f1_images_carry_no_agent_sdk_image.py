"""D0f F1: no published-target image (runtime, product, ops) carries @anthropic-ai/claude-agent-sdk.

Order: docs/work/orders/D0f-board-without-agent-sdk.md, F1, frozen at D0f's base. The falsifier is a
per-image file-system check extended into bundle contents and source maps. Each test runs the reusable
check exactly as D0b's publish job runs it against a pushed digest:

    python tests/image/agent_sdk_absence.py [--expect-board] IMAGE

so a green test here is that command's exit 0 (clean, with every instrument control held). Exit 1 is a
finding; exit 2 is "cannot state a result" (a control failed or the image could not be read), which is
also red. What the check reads, its markers and its controls are documented in that file and proved on
synthetic images by test_d0f_f1_scanner.py.

`agents` is not a published target in this order (it carries the claude CLI by design) and is not
checked. At D0f's base runtime is already clean (it has no board); product and ops carry the SDK in
/app/dist/cli.js and /app/dist/cli.js.map.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from image_harness import PRODUCT, RUNTIME, required

pytestmark = pytest.mark.image

CHECK = Path(__file__).resolve().parent / "agent_sdk_absence.py"
OPS = "AGENT_TOOLING_TEST_IMAGE_OPS"
TARGETS = {"runtime": (RUNTIME, False), "product": (PRODUCT, True), "ops": (OPS, True)}


@pytest.mark.parametrize("target", sorted(TARGETS))
def test_published_image_carries_no_claude_agent_sdk(target):
    """GREEN-IF `agent_sdk_absence.py` exits 0 for the image: no regular file of the package, no source
    map listing a file of it, and no file (in any layer, any gzip stream or zip member) containing an SDK
    code marker; with the identity file read and, for product and ops, the board bundle and its map read."""
    variable, expect_board = TARGETS[target]
    image = required(variable)
    argv = [sys.executable, str(CHECK), *(["--expect-board"] if expect_board else []), image]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        pytest.fail(f"`{' '.join(argv)}` did not finish within 3600 s", pytrace=False)
    output = (result.stdout + result.stderr).strip()
    assert result.returncode != 2, f"the check could not state a result for {target} ({image}):\n{output}"
    assert result.returncode == 0, f"{target} ({image}) carries claude-agent-sdk:\n{output}"
