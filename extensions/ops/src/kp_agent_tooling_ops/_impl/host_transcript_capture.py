"""Bounded extraction of sanitized call evidence from a host JSONL transcript.

Raw transcript content is read only in memory.  The returned bundle contains
content-addressed provenance, selected sanitized calls, and explicit omissions; it
never contains the raw transcript or unselected call bodies.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.leaf import sha256_hex as _sha
from kp_agent_tooling_ops._impl.trial_response_capture import CaptureError, capture_call, compact_summary


MAX_TRANSCRIPT_BYTES = 64_000_000
MAX_TRANSCRIPT_LINES = 200_000
MAX_LINE_BYTES = 4_000_000
MAX_SELECTED_CALLS = 64
_PREFIXES = ("mcp__ops-agent-tooling__", "mcp__ops_agent_tooling__")
_OPERATIONS = {
    "tooling_identity": "tooling.identity",
    "knowledge_retrieve": "knowledge.retrieve",
    "knowledge_context": "knowledge.context",
    "knowledge_reference_diagnostics": "knowledge.reference_diagnostics",
    "navigation_source": "navigation.source",
    "delivery_read": "delivery.read",
}


def _canonical(value: Any) -> bytes:
    return leaf.canonical_bytes(value, ascii=True, allow_nan=False)


def _session_of(row: Mapping[str, Any]) -> str | None:
    for key in ("session_id", "sessionId", "conversation_id", "conversationId"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _operation(name: str) -> str:
    prefix = next((candidate for candidate in _PREFIXES if name.startswith(candidate)), "")
    suffix = name[len(prefix):]
    if suffix in _OPERATIONS:
        return _OPERATIONS[suffix]
    namespace, separator, operation = suffix.partition("_")
    return f"{namespace}.{operation}" if separator else suffix


def _blocks(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    message = row.get("message") or {}
    content = message.get("content") if isinstance(message, Mapping) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, Mapping)]


def _structured(response: Mapping[str, Any]) -> Mapping[str, Any]:
    value = response.get("structuredContent")
    if isinstance(value, Mapping):
        return value
    content = response.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if isinstance(content, list):
        text = "".join(str(block.get("text", "")) for block in content
                       if isinstance(block, Mapping) and block.get("type") == "text")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CaptureError("host result text is not JSON") from exc
    if not isinstance(value, Mapping):
        raise CaptureError("host result has no object response")
    return value


@dataclass(frozen=True)
class _Use:
    host_id: str
    operation: str
    arguments: Mapping[str, Any]
    record_id: str


@dataclass(frozen=True)
class _Result:
    response: Mapping[str, Any]
    record_id: str


def _response(block: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    content = block.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if content is None:
        content = []
    meta = row.get("mcpMeta") or {}
    response: dict[str, Any] = {
        "structuredContent": meta.get("structuredContent") if isinstance(meta, Mapping) else None,
        "content": content,
    }
    if "is_error" in block:
        response["isError"] = block["is_error"]
    elif "isError" in block:
        response["isError"] = block["isError"]
    return response


def extract_host_transcript(
    raw: bytes,
    *,
    session_id: str,
    call_ids: Sequence[str],
    transport: str = "native_mcp",
) -> dict[str, Any]:
    """Extract exactly ``call_ids`` from ``session_id`` into a sanitized bundle."""
    if not isinstance(session_id, str) or not session_id:
        raise CaptureError("session_id is required")
    requested = list(call_ids)
    if not requested or len(requested) > MAX_SELECTED_CALLS or len(set(requested)) != len(requested):
        raise CaptureError("call_ids must contain 1..64 unique IDs")
    if len(raw) > MAX_TRANSCRIPT_BYTES:
        raise CaptureError("host transcript exceeds byte limit")
    lines = raw.splitlines(keepends=True)
    if len(lines) > MAX_TRANSCRIPT_LINES:
        raise CaptureError("host transcript exceeds line limit")

    uses: dict[str, _Use] = {}
    results: dict[str, _Result] = {}
    result_occurrences: dict[str, int] = {}
    selected_rows = 0
    omitted_sessions = 0
    omitted_unbound_records = 0
    malformed_rows: list[dict[str, Any]] = []
    for number, line in enumerate(lines, 1):
        if len(line) > MAX_LINE_BYTES:
            raise CaptureError(f"host transcript line {number} exceeds limit")
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            malformed_rows.append({"line": number, "record_id": leaf.transcript_record_id(line),
                                   "reason": "invalid_json"})
            continue
        if not isinstance(row, Mapping):
            malformed_rows.append({"line": number, "record_id": leaf.transcript_record_id(line),
                                   "reason": "non_object"})
            continue
        row_session = _session_of(row)
        if row_session is None:
            omitted_unbound_records += 1
            continue
        if row_session != session_id:
            omitted_sessions += 1
            continue
        selected_rows += 1
        rid = leaf.transcript_record_id(line)
        for block in _blocks(row):
            kind = block.get("type")
            if kind == "tool_use" and isinstance(block.get("id"), str):
                name = block.get("name")
                arguments = block.get("input", {})
                if (isinstance(name, str) and name.startswith(_PREFIXES)
                        and isinstance(arguments, Mapping)):
                    host_id = block["id"]
                    item = _Use(host_id, _operation(name), arguments, rid)
                    if host_id in uses and uses[host_id] != item:
                        raise CaptureError(f"conflicting duplicate tool use {host_id}")
                    uses[host_id] = item
            elif kind == "tool_result" and isinstance(block.get("tool_use_id"), str):
                host_id = block["tool_use_id"]
                item = _Result(_response(block, row), rid)
                if host_id in results and results[host_id] != item:
                    raise CaptureError(f"conflicting duplicate tool result {host_id}")
                results[host_id] = item
                result_occurrences[host_id] = result_occurrences.get(host_id, 0) + 1

    missing = [call_id for call_id in requested if call_id not in uses or call_id not in results]
    if missing:
        raise CaptureError("selected calls missing use or result: " + ", ".join(missing))

    page_by_key: dict[tuple[str, int], Mapping[str, Any]] = {}
    page_call_ids: dict[tuple[str, int], str] = {}
    page_host_call_ids: dict[tuple[str, int], str] = {}
    duplicate_pages = 0
    for host_id, use in uses.items():
        if use.operation != "delivery.read" or host_id not in results:
            continue
        response = results[host_id].response
        page = _structured(response)
        token, offset = page.get("token"), page.get("offset")
        if not isinstance(token, str) or type(offset) is not int:
            continue
        if (use.arguments.get("token") not in (None, token)
                or use.arguments.get("offset") not in (None, offset)):
            raise CaptureError(f"delivery request/result identity mismatch for {host_id}")
        key = (token, offset)
        if key in page_by_key:
            if _canonical(page_by_key[key]) != _canonical(response):
                raise CaptureError(f"conflicting delivery page at {token}:{offset}")
            duplicate_pages += 1
            continue
        page_by_key[key] = response
        page_identity = {
            "session_id": session_id, "host_call_id": host_id,
            "use_record_id": use.record_id, "result_record_id": results[host_id].record_id,
            "delivery_token": token, "offset": offset,
        }
        page_call_ids[key] = leaf.content_id("host-transcript-call", page_identity, ascii=True, allow_nan=False)
        page_host_call_ids[key] = host_id
        duplicate_pages += result_occurrences.get(host_id, 1) - 1

    captures = []
    extraction_calls = []
    for host_id in requested:
        use, result = uses[host_id], results[host_id]
        if use.operation == "delivery.read":
            raise CaptureError("select root calls; delivery pages are joined automatically")
        record: dict[str, Any] = {
            "operation": use.operation, "arguments": use.arguments, "response": result.response,
        }
        initial = _structured(result.response)
        page_ids: list[str] = []
        page_host_ids: list[str] = []
        if initial.get("status") == "continued":
            token = initial.get("token")
            if not isinstance(token, str):
                raise CaptureError(f"continued call {host_id} has no delivery token")
            keys = sorted((key for key in page_by_key if key[0] == token), key=lambda key: key[1])
            record["delivery_pages"] = [page_by_key[key] for key in keys]
            page_ids = [page_call_ids[key] for key in keys]
            page_host_ids = [page_host_call_ids[key] for key in keys]
        capture = capture_call(record, transport=transport, evidence_source="host_transcript")
        identity_material = {
            "session_id": session_id, "host_call_id": host_id,
            "use_record_id": use.record_id, "result_record_id": result.record_id,
            "delivery_page_call_ids": page_ids,
        }
        immutable_id = leaf.content_id("host-transcript-call", identity_material, ascii=True, allow_nan=False)
        capture["host_transcript_provenance"] = {
            "call_id": immutable_id, "host_call_id": host_id,
            "use_record_id": use.record_id, "result_record_id": result.record_id,
            "delivery_page_call_ids": page_ids,
            "delivery_page_host_call_ids": page_host_ids,
        }
        captures.append(capture)
        extraction_calls.append({"call_id": immutable_id, "host_call_id": host_id,
                                 "summary": compact_summary(capture)})

    material = {
        "schema": "ops.host-transcript-extraction.v1",
        "session_id": session_id,
        "transcript_sha256": _sha(raw),
        "selected_host_call_ids": requested,
        "calls": captures,
    }
    return {
        "schema": "ops.host-transcript-extraction.v1",
        "evidence_class": "host_transcript_derived",
        "wire_attestation": False,
        "proof_scope": {
            "establishes": "Selected host transcript records were deterministically extracted and sanitized.",
            "excludes": [
                "wire-level transport attestation", "unselected transcript content",
                "claim that model visibility before capture invalidates the transcript record",
            ],
        },
        "session": {"id": session_id, "selected_record_count": selected_rows},
        "transcript": {"sha256": _sha(raw), "bytes": len(raw), "line_count": len(lines),
                       "raw_persisted": False},
        "extraction_sha256": leaf.canonical_sha256(material, ascii=True, allow_nan=False),
        "calls": extraction_calls,
        "captures": captures,
        "omissions": {
            "other_session_records": omitted_sessions,
            "unbound_session_records": omitted_unbound_records,
            "unselected_tool_calls": len([key for key, use in uses.items()
                                          if use.operation != "delivery.read" and key not in requested]),
            "duplicate_delivery_pages": duplicate_pages,
            "malformed_records": malformed_rows,
            "raw_transcript": "not_persisted",
        },
        "failures": [],
    }


def extract_host_transcript_file(
    path: Path, *, session_id: str, call_ids: Sequence[str], transport: str = "native_mcp"
) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_TRANSCRIPT_BYTES + 1)
    if len(raw) > MAX_TRANSCRIPT_BYTES:
        raise CaptureError("host transcript exceeds byte limit")
    return extract_host_transcript(raw, session_id=session_id,
                                   call_ids=call_ids, transport=transport)
