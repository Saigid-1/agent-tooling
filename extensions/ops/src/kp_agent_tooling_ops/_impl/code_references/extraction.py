"""Byte-exact Markdown reference extraction without model calls."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Collection, Sequence

from .models import ChunkExtraction, ChunkRange, ExtractionResult, ReferenceCandidate

EXTRACTION_POLICY_VERSION = "doc-code-reference-extraction.v1"
_INLINE = re.compile(rb"(?<![`\\])(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_LINK = re.compile(rb"(?<!!)\[[^\]\r\n]*\]\((<?)([^\s)>]+)(>?)\)")
_IMAGE = re.compile(rb"!\[[^\]\r\n]*\]\([^\r\n)]*\)")
_ESCAPED_CODE = re.compile(rb"\\(`+).*?\1")
_HTML_OPEN = re.compile(rb"<[A-Za-z][^>]*>")
_HTML_CLOSE = re.compile(rb"</[A-Za-z][^>]*>")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?\Z")
_LINE_HINT = re.compile(r":L([0-9]+)(?:-([0-9]+))?\Z")
_PATHISH = re.compile(r"(?:[^/]+/)+[^/]+\.[^/.]+(?::[^:]*)?\Z")


def extract_code_references(document_bytes: bytes, *, chunks: Sequence[ChunkRange],
                            registry_view: Collection[str] | None) -> ExtractionResult:
    try:
        document_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return ExtractionResult(EXTRACTION_POLICY_VERSION, "error", "invalid_utf8", (), 0)
    spans: dict[tuple[int, int], tuple[bytes, bool]] = {}
    fence: tuple[bytes, int] | None = None
    html_block = False
    position = 0
    for line in document_bytes.splitlines(keepends=True):
        indent = len(line) - len(line.lstrip(b" "))
        stripped = line[indent:]
        fence_match = re.match(rb"(`{3,}|~{3,})", stripped)
        fence_rest = (stripped[len(fence_match.group(1)):].strip()
                      if fence_match is not None else b"")
        closes = (fence is not None and fence_match is not None
                  and fence_match.group(1)[:1] == fence[0]
                  and len(fence_match.group(1)) >= fence[1] and not fence_rest)
        opens = (fence is None and fence_match is not None
                 and not (fence_match.group(1).startswith(b"`") and b"`" in fence_rest))
        if indent <= 3 and fence_match and (opens or closes):
            marker = fence_match.group(1)
            fence = None if fence is not None else (marker[:1], len(marker))
            position += len(line)
            continue
        if fence is None and indent <= 3 and _HTML_OPEN.match(stripped):
            html_block = _HTML_CLOSE.search(stripped) is None
            position += len(line)
            continue
        if html_block:
            if _HTML_CLOSE.search(stripped):
                html_block = False
            position += len(line)
            continue
        if fence is None and not line.startswith((b"    ", b"\t")):
            link_ranges = ([(m.start(), m.end()) for m in _LINK.finditer(line)]
                           + [(m.start(), m.end()) for m in _IMAGE.finditer(line)]
                           + [(m.start(), m.end()) for m in _ESCAPED_CODE.finditer(line)])
            inline_source = bytearray(line)
            for start, end in link_ranges:
                inline_source[start:end] = b" " * (end - start)
            for match in _INLINE.finditer(bytes(inline_source)):
                raw = line[match.start(2):match.end(2)]
                spans[(position + match.start(2), len(raw))] = (raw, False)
            for match in _LINK.finditer(line):
                spans[(position + match.start(2), len(match.group(2)))] = (match.group(2), True)
        position += len(line)
    candidates = []
    for (offset, length), (raw, is_link) in sorted(spans.items()):
        literal = raw.decode("utf-8")
        candidate = _classify(literal, is_link=is_link, registry_view=registry_view)
        if candidate is None:
            continue
        kind, lookup, hint, repo = candidate
        candidates.append(ReferenceCandidate(kind, offset, length,
            hashlib.sha256(raw).hexdigest(), literal, lookup, hint, repo))
    owned = set()
    per_chunk = []
    for chunk in chunks:
        selected = [c for c in candidates if chunk.byte_offset <= c.ref_byte_offset <
                    chunk.byte_offset + chunk.byte_length]
        owned.update((c.ref_byte_offset, c.ref_byte_length) for c in selected)
        retained, overflow = selected[:40], selected[40:]
        per_chunk.append(ChunkExtraction(chunk.chunk_content_id, tuple(retained), len(selected),
            len(retained), len(overflow), dict(Counter(c.kind for c in overflow))))
    return ExtractionResult(EXTRACTION_POLICY_VERSION, "complete", None, tuple(per_chunk),
        sum((c.ref_byte_offset, c.ref_byte_length) not in owned for c in candidates))


def _classify(literal: str, *, is_link: bool, registry_view: Collection[str] | None):
    value = literal
    hint = None
    malformed_hint = False
    match = _LINE_HINT.search(value)
    if match and "/" in value:
        start, end = int(match.group(1)), int(match.group(2) or match.group(1))
        hint = (start, end)
        value = value[:match.start()]
    elif "/" in value and ":" in value.rsplit("/", 1)[-1]:
        malformed_hint = True
        value = value.rsplit(":", 1)[0]
    prefix = None
    path_value = value
    if "//" in path_value and not path_value.startswith("//"):
        prefix, path_value = path_value.split("//", 1)
    path_like = (bool(_PATHISH.fullmatch(path_value))
                 or (("/" in path_value or "\\" in path_value) and "." in path_value.rsplit("/", 1)[-1])
                 or (is_link and "/" not in path_value and "." in path_value))
    if path_like:
        repo = prefix
        lookup = path_value[2:] if path_value.startswith("./") else path_value
        if malformed_hint:
            hint = (0, 0)
        return "path", lookup, hint, repo
    if is_link:
        return None
    operation_value = value[:-2] if value.endswith("()") else value
    if registry_view is not None and operation_value in registry_view:
        return "operation", operation_value, None, None
    if _IDENT.fullmatch(value) and not re.fullmatch(r"[a-z]+", operation_value):
        return "symbol", operation_value, None, None
    return None
