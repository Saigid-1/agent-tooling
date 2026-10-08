"""T6a P1 — one tree yields both variants (docs/work/orders/T6a-images-and-ci.md).

"In `product`, every core `[project.scripts]` entry point resolves and
`kp_agent_tooling_ops` is not importable. In `ops`, every core and extension entry
point resolves. `kp-agent-tooling … serve` then lists the extension tools when the
config enables them, and the core tools when it doesn't."
Falsifier: extension code in `product`, a missing entry point in either variant, or an
`ops` server that won't list configured extension tools.

Also from the order's interface surface: "`ops` is `product` plus the extension
installed from `extensions/ops`, with its OPS assets and console scripts" and
"`product` and `agents` contain no `kp_agent_tooling_ops`".

With the core alone, a configured extension tool is not refused: the server serves the
core subset and reports the gap through `tooling.identity` as `unavailable_tools`
(the T1 ruling). The product image is held to that with the same configuration.

An entry point resolves when, in the image, (for a script) an executable of that name is
on PATH with a usable `#!` interpreter, and the entry point is installed under its
declared group and name with the declared value, and loading it succeeds in the
interpreter that runs it.
"""
from __future__ import annotations

import json

import pytest

from t6a_variants import (
    CORE_TOOLS, EXTENSION_TOOLS, core_entry_points, extension_asset_digests, extension_assets,
    extension_entry_points, extension_files, extension_presence, image_for, serve,
    unresolved_entry_points,
)

pytestmark = pytest.mark.image

PROVIDER_NOT_INSTALLED = "provider_not_installed"


def duplicates(names):
    return sorted({name for name in names if names.count(name) > 1})


def exactly(session, expected) -> None:
    names = session.names()
    extra, missing = sorted(set(names) - set(expected)), sorted(set(expected) - set(names))
    assert not (extra or missing or duplicates(names)), (
        f"tools/list with the {session.config_name} configuration: extra={extra} missing={missing} "
        f"duplicated={duplicates(names)}; expected exactly {sorted(expected)}\n" + session.describe())


# ------------------------------------------------------------------ product


def test_product_resolves_every_core_entry_point():
    image = image_for("product")
    missing = unresolved_entry_points(image, core_entry_points())
    assert not missing, ("core entry points (packages/tooling/pyproject.toml) that do not resolve in "
                         "product:\n" + "\n".join(missing))


@pytest.mark.parametrize("variant", ["product", "agents"])
def test_variant_cannot_import_the_extension(variant):
    image = image_for(variant)
    report = extension_presence(image)
    assert report, f"no Python interpreter was found to observe in {variant} ({image})"
    broken = {interpreter: seen for interpreter, seen in report.items() if "probe_error" in seen}
    assert not broken, f"the import probe failed in {variant}: {json.dumps(broken, indent=1)}"
    # Absence only counts when observed in the variant's own Python (the one with the core).
    assert any(seen.get("core_importable") for seen in report.values()), (
        f"no interpreter in {variant} ({image}) imports kp_agent_tooling, so the extension's absence "
        f"was not observed where the product runs: {json.dumps(report, indent=1)}")
    present = {interpreter: seen for interpreter, seen in report.items()
               if seen.get("importable") or seen.get("distribution") or seen.get("entry_points")}
    assert not present, (f"kp_agent_tooling_ops is importable or installed in {variant} ({image}):\n"
                         + json.dumps(present, indent=1))


@pytest.mark.parametrize("variant", ["product", "agents"])
def test_variant_contains_no_extension_files(variant):
    image = image_for(variant)
    found, core = extension_files(image)
    assert core, (f"the file search in {variant} ({image}) found no kp_agent_tooling package directory, "
                  "so it did not reach the installed packages; an empty result proves nothing")
    assert not found, (f"{variant} ({image}) contains extension files or extension console scripts:\n"
                       + "\n".join(found))


def test_product_serve_lists_only_the_core_subset_of_an_extension_config():
    session = serve(image_for("product"), "extension")
    exactly(session, CORE_TOOLS)


def test_product_identity_reports_the_configured_extension_tools_as_unavailable():
    session = serve(image_for("product"), "extension")
    identity = session.result("tooling.identity")
    gap = identity.get("unavailable_tools")
    assert isinstance(gap, list) and all(isinstance(row, dict) for row in gap), (
        "tooling.identity in product reports no unavailable_tools list for a configuration that enables "
        f"{list(EXTENSION_TOOLS)}: {json.dumps(identity)[:2000]}\n" + session.describe())
    names = [row.get("name") for row in gap]
    assert sorted(names) == sorted(EXTENSION_TOOLS), (
        f"unavailable_tools names {names}, expected exactly {sorted(EXTENSION_TOOLS)}")
    wrong = [row for row in gap if row.get("reason") != PROVIDER_NOT_INSTALLED]
    assert not wrong, f"unavailable_tools rows whose reason is not {PROVIDER_NOT_INSTALLED!r}: {wrong}"


# ---------------------------------------------------------------------- ops


def test_ops_resolves_every_core_and_extension_entry_point():
    image = image_for("ops")
    declared = core_entry_points() + extension_entry_points()
    missing = unresolved_entry_points(image, declared)
    assert not missing, ("core and extension entry points (packages/tooling/pyproject.toml, "
                         "extensions/ops/pyproject.toml) that do not resolve in ops:\n" + "\n".join(missing))


def test_ops_serve_lists_the_configured_extension_tools():
    session = serve(image_for("ops"), "extension")
    exactly(session, (*CORE_TOOLS, *EXTENSION_TOOLS))


def test_ops_serve_lists_only_core_tools_when_the_config_enables_no_extension_tool():
    session = serve(image_for("ops"), "core")
    exactly(session, CORE_TOOLS)


def test_ops_carries_the_extension_assets():
    image = image_for("ops")
    expected = extension_assets()
    report = extension_asset_digests(image, sorted(expected))
    assert not report.get("error") and isinstance(report.get("assets"), dict), (
        f"could not read the extension's package data in ops ({image}): {json.dumps(report)}")
    differing = {relative: {"tree": digest, "image": report["assets"].get(relative)}
                 for relative, digest in expected.items() if report["assets"].get(relative) != digest}
    assert not differing, ("OPS assets in ops differ from extensions/ops (sha256), read through "
                           f"importlib.resources in {report.get('serving')}:\n" + json.dumps(differing, indent=1))
