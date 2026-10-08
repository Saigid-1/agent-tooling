"""O3: the optional image target that carries OpenCode, and the default image that does not.

Order: docs/work/orders/O3-opencode-optional-image.md. Q1: a new Dockerfile target, FROM product-base, adds
the OpenCode CLI at a pinned version and carries no claude or codex. Q2: the default images do not carry it.
Q4: the pin is recorded in an image label and in one constant the tests import. Falsifiers covered here:
"the new image lacks `opencode` at the pinned version", "the image carries `claude`, `codex` or the Claude
Agent SDK", and "a default image carries `opencode`". The seams (the image variable, the pin constant, the
label) are named once, in tests/image/o3_seams.py. The offline run and the LICENSE text are in
test_o3_opencode_offline_image.py and test_o3_opencode_licence_text_image.py.

Instruments reused: the agents-CLI image test's scan (`_scan` in test_image_agents_and_credentials.py: a
positive control that the image carries kp-agent-tooling, and a completion marker), with its script
extended so that `opencode` is scanned for beside `claude` and `codex`; and D0f's
tests/image/agent_sdk_absence.py, run exactly as D0f's image test runs it.

At the order's base there is no such target (`docker build --target opencode` fails), so every test here
that needs the new image is RED; `test_default_image_has_no_opencode` is a guard and is GREEN at base.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from image_harness import image_for, image_labels, required, run_once, unresolved
from o3_seams import OPENCODE_IMAGE, VERSION_LABEL, pinned_version, prints_version
from test_image_agents_and_credentials import _scan

pytestmark = pytest.mark.image

CHECK = Path(__file__).resolve().parent / "agent_sdk_absence.py"
OTHER_AGENT_CLIS = ("claude", "codex")

# The agents-CLI image test's AGENT_CLI_SCRIPT, extended by one name: opencode.
AGENT_CLI_SCRIPT_WITH_OPENCODE = r"""
command -v kp-agent-tooling >/dev/null 2>&1 && echo "CONTROL kp-agent-tooling"
for n in claude codex opencode; do p=$(command -v "$n" 2>/dev/null) && echo "ONPATH $n $p"; done
find / -xdev \( -type f -o -type l \) \( -name claude -o -name codex -o -name opencode \) -perm /111 2>/dev/null | sed 's/^/FOUND /'
echo SCAN-DONE
"""


def _name(line: str) -> str:
    """The CLI a scan line is about: `ONPATH <name> <path>` or `FOUND <path>`."""
    kind, _, rest = line.partition(" ")
    return rest.split(" ", 1)[0] if kind == "ONPATH" else Path(rest).name


def test_opencode_answers_its_pinned_version():
    """GREEN-IF `opencode --version`, run by /bin/sh as the image's default user (the image's entry
    bypassed, the default bridge network attached), exits 0 and prints the pinned version as a whole
    token. Run time without a network is test_o3_opencode_offline_image.py's property, not this one's."""
    image = required(OPENCODE_IMAGE)
    pinned = pinned_version()
    result = run_once(image, ["-c", "opencode --version"], entrypoint="/bin/sh", network="bridge", timeout=180)
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"`opencode --version` exited {result.returncode}: {output[-800:]}"
    assert prints_version(output, pinned), \
        f"`opencode --version` answered {output.strip()[:300]!r}; the pinned version is {pinned!r}"


def test_opencode_image_labels_its_pinned_version():
    """GREEN-IF the image's label `VERSION_LABEL` equals the pinned version (Q4: where O1 and O2 read it)."""
    image = required(OPENCODE_IMAGE)
    pinned = pinned_version()
    labels = image_labels(image)
    ours = {key: value for key, value in labels.items() if "agent-tooling" in key}
    assert labels.get(VERSION_LABEL) == pinned, (
        f"label {VERSION_LABEL!r} is {labels.get(VERSION_LABEL)!r}; the pinned version is {pinned!r}. "
        f"The image's agent-tooling labels: {ours}")


def test_opencode_target_carries_opencode_and_no_other_agent_cli():
    """GREEN-IF the extended scan, with its positive control held, finds `opencode` on PATH (expected in
    this target only) and finds no `claude` or `codex` on PATH or as an executable file or link anywhere."""
    image = required(OPENCODE_IMAGE)
    found = _scan(image, AGENT_CLI_SCRIPT_WITH_OPENCODE)
    assert any(line.startswith("ONPATH opencode ") for line in found), (
        "opencode is not on PATH in the OpenCode target:\n" + "\n".join(found))
    others = [line for line in found if _name(line) in OTHER_AGENT_CLIS]
    assert not others, "the OpenCode target carries another agent CLI:\n" + "\n".join(others)


def test_opencode_target_carries_no_claude_agent_sdk():
    """GREEN-IF the image is the OpenCode target (`opencode` resolves on PATH: a control, so a clean result
    on some other image proves nothing) and `agent_sdk_absence.py --expect-board IMAGE` exits 0: no file of
    @anthropic-ai/claude-agent-sdk, no source map listing one, no SDK code marker, every control held."""
    image = required(OPENCODE_IMAGE)
    missing = unresolved(image, ["opencode"])
    assert not missing, f"control failed: {image} is not the OpenCode target: {missing}"
    argv = [sys.executable, str(CHECK), "--expect-board", image]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        pytest.fail(f"`{' '.join(argv)}` did not finish within 3600 s", pytrace=False)
    output = (result.stdout + result.stderr).strip()
    assert result.returncode != 2, f"the check could not state a result for {image}:\n{output}"
    assert result.returncode == 0, f"the OpenCode target ({image}) carries claude-agent-sdk:\n{output}"


@pytest.mark.parametrize("target", ["product"])
def test_default_image_has_no_opencode(target):
    """GREEN-IF the default image, under the extended scan with its positive control held, has no
    `opencode` on PATH, no executable file or link named `opencode`, and no label naming opencode."""
    image = image_for(target)
    found = [line for line in _scan(image, AGENT_CLI_SCRIPT_WITH_OPENCODE) if _name(line) == "opencode"]
    labels = sorted(key for key in image_labels(image) if "opencode" in key.lower())
    assert not found and not labels, (
        f"the default {target} image carries opencode:\n" + "\n".join(found + [f"label {k}" for k in labels]))
