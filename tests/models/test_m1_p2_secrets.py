"""M1 P2 — secrets are safe (docs/work/orders/M1-model-gateway.md).

"API keys are read only from api_key_file (private, owner-only). They never appear in
tool output, errors, logs, provenance or artifacts. A world-readable key file is
refused."
Falsifier: a key in any output, or a permissive key file accepted.

Each key is a fresh sentinel. The gateway's environment also carries decoy values in
OPENROUTER_API_KEY, OPENAI_API_KEY and KP_AGENT_MODELS_API_KEY, so "read only from
api_key_file" is observable: the stub records which known key the Authorization header
carried. "Appears" is checked for the whole key and for its middle 16 characters, in
every file under the test's scratch tree except the key file itself (artifact_root,
the gateway's HOME and TMPDIR, serve stdout/stderr logs, doctor output, the stub's own
request log) and in every JSON-RPC line the server wrote.

"Refused" for a permissive key file is read as any of: serve does not start, the
route's tool is not listed, or calling it returns an error; in every case the key must
never reach the provider. Owner-only is read literally, so a group-readable file (0640)
is permissive too.
"""
from __future__ import annotations

import pytest

from m1_harness import provider, tool_names

OA = "openai-compatible"
OR = "openrouter"
FAILURE_TOKENS = ("refus", "permission", "insecure", "world", "644", "604", "640", "mode", "private",
                  "unsafe", "fail", "error", "not ok", "invalid", "readable", "owner")


@pytest.mark.parametrize("kind", [OR, OA])
def test_p2_authorization_carries_the_key_from_api_key_file(world, kind):
    key = world.key("file-key", kind)
    model = f"m1vendor/text-{world.nonce}"
    stub = world.stub("main", models=[model])
    world.write_config(routes={"text.complete": {"provider": "prov-main", "model": model}},
                       providers={"prov-main": provider(stub, key, kind)})
    gateway = world.gateway().start()
    outcome = gateway.call_capability("text.complete")
    assert not outcome.is_error_shaped(), outcome.describe()
    posts = stub.posts()
    assert len(posts) == 1
    assert posts[0].authorization_key == "file-key", (
        "the Authorization header did not carry the key from api_key_file "
        f"(it carried: {posts[0].authorization_key}; decoys come from the environment)")
    assert (posts[0].authorization_scheme or "").lower() == "bearer", (
        f"Authorization scheme is {posts[0].authorization_scheme!r}, expected Bearer")
    carried = {r.key_name for r in stub.requests if r.key_name}
    assert not any(name.startswith("decoy:") for name in carried), (
        f"a request carried a key from the environment instead of api_key_file: {sorted(carried)}")


def test_p2_key_never_appears_in_results_logs_provenance_or_artifacts(world):
    key = world.key("sentinel")
    model = f"m1vendor/text-{world.nonce}"
    stub = world.stub("main", models=[model])
    world.write_config(routes={"text.complete": {"provider": "prov-main", "model": model}},
                       providers={"prov-main": provider(stub, key, OA)})
    gateway = world.gateway().start()
    for _ in range(2):
        outcome = gateway.call_capability("text.complete")
        assert not outcome.is_error_shaped(), outcome.describe()
    gateway.stop()
    world.doctor()
    world.doctor("--live")
    assert stub.posts() and all(r.authorization_key == "sentinel" for r in stub.posts()), (
        "control: the sentinel key must have reached the provider")
    sightings = world.secret_sightings(key)
    assert not sightings, f"the API key appears in: {sightings}"


def test_p2_key_never_appears_when_the_provider_echoes_it_in_an_error(world):
    key = world.key("sentinel")
    model = f"m1vendor/text-{world.nonce}"
    stub = world.stub("main", models=[model], mode="status:401", echo_key_in_errors=True)
    world.write_config(routes={"text.complete": {"provider": "prov-main", "model": model}},
                       providers={"prov-main": provider(stub, key, OA)})
    gateway = world.gateway().start()
    outcome = gateway.call_capability("text.complete")
    assert outcome.is_error_shaped(), "a provider 401 produced a success-shaped result\n" + outcome.describe()
    gateway.stop()
    world.doctor("--live")
    assert stub.posts() and stub.posts()[0].authorization_key == "sentinel", (
        "control: the sentinel key must have reached the provider (which echoes it back)")
    sightings = world.secret_sightings(key)
    assert not sightings, f"the API key echoed by the provider's error appears in: {sightings}"


@pytest.mark.parametrize("mode", [0o644, 0o604, 0o640], ids=["0644", "0604", "0640"])
def test_p2_permissive_key_file_is_refused(world, mode):
    good, bad = world.key("good"), world.key("bad", mode=mode)
    text_model, image_model = f"m1vendor/text-{world.nonce}", f"m1vendor/image-{world.nonce}"
    good_stub = world.stub("good", models=[text_model])
    bad_stub = world.stub("bad", models=[image_model])
    world.write_config(routes={"text.complete": {"provider": "prov-good", "model": text_model},
                               "image.generate": {"provider": "prov-bad", "model": image_model}},
                       providers={"prov-good": provider(good_stub, good, OA),
                                  "prov-bad": provider(bad_stub, bad, OA)})
    gateway = world.gateway()
    refusal = gateway.try_start()
    if refusal is None:
        if "model.image.generate" in tool_names(gateway.list_tools()):
            outcome = gateway.call_capability("image.generate")
            assert outcome.is_error_shaped(), (
                f"a key file with mode {mode:o} was accepted: the call succeeded\n" + outcome.describe())
    used = [r.path for r in bad_stub.requests if r.key_name == "bad"]
    assert bad_stub.settled_post_count() == 0 and not used, (
        f"the key from a mode-{mode:o} key file reached the provider: {used}")
    gateway.stop()
    assert not world.secret_sightings(bad), f"the refused key appears in: {world.secret_sightings(bad)}"

    # Control: the same configuration with an owner-only key file works.
    bad.path.chmod(0o600)
    gateway.start()
    outcome = gateway.call_capability("image.generate")
    assert not outcome.is_error_shaped(), "control: with mode 600 the route must work\n" + outcome.describe()
    assert bad_stub.post_count() == 1 and bad_stub.posts()[0].authorization_key == "bad"


def test_p2_doctor_flags_a_permissive_key_file(world):
    key = world.key("main")
    model = f"m1vendor/text-{world.nonce}"
    stub = world.stub("main", models=[model])
    world.write_config(routes={"text.complete": {"provider": "prov-main", "model": model}},
                       providers={"prov-main": provider(stub, key, OA)})
    healthy = world.doctor()
    healthy_text = ((healthy.stdout or "") + (healthy.stderr or "")).lower()
    assert healthy.returncode == 0, f"control: doctor on a healthy config exited {healthy.returncode}\n{healthy_text[-2000:]}"
    key.path.chmod(0o644)
    permissive = world.doctor()
    permissive_text = ((permissive.stdout or "") + (permissive.stderr or "")).lower()
    new_tokens = [t for t in FAILURE_TOKENS if t in permissive_text and t not in healthy_text]
    assert permissive.returncode != 0 or new_tokens, (
        "doctor reports a world-readable key file exactly as it reports a private one "
        f"(exit {permissive.returncode}, no failure wording):\n{permissive_text[-2000:]}")
    assert not world.secret_sightings(key), f"the key appears in: {world.secret_sightings(key)}"
