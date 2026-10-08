"""O3: the optional `opencode` image target (docs/work/orders/O3-opencode-optional-image.md).

Images, by variable (a test whose variable is unset FAILS; it never skips):
- AGENT_TOOLING_TEST_IMAGE_OPENCODE  the `opencode` target (opencode_pin.OPENCODE_IMAGE_VARIABLE)
- AGENT_TOOLING_TEST_IMAGE, AGENT_TOOLING_TEST_IMAGE_RUNTIME, AGENT_TOOLING_TEST_IMAGE_OPS
  the default images, which must carry no opencode.

The pinned version is imported from opencode_pin, never read from the image under test.

- Q1/Q4: `opencode --version` answers the pin in a container with no network, so nothing is fetched
  at run time; the binary on PATH is the one the lockfile's platform package for the image's
  architecture installed (same file), so the wrapper's networked postinstall fallback did not supply
  it; the version labels state the pin; auto-update is off.
- Q3: the upstream LICENSE is at OPENCODE_LICENSE_PATH with the recorded sha256.
- Absences: the `opencode` image carries no claude or codex CLI (opencode is the expected-present
  binary, a positive control) and no Claude Agent SDK (D0f's check, as D0b runs it); a default image
  carries no opencode. Each scan holds a positive control, so a broken image cannot pass vacuously.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from image_harness import PRODUCT, RUNTIME, image_env, image_labels, required, run_once, sh
from opencode_pin import (
    OCI_VERSION_LABEL, OPENCODE_IMAGE_VARIABLE, OPENCODE_LICENSE_PATH, OPENCODE_LICENSE_SHA256, OPENCODE_PACKAGE,
    OPENCODE_VERSION, OPENCODE_VERSION_LABEL,
)

pytestmark = pytest.mark.image

OPS = "AGENT_TOOLING_TEST_IMAGE_OPS"
SDK_CHECK = Path(__file__).resolve().parent / "agent_sdk_absence.py"
PINNED = re.compile(rf"(?<![\w.]){re.escape(OPENCODE_VERSION)}(?![\w.])")


def _opencode_image() -> str:
    return required(OPENCODE_IMAGE_VARIABLE)


def test_opencode_answers_the_pin_with_no_network():
    """GREEN-IF `docker run --rm --network none --entrypoint opencode IMAGE --version`, as the image's own
    user, exits 0 and prints exactly the pinned version."""
    image = _opencode_image()
    result = run_once(image, ["--version"], entrypoint="opencode", network="none", timeout=180)
    output = (result.stdout + result.stderr).strip()
    assert result.returncode == 0, f"`opencode --version` exited {result.returncode}: {output[-800:]}"
    assert PINNED.search(output), f"`opencode --version` answered {output[:300]!r}; pinned {OPENCODE_VERSION!r}"


# Resolves `opencode` on PATH to its file, derives the node_modules it was installed into, and reports
# the platform package of this architecture and whether the binary is that package's binary (same
# file: the postinstall links it in place; a fallback install would leave a file from a deleted
# temporary directory).
PROVENANCE_SCRIPT = r"""
p=$(command -v opencode) || { echo "NOPATH"; exit 0; }
real=$(readlink -f "$p"); echo "REAL $real"
nm=${real%/opencode-ai/bin/*}
[ "$nm" != "$real" ] || { echo "NOTWRAPPER"; exit 0; }
arch=$(node -p process.arch); echo "ARCH $arch"
for d in "$nm"/opencode-linux-*; do [ -d "$d" ] && echo "INSTALLED ${d##*/} $(node -p "require('$d/package.json').version")"; done
[ -d "$nm/opencode-linux-$arch" ] && echo "PLATFORM opencode-linux-$arch"
echo "WRAPPER $(node -p "require('$nm/opencode-ai/package.json').version")"
for s in "$arch" "$arch-baseline"; do
  b="$nm/opencode-linux-$s/bin/opencode"
  [ -f "$b" ] && [ "$real" -ef "$b" ] && echo "SAMEFILE opencode-linux-$s"
done
echo DONE
"""


def test_binary_comes_from_the_lockfiles_platform_package():
    """GREEN-IF `opencode` on PATH resolves into an `opencode-ai` install whose node_modules holds
    `opencode-linux-<arch>` at the pin, and the binary is the same file as that platform package's
    (or, on x64, its `-baseline` variant's) binary."""
    image = _opencode_image()
    result = sh(image, PROVENANCE_SCRIPT, timeout=180)
    lines = result.stdout.splitlines()
    assert "DONE" in lines, f"probe did not finish: exit {result.returncode}: {result.stdout[-500:]} {result.stderr[-500:]}"
    facts = dict(line.split(" ", 1) for line in lines if " " in line)
    arch = facts.get("ARCH")
    assert facts.get("PLATFORM") == f"opencode-linux-{arch}", f"no opencode-linux-{arch} package installed:\n{result.stdout}"
    assert facts.get("WRAPPER") == OPENCODE_VERSION, result.stdout
    installed = [line.split(" ")[1:] for line in lines if line.startswith("INSTALLED ")]
    assert installed and all(version == OPENCODE_VERSION for _name, version in installed), installed
    same = [line.split(" ", 1)[1] for line in lines if line.startswith("SAMEFILE ")]
    assert same, f"the binary on PATH is not a lockfile platform package's binary:\n{result.stdout}"


def test_license_is_at_the_documented_path_with_the_recorded_sha256():
    image = _opencode_image()
    result = sh(image, 'test -f "$1" && test ! -L "$1" && sha256sum "$1"', OPENCODE_LICENSE_PATH, timeout=120)
    assert result.returncode == 0, f"{OPENCODE_LICENSE_PATH}: exit {result.returncode}: {result.stderr.strip()[:300]}"
    assert result.stdout.split()[0] == OPENCODE_LICENSE_SHA256, result.stdout


def test_build_leaves_nothing_root_owned_in_tmp_or_state():
    """The build ran npm and the binary as root; nothing it wrote may remain where the image's user
    writes at run time (a root-owned directory there would refuse that user)."""
    image = _opencode_image()
    result = sh(image, 'find /tmp /state -mindepth 1 -user 0 2>/dev/null; echo SCAN-DONE', user="0:0", timeout=120)
    lines = result.stdout.splitlines()
    assert "SCAN-DONE" in lines, f"scan did not finish: exit {result.returncode}: {result.stderr[-300:]}"
    left = [line for line in lines if line != "SCAN-DONE"]
    assert not left, "the build left root-owned files where the image's user writes:\n" + "\n".join(left)


def test_labels_state_the_pin_and_auto_update_is_off():
    image = _opencode_image()
    labels = image_labels(image)
    assert labels.get(OPENCODE_VERSION_LABEL) == OPENCODE_VERSION, labels
    assert labels.get(OCI_VERSION_LABEL) == OPENCODE_VERSION, labels
    assert image_env(image).get("OPENCODE_DISABLE_AUTOUPDATE") == "1", image_env(image)


AGENT_CLI_SCRIPT = r"""
command -v kp-agent-tooling >/dev/null 2>&1 && echo "CONTROL kp-agent-tooling"
for n in claude codex opencode; do p=$(command -v "$n" 2>/dev/null) && echo "ONPATH $n $p"; done
find / -xdev \( -type f -o -type l \) \( -name claude -o -name codex -o -name opencode -o -name opencode.exe \) \
  -perm /111 2>/dev/null | sed 's/^/FOUND /'
find / -xdev -type d \( -name opencode-ai -o -name 'opencode-linux-*' -o -name claude-code -o -name codex \) \
  -path '*/node_modules/*' 2>/dev/null | sed 's/^/PACKAGE /'
echo SCAN-DONE
"""


def _scan(image: str) -> list[str]:
    result = sh(image, AGENT_CLI_SCRIPT, user="0:0", timeout=900)
    lines = result.stdout.splitlines()
    assert "SCAN-DONE" in lines, f"scan did not finish: exit {result.returncode}: {result.stderr[-500:]}"
    assert "CONTROL kp-agent-tooling" in lines, \
        f"positive control failed: {image} does not carry kp-agent-tooling, so an absence would prove nothing"
    return [line for line in lines if line not in ("SCAN-DONE", "CONTROL kp-agent-tooling")]


def test_opencode_image_carries_opencode_and_no_claude_or_codex():
    found = _scan(_opencode_image())
    assert any(line.startswith("ONPATH opencode ") for line in found), \
        "positive control failed: opencode is not on PATH in the opencode image:\n" + "\n".join(found)
    other = [line for line in found if re.search(r"(?:^|[ /])(?:claude|codex|claude-code)$", line)
             or line.startswith(("ONPATH claude", "ONPATH codex"))]
    assert not other, "the opencode image carries another agent CLI:\n" + "\n".join(other)


@pytest.mark.parametrize("variable", [PRODUCT, RUNTIME, OPS], ids=["product", "runtime", "ops"])
def test_default_image_carries_no_opencode(variable):
    found = _scan(required(variable))
    opencode = [line for line in found if "opencode" in line]
    assert not opencode, f"{variable} carries opencode:\n" + "\n".join(opencode)


def test_opencode_image_carries_no_claude_agent_sdk():
    """GREEN-IF `python tests/image/agent_sdk_absence.py --expect-board IMAGE` exits 0 (D0f's check, every
    instrument control held); 1 is a finding and 2 is "cannot state a result", both red."""
    image = _opencode_image()
    argv = [sys.executable, str(SDK_CHECK), "--expect-board", image]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        pytest.fail(f"`{' '.join(argv)}` did not finish within 3600 s", pytrace=False)
    output = (result.stdout + result.stderr).strip()
    assert result.returncode != 2, f"the check could not state a result for {image}:\n{output}"
    assert result.returncode == 0, f"{image} carries claude-agent-sdk:\n{output}"


def test_pin_names_the_installed_package():
    """The constant's package name is the one the image installed (guards a rename of the npm package)."""
    image = _opencode_image()
    result = sh(image, 'readlink -f "$(command -v opencode)"', timeout=120)
    assert f"/node_modules/{OPENCODE_PACKAGE}/" in result.stdout, result.stdout
