import hashlib
import json

import pytest

from kp_agent_tooling._impl.tool_delivery import DeliveryStore


def test_inline_and_exact_continuation_across_instances(tmp_path):
    store = DeliveryStore(tmp_path, max_inline_bytes=1024, max_page_bytes=256)
    small = {"value": "é"}
    assert store.deliver(small) == small
    large = {"status": "ok", "body": "é" * 1000}
    envelope = store.deliver(large)
    assert envelope["status"] == "continued"
    assert len(store.encode(envelope)) <= 1024
    second = DeliveryStore(tmp_path, max_inline_bytes=1024, max_page_bytes=256)
    pieces = []
    offset = 0
    while True:
        page = second.read(envelope["token"], offset)
        assert page["offset"] == offset
        assert page["bytes"] <= 256
        assert len(second.encode(page)) <= 1024
        pieces.append(page["text"])
        if page["complete"]:
            assert page["next_offset"] is None
            break
        offset = page["next_offset"]
    raw = "".join(pieces).encode("ascii")
    assert len(raw) == envelope["total_bytes"]
    assert hashlib.sha256(raw).hexdigest() == envelope["sha256"]
    assert json.loads(raw) == large
    assert second.deliver(large) == envelope


def test_rejects_path_escape_unknown_and_modified_artifact(tmp_path):
    store = DeliveryStore(tmp_path, max_inline_bytes=1024, max_page_bytes=256)
    token = store.deliver({"body": "x" * 2000})["token"]
    for bad in ("../secret", "/etc/passwd", "A" * 64, "0" * 64):
        with pytest.raises(ValueError):
            store.read(bad)
    with pytest.raises(ValueError):
        store.read(token, offset=-1)
    with pytest.raises(ValueError):
        store.read(token, limit=257)
    (tmp_path / token).chmod(0o600)
    (tmp_path / token).write_text("changed")
    with pytest.raises(ValueError, match="integrity"):
        store.read(token)
    with pytest.raises(ValueError, match="integrity"):
        store.deliver({"body": "x" * 2000})


def test_oversized_result_is_explicit_refusal(tmp_path):
    store = DeliveryStore(tmp_path, max_inline_bytes=1024, max_page_bytes=256,
                          max_stored_bytes=1500)
    result = store.deliver({"body": "x" * 2000})
    assert result["status"] == "error"
    assert result["reason"] == "delivery_result_oversized"
    assert result["result_bytes"] > result["max_stored_bytes"]
    assert not list(tmp_path.iterdir())


def test_larger_requested_pages_preserve_small_inline_budget(tmp_path):
    store=DeliveryStore(tmp_path,max_page_bytes=16384,max_response_bytes=48000)
    value={'body':'quoted \" and unicode é '*2000}
    envelope=store.deliver(value)
    assert envelope['status']=='continued'
    parts=[];offset=0
    while True:
        page=store.read(envelope['token'],offset,16384)
        assert len(store.encode(page))<=48000
        parts.append(page['text'])
        if page['complete']:break
        offset=page['next_offset']
    assert len(parts)<10
    assert json.loads(''.join(parts))==value
    with pytest.raises(ValueError):store.read(envelope['token'],limit=16385)


def test_small_metadata_can_be_retained_by_reference(tmp_path):
    store=DeliveryStore(tmp_path)
    value={'provider':'fixture','environment':{'revision':'a'*40}}
    envelope=store.deliver(value,force=True)
    page=store.read(envelope['token'])
    assert page['complete'] and json.loads(page['text'])==value
