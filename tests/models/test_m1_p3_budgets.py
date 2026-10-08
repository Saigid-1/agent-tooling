"""M1 P3 — budgets are enforced before the network (docs/work/orders/M1-model-gateway.md).

"A capped or unconfirmed call makes no request. Counters survive a restart."
Falsifier: a request sent past a cap, or counters reset on restart.
Interface: "A call that would exceed a cap is refused before any network request, with
a structured reason. With confirm_over_usd set, a call estimated above that amount
returns confirmation_required and makes no call; the tool accepts an explicit
confirm: true on retry."

"Request" is a POST to the provider (a paid call); metadata GETs are not counted.
A restart is a stop of the serve process (stdin closed, process exits) and a fresh
start with the same config. Cost reaches the gateway the way OpenRouter reports it
(`usage.cost` in the response, and GET /generation), so the USD tests use an
`openrouter` provider. The order does not say where a cost estimate comes from, so the
confirmation test accepts either source: the stub's /models pricing (refused before
the first call) or the cost the previous call reported (refused before the second).
"""
from __future__ import annotations

from m1_harness import provider

OA = "openai-compatible"
OR = "openrouter"
# Words by which a structured refusal names the budget it hit (substring, case-insensitive).
CALL_CAP_WORDS = ("budget", "limit", "quota", "exceed", "max_calls_per_hour", "per_hour", "per hour")
USD_CAP_WORDS = ("budget", "limit", "quota", "exceed", "max_usd_per_day", "usd", "per_day", "per day",
                 "spend", "spent")


def single_route(world, kind, budget, **stub_options):
    key = world.key("main", kind)
    model = f"m1vendor/text-{world.nonce}"
    stub = world.stub("main", models=[model], **stub_options)
    world.write_config(routes={"text.complete": {"provider": "prov-main", "model": model}},
                       providers={"prov-main": provider(stub, key, kind)},
                       budgets={"text.complete": budget})
    return stub


def assert_succeeds(outcome, label):
    assert not outcome.is_error_shaped(), f"{label} should succeed within budget\n" + outcome.describe()


def assert_structured_refusal(outcome, words, label):
    assert outcome.is_error_shaped(), f"{label}: a call past the cap was not refused\n" + outcome.describe()
    assert outcome.is_structured(), f"{label}: the refusal carries no structured (JSON) reason\n" + outcome.describe()
    lowered = outcome.lowered()
    assert any(word in lowered for word in words), (
        f"{label}: the refusal does not name the budget or cap it hit\n" + outcome.describe())


def test_p3_calls_per_hour_cap_refuses_before_the_network(world):
    stub = single_route(world, OA, {"max_calls_per_hour": 2, "max_usd_per_day": 1000.0})
    gateway = world.gateway().start()
    assert_succeeds(gateway.call_capability("text.complete"), "call 1 of 2")
    assert_succeeds(gateway.call_capability("text.complete"), "call 2 of 2")
    refused = gateway.call_capability("text.complete")
    assert_structured_refusal(refused, CALL_CAP_WORDS, "call 3 with max_calls_per_hour=2")
    assert stub.settled_post_count() == 2, f"the capped call reached the provider ({stub.post_count()} POSTs)"


def test_p3_calls_per_hour_counter_survives_restart(world):
    stub = single_route(world, OA, {"max_calls_per_hour": 2, "max_usd_per_day": 1000.0})
    gateway = world.gateway().start()
    assert_succeeds(gateway.call_capability("text.complete"), "call 1 of 2")
    gateway.restart()
    assert_succeeds(gateway.call_capability("text.complete"), "call 2 of 2 (after a restart)")
    gateway.restart()
    refused = gateway.call_capability("text.complete")
    assert_structured_refusal(refused, CALL_CAP_WORDS, "call 3 after restarts, max_calls_per_hour=2")
    assert stub.settled_post_count() == 2, (
        f"after restarts the call counter reset: {stub.post_count()} POSTs with max_calls_per_hour=2")


def test_p3_usd_per_day_cap_refuses_before_the_network(world):
    stub = single_route(world, OR, {"max_calls_per_hour": 1000, "max_usd_per_day": 1.0}, cost=1.5)
    gateway = world.gateway().start()
    assert_succeeds(gateway.call_capability("text.complete"), "the first call (nothing spent yet)")
    refused = gateway.call_capability("text.complete")
    assert_structured_refusal(refused, USD_CAP_WORDS, "a call after 1.5 USD spent of 1.0")
    assert stub.settled_post_count() == 1, f"the capped call reached the provider ({stub.post_count()} POSTs)"


def test_p3_usd_per_day_counter_survives_restart(world):
    stub = single_route(world, OR, {"max_calls_per_hour": 1000, "max_usd_per_day": 1.0}, cost=1.5)
    gateway = world.gateway().start()
    assert_succeeds(gateway.call_capability("text.complete"), "the first call (nothing spent yet)")
    gateway.restart()
    refused = gateway.call_capability("text.complete")
    assert_structured_refusal(refused, USD_CAP_WORDS, "a call after a restart, 1.5 USD spent of 1.0")
    assert stub.settled_post_count() == 1, (
        f"after a restart the spend counter reset: {stub.post_count()} POSTs with 1.5 USD spent of 1.0")


def test_p3_confirm_over_usd_requires_confirmation_before_any_call(world):
    pricing = {"prompt": "0.001", "completion": "0.001", "request": "5", "image": "0"}
    stub = single_route(world, OR, {"max_calls_per_hour": 1000, "max_usd_per_day": 1000.0,
                                    "confirm_over_usd": 1.0}, cost=5.0, pricing=pricing)
    gateway = world.gateway().start()
    held = None
    for attempt in (1, 2):
        before = stub.post_count()
        outcome = gateway.call_capability("text.complete")
        if outcome.requires_confirmation():
            assert stub.settled_post_count() == before, (
                f"attempt {attempt} returned confirmation_required but made a paid request")
            held = outcome
            break
        assert_succeeds(outcome, f"attempt {attempt} (no estimate yet)")
        assert stub.post_count() == before + 1
    assert held is not None, ("no confirmation_required: a call estimated at 5.0 USD (pricing request=5, "
                              "and the previous call reported cost 5.0) ran with confirm_over_usd=1.0")
    before = stub.post_count()
    confirmed = gateway.call_capability("text.complete", extra={"confirm": True})
    assert_succeeds(confirmed, "the retry with confirm: true")
    assert stub.settled_post_count() == before + 1, "the confirmed retry did not make exactly one request"
