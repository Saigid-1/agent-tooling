"""D0b R4 (image-marked): every published target's labels, read from the image itself.

Order: docs/work/orders/D0b-release-workflow.md, R4 and its falsifiers ("a revision label that is not the
public tag's commit; a source label that is not the public URL; no licences label; a build-identity revision
differing from the label"). Run at R6 step 2 on the pushed digests, these read what was published; the
images are named by the image-suite variables (runtime, product, ops, opencode) and the build argument by
AGENT_TOOLING_TEST_SOURCE_REVISION (the tag's commit in the release workflow). A test FAILS, never skips,
on an unset variable.
"""
from __future__ import annotations

import json

import pytest

from d0b_seams import IMAGE_VARIABLES, LICENCES, PUBLIC_SOURCE_URL, PUBLISHED_TARGETS, SOURCE_REVISION_VARIABLE
from image_harness import image_labels, required, sh

pytestmark = pytest.mark.image

IDENTITY_PATH = "/usr/local/share/agent-tooling-build-identity.json"
SOURCE_LABEL = "org.opencontainers.image.source"


def _shown(key: str, value):
    """A wrong label as a failure may show it: a source that is not the public URL is described, never quoted, so
    no failure message names another repository owner."""
    if key != SOURCE_LABEL or value is None:
        return value
    return f"not the public URL (a value of {len(value)} characters)"


@pytest.mark.parametrize("target", PUBLISHED_TARGETS)
def test_r4_published_image_labels_its_public_source_licences_and_revision(target):
    """GREEN-IF the target's image carries org.opencontainers.image.source = https://github.com/Saigid-1/agent-tooling,
    org.opencontainers.image.licenses = AGPL-3.0-only (runtime, product, ops) or AGPL-3.0-only AND MIT (opencode),
    and org.opencontainers.image.revision = AGENT_TOOLING_TEST_SOURCE_REVISION (a real revision, not `unknown`); and
    its build-identity file declares that same revision."""
    image = required(IMAGE_VARIABLES[target])
    revision = required(SOURCE_REVISION_VARIABLE)
    assert revision != "unknown", f"{SOURCE_REVISION_VARIABLE} must be the real build argument, not 'unknown'"
    labels = image_labels(image)
    expected = {
        "org.opencontainers.image.source": PUBLIC_SOURCE_URL,
        "org.opencontainers.image.licenses": LICENCES[target],
        "org.opencontainers.image.revision": revision,
    }
    wrong = {key: _shown(key, labels.get(key)) for key, value in expected.items() if labels.get(key) != value}
    assert not wrong, f"{target} ({image}) labels differ from R4: {wrong} (expected {expected})"
    result = sh(image, f"cat {IDENTITY_PATH}", timeout=120)
    assert result.returncode == 0, f"{IDENTITY_PATH} unreadable in {image}: {result.stderr.strip()[:300]}"
    declared = json.loads(result.stdout).get("source_revision")
    assert declared == revision, f"{target}: the build-identity file declares {declared!r}, the label {revision!r}"
