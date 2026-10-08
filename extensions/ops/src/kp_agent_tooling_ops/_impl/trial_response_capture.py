"""Bounded, sanitized capture of MCP trial responses.

The capture keeps one canonical JSON result per call.  MCP's text mirror is recorded
as an omission when it is byte-for-byte equivalent to ``structuredContent``; delivery
page text is verified and reassembled before it is retained once.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Mapping, Sequence

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.leaf import sha256_hex as _sha


MAX_CALL_BYTES = 2_000_000
_SECRET_KEYS = re.compile(
    r"(?:^|_)(?:api_?key|authorization|cookie|credential|password|secret|token)(?:$|_)",
    re.IGNORECASE,
)
_BULK_KEYS = re.compile(
    r"^(?:text|excerpt|content|messages|transcript|prompt|.*_(?:text|content))$",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")


class CaptureError(ValueError):
    """The supplied response cannot produce a complete trustworthy capture."""


def _canonical(value: Any) -> bytes:
    return leaf.canonical_bytes(value, ascii=True, allow_nan=False)


@dataclass
class _Sanitizer:
    omissions: list[dict[str, Any]]

    def apply(self, value: Any, path: str = "") -> Any:
        if isinstance(value, Mapping):
            result = {}
            for key, child in value.items():
                name = str(key)
                child_path = f"{path}/{name}"
                if _SECRET_KEYS.search(name):
                    result[name] = "[REDACTED]"
                    self.omissions.append({"path": child_path, "reason": "sensitive_field_redacted"})
                elif _BULK_KEYS.search(name) and isinstance(child, (str, list)):
                    result[name] = "[OMITTED]"
                    child_bytes = len(_canonical(child)) if isinstance(child, list) else len(child.encode())
                    self.omissions.append({
                        "path": child_path, "reason": "bulk_source_or_transcript_omitted",
                        "utf8_bytes": child_bytes,
                    })
                else:
                    result[name] = self.apply(child, child_path)
            return result
        if isinstance(value, list):
            return [self.apply(child, f"{path}/{index}") for index, child in enumerate(value)]
        if isinstance(value, str):
            replaced = _BEARER.sub("Bearer [REDACTED]", value)
            if replaced != value:
                self.omissions.append({"path": path, "reason": "bearer_credential_redacted"})
            return replaced
        return value


def _response_parts(response: Mapping[str, Any]) -> tuple[Any, list[Mapping[str, Any]]]:
    structured = response.get("structuredContent")
    content = response.get("content") or []
    if not isinstance(content, list):
        raise CaptureError("response content must be a list")
    if structured is None:
        texts = [row.get("text", "") for row in content
                 if isinstance(row, Mapping) and row.get("type") == "text"]
        if not texts:
            raise CaptureError("response has no structuredContent or text JSON")
        try:
            structured = json.loads("".join(texts))
        except json.JSONDecodeError as exc:
            raise CaptureError("response text is not JSON") from exc
    if not isinstance(structured, Mapping):
        raise CaptureError("response result must be an object")
    return structured, content


def _text_mirror(content: Sequence[Mapping[str, Any]]) -> str | None:
    if not content or any(not isinstance(row, Mapping) or row.get("type") != "text" for row in content):
        return None
    return "".join(str(row.get("text", "")) for row in content)


def _delivery_payload(initial: Mapping[str, Any], pages: Sequence[Mapping[str, Any]]) -> tuple[Any, list[dict[str, Any]]]:
    total = initial.get("total_bytes")
    token, expected_sha = initial.get("token"), initial.get("sha256")
    if (type(total) is not int or not 0 < total <= MAX_CALL_BYTES
            or not isinstance(token, str) or token != expected_sha
            or not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha)):
        raise CaptureError("invalid delivery size")
    offset, chunks, page_receipts = 0, [], []
    for page_response in pages:
        if page_response.get("isError"):
            raise CaptureError("errored delivery page")
        page, _ = _response_parts(page_response)
        text = page.get("text")
        if not isinstance(text, str):
            raise CaptureError("delivery page text missing")
        raw = text.encode("ascii")
        end = offset + len(raw)
        if (page.get("status") != "ok" or page.get("token") != token
                or page.get("sha256") != expected_sha or page.get("total_bytes") != total
                or page.get("offset") != offset or page.get("bytes") != len(raw) or not raw
                or end > total or page.get("next_offset") != (end if end < total else None)
                or page.get("complete") != (end == total)):
            raise CaptureError("inconsistent delivery page")
        chunks.append(raw)
        page_receipts.append({
            "offset": offset, "bytes": len(raw), "next_offset": page.get("next_offset"),
            "complete": page.get("complete"), "chunk_sha256": _sha(raw),
            "text_omitted": "reassembled_payload_retained_once",
        })
        offset = end
    if offset != total:
        raise CaptureError("incomplete delivery pages")
    raw = b"".join(chunks)
    if _sha(raw) != expected_sha:
        raise CaptureError("delivery digest mismatch")
    try:
        return json.loads(raw), page_receipts
    except json.JSONDecodeError as exc:
        raise CaptureError("reassembled delivery is not JSON") from exc


def capture_call(
    record: Mapping[str, Any], *, transport: str, evidence_source: str = "direct_export"
) -> dict[str, Any]:
    """Return one deterministic capture artifact from an exported tool-call record."""
    if transport not in {"native_mcp", "facade_cli", "local_mcp"}:
        raise CaptureError("unsupported transport provenance")
    if evidence_source not in {"direct_export", "host_transcript"}:
        raise CaptureError("unsupported evidence source")
    operation = record.get("operation")
    response = record.get("response")
    if not isinstance(operation, str) or not operation or not isinstance(response, Mapping):
        raise CaptureError("operation and response are required")
    initial, content = _response_parts(response)
    if response.get("isError") and initial.get("status") == "continued":
        raise CaptureError("errored response cannot begin continued delivery")
    pages = record.get("delivery_pages", [])
    if not isinstance(pages, list):
        raise CaptureError("delivery_pages must be a list")
    page_receipts: list[dict[str, Any]] = []
    result = initial
    if initial.get("status") == "continued":
        result, page_receipts = _delivery_payload(initial, pages)
    elif pages:
        raise CaptureError("delivery pages supplied for a non-continued response")

    original_result_raw, original_sha256 = leaf.canonical_bytes_sha256(result, ascii=True, allow_nan=False)
    if len(original_result_raw) > MAX_CALL_BYTES:
        raise CaptureError("original result exceeds capture limit")
    sanitizer = _Sanitizer([])
    arguments = sanitizer.apply(record.get("arguments", {}), "/arguments")
    sanitized_result = sanitizer.apply(result, "/result")
    result_raw, result_sha256 = leaf.canonical_bytes_sha256(sanitized_result, ascii=True, allow_nan=False)
    if len(result_raw) > MAX_CALL_BYTES:
        raise CaptureError("sanitized result exceeds capture limit")

    mirror = _text_mirror(content)
    mirror_receipt: dict[str, Any]
    if mirror is not None:
        try:
            duplicate = _canonical(json.loads(mirror)) == _canonical(initial)
        except json.JSONDecodeError:
            duplicate = False
        if duplicate:
            mirror_receipt = {"status": "omitted_duplicate", "sha256": _sha(mirror.encode()),
                              "utf8_bytes": len(mirror.encode())}
        else:
            mirror_receipt = {"status": "omitted_noncanonical", "utf8_bytes": len(mirror.encode())}
            sanitizer.omissions.append({"path": "/response/content", "reason": "noncanonical_text_omitted"})
    else:
        mirror_receipt = {"status": "absent_or_nontext"}

    return {
        "schema": "ops.trial-response-capture.v1",
        "operation": operation,
        "transport": transport,
        "transport_provenance": {
            "declared_by_operator": True,
            "capture_attests_transport": False,
            "note": "A native export remains native evidence; facade replay is a separate call and cannot prove native pagination.",
        },
        "arguments": arguments,
        # Absence is evidence of an unknown host field, not evidence of success.
        "is_error": response.get("isError") if "isError" in response else None,
        "evidence_source": {
            "kind": evidence_source,
            "wire_attestation": False,
            "note": (
                "Extracted host transcript evidence; transcript observation and wire "
                "attestation are separate proof scopes."
                if evidence_source == "host_transcript"
                else "Capture preserves an exported response but does not attest the wire."
            ),
        },
        "result": sanitized_result,
        "result_integrity": {
            "original_canonical_bytes": len(original_result_raw), "original_sha256": original_sha256,
            "persisted_sanitized_canonical_bytes": len(result_raw),
            "persisted_sanitized_sha256": result_sha256,
        },
        "delivery": {
            "continued": initial.get("status") == "continued", "pages": page_receipts,
            "page_count": len(page_receipts), "complete": initial.get("status") != "continued" or bool(page_receipts),
            "wire_sha256": initial.get("sha256") if initial.get("status") == "continued" else None,
            "wire_total_bytes": initial.get("total_bytes") if initial.get("status") == "continued" else None,
            "wire_integrity_verified": initial.get("status") == "continued" and bool(page_receipts),
        },
        "text_mirror": mirror_receipt,
        "omissions": sanitizer.omissions,
    }


def compact_summary(capture: Mapping[str, Any]) -> dict[str, Any]:
    result = capture["result"]
    keys = sorted(result) if isinstance(result, Mapping) else []
    return {
        "operation": capture["operation"], "transport": capture["transport"],
        "is_error": capture["is_error"], "status": result.get("status") if isinstance(result, Mapping) else None,
        "evidence_source": capture.get("evidence_source", {}).get("kind", "unknown"),
        "original_result_sha256": capture["result_integrity"]["original_sha256"],
        "persisted_result_sha256": capture["result_integrity"]["persisted_sanitized_sha256"],
        "persisted_result_bytes": capture["result_integrity"]["persisted_sanitized_canonical_bytes"],
        "result_keys": keys[:24], "result_key_count": len(keys),
        "delivery_pages": capture["delivery"]["page_count"],
        "delivery_complete": capture["delivery"]["complete"],
        "text_mirror": capture["text_mirror"]["status"],
        "omission_count": len(capture["omissions"]),
    }
