"""Session fixtures for the T1 boundary tests (docs/work/orders/T1-extension-seam.md).

P3 and P4 observe installed distributions, never this process's imports:

* `core_env`: a scratch venv under TMPDIR with a copy of packages/tooling installed
  (the core alone). It must not see kp-agent-tooling-ops.
* `extension_env`: the same, plus a copy of extensions/ops installed. The order
  expects that distribution, so when extensions/ops/pyproject.toml is absent (or the
  install does not yield kp-agent-tooling-ops) every test that needs it FAILS; nothing
  here skips.

Scratch state lives under pytest's basetemp, which follows TMPDIR.
"""
from __future__ import annotations

import json

import pytest

from t1_contract import (CORE_PROJECT, EXTENSION_DISTRIBUTION, EXTENSION_PROJECT,
                         FAKE_GATEWAY_CATALOG, pre_move_surface)
from t1_harness import (EXTENSION_LIFECYCLE_MODULE, FakeGateway, HarnessError, Unavailable,
                        build_dependency_farm, build_environment, list_tools_over_stdio, make_world,
                        need, presence, provision_portable)


@pytest.fixture(scope="session")
def t1_scratch(tmp_path_factory):
    return tmp_path_factory.mktemp("t1-boundary").resolve()


@pytest.fixture(scope="session")
def dependency_farm(t1_scratch):
    try:
        return build_dependency_farm(t1_scratch / "farm")
    except HarnessError as error:
        return Unavailable(f"dependency farm: {error}")


@pytest.fixture(scope="session")
def core_env(t1_scratch, dependency_farm):
    if isinstance(dependency_farm, Unavailable):
        return dependency_farm
    try:
        environment = build_environment(t1_scratch / "core-only", dependency_farm, [CORE_PROJECT],
                                        with_extension=False)
        found = presence(environment)
    except HarnessError as error:
        return Unavailable(f"core-only scratch venv: {error}")
    if found["extension"] or found["extension_package"]:
        return Unavailable("the core-only scratch venv can see the extension; 'core alone' is not "
                           f"established: {found}")
    return environment


@pytest.fixture(scope="session")
def extension_env(t1_scratch, dependency_farm):
    if not (EXTENSION_PROJECT / "pyproject.toml").is_file():
        return Unavailable(
            "order T1 expects the extension distribution kp-agent-tooling-ops at extensions/ops "
            "(extensions/ops/pyproject.toml); it is absent, so the with-extension surface cannot "
            "be observed")
    if isinstance(dependency_farm, Unavailable):
        return dependency_farm
    try:
        environment = build_environment(t1_scratch / "core-plus-extension", dependency_farm,
                                        [CORE_PROJECT, EXTENSION_PROJECT], with_extension=True)
        found = presence(environment)
    except HarnessError as error:
        return Unavailable(f"core+extension scratch venv: {error}")
    if not (found["core"] and found["extension"] and found["extension_package"]):
        return Unavailable(f"installing extensions/ops did not yield {EXTENSION_DISTRIBUTION} with an "
                           f"importable kp_agent_tooling_ops: {found}")
    return environment


@pytest.fixture(scope="session")
def fake_gateway():
    with FakeGateway(json.loads(FAKE_GATEWAY_CATALOG.read_text())) as gateway:
        yield gateway


def _listings(scratch, environment, label, gateway_url=None):
    cache = {}

    def get(config_name):
        env = need(environment)
        if config_name in cache:
            return cache[config_name]
        surface = pre_move_surface()
        try:
            if "world" not in cache:
                cache["world"] = make_world(scratch / f"{label}-world", surface["aux_templates"],
                                            gateway_url=gateway_url)
            world = cache["world"]
            if config_name == "portable" and "@LIFECYCLE@" not in world.values:
                provision_portable(world, surface["aux_templates"], env, EXTENSION_LIFECYCLE_MODULE)
            config = world.write_config(config_name, surface["configs"][config_name]["template"])
        except HarnessError as error:
            pytest.fail(f"could not provision the {config_name} configuration: {error}", pytrace=False)
        cache[config_name] = list_tools_over_stdio(env, config, label=f"{label}-{config_name}")
        return cache[config_name]

    return get


@pytest.fixture(scope="session")
def core_listings(t1_scratch, core_env):
    """config name -> Listing from `kp-agent-tooling ... serve` with the core alone."""
    return _listings(t1_scratch, core_env, "core-only")


@pytest.fixture(scope="session")
def extension_listings(t1_scratch, extension_env, fake_gateway):
    """config name -> Listing with the core and the extension installed."""
    return _listings(t1_scratch, extension_env, "with-extension", gateway_url=fake_gateway.url)
