"""S1a (b): the first test of the gateway's `budget_unconfigured` refusal (census §12 found none).

Order: docs/work/orders/S1a-scheduled-summarizer.md, falsifier (b): "Also a first test of
`budget_unconfigured` (none exists, census §12)". The refusal is the gateway's own (model_gateway.py, the
budget step of the pre-network order), so this test calls the gateway, not the role, and is GREEN at the
order's base: it pins existing behaviour that the role's path relies on. The role's own reading of a
missing budget is case `budget` of tests/test_s1a_a_not_configured_no_network.py.

The world: the S1a world's gateway configuration (tests/s1a_harness.py) with the `memory.summarize` route
to the loopback stub, a valid key file, and NO budget for the capability. One call of
`model.memory.summarize` through `ModelGateway.call`, with the gateway's real HTTP transport, under the
S1a network guard.

GREEN-IF `test_budget_unconfigured_refuses_before_the_network`: the result has status `refused`, category
`budget_unconfigured` and `requests_sent` 0; the guard saw zero network events and the stub received
nothing. Control `test_the_same_call_with_a_budget_reaches_the_stub`: with a priced budget the same call
reaches the stub once (so "zero" is a measurement).

Mutant that must be RED: remove the budget check (the call then fails later as `internal_error`, still
without a request, so the category assertion is the one that catches it).
"""
from __future__ import annotations

import s1a_seams as seams
from s1a_harness import NetworkGuard, guarded, make_world  # noqa: F401 - fixture


def _call(world):
    from kp_agent_tooling._impl.service.model_gateway import ModelGateway
    guard = NetworkGuard(world.stub.address)
    with guarded(guard):
        result = ModelGateway(world.gateway_path).call('model.' + seams.CAPABILITY, {'prompt': 'S1a probe.'})
    return result, guard


def test_budget_unconfigured_refuses_before_the_network(make_world):
    world = make_world().initialize()
    world.write_gateway(budget=None)
    result, guard = _call(world)
    assert (result.get('status'), result.get('category'), result.get('requests_sent')) == (
        'refused', 'budget_unconfigured', 0), f'result {result}'
    assert not guard.network() and not world.stub.requests, (
        f'network activity: {guard.events} stub: {world.stub.requests}')


def test_the_same_call_with_a_budget_reaches_the_stub(make_world):
    world = make_world().initialize()
    result, guard = _call(world)
    assert result.get('status') == 'ok' and result.get('requests_sent') == 1, f'result {result}'
    assert len(world.stub.requests) == 1 and guard.connections(), guard.events
