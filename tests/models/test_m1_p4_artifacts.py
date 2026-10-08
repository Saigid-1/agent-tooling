"""M1 P4 — artifacts have provenance (docs/work/orders/M1-model-gateway.md).

"Every successful call writes its output under artifact_root with a provenance record,
and the tool returns only the reference and metadata. Paths never escape the root."
Falsifier: missing provenance, inline oversized output, or path traversal.
Interface: "Binary inputs are artifact references under artifact_root ... Outputs are
artifact references with provenance: capability, provider, model, request digest, cost
if reported, and timestamp."

Reading notes: the all-capability test uses an `openai-compatible` provider, which the
order says covers every modality; reported cost is asserted where the provider reports
it the OpenRouter way (`openrouter` kind). "Oversized" is read as: a result line
larger than 64 KiB, or any 2 KiB slice of a 256 KiB text or ~1 MiB image output
inline; small inline previews are not asserted against. The request digest is checked
for presence (a hex digest value), not for its preimage. The input reference spelling
is not named by the order: the harness tries a path relative to artifact_root, an
absolute path, and a file:// URI, and attacks use the spelling that was accepted.
"""
from __future__ import annotations

import base64
import os

import pytest

from m1_harness import (BINARY_INPUT, CAPABILITIES, _inside, body_carries, call_with_input, documents_in,
                        missing_provenance, provenance_nodes, provenance_on_disk, provider, spell_like,
                        write_input_audio)
from m1_stub_provider import _wav

OA = "openai-compatible"
OR = "openrouter"


def expected_output(stub, capability) -> list[bytes]:
    if capability == "image.generate":
        return [stub.image, base64.b64encode(stub.image)]
    if capability == "audio.speak":
        return [stub.audio, base64.b64encode(stub.audio)]
    return [stub.text.encode()]


def output_files(outcome, world, exclude=()):
    refs = outcome.references(world.artifact_root)
    escaped = [p for p in refs if not _inside(p, world.artifact_root)]
    assert not escaped, f"the result references files outside artifact_root: {escaped}"
    return [p for p in refs if p.resolve() not in {e.resolve() for e in exclude}]


def assert_provenance(outcome, world, *, capability, model, names, cost=None):
    in_result = [n for p in outcome.payloads for n in provenance_nodes(
        p, capability=capability, model=model, provider_names=names, cost=cost)]
    assert in_result, ("the result carries no provenance; missing: "
                       f"{missing_provenance(outcome.payloads, capability=capability, model=model, provider_names=names, cost=cost)}"
                       f"\n{outcome.describe()}")
    on_disk = provenance_on_disk(world, capability=capability, model=model, provider_names=names, cost=cost)
    documents = documents_in(world.files_under().keys())
    assert on_disk, ("no provenance record under artifact_root; best effort missing: "
                     f"{missing_provenance(documents, capability=capability, model=model, provider_names=names, cost=cost)}"
                     f"; files: {sorted(str(p.relative_to(world.artifact_root)) for p in world.files_under())}")


@pytest.mark.parametrize("capability", CAPABILITIES)
def test_p4_successful_call_writes_an_artifact_with_provenance(world, capability):
    key = world.key("main")
    model = f"m1vendor/{capability.replace('.', '-')}-{world.nonce}"
    stub = world.stub("main", models=[model])
    world.write_config(routes={capability: {"provider": "prov-main", "model": model}},
                       providers={"prov-main": provider(stub, key, OA)})
    gateway = world.gateway().start()
    inputs = []
    if capability in BINARY_INPUT:
        inputs.append(write_input_audio(world, f"inputs/clip-{world.nonce}.wav", stub))
        outcome, form = call_with_input(gateway, world, capability, f"inputs/clip-{world.nonce}.wav")
        assert form, "no artifact-reference spelling of an input under artifact_root was accepted\n" + outcome.describe()
        assert body_carries(stub, stub.audio), "the referenced input audio did not reach the provider"
    else:
        outcome = gateway.call_capability(capability)
    assert not outcome.is_error_shaped(), outcome.describe()
    assert stub.settled_post_count() == 1, f"one call made {stub.post_count()} provider requests"
    files = output_files(outcome, world, exclude=inputs)
    assert files, "the result references no output artifact under artifact_root\n" + outcome.describe()
    wanted = expected_output(stub, capability)
    holders = [p for p in files if any(w in p.read_bytes() for w in wanted)]
    assert holders, (f"no referenced artifact holds the provider's {capability} output; referenced: "
                     f"{[str(p) for p in files]}")
    assert_provenance(outcome, world, capability=capability, model=model, names={"prov-main", OA})


def test_p4_reported_cost_is_recorded_in_provenance(world):
    key = world.key("main", OR)
    model = f"m1vendor/text-{world.nonce}"
    stub = world.stub("main", models=[model], cost=0.0137)
    world.write_config(routes={"text.complete": {"provider": "prov-main", "model": model}},
                       providers={"prov-main": provider(stub, key, OR)})
    gateway = world.gateway().start()
    outcome = gateway.call_capability("text.complete")
    assert not outcome.is_error_shaped(), outcome.describe()
    assert output_files(outcome, world), "the result references no output artifact\n" + outcome.describe()
    assert_provenance(outcome, world, capability="text.complete", model=model, names={"prov-main", OR},
                      cost=0.0137)


@pytest.mark.parametrize("capability", ["text.complete", "image.generate"])
def test_p4_oversized_output_is_referenced_not_inlined(world, capability):
    key = world.key("main")
    model = f"m1vendor/{capability.replace('.', '-')}-{world.nonce}"
    stub = world.stub("main", models=[model], text_size=256 * 1024, image_side=600)
    world.write_config(routes={capability: {"provider": "prov-main", "model": model}},
                       providers={"prov-main": provider(stub, key, OA)})
    gateway = world.gateway().start()
    outcome = gateway.call_capability(capability)
    assert not outcome.is_error_shaped(), outcome.describe()
    payload = stub.image if capability == "image.generate" else stub.text.encode()
    encoded = base64.b64encode(payload).decode() if capability == "image.generate" else payload.decode()
    assert len(payload) >= 256 * 1024
    middle = len(encoded) // 2
    slices = [encoded[middle: middle + 2048], encoded[-2048:]]
    assert len(outcome.blob()) <= 64 * 1024 and not any(s in outcome.blob() for s in slices), (
        f"the {len(payload)}-byte {capability} output came back inline ({len(outcome.blob())}-byte result)")
    files = output_files(outcome, world)
    assert any(any(w in p.read_bytes() for w in expected_output(stub, capability)) for p in files), (
        f"no referenced artifact holds the full {capability} output; referenced: {[str(p) for p in files]}")


@pytest.mark.parametrize("attack", ["dotdot", "absolute", "symlink"])
def test_p4_input_reference_outside_the_root_is_refused(world, attack):
    key = world.key("main")
    model = f"m1vendor/transcribe-{world.nonce}"
    stub = world.stub("main", models=[model])
    world.write_config(routes={"audio.transcribe": {"provider": "prov-main", "model": model}},
                       providers={"prov-main": provider(stub, key, OA)})
    outside_bytes = _wav(f"M1-OUTSIDE-{world.nonce}")
    outside = world.outside / "secret.wav"
    outside.write_bytes(outside_bytes)
    write_input_audio(world, "inputs/clip.wav", stub)
    gateway = world.gateway().start()
    control, form = call_with_input(gateway, world, "audio.transcribe", "inputs/clip.wav")
    assert form and not control.is_error_shaped(), (
        "control: an input under artifact_root must be accepted\n" + control.describe())
    before = stub.post_count()

    if attack == "dotdot":
        reference = spell_like(form, world, "../outside/secret.wav")
    elif attack == "absolute":
        reference = ("file://" if form.startswith("file://") else "") + str(outside)
    else:
        os.symlink(outside, world.artifact_root / "inputs" / "link.wav")
        reference = spell_like(form, world, "inputs/link.wav")
    outcome = gateway.call_capability("audio.transcribe", input_ref=reference)
    assert outcome.is_error_shaped(), (f"an input reference escaping artifact_root ({attack}) was accepted\n"
                                       + outcome.describe())
    assert stub.settled_post_count() == before, f"the {attack} reference produced a provider request"
    assert not body_carries(stub, outside_bytes), "a file outside artifact_root was sent to the provider"
