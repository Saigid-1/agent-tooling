"""O3: the OpenCode target ships the upstream LICENSE text, and the default image does not.

Falsifier added by the Coordinator, 2026-10-06, after the licence decision: the OpenCode binary packages
ship under the upstream MIT LICENSE at the pinned tag (their npm metadata carries no licence field), so
the target's image carries that LICENSE text at `LICENSE_PATH` (/usr/local/share/doc/opencode/LICENSE, the
Coordinator's specification). The file is non-empty and begins with `MIT License`; a default image
(`product`) does not carry it. Seams: tests/image/o3_seams.py.

RED at the order's base for the target (there is no OpenCode target); the default-image test is a guard,
GREEN at base.
"""
from __future__ import annotations

import pytest

from image_harness import image_for, required, sh
from o3_seams import LICENSE_PATH, OPENCODE_IMAGE
from test_image_agents_and_credentials import _scan

pytestmark = pytest.mark.image

LICENSE_HEAD = "MIT License"

# Reads the file as root, so the check is about what the image ships, not who may read it.
READ_LICENSE_SCRIPT = r"""
if [ -L "$1" ]; then echo "SYMLINK $(readlink "$1")"; fi
if [ ! -f "$1" ]; then echo "NOFILE"; exit 3; fi
if [ ! -s "$1" ]; then echo "EMPTY"; exit 4; fi
printf 'HEAD '; head -c 200 "$1"
"""

# The agents-CLI image test's scan conventions (positive control, completion marker) for one path.
PRESENT_SCRIPT = r"""
command -v kp-agent-tooling >/dev/null 2>&1 && echo "CONTROL kp-agent-tooling"
if [ -e "$1" ] || [ -L "$1" ]; then echo "PRESENT $1"; fi
echo SCAN-DONE
"""


def test_opencode_target_ships_the_upstream_mit_licence_text():
    """GREEN-IF `LICENSE_PATH` in the OpenCode target is a regular file (or a link to one), non-empty, and
    its content begins with `MIT License`."""
    image = required(OPENCODE_IMAGE)
    result = sh(image, READ_LICENSE_SCRIPT, LICENSE_PATH, user="0:0", timeout=120)
    output = result.stdout
    assert result.returncode == 0, (
        f"{LICENSE_PATH} in {image}: {'absent or not a regular file' if 'NOFILE' in output else 'empty' if 'EMPTY' in output else 'unreadable'} "
        f"(exit {result.returncode}): {(output + result.stderr)[-500:]!r}")
    content = output.split("HEAD ", 1)[1] if "HEAD " in output else ""
    assert content.startswith(LICENSE_HEAD), (
        f"{LICENSE_PATH} in {image} begins {content[:120]!r}, not {LICENSE_HEAD!r}")


@pytest.mark.parametrize("target", ["product"])
def test_default_image_does_not_carry_the_opencode_licence(target):
    """GREEN-IF the default image, with the scan's positive control held, has nothing at `LICENSE_PATH`."""
    image = image_for(target)
    found = _scan(image, PRESENT_SCRIPT, LICENSE_PATH)
    assert not found, f"the default {target} image carries the OpenCode LICENSE: {found}"
