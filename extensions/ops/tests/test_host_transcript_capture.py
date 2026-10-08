import hashlib
import json
import os
import sys

import pytest

from kp_agent_tooling_ops._impl.host_transcript_capture import extract_host_transcript
from kp_agent_tooling_ops._impl.trial_response_capture import CaptureError
from kp_agent_tooling_ops.transcript_cli import main as extract_main


def _line(session, *blocks, meta=None):
    row = {"sessionId": session, "message": {"content": list(blocks)}}
    if meta is not None:
        row["mcpMeta"] = {"structuredContent": meta}
    return json.dumps(row, separators=(",", ":")).encode() + b"\n"


def _use(call_id, name, arguments=None):
    return {"type": "tool_use", "id": call_id,
            "name": "mcp__ops-agent-tooling__" + name, "input": arguments or {}}


def _result(call_id, value, *, is_error="missing"):
    block = {"type": "tool_result", "tool_use_id": call_id,
             "content": [{"type": "text", "text": json.dumps(value, separators=(",", ":"))}]}
    if is_error != "missing":
        block["is_error"] = is_error
    return block


def _page(call_id, token, raw, offset, size):
    part = raw[offset:offset + size]
    end = offset + len(part)
    value = {"status": "ok", "token": token, "sha256": token,
             "total_bytes": len(raw), "offset": offset, "bytes": len(part),
             "text": part.decode("ascii"), "next_offset": end if end < len(raw) else None,
             "complete": end == len(raw)}
    return (_line("s", _use(call_id, "delivery_read", {"token": token, "offset": offset})),
            _line("s", _result(call_id, value), meta=value))


def test_parallel_reordered_pages_join_by_token_and_offset_and_unknown_error():
    a_raw = json.dumps({"status": "ok", "stream": "a", "rows": list(range(8))},
                       separators=(",", ":")).encode()
    b_raw = json.dumps({"status": "ok", "stream": "b", "rows": list(range(5))},
                       separators=(",", ":")).encode()
    a_token, b_token = hashlib.sha256(a_raw).hexdigest(), hashlib.sha256(b_raw).hexdigest()
    a_initial = {"status": "continued", "token": a_token, "sha256": a_token,
                 "total_bytes": len(a_raw)}
    b_initial = {"status": "continued", "token": b_token, "sha256": b_token,
                 "total_bytes": len(b_raw)}
    rows = [
        _line("s", _use("a", "knowledge_context")),
        _line("s", _use("b", "knowledge_retrieve")),
        _line("s", _result("b", b_initial), meta=b_initial),
        _line("s", _result("a", a_initial), meta=a_initial),
    ]
    pages = []
    for call_id, token, raw in (("ap", a_token, a_raw), ("bp", b_token, b_raw)):
        for index, offset in enumerate(range(0, len(raw), 13)):
            pages.append(_page(f"{call_id}{index}", token, raw, offset, 13))
    # Interleave streams and reverse offsets: transcript order must not control assembly.
    for use, result in reversed(pages[::2] + pages[1::2]):
        rows.extend((use, result))
    bundle = extract_host_transcript(b"".join(rows), session_id="s", call_ids=["a", "b"])
    assert [c["result"]["stream"] for c in bundle["captures"]] == ["a", "b"]
    assert all(c["is_error"] is None for c in bundle["captures"])
    assert bundle["evidence_class"] == "host_transcript_derived"
    assert bundle["wire_attestation"] is False
    assert bundle["proof_scope"]["excludes"][2].startswith("claim that model visibility")


def test_exact_duplicate_page_is_counted_but_conflict_is_refused():
    raw = b'{"status":"ok"}'
    token = hashlib.sha256(raw).hexdigest()
    initial = {"status": "continued", "token": token, "sha256": token,
               "total_bytes": len(raw)}
    use, result = _page("page", token, raw, 0, len(raw))
    transcript = b"".join([
        _line("s", _use("root", "knowledge_context")),
        _line("s", _result("root", initial), meta=initial), use, result, use, result,
    ])
    bundle = extract_host_transcript(transcript, session_id="s", call_ids=["root"])
    assert bundle["omissions"]["duplicate_delivery_pages"] == 1

    bad = {"status": "ok", "token": token, "sha256": token, "total_bytes": len(raw),
           "offset": 0, "bytes": len(raw), "text": '{"status":"no"}',
           "next_offset": None, "complete": True}
    conflict = transcript + _line("s", _use("page2", "delivery_read")) + _line(
        "s", _result("page2", bad), meta=bad)
    with pytest.raises(CaptureError, match="conflicting delivery page"):
        extract_host_transcript(conflict, session_id="s", call_ids=["root"])


def test_selection_omissions_hashes_and_sanitization_are_deterministic():
    value = {"status": "ok", "transcript": "do not persist", "token": "secret"}
    selected = _line("s", _use("chosen", "tooling_identity", {"api_key": "private"})) + _line(
        "s", _result("chosen", value, is_error=False), meta=value)
    raw = (_line("other", _use("foreign", "tooling_identity")) + selected
           + _line("s", _use("ignored", "knowledge_context")))
    one = extract_host_transcript(raw, session_id="s", call_ids=["chosen"])
    two = extract_host_transcript(raw, session_id="s", call_ids=["chosen"])
    assert one == two
    rendered = json.dumps(one)
    assert "do not persist" not in rendered and '"private"' not in rendered
    assert one["transcript"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert one["transcript"]["raw_persisted"] is False
    assert one["omissions"]["other_session_records"] == 1
    assert one["omissions"]["unbound_session_records"] == 0
    assert one["omissions"]["unselected_tool_calls"] == 1
    assert one["calls"][0]["call_id"].startswith("host-transcript-call:sha256:")
    provenance = one["captures"][0]["host_transcript_provenance"]
    assert provenance["use_record_id"].startswith("host-transcript-record:sha256:")


def test_missing_calls_truncation_and_incomplete_pages_fail_explicitly():
    raw = _line("s", _use("only-use", "tooling_identity"))
    with pytest.raises(CaptureError, match="missing use or result"):
        extract_host_transcript(raw, session_id="s", call_ids=["only-use"])
    with pytest.raises(CaptureError, match="line 1 exceeds limit"):
        extract_host_transcript(b"{" + b" " * 4_000_001, session_id="s", call_ids=["x"])

    payload = b'{"status":"ok"}'
    token = hashlib.sha256(payload).hexdigest()
    initial = {"status": "continued", "token": token, "sha256": token,
               "total_bytes": len(payload)}
    incomplete = (_line("s", _use("root", "knowledge_context"))
                  + _line("s", _result("root", initial), meta=initial))
    with pytest.raises(CaptureError, match="incomplete"):
        extract_host_transcript(incomplete, session_id="s", call_ids=["root"])


def test_unbound_records_are_not_silently_assigned_to_selected_session():
    value = {"status": "ok"}
    unbound = json.dumps({"message": {"content": [
        _use("unbound", "tooling_identity"), _result("unbound", value),
    ]}, "mcpMeta": {"structuredContent": value}}).encode() + b"\n"
    with pytest.raises(CaptureError, match="selected calls missing use or result"):
        extract_host_transcript(unbound, session_id="s", call_ids=["unbound"])

    bound = (_line("s", _use("chosen", "tooling_identity"))
             + _line("s", _result("chosen", value), meta=value))
    bundle = extract_host_transcript(unbound + bound, session_id="s", call_ids=["chosen"])
    assert bundle["omissions"]["unbound_session_records"] == 1
    assert bundle["session"]["selected_record_count"] == 2


@pytest.mark.parametrize(("name", "expected"), [
    ("knowledge_reference_recovery", "knowledge.reference_recovery"),
    ("navigation_search_page", "navigation.search_page"),
])
def test_operation_normalization_only_replaces_namespace_separator(name, expected):
    value = {"status": "ok"}
    raw = (_line("s", _use("call", name))
           + _line("s", _result("call", value), meta=value))
    bundle = extract_host_transcript(raw, session_id="s", call_ids=["call"])
    assert bundle["captures"][0]["operation"] == expected


def test_file_read_is_bounded_even_when_stat_would_have_been_stale(tmp_path, monkeypatch):
    from kp_agent_tooling_ops._impl import host_transcript_capture as module

    source = tmp_path / "growing.jsonl"
    source.write_bytes(b"x" * 65)
    monkeypatch.setattr(module, "MAX_TRANSCRIPT_BYTES", 64)
    with pytest.raises(CaptureError, match="exceeds byte limit"):
        module.extract_host_transcript_file(source, session_id="s", call_ids=["call"])


def test_cli_writes_only_private_sanitized_artifacts(tmp_path, monkeypatch, capsys):
    value = {"status": "ok", "transcript": "private raw words"}
    source = tmp_path / "host.jsonl"
    source.write_bytes(_line("s", _use("call", "tooling_identity"))
                       + _line("s", _result("call", value), meta=value))
    output = tmp_path / "capture"
    monkeypatch.setattr(sys, "argv", ["extract", str(source), str(output),
                                      "--session-id", "s", "--call-id", "call"])
    assert extract_main() == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["call_count"] == 1
    assert os.stat(output).st_mode & 0o777 == 0o700
    assert all(os.stat(path).st_mode & 0o777 == 0o600 for path in output.iterdir())
    assert "private raw words" not in (output / "call-0001.json").read_text()
    assert "private raw words" not in (output / "manifest.json").read_text()
