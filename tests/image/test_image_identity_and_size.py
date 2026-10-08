"""S2 P4 (revision identity), P5 (size budget) and the slim `runtime` surface."""
from __future__ import annotations

import pytest

from image_harness import (
    REFERENCE_BYTES, REVISION, REVISION_LABEL, image_for, image_labels, reference_bytes, required, sh,
    unpacked_size, unresolved,
)

pytestmark = pytest.mark.image

BUDGET_BYTES = 150_000_000  # "no more than 150 MB larger", decimal like Docker's 2.33 GB

RUNTIME_ABSENCE_SCRIPT = r"""
python -c 'import kp_agent_tooling' >/dev/null 2>&1 && echo "CONTROL kp_agent_tooling"
case "$1" in
  torch)
    python -c 'import importlib.util as u, sys; sys.exit(0 if u.find_spec("torch") is None else 1)' || echo "IMPORTABLE torch"
    find / -xdev \( -type d \( -path '*-packages/torch' \) -o -name 'torch-*.dist-info' \) -print 2>/dev/null | sed 's/^/FOUND /' ;;
  node)
    for n in node nodejs; do p=$(command -v "$n" 2>/dev/null) && echo "ONPATH $n $p"; done
    find / -xdev \( -type f -o -type l \) \( -name node -o -name nodejs \) -perm /111 2>/dev/null | sed 's/^/FOUND /' ;;
  serena)
    python -c 'import importlib.util as u, sys; sys.exit(0 if u.find_spec("serena") is None else 1)' || echo "IMPORTABLE serena"
    p=$(command -v serena 2>/dev/null) && echo "ONPATH serena $p"
    find / -xdev \( -path '*-packages/serena' -o -name 'serena_agent-*.dist-info' -o -path /opt/serena \) -print 2>/dev/null \
      | sed 's/^/FOUND /' ;;
esac
echo SCAN-DONE
"""


@pytest.mark.parametrize("target", ["product", "runtime", "agents"])
def test_revision_label_equals_the_build_arg(target):
    expected = required(REVISION)
    assert expected != "unknown", f"{REVISION} must be the real SOURCE_REVISION, not 'unknown'"
    labels = image_labels(image_for(target))
    assert REVISION_LABEL in labels, f"{target} has no {REVISION_LABEL} label: {labels}"
    assert labels[REVISION_LABEL] == expected, f"{target} {REVISION_LABEL}={labels[REVISION_LABEL]!r}, build arg {expected!r}"


def test_product_is_within_150_mb_of_the_full_reference():
    image = image_for("product")
    control = unresolved(image, ["kp-agent-tooling", "kanban"])
    assert not control, f"positive control failed: the image is not a product image, so its size proves nothing: {control}"
    size, instrument = unpacked_size(image)
    reference = reference_bytes()
    assert size <= reference + BUDGET_BYTES, (
        f"product is {size} bytes ({instrument}); reference full is {reference} ({REFERENCE_BYTES}); "
        f"excess {size - reference} > {BUDGET_BYTES}"
    )


@pytest.mark.parametrize("absent", ["torch", "node", "serena"])
def test_runtime_is_slim(absent):
    result = sh(image_for("runtime"), RUNTIME_ABSENCE_SCRIPT, absent, user="0:0", timeout=900)
    lines = result.stdout.splitlines()
    assert "SCAN-DONE" in lines, f"scan did not finish: exit {result.returncode}: {result.stderr[-500:]}"
    assert "CONTROL kp_agent_tooling" in lines, \
        "positive control failed: runtime's python does not import kp_agent_tooling, so an absence would prove nothing"
    found = [line for line in lines if line not in ("SCAN-DONE", "CONTROL kp_agent_tooling")]
    assert not found, f"runtime contains {absent}:\n" + "\n".join(found[:40])
