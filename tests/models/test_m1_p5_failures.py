"""M1 P5 — failures are honest (docs/work/orders/M1-model-gateway.md).

"A provider error or an unsupported modality returns a structured error naming the
capability and provider class. Nothing is written as success, and no paid retry
happens silently."
Falsifier: a success-shaped result on failure, or a hidden retry.

Reading notes: "provider class" is read as the provider's `kind` (the configured
provider *name* never contains it here). "Structured" means a JSON object in the result
(structuredContent, a JSON text content, or a JSON-RPC error's data). "Nothing is
written as success": no new provenance record for the failed call under artifact_root
unless it (or an enclosing object) is marked as a failure. A retry is counted at the
provider stub after a grace period; every POST is a paid call, so even a 429 must not
be retried silently. "Unsupported modality" is produced at the provider: its /models
metadata lists text-only modalities and its media endpoints answer 404/400. For an
`openai-compatible` provider (which the order says covers every modality) the same
capability first succeeds against a capable provider, so the failure is attributable
to the modality; for an `openrouter` provider no such control is asserted, because the
order lets the gateway decline a modality OpenRouter lacks.
"""
from __future__ import annotations

import pytest

from m1_harness import (call_with_input, new_success_records, provenance_on_disk, provider,
                        reference_spelling, spell_like, write_input_audio)
from m1_stub_provider import OK, UNSUPPORTED

OA = "openai-compatible"
OR = "openrouter"
MEDIA = ("image.generate", "audio.transcribe", "audio.speak")


def assert_honest_failure(world, outcome, *, capability, kind, label, before_records, model, names):
    assert outcome.is_error_shaped(), f"{label}: a success-shaped result on failure\n" + outcome.describe()
    assert outcome.is_structured(), f"{label}: the error is not structured (no JSON object)\n" + outcome.describe()
    blob = outcome.blob()
    assert capability in blob, f"{label}: the error does not name the capability {capability}\n" + outcome.describe()
    assert kind in blob, f"{label}: the error does not name the provider class {kind}\n" + outcome.describe()
    after = provenance_on_disk(world, capability=capability, model=model, provider_names=names)
    written = new_success_records(before_records, after)
    assert not written, f"{label}: a provenance record for the failed call is not marked as a failure: {written[:2]}"


@pytest.mark.parametrize("mode", ["status:500", "status:429", "status:503-then-ok", "error-envelope-200",
                                  "non-json-200"],
                         ids=["http-500", "http-429", "http-503-then-ok", "error-envelope-200", "non-json-200"])
def test_p5_provider_error_is_structured_and_never_retried(world, mode):
    key = world.key("main")
    model = f"m1vendor/text-{world.nonce}"
    stub = world.stub("main", models=[model], mode=mode)
    names = {"prov-main", OA}
    world.write_config(routes={"text.complete": {"provider": "prov-main", "model": model}},
                       providers={"prov-main": provider(stub, key, OA)})
    gateway = world.gateway().start()
    before = provenance_on_disk(world, capability="text.complete", model=model, provider_names=names)
    outcome = gateway.call_capability("text.complete")
    assert_honest_failure(world, outcome, capability="text.complete", kind=OA, label=mode,
                          before_records=before, model=model, names=names)
    posts = stub.settled_post_count(1.0)
    assert posts == 1, f"{mode}: the gateway made {posts} provider requests for one call (hidden retry)"


def media_call(gateway, world, capability, form):
    if capability == "audio.transcribe":
        return gateway.call_capability(capability, input_ref=spell_like(form, world, "inputs/clip.wav"))
    return gateway.call_capability(capability)


@pytest.mark.parametrize("capability", MEDIA)
def test_p5_unsupported_modality_is_a_structured_error_openai_compatible(world, capability):
    key = world.key("main")
    model = f"m1vendor/{capability.replace('.', '-')}-{world.nonce}"
    stub = world.stub("main", models=[model], mode=OK)
    names = {"prov-main", OA}
    world.write_config(routes={capability: {"provider": "prov-main", "model": model}},
                       providers={"prov-main": provider(stub, key, OA)})
    write_input_audio(world, "inputs/clip.wav", stub)
    gateway = world.gateway().start()
    if capability == "audio.transcribe":
        control, form = call_with_input(gateway, world, capability, "inputs/clip.wav")
    else:
        control, form = gateway.call_capability(capability), "inputs/clip.wav"
    assert not control.is_error_shaped(), "control: the capability must work when supported\n" + control.describe()
    gateway.stop()

    stub.mode = UNSUPPORTED
    gateway.start()
    before_posts = stub.post_count()
    before = provenance_on_disk(world, capability=capability, model=model, provider_names=names)
    outcome = media_call(gateway, world, capability, form)
    assert_honest_failure(world, outcome, capability=capability, kind=OA, label="unsupported modality",
                          before_records=before, model=model, names=names)
    assert stub.settled_post_count(1.0) - before_posts <= 1, "the unsupported call was retried"


@pytest.mark.parametrize("capability", MEDIA)
def test_p5_unsupported_modality_is_a_structured_error_openrouter(world, capability):
    key = world.key("main", OR)
    text_model = f"m1vendor/text-{world.nonce}"
    model = f"m1vendor/{capability.replace('.', '-')}-{world.nonce}"
    stub = world.stub("main", models=[text_model, model], mode=UNSUPPORTED)
    names = {"prov-main", OR}
    world.write_config(routes={"text.complete": {"provider": "prov-main", "model": text_model},
                               capability: {"provider": "prov-main", "model": model}},
                       providers={"prov-main": provider(stub, key, OR)})
    write_input_audio(world, "inputs/clip.wav", stub)
    gateway = world.gateway().start()
    control = gateway.call_capability("text.complete")
    assert not control.is_error_shaped(), "control: text.complete must work on this provider\n" + control.describe()
    # Spell the input reference the way the gateway spells its own output references.
    form = reference_spelling(control, world) or "inputs/clip.wav"
    before_posts = stub.post_count()
    before = provenance_on_disk(world, capability=capability, model=model, provider_names=names)
    outcome = media_call(gateway, world, capability, form)
    assert_honest_failure(world, outcome, capability=capability, kind=OR, label="unsupported modality",
                          before_records=before, model=model, names=names)
    assert stub.settled_post_count(1.0) - before_posts <= 1, "the unsupported call was retried"
