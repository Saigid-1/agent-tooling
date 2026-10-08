"""S1a P3 and falsifier (c): the summarizer role is optional and never selected unless asked.

Order: docs/work/orders/S1a-scheduled-summarizer.md, P3 ("A new component `summarizer`",
"Egress", "Mounts") and falsifier (c):
- O3's A1 pin passes unedited (tests/test_o3_a1_default_plan_pin.py, run as it is);
- a plan with `--components tooling,summarizer` renders the service and adds `summarizer` to
  `COMPOSE_PROFILES`;
- a rendered-compose test shows the summarizer is the only added service;
- `capture` AND `indexer` keep `network_mode: none` (the indexer half is new).
The rendered configuration comes from `docker compose config` when the docker CLI exists,
otherwise from the labelled YAML parse of tests/install/s3_harness.py (no container starts).
"""
from __future__ import annotations

import json

import pytest

from s3_harness import ALL_COMPONENTS, World, binds, read_dotenv, rendered_config

# The services of the manifest before S1a (T12b's set: tooling roles plus telemetry's two).
BEFORE = {"tooling", "refresh", "capture", "board", "indexer", "tempo", "collector"}
KEY_TARGET = "/config/summarizer.key"


@pytest.fixture(scope="module")
def selected(tmp_path_factory, installer):
    world = World.create(tmp_path_factory.mktemp("s1a"), components=("tooling", "summarizer"))
    proc, plan = world.plan()
    assert proc.returncode == 0 and isinstance(plan, dict), proc.stdout + proc.stderr
    world.apply_ok()
    config, instrument = rendered_config(world)
    return world, plan, config.get("services") or {}, instrument


@pytest.fixture(scope="module")
def everything(tmp_path_factory, installer):
    world = World.create(tmp_path_factory.mktemp("s1a-all"), components=(*ALL_COMPONENTS, "summarizer"))
    world.apply_ok()
    config, instrument = rendered_config(world)
    return world, config.get("services") or {}, instrument


def test_plan_selects_the_role_only_when_asked(selected):
    world, plan, _, _ = selected
    assert plan["inputs"]["components"] == ["tooling", "summarizer"]
    assert plan["compose"]["profiles"] == ["summarizer"]
    env = read_dotenv(world.root / ".env")
    assert env["COMPOSE_PROFILES"] == "summarizer"
    rows = {name: row for name, row in plan["operator_files"].items() if row["component"] == "summarizer"}
    assert {name: row["use"] for name, row in rows.items()} == {
        "config/summarizer/approval.json": "required",
        "config/summarizer/gateway.json": "required",
        "config/summarizer/summarizer.env": "optional",
        "secrets/summarizer.key": "placeholder",
    }, rows


def test_default_plan_renders_no_summarizer_profile(tmp_path, installer):
    world = World.create(tmp_path, components=("tooling",))
    proc, plan = world.plan()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert plan["compose"]["profiles"] == [] and "summarizer" not in json.dumps(plan["operator_files"])


def test_summarizer_is_the_only_added_service(everything):
    _, services, instrument = everything
    assert set(services) - BEFORE == {"summarizer"}, f"{instrument}: {sorted(services)}"
    assert BEFORE <= set(services), f"{instrument}: {sorted(services)}"


def test_capture_and_indexer_keep_no_network(everything):
    _, services, instrument = everything
    for role in ("capture", "indexer"):
        assert services[role].get("network_mode") == "none", f"{instrument}: {role} {services[role].get('network_mode')}"


def test_summarizer_has_egress_its_profile_and_command(everything):
    _, services, instrument = everything
    summarizer = services["summarizer"]
    assert summarizer.get("network_mode") in (None, "default"), f"{instrument}: {summarizer.get('network_mode')}"
    assert summarizer.get("profiles") == ["summarizer"], summarizer.get("profiles")
    command = summarizer.get("command") or []
    assert command[0] == "kp-agent-summarizer" and "--watch" in command, command
    assert summarizer.get("restart") == "unless-stopped" and summarizer.get("read_only") is True


def test_summarizer_mounts_only_state_operator_files_and_the_key(everything):
    world, services, instrument = everything
    summarizer = services["summarizer"]
    mounts = {(m.get("type"), m.get("target")): m for m in summarizer.get("volumes") or []}
    assert set(mounts) == {("bind", "/state"), ("volume", "/state/memory"), ("bind", "/config"),
                           ("bind", KEY_TARGET)}, f"{instrument}: {sorted(mounts)}"
    assert mounts[("bind", "/state")].get("read_only") is not True
    assert mounts[("bind", "/config")].get("read_only") is True
    key = mounts[("bind", KEY_TARGET)]
    assert key.get("read_only") is True
    assert key["source"].endswith("/secrets/summarizer.key")
    others = [name for name, svc in services.items() if name != "summarizer"
              and any("summarizer.key" in str(b.get("source", "")) for b in binds(svc))]
    assert not others, f"{instrument}: the summarizer key is bound into {others}"


def test_apply_renders_the_key_mount_point_and_placeholder(selected):
    world, _, _, _ = selected
    assert (world.root / "config" / "summarizer.key").read_bytes() == b""
    assert (world.root / "secrets" / "summarizer.key").stat().st_size == 0
    assert (world.root / "config" / "summarizer").is_dir()
