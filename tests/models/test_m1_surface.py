"""M1 interface surface (docs/work/orders/M1-model-gateway.md, "Interface surface").

* Console script `kp-agent-models` (core package), `--config <path> serve` is a stdio MCP
  server.
* One tool per configured capability, named `model.<capability>`; unconfigured
  capabilities are not listed.
* `doctor` reports each route's status and the budget state, and never makes a paid
  call unless `--live` is given.
* Inputs are bounded.

Reading notes (see m1_harness for the shared ones): only tools named `model.*` are
compared with the routes, so the order is not read as forbidding other tools; doctor's
output format is unspecified, so it is read as text (stdout + stderr) by value; a
healthy configuration is expected to make doctor exit 0. Every POST to a provider stub
counts as a paid call; GETs (model metadata) do not.
"""
from __future__ import annotations

import json
import subprocess
import sys

from m1_harness import SCRIPT, generous_budget, provider, require_gateway, tool_names

OA = "openai-compatible"
OR = "openrouter"


def model_tools(names):
    return sorted(n for n in names if isinstance(n, str) and n.startswith("model."))


PROBE = """
import importlib.metadata, json
try:
    dist = importlib.metadata.distribution("kp-agent-tooling")
except importlib.metadata.PackageNotFoundError:
    print(json.dumps(None))
else:
    print(json.dumps(sorted(ep.name for ep in dist.entry_points if ep.group == "console_scripts")))
"""


def test_console_script_is_declared_by_the_core_distribution(tmp_path):
    require_gateway()
    # Asked of the interpreter outside pytest: pytest's `pythonpath` puts packages/tooling/src
    # (and any egg-info an editable install left there) ahead of the installed distribution.
    proc = subprocess.run([sys.executable, "-I", "-c", PROBE], capture_output=True, text=True, timeout=60,
                          cwd=str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    scripts = json.loads(proc.stdout)
    assert scripts is not None, "the core distribution kp-agent-tooling is not installed in the test interpreter"
    assert SCRIPT in scripts, (f"{SCRIPT} is installed next to the interpreter but is not a console "
                               f"script of the core distribution kp-agent-tooling (it declares {scripts})")


def test_serve_lists_one_model_tool_per_configured_capability(world):
    key = world.key("main")
    text_model, speak_model = f"m1vendor/text-{world.nonce}", f"m1vendor/speak-{world.nonce}"
    stub = world.stub("main", models=[text_model, speak_model])
    world.write_config(routes={"text.complete": {"provider": "prov-main", "model": text_model},
                               "audio.speak": {"provider": "prov-main", "model": speak_model}},
                       providers={"prov-main": provider(stub, key, OA)})
    gateway = world.gateway().start()
    tools = gateway.list_tools()
    names = tool_names(tools)
    assert model_tools(names) == ["model.audio.speak", "model.text.complete"], (
        f"tools/list for routes {{text.complete, audio.speak}} returned {names}; expected exactly one "
        "model.<capability> tool per configured capability and none for unconfigured ones")
    for tool in tools:
        if tool.get("name") in ("model.audio.speak", "model.text.complete"):
            schema = tool.get("inputSchema")
            assert isinstance(schema, dict) and schema.get("type") == "object", (
                f"{tool.get('name')} advertises no object inputSchema: {schema!r}")
    assert stub.settled_post_count() == 0, "listing tools made a paid (POST) request to the provider"


def test_doctor_reports_every_route_without_a_paid_call(world):
    key_or, key_oa = world.key("or", OR), world.key("oa", OA)
    text_model, image_model = f"m1vendor/text-{world.nonce}", f"m1vendor/image-{world.nonce}"
    stub_or = world.stub("or", models=[text_model])
    stub_oa = world.stub("oa", models=[image_model])
    world.write_config(routes={"text.complete": {"provider": "prov-or", "model": text_model},
                               "image.generate": {"provider": "prov-oa", "model": image_model}},
                       providers={"prov-or": provider(stub_or, key_or, OR),
                                  "prov-oa": provider(stub_oa, key_oa, OA)})
    proc = world.doctor()
    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, (f"doctor exited {proc.returncode} for a healthy configuration\n"
                                  f"{output[-3000:]}")
    for capability in ("text.complete", "image.generate"):
        assert capability in output, f"doctor's report does not mention the route {capability}:\n{output[-3000:]}"
    paid = stub_or.settled_post_count() + stub_oa.post_count()
    assert paid == 0, f"doctor without --live made {paid} paid (POST) request(s) to the providers"


def test_doctor_reports_budget_state(world):
    key = world.key("or", OR)
    model = f"m1vendor/text-{world.nonce}"
    stub = world.stub("or", models=[model], cost=0.25)
    world.write_config(routes={"text.complete": {"provider": "prov-or", "model": model}},
                       providers={"prov-or": provider(stub, key, OR)},
                       budgets={"text.complete": {"max_calls_per_hour": 37, "max_usd_per_day": 12.5}})
    gateway = world.gateway().start()
    outcome = gateway.call_capability("text.complete")
    assert not outcome.is_error_shaped(), "precondition: one call must succeed\n" + outcome.describe()
    gateway.stop()
    assert stub.post_count() == 1
    proc = world.doctor()
    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, f"doctor exited {proc.returncode}\n{output[-3000:]}"
    assert "37" in output and "12.5" in output, (
        f"doctor does not report the configured caps (37 calls/hour, 12.5 USD/day):\n{output[-3000:]}")
    assert "0.25" in output or "12.25" in output, (
        "doctor does not report budget state after one call that reported cost 0.25 "
        f"(neither spent 0.25 nor remaining 12.25 appears):\n{output[-3000:]}")
    assert stub.settled_post_count() == 1, "doctor without --live made a paid (POST) request"


def test_oversized_inline_input_is_refused_before_the_network(world):
    key = world.key("main")
    model = f"m1vendor/text-{world.nonce}"
    stub = world.stub("main", models=[model])
    world.write_config(routes={"text.complete": {"provider": "prov-main", "model": model}},
                       providers={"prov-main": provider(stub, key, OA)},
                       budgets={"text.complete": generous_budget()})
    gateway = world.gateway().start()
    control = gateway.call_capability("text.complete")
    assert not control.is_error_shaped(), "precondition: a small call must succeed\n" + control.describe()
    assert stub.post_count() == 1
    huge = "x" * (32 * 1024 * 1024)  # 32 MiB of inline prompt: beyond any model's context
    outcome = gateway.call_capability("text.complete", text=huge, validate=False)
    assert outcome.is_error_shaped(), ("a 32 MiB inline input was accepted (inputs are not bounded)\n"
                                       + outcome.describe()[:1500])
    assert stub.settled_post_count() == 1, "the oversized input reached the provider"
