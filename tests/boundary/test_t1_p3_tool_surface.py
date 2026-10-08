"""P3 — the tool surface is partitioned (docs/work/orders/T1-extension-seam.md).

"With the core alone, tools/list over stdio MCP (kp-agent-tooling --config ... serve)
returns exactly the core tools the config enables, and never loads an extension module
while listing. With the extension installed and the same config, the names and input
schemas of all tools the config enables equal what the order's merge-base tree, which
is the pre-move code, returns for that config."
Falsifier: any extra or missing tool, a schema difference, or an extension import during
a core-only listing.

Expected surfaces come only from fixtures/pre_move_tools_list.json, captured from the
merge-base tree by `python tests/boundary/t1_harness.py capture` (its configurations are
frozen there too, so "the same config" is literally the same). The core/extension split
of tool names is the order's: see t1_contract.is_extension_tool_name.

Core-alone configurations: core-implicit (no enabled_tools; local knowledge), core-explicit
(enabled_tools = every core tool) and image-example (the merge-base deploy example, which
enables verification.guide). The with-extension comparison runs every frozen configuration.
"""
from __future__ import annotations

import json

import pytest

from t1_contract import is_extension_module, is_extension_tool_name, pre_move_tools
from t1_harness import CONFIG_NAMES

CORE_ALONE_CONFIGS = ("core-implicit", "core-explicit", "image-example")


def describe(listing) -> str:
    return (f"server argv: {listing.argv}\nlisting error: {listing.error}\n"
            f"server stderr:\n{listing.stderr_tail}")


def duplicates(names):
    return sorted({name for name in names if names.count(name) > 1})


@pytest.mark.parametrize("config_name", CORE_ALONE_CONFIGS)
def test_p3_core_alone_lists_exactly_the_core_tools_the_config_enables(config_name, core_listings):
    listing = core_listings(config_name)
    assert listing.error is None, "tools/list did not answer with the core alone\n" + describe(listing)
    names = listing.names()
    expected = {tool["name"] for tool in pre_move_tools(config_name)
                if not is_extension_tool_name(tool["name"])}
    assert expected, f"the merge-base surface for {config_name} enables no core tool"
    extra, missing = sorted(set(names) - expected), sorted(expected - set(names))
    assert not (duplicates(names) or extra or missing), (
        f"core-alone tools/list for {config_name}: extra={extra} missing={missing} "
        f"duplicated={duplicates(names)} (expected exactly {len(expected)} core tools)")


def test_p3_core_alone_listing_loads_no_extension_module(core_listings):
    offenders = {}
    for config_name in CORE_ALONE_CONFIGS:
        listing = core_listings(config_name)
        # The instrument must have observed the server's own imports, or zero proves nothing.
        assert "kp_agent_tooling.cli" in listing.loaded_modules, (
            f"the import audit for {config_name} did not record kp_agent_tooling.cli; "
            "the scratch venv's import recorder did not run\n" + describe(listing))
        loaded = sorted({name for name in listing.loaded_modules if is_extension_module(name)})
        if loaded:
            offenders[config_name] = loaded
    assert not offenders, ("the core-alone serve process loaded extension (kp_agent_tooling_ops or "
                           "MOVE) modules while answering tools/list:\n"
                           + json.dumps(offenders, indent=1))


@pytest.mark.parametrize("config_name", CONFIG_NAMES)
def test_p3_with_extension_names_and_input_schemas_equal_the_merge_base(config_name, extension_listings):
    listing = extension_listings(config_name)
    assert listing.error is None, "tools/list did not answer with the extension installed\n" + describe(listing)
    names = listing.names()
    expected = {tool["name"]: tool["inputSchema"] for tool in pre_move_tools(config_name)}
    actual = {tool["name"]: tool.get("inputSchema") for tool in listing.tools}
    extra, missing = sorted(set(actual) - set(expected)), sorted(set(expected) - set(actual))
    differing = sorted(name for name in set(actual) & set(expected) if actual[name] != expected[name])
    detail = "".join(f"\n--- {name}\nmerge base: {json.dumps(expected[name], sort_keys=True)}"
                     f"\nobserved:   {json.dumps(actual[name], sort_keys=True)}" for name in differing[:3])
    assert not (duplicates(names) or extra or missing or differing), (
        f"with-extension tools/list for {config_name} differs from the merge base: extra={extra} "
        f"missing={missing} duplicated={duplicates(names)} schema_differs={differing}" + detail)
