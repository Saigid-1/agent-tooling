"""Bounded, lossless JSON delivery for OPS-owned tool transports.

Large results become immutable, content-addressed artifacts. A continuation reads
only a validated digest from this store, never a caller-supplied filesystem path.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from kp_agent_tooling._impl import leaf


_TOKEN = re.compile(r"[0-9a-f]{64}\Z")


class DeliveryStore:
    def __init__(self, root=None, *, max_inline_bytes=12_000, max_page_bytes=4_096,
                 max_stored_bytes=2_000_000, max_response_bytes=None):
        self.root = Path(root) if root is not None else Path(tempfile.gettempdir()) / f"ops-tool-delivery-{os.getuid()}"
        self.max_inline_bytes = max_inline_bytes
        self.max_page_bytes = max_page_bytes
        self.max_response_bytes = max_response_bytes or max_inline_bytes
        self.max_stored_bytes = max_stored_bytes
        if max_inline_bytes < 1024 or not 256 <= max_page_bytes <= self.max_response_bytes // 2:
            raise ValueError("invalid delivery limits")
        if not max_inline_bytes <= self.max_response_bytes <= 100_000:
            raise ValueError("invalid response budget")
        if max_stored_bytes < max_inline_bytes:
            raise ValueError("invalid stored-result limit")
        leaf.mkdir_private(self.root, parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("delivery root must be a directory")
        leaf.chmod_private(self.root, directory=True)

    @staticmethod
    def encode(value):
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")

    def deliver(self, value, *, force=False):
        raw = self.encode(value)
        if len(raw) <= self.max_inline_bytes and not force:
            return value
        if len(raw) > self.max_stored_bytes:
            return {"status": "error", "reason": "delivery_result_oversized",
                    "result_bytes": len(raw), "max_stored_bytes": self.max_stored_bytes,
                    "sha256": hashlib.sha256(raw).hexdigest()}
        digest = hashlib.sha256(raw).hexdigest()
        path = self.root / digest
        if not path.exists():
            leaf.publish_once(path, raw, temp_prefix=".pending-", temp_dir=self.root, chmod_mode=0o400,
                              cleanup="if-exists")
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("delivery integrity failure")
        return {
            "status": "continued", "delivery": "ops.json-bytes.v1",
            "sha256": digest, "token": digest, "total_bytes": len(raw),
            "page_size_bytes": self.max_page_bytes,
            "next_call": {"name": "delivery.read", "arguments": {"token": digest, "offset": 0}},
            "reassembly": "Concatenate text pages by offset; verify ASCII byte count and sha256; parse JSON. A fragment alone is not evidence.",
        }

    def read(self, token, offset=0, limit=None):
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("invalid delivery token")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("invalid delivery offset")
        if limit is None:
            limit = self.max_page_bytes
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self.max_page_bytes:
            raise ValueError("invalid delivery limit")
        path = self.root / token
        if path.is_symlink() or not path.is_file():
            raise ValueError("delivery unavailable")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != token:
            raise ValueError("delivery integrity failure")
        if offset > len(raw):
            raise ValueError("offset beyond delivery")
        while True:
            part = raw[offset:offset + limit]
            following = offset + len(part)
            page = {
                "status": "ok", "delivery": "ops.json-bytes.v1", "token": token,
                "sha256": token, "total_bytes": len(raw), "offset": offset,
                "bytes": len(part), "text": part.decode("ascii"),
                "next_offset": following if following < len(raw) else None,
                "complete": following == len(raw),
            }
            if len(self.encode(page)) <= self.max_response_bytes:
                return page
            if limit == 1:
                raise ValueError("delivery limit too small for page metadata")
            limit = max(1, limit // 2)
