"""M1 P1 — routing is configuration (docs/work/orders/M1-model-gateway.md).

"Changing a capability's provider or model in the config changes the outbound request's
target with no code change. Adding a capability to routes, with a provider kind that
already exists, lists a new tool."
Falsifier: a code change needed, or a stale route used.

The config is edited between server processes (stop, rewrite, start): the order does
not promise hot reload, so none is asserted. "Target" is observed at the loopback
provider stubs: which stub received the POST, the model the body names, and the key the
Authorization header carried.
"""
from __future__ import annotations

from m1_harness import generous_budget, provider, tool_names

OA = "openai-compatible"


def names_model(record, model: str) -> bool:
    body = record.json_body()
    if isinstance(body, dict) and body.get("model") == model:
        return True
    return model.encode() in record.body or model.replace("/", "\\/").encode() in record.body


def test_p1_model_change_in_config_changes_the_outbound_model(world):
    key = world.key("main")
    first, second = f"m1vendor/alpha-model-{world.nonce}", f"m1vendor/omega-model-{world.nonce}"
    stub = world.stub("main", models=[first, second])
    providers = {"prov-main": provider(stub, key, OA)}
    world.write_config(routes={"text.complete": {"provider": "prov-main", "model": first}}, providers=providers)
    gateway = world.gateway().start()
    outcome = gateway.call_capability("text.complete")
    assert not outcome.is_error_shaped(), outcome.describe()
    posts = stub.posts()
    assert len(posts) == 1 and names_model(posts[0], first) and not names_model(posts[0], second), (
        f"before the edit the request should name {first}; requests: {[p.path for p in posts]}")
    gateway.stop()

    world.write_config(routes={"text.complete": {"provider": "prov-main", "model": second}}, providers=providers)
    gateway.start()
    outcome = gateway.call_capability("text.complete")
    assert not outcome.is_error_shaped(), outcome.describe()
    posts = stub.posts()
    assert len(posts) == 2, f"expected exactly one more request after the edit, saw {len(posts)} in total"
    assert names_model(posts[1], second), f"after the edit the outbound request does not name {second}"
    assert not names_model(posts[1], first), f"after the edit the outbound request still names {first} (stale route)"


def test_p1_provider_change_in_config_changes_the_outbound_target(world):
    key_alpha, key_beta = world.key("alpha"), world.key("beta")
    model = f"m1vendor/text-{world.nonce}"
    alpha, beta = world.stub("alpha", models=[model]), world.stub("beta", models=[model])
    providers = {"prov-alpha": provider(alpha, key_alpha, OA), "prov-beta": provider(beta, key_beta, OA)}
    world.write_config(routes={"text.complete": {"provider": "prov-alpha", "model": model}}, providers=providers)
    gateway = world.gateway().start()
    outcome = gateway.call_capability("text.complete")
    assert not outcome.is_error_shaped(), outcome.describe()
    assert alpha.post_count() == 1 and beta.post_count() == 0, (
        f"routed to prov-alpha: alpha saw {alpha.post_count()} POSTs, beta {beta.post_count()}")
    assert alpha.posts()[0].authorization_key == "alpha"
    gateway.stop()

    world.write_config(routes={"text.complete": {"provider": "prov-beta", "model": model}}, providers=providers)
    gateway.start()
    outcome = gateway.call_capability("text.complete")
    assert not outcome.is_error_shaped(), outcome.describe()
    assert beta.post_count() == 1, f"after re-routing to prov-beta, beta saw {beta.post_count()} POSTs"
    assert alpha.settled_post_count() == 1, "after re-routing to prov-beta, the request still went to prov-alpha"
    assert beta.posts()[0].authorization_key == "beta", (
        "the re-routed request did not carry prov-beta's key from its api_key_file "
        f"(carried: {beta.posts()[0].authorization_key})")


def test_p1_adding_a_route_lists_a_new_tool_that_reaches_the_provider(world):
    key = world.key("main")
    text_model, image_model = f"m1vendor/text-{world.nonce}", f"m1vendor/image-{world.nonce}"
    stub = world.stub("main", models=[text_model, image_model])
    providers = {"prov-main": provider(stub, key, OA)}
    routes = {"text.complete": {"provider": "prov-main", "model": text_model}}
    world.write_config(routes=routes, providers=providers)
    gateway = world.gateway().start()
    before = sorted(n for n in tool_names(gateway.list_tools()) if n.startswith("model."))
    assert before == ["model.text.complete"], f"with one route, tools/list model tools are {before}"
    gateway.stop()

    routes["image.generate"] = {"provider": "prov-main", "model": image_model}
    world.write_config(routes=routes, providers=providers,
                       budgets={c: generous_budget() for c in routes})
    gateway.start()
    after = sorted(n for n in tool_names(gateway.list_tools()) if n.startswith("model."))
    assert after == ["model.image.generate", "model.text.complete"], (
        f"after adding an image.generate route on an existing provider, model tools are {after}")
    outcome = gateway.call_capability("image.generate")
    assert not outcome.is_error_shaped(), outcome.describe()
    posts = stub.posts()
    assert posts and names_model(posts[-1], image_model), (
        f"the new tool's request does not name the new route's model {image_model}")
