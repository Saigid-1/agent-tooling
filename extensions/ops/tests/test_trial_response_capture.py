import hashlib
import json
import os
import sys

import pytest

from kp_agent_tooling_ops._impl.trial_response_capture import CaptureError, capture_call, compact_summary
from kp_agent_tooling_ops.capture_cli import main as capture_main


def _response(value):
    text = json.dumps(value, separators=(",", ":"))
    return {"isError": False, "structuredContent": value,
            "content": [{"type": "text", "text": text}]}


def test_capture_retains_result_once_sanitizes_and_emits_compact_summary():
    value = {"status": "ok", "token": "secret-token", "authorization": "Bearer abc.def",
             "code_reference_context": {"diagnostics": [{"reason": "unresolved"}]},
             "transcript": "private transcript", "rows": [1, 2, 3]}
    capture = capture_call({"operation": "knowledge.retrieve",
                            "arguments": {"api_key": "key", "query": "Bearer xyz"},
                            "response": _response(value)}, transport="native_mcp")
    rendered = json.dumps(capture)
    assert "secret-token" not in rendered and "abc.def" not in rendered
    assert "private transcript" not in rendered and '"key"' not in rendered
    assert capture["result"]["token"] == "[REDACTED]"
    assert capture["result"]["code_reference_context"]["diagnostics"][0]["reason"] == "unresolved"
    assert capture["text_mirror"]["status"] == "omitted_duplicate"
    assert capture["transport_provenance"]["capture_attests_transport"] is False
    assert capture["result_integrity"]["original_sha256"] != (
        capture["result_integrity"]["persisted_sanitized_sha256"])
    summary = compact_summary(capture)
    assert summary["status"] == "ok" and "result" not in summary


def test_continued_capture_checks_every_page_and_retains_payload_once():
    payload = {"status": "review_required", "rows": list(range(20))}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    initial = {"status": "continued", "token": digest, "sha256": digest,
               "total_bytes": len(raw)}
    pages = []
    for offset in range(0, len(raw), 17):
        part = raw[offset:offset + 17]
        end = offset + len(part)
        pages.append(_response({"status": "ok", "token": digest, "sha256": digest,
            "total_bytes": len(raw), "offset": offset, "bytes": len(part),
            "text": part.decode("ascii"), "next_offset": end if end < len(raw) else None,
            "complete": end == len(raw)}))
    capture = capture_call({"operation": "knowledge.context", "response": _response(initial),
                            "delivery_pages": pages}, transport="native_mcp")
    assert capture["result"] == payload
    assert capture["delivery"]["page_count"] == len(pages)
    assert all("text" not in page for page in capture["delivery"]["pages"])
    assert capture["delivery"]["wire_sha256"] == digest
    assert capture["delivery"]["wire_total_bytes"] == len(raw)
    assert capture["delivery"]["wire_integrity_verified"] is True
    assert digest != capture["result_integrity"]["original_sha256"]
    assert capture["result_integrity"]["persisted_sanitized_sha256"] == hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_incomplete_or_corrupt_delivery_is_refused():
    raw = b'{"status":"ok"}'
    digest = hashlib.sha256(raw).hexdigest()
    initial = {"status": "continued", "token": digest, "sha256": digest, "total_bytes": len(raw)}
    with pytest.raises(CaptureError, match="incomplete"):
        capture_call({"operation": "x", "response": _response(initial), "delivery_pages": []},
                     transport="native_mcp")

    bad = _response({"status": "ok", "token": digest, "sha256": digest,
        "total_bytes": len(raw), "offset": 1, "bytes": len(raw), "text": raw.decode(),
        "next_offset": None, "complete": True})
    with pytest.raises(CaptureError, match="inconsistent"):
        capture_call({"operation": "x", "response": _response(initial), "delivery_pages": [bad]},
                     transport="native_mcp")
    bad["structuredContent"]["offset"] = 0
    bad["structuredContent"]["text"] = '{"status":"no"}'
    with pytest.raises(CaptureError, match="inconsistent|digest"):
        capture_call({"operation": "x", "response": _response(initial), "delivery_pages": [bad]},
                     transport="native_mcp")
    bad["isError"] = True
    with pytest.raises(CaptureError, match="errored"):
        capture_call({"operation": "x", "response": _response(initial), "delivery_pages": [bad]},
                     transport="native_mcp")


def test_facade_capture_cannot_be_presented_as_native_proof():
    capture = capture_call({"operation": "knowledge.context", "response": _response({"status": "ok"})},
                           transport="facade_cli")
    assert capture["transport"] == "facade_cli"
    assert "cannot prove native pagination" in capture["transport_provenance"]["note"]


def test_top_level_tool_error_is_retained_as_failed_evidence():
    response = _response({"status": "unavailable", "reason": "generation_mismatch"})
    response["isError"] = True
    capture = capture_call({"operation": "knowledge.reference_diagnostics", "response": response},
                           transport="native_mcp")
    assert capture["is_error"] is True
    assert compact_summary(capture)["status"] == "unavailable"


def test_missing_host_error_flag_remains_unknown():
    response = _response({"status": "ok"})
    del response["isError"]
    capture = capture_call({"operation": "tooling.identity", "response": response},
                           transport="native_mcp")
    assert capture["is_error"] is None
    assert compact_summary(capture)["is_error"] is None


def test_cli_refuses_empty_input_and_writes_private_hashed_artifacts(tmp_path, monkeypatch, capsys):
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    monkeypatch.setattr(sys, "argv", ["capture", str(empty), str(tmp_path / "empty-out"),
                                      "--transport", "native_mcp"])
    with pytest.raises(CaptureError, match="no capture records"):
        capture_main()

    source = tmp_path / "one.jsonl"
    source.write_text(json.dumps({"operation": "tooling.identity", "arguments": {},
                                  "response": _response({"status": "ok"})}) + "\n")
    output = tmp_path / "capture"
    monkeypatch.setattr(sys, "argv", ["capture", str(source), str(output),
                                      "--transport", "native_mcp"])
    assert capture_main() == 0
    printed = json.loads(capsys.readouterr().out)
    artifact = output / printed["artifacts"][0]["path"]
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == printed["artifacts"][0]["sha256"]
    assert os.stat(output).st_mode & 0o777 == 0o700
    assert os.stat(artifact).st_mode & 0o777 == 0o600
