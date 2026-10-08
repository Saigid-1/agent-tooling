"""Original gapless UTF-8 chunking and immutable chunk identities."""
import re

from kp_agent_tooling._impl import leaf
_HEADING = re.compile(rb"(?m)^#{1,6} ([^\n]+)")

def _chunk_specs(blob: bytes, window_bytes: int):
    headings = list(_HEADING.finditer(blob))
    bounds = sorted({0, len(blob), *(match.start() for match in headings)})
    for start, stop in zip(bounds, bounds[1:]):
        heading = next(
            (
                match.group(1).decode("utf-8").strip()
                for match in reversed(headings)
                if match.start() <= start
            ),
            "",
        )
        offset = start
        while offset < stop:
            end = min(offset + window_bytes, stop)
            while end < stop and blob[end] & 0xC0 == 0x80:
                end -= 1
            if end <= offset:
                raise ValueError("window_bytes cannot contain one UTF-8 scalar")
            yield offset, end - offset, heading
            offset = end

def _chunk_id(repo_key: str, path: str, blob_sha: str, offset: int, length: int) -> str:
    return leaf.deskdoc_chunk_id(repo_key, path, blob_sha, offset, length)
