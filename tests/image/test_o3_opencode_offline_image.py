"""O3: the OpenCode binary does not come from the network at run time.

Falsifier added by the Coordinator, 2026-10-06: `docker run --rm --network none <target image> opencode
--version` prints the pinned version. A target whose `opencode` fetches its binary on first run (for
example an npm wrapper whose platform package was not installed at build time) fails it, while the
version check with a network attached (test_o3_opencode_image.py) can still pass.

How it runs. Through the image's own entry (`tooling-container`, not bypassed), as the image's default
user, with `--rm --network none`, as the Coordinator's command states. That entry execs any command only
after its state check, which needs a /state carrying the state-volume marker and a /config/navigation.json
(measured at the order's base on the product image: without them it exits 1 with "Required state volume
marker absent", then "Required navigation configuration absent", for any command). So the run adds exactly
those two mounts, prepared as the S2 entry tests prepare them (`prepare_state`, `navigation_config`), and
nothing else. Seams: tests/image/o3_seams.py.

RED at the order's base: there is no OpenCode target.
"""
from __future__ import annotations

import pytest

from image_harness import entrypoint_names, navigation_config, prepare_state, required, run_once
from o3_seams import OPENCODE_IMAGE, pinned_version, prints_version

pytestmark = pytest.mark.image


def test_opencode_answers_its_pinned_version_with_no_network(tmp_path):
    """GREEN-IF the image's entry is `tooling-container` (so the run is the image's own), and
    `docker run --rm --network none IMAGE opencode --version`, with only the /state and /config mounts that
    entry requires, exits 0 and prints the pinned version as a whole token."""
    image = required(OPENCODE_IMAGE)
    pinned = pinned_version()
    names = entrypoint_names(image)
    assert "tooling-container" in names, f"{image} ENTRYPOINT is {names}; the target's entry is tooling-container"
    state = prepare_state(tmp_path / "state", navigation=True)
    config = navigation_config(tmp_path / "config")
    run_args = ["-v", f"{state.resolve()}:/state", "-v", f"{config.resolve()}:/config:ro"]
    result = run_once(image, ["opencode", "--version"], run_args=run_args, network="none", timeout=180)
    output = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"`docker run --rm --network none {image} opencode --version` exited {result.returncode}: {output[-800:]}")
    assert prints_version(output, pinned), (
        f"with no network, `opencode --version` answered {output.strip()[:300]!r}; the pinned version is {pinned!r}")
