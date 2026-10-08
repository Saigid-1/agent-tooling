"""The OpenCode CLI pin (order O3, Q4): the one version O1's and O2's tests run against.

Import it, never restate it:

    from opencode_pin import OPENCODE_VERSION          # a test module in tests/image
    from image.opencode_pin import OPENCODE_VERSION    # a test module elsewhere under tests/

The `opencode` target of deploy/Dockerfile (`ARG OPENCODE_VERSION`), deploy/image/opencode/package.json
and its lockfile carry the same value; test_o3_opencode_pin.py holds them equal to this constant. A built
`opencode` image states it in the labels below, and `opencode --version` answers it
(test_o3_opencode_image.py).
"""
from __future__ import annotations

OPENCODE_PACKAGE = "opencode-ai"
OPENCODE_VERSION = "1.18.34"

# The optional image target that carries the CLI, and the variable naming a built one for the image suite.
OPENCODE_TARGET = "opencode"
OPENCODE_IMAGE_VARIABLE = "AGENT_TOOLING_TEST_IMAGE_OPENCODE"

# Labels of the `opencode` target that state the pin.
OPENCODE_VERSION_LABEL = "agent-tooling.opencode.version"
OCI_VERSION_LABEL = "org.opencontainers.image.version"

# The upstream LICENSE at tag v1.18.34 (https://github.com/anomalyco/opencode, redirected from
# sst/opencode), committed byte for byte as deploy/image/opencode/LICENSE and carried in the image here.
OPENCODE_LICENSE_PATH = "/usr/local/share/doc/opencode/LICENSE"
OPENCODE_LICENSE_SHA256 = "625f0f619133f89bbbb2abe37369613dfa1885eba1e50d02170deb62bb42cb6b"
OPENCODE_LICENSE_BYTES = 1065
