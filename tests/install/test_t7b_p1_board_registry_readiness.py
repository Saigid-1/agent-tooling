"""T7b P1 (non-container part): `verify` readiness names the board's registry config.

Order: docs/work/orders/T7b-board-wiring.md, P1:
"`kp-agent-install verify` readiness names `config/launch/registry.json` for `board`."
The container half (the Desks menu over the board's HTTP interface, with and without
the registry) is in test_t7b_p1_desks_menu_image.py (image-marked).

Readings (repeated in the arm report under AMBIGUITY):
- "names ... for board": while `$root/config/launch/registry.json` is absent, the
  `board` entry of `verify`'s `readiness` reports it absent, root-relative: it appears in
  a list under a key containing "missing", or in an object of that entry that carries
  `not_configured` (tests/install/t7a_harness.py, `names_root_relative`);
- once the operator has written it (non-empty, 0600), no part of the `board` entry
  reports it absent any longer;
- the board is not required to be `not_configured` as a whole (the order calls the
  registry the Desks menu's configuration, and the board keeps serving without it), and
  `verify`'s exit status is not asserted beyond "no drift" (DOCKER.md: readiness does not
  fail verify).
"""
from __future__ import annotations

import json

import pytest

from s3_harness import explain
from t7a_harness import NOT_CONFIGURED, names_root_relative, write_private
from t7b_harness import REGISTRY_RELATIVE, registry_document


def _verify(world) -> tuple[object, dict]:
    proc = world.run("verify", ["--runtime-root", str(world.root)])
    try:
        document = json.loads(proc.stdout)
    except ValueError:
        pytest.fail("verify did not print a JSON document\n" + explain(proc), pytrace=False)
    return proc, document


def _board_entry(document: dict, proc) -> object:
    readiness = document.get("readiness")
    assert isinstance(readiness, dict) and "board" in readiness, (
        "verify has no readiness entry for the selected `board` component\n" + explain(proc))
    return readiness["board"]


def reported_absent(entry, relative: str) -> list[str]:
    """Every place in a readiness entry that reports `relative` as absent."""
    found = []

    def walk(value, trail):
        if isinstance(value, dict):
            own = {k: v for k, v in value.items() if not isinstance(v, dict)}
            if any(v == NOT_CONFIGURED for v in own.values()) and names_root_relative(json.dumps(own), relative):
                found.append("/".join(trail) or "<entry>")
            for key, child in value.items():
                if "missing" in key.lower() and names_root_relative(json.dumps(child), relative):
                    found.append("/".join([*trail, key]))
                walk(child, [*trail, key])
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, [*trail, str(index)])

    walk(entry, [])
    return found


@pytest.mark.parametrize("components", [("tooling", "board"), ("tooling", "capture", "board")],
                         ids=["tooling,board", "tooling,capture,board"])
def test_verify_names_the_board_registry_config_until_it_is_written(world, components):
    """GREEN-IF the board's readiness entry reports `config/launch/registry.json` absent (root-relative)
    while it is absent, and no longer once the operator has written it."""
    world.apply_ok(world.args(components=components))
    proc, document = _verify(world)
    assert document.get("status") != "drift" and not document.get("drift"), (
        "a fresh installation must not report drift\n" + explain(proc))
    entry = _board_entry(document, proc)
    assert not (world.root / REGISTRY_RELATIVE).exists(), "precondition: the registry config is absent"
    places = reported_absent(entry, REGISTRY_RELATIVE)
    assert places, (f"verify's `board` readiness does not name {REGISTRY_RELATIVE} as absent: "
                    f"{json.dumps(entry, sort_keys=True)}")

    write_private(world.root / REGISTRY_RELATIVE, json.dumps(registry_document()) + "\n")
    proc, document = _verify(world)
    entry = _board_entry(document, proc)
    stale = reported_absent(entry, REGISTRY_RELATIVE)
    assert not stale, (f"after the operator wrote {REGISTRY_RELATIVE}, verify's `board` readiness still "
                       f"reports it absent at {stale}: {json.dumps(entry, sort_keys=True)}")
    drifted = [row for row in document.get("drift") or [] if row.get("path") == REGISTRY_RELATIVE]
    assert not drifted, f"the operator's registry config is reported as installer drift: {drifted}"
