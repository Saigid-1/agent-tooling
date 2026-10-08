#!/usr/bin/env python3
"""No published image carries @anthropic-ai/claude-agent-sdk (order D0f, falsifier F1).

    python tests/image/agent_sdk_absence.py [--expect-board] IMAGE [IMAGE ...]

IMAGE is any reference the local Docker daemon holds: a tag, or `repo@sha256:<digest>`
for a pushed image (D0b's publish job pulls the digest, then runs this file on it).
Exit 0: every image is clean and every instrument control held. Exit 1: an image
carries the SDK. Exit 2: the check could not run or a control failed, so a clean
result would prove nothing.

What it reads. `docker save IMAGE` streams the image as published: every layer blob,
the image config and the manifests. Each layer is read as the tar it is, so a file
that a later layer deletes is still read, because the published layer still carries
it. Nothing from the image is executed. Inside every layer it reads every regular
file, every gzip stream (an npm cache `.tgz`, a `.gz`) decompressed, and every zip
member (a wheel, a jar). Standard library only (Python 3.11+); a zstd layer, which
the standard library cannot read before Python 3.14, is reported as "cannot run".

A finding is any of:

- `package-file`: a regular file under an `@anthropic-ai/claude-agent-sdk` package
  directory, or under one of its platform packages (`claude-agent-sdk-<platform>`);
- `source-map`: a source map whose `sources` list a file of that package (the bundle
  measured at main lists `../node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs`);
- `marker`: a file whose bytes contain one of `MARKERS`, strings of the SDK's own code
  in `sdk.mjs` 0.2.128 that no other component of these images is known to contain.
  The package NAME is deliberately not a marker: an install action, its notice and
  NOTICE name the package without carrying its code.

Instrument controls (exit 2 when one fails; a clean result needs all of them):

- at least one layer and one regular file were read;
- the build identity file was read and its content contains its schema string, so
  file contents, not only names, were scanned;
- `--expect-board` (product, ops): the board bundle `/app/dist/cli.js` was read and its
  source map parsed, with `src/cli.ts` among its sources, so the place where the SDK was
  measured is the place that was scanned.
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import re
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass, field
from typing import IO, Iterable

# Strings of the SDK's own code (sdk.mjs of @anthropic-ai/claude-agent-sdk 0.2.128).
# Each is present in that file and in the board bundle measured at D0f's base,
# and absent from the board bundle's own sources.
MARKERS: tuple[bytes, ...] = (
    b"CLAUDE_AGENT_SDK_VERSION",                   # env var query() sets for its child
    b"ProcessTransport is not ready for writing",  # its transport's error text
    b"Failed to spawn Claude Code process",        # its spawn error text
    b"CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH",          # env var it passes to the CLI
    b"DEBUG_CLAUDE_AGENT_SDK",                     # its debug switch
)
SOURCE_PACKAGE = "@anthropic-ai/claude-agent-sdk/"
PACKAGE_DIRECTORY = re.compile(r"(?:^|/)@anthropic-ai/claude-agent-sdk(?:-[A-Za-z0-9_.-]+)?/")
IDENTITY_PATH = "/usr/local/share/agent-tooling-build-identity.json"
IDENTITY_CONTROL = b"agent-tooling.build-identity.v1"
BOARD_BUNDLE = "/app/dist/cli.js"
BOARD_MAP = "/app/dist/cli.js.map"
BOARD_ENTRY_SOURCE = "src/cli.ts"

CHUNK = 1 << 20
OVERLAP = max(len(marker) for marker in MARKERS) - 1
MAX_NESTED_BYTES = 2 << 30   # decompressed bytes read from one nested gzip stream
MAX_ZIP_BYTES = 512 << 20    # a zip is read into memory (zipfile needs to seek)
MAX_MAP_BYTES = 512 << 20
GZIP_MAGIC = b"\x1f\x8b"
ZIP_MAGIC = b"PK\x03\x04"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class CannotRun(Exception):
    """The instrument could not read what it needs, so no result can be stated."""


@dataclass(frozen=True)
class Finding:
    kind: str     # package-file | source-map | marker
    layer: str    # the layer blob (or "blob") the file came from
    path: str     # the file, "outer!inner" for a file inside an archive
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.path} [{self.layer}] {self.detail}"


@dataclass
class Report:
    image: str
    findings: list[Finding] = field(default_factory=list)
    layers: int = 0
    files: int = 0
    bytes: int = 0
    nested: int = 0
    maps_parsed: int = 0
    identity_read: bool = False
    board_bundle_read: bool = False
    board_map_entry: bool = False
    board_map_sources: int = 0

    def controls(self, expect_board: bool) -> list[str]:
        """The failed instrument controls; empty when every control held."""
        failed = []
        if self.layers < 1 or self.files < 1:
            failed.append(f"read {self.layers} layers and {self.files} regular files")
        if not self.identity_read:
            failed.append(f"the content of {IDENTITY_PATH} was not read with {IDENTITY_CONTROL.decode()!r} in it")
        if expect_board and not self.board_bundle_read:
            failed.append(f"the board bundle {BOARD_BUNDLE} was not read")
        if expect_board and not self.board_map_entry:
            failed.append(f"{BOARD_MAP} was not parsed with {BOARD_ENTRY_SOURCE!r} among its "
                          f"{self.board_map_sources} sources")
        return failed

    def summary(self) -> str:
        return (f"{self.image}: {len(self.findings)} findings; read {self.layers} layers, {self.files} files, "
                f"{self.bytes} bytes, {self.nested} nested archives, {self.maps_parsed} source maps")


class _Peek:
    """A forward-only stream whose first bytes can be inspected before it is consumed."""

    def __init__(self, raw: IO[bytes], size: int = 512):
        self._raw = raw
        self.head = self._read_exact(size)
        self._offset = 0

    def _read_exact(self, size: int) -> bytes:
        parts, wanted = [], size
        while wanted > 0:
            part = self._raw.read(wanted)
            if not part:
                break
            parts.append(part)
            wanted -= len(part)
        return b"".join(parts)

    def read(self, size: int = -1) -> bytes:
        if self._offset < len(self.head):
            if size is None or size < 0:
                out = self.head[self._offset:] + self._raw.read()
                self._offset = len(self.head)
                return out
            out = self.head[self._offset:self._offset + size]
            self._offset += len(out)
            if len(out) < size:
                out += self._raw.read(size - len(out))
            return out
        return self._raw.read(size)


def _absolute(name: str) -> str:
    """A layer member's name as the absolute path it has in the image."""
    while name.startswith("./"):
        name = name[2:]
    return "/" + name.lstrip("/")


def _is_tar(head: bytes) -> bool:
    return len(head) >= 262 and head[257:262] == b"ustar"


def _markers_in(stream: IO[bytes], limit: int | None = None) -> tuple[set[bytes], int, bytes]:
    """The markers found in a stream, the bytes read, and the stream's first bytes."""
    found: set[bytes] = set()
    tail, total, head = b"", 0, b""
    while True:
        want = CHUNK if limit is None else min(CHUNK, limit - total)
        if want <= 0:
            break
        chunk = stream.read(want)
        if not chunk:
            break
        if not head:
            head = chunk[:8]
        total += len(chunk)
        window = tail + chunk
        for marker in MARKERS:
            if marker not in found and marker in window:
                found.add(marker)
        tail = window[-OVERLAP:]
    return found, total, head


class Scanner:
    def __init__(self, image: str):
        self.report = Report(image)

    # -- findings -------------------------------------------------------------------
    def _add(self, kind: str, layer: str, path: str, detail: str) -> None:
        self.report.findings.append(Finding(kind, layer, path, detail))

    def _markers(self, layer: str, path: str, found: Iterable[bytes]) -> None:
        for marker in sorted(found):
            self._add("marker", layer, path, f"contains {marker.decode()!r}")

    # -- one file -------------------------------------------------------------------
    def scan_file(self, layer: str, path: str, data: IO[bytes], size: int, depth: int = 0) -> None:
        """Scan one regular file; nested gzip and zip content is scanned one level deep."""
        if PACKAGE_DIRECTORY.search(path):
            self._add("package-file", layer, path, "a file of the claude-agent-sdk package")
        peek = _Peek(data, 8)
        head = peek.head
        if depth == 0 and head.startswith(GZIP_MAGIC):
            self._scan_gzip(layer, path, peek)
            return
        if depth == 0 and head.startswith(ZIP_MAGIC) and size <= MAX_ZIP_BYTES:
            self._scan_zip(layer, path, peek.read())
            return
        if path.endswith(".map") and size <= MAX_MAP_BYTES:
            content = peek.read()
            self.report.bytes += len(content)
            self._markers(layer, path, {m for m in MARKERS if m in content})
            self._scan_map(layer, path, content)
            return
        found, total, _ = _markers_in(peek)
        self.report.bytes += total
        self._markers(layer, path, found)

    def _scan_map(self, layer: str, path: str, content: bytes) -> None:
        try:
            document = json.loads(content)
        except ValueError:
            return
        sources = document.get("sources") if isinstance(document, dict) else None
        if not isinstance(sources, list):
            return
        self.report.maps_parsed += 1
        listed = sorted({s for s in sources if isinstance(s, str) and SOURCE_PACKAGE in s})
        for source in listed:
            self._add("source-map", layer, path, f"lists source {source}")
        if path == BOARD_MAP:
            self.report.board_map_sources = len(sources)
            if any(isinstance(s, str) and s.endswith(BOARD_ENTRY_SOURCE) for s in sources):
                self.report.board_map_entry = True

    def _scan_gzip(self, layer: str, path: str, stream: IO[bytes]) -> None:
        self.report.nested += 1
        try:
            inner = gzip.GzipFile(fileobj=stream)
            peek = _Peek(inner, 512)
            if _is_tar(peek.head):
                with tarfile.open(fileobj=peek, mode="r|") as archive:
                    for member in archive:
                        name = f"{path}!{member.name}"
                        if member.isfile():
                            handle = archive.extractfile(member)
                            if handle is not None:
                                self.scan_file(layer, name, handle, member.size, depth=1)
            else:
                found, total, _ = _markers_in(peek, MAX_NESTED_BYTES)
                self.report.bytes += total
                self._markers(layer, f"{path}!(gunzipped)", found)
        except (OSError, EOFError, tarfile.TarError, gzip.BadGzipFile):
            # Not a readable gzip stream after all; scan the raw bytes it had left.
            found, total, _ = _markers_in(stream)
            self.report.bytes += total
            self._markers(layer, path, found)

    def _scan_zip(self, layer: str, path: str, content: bytes) -> None:
        self.report.nested += 1
        self.report.bytes += len(content)
        self._markers(layer, path, {m for m in MARKERS if m in content})
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    with archive.open(info) as handle:
                        self.scan_file(layer, f"{path}!{info.filename}", handle, info.file_size, depth=1)
        except (zipfile.BadZipFile, OSError, EOFError):
            pass

    # -- one layer and the whole image ------------------------------------------------
    def scan_layer(self, layer: str, archive: tarfile.TarFile) -> None:
        self.report.layers += 1
        for member in archive:
            if not member.isfile():
                continue
            path = _absolute(member.name)
            handle = archive.extractfile(member)
            if handle is None:
                continue
            self.report.files += 1
            if path == IDENTITY_PATH:
                content = handle.read()
                self.report.bytes += len(content)
                self._markers(layer, path, {m for m in MARKERS if m in content})
                if IDENTITY_CONTROL in content:
                    self.report.identity_read = True
                continue
            if path == BOARD_BUNDLE:
                self.report.board_bundle_read = True
            self.scan_file(layer, path, handle, member.size)

    def scan_saved(self, stream: IO[bytes]) -> Report:
        """Scan the output of `docker save` (OCI layout or docker-archive)."""
        try:
            with tarfile.open(fileobj=stream, mode="r|") as saved:
                for member in saved:
                    if not member.isfile():
                        continue
                    blob = saved.extractfile(member)
                    if blob is None:
                        continue
                    self._scan_blob(member.name, blob)
        except tarfile.TarError as error:
            raise CannotRun(f"the saved image is not a readable tar stream: {error}") from error
        return self.report

    def _scan_blob(self, name: str, blob: IO[bytes]) -> None:
        peek = _Peek(blob, 512)
        head = peek.head
        if head.startswith(ZSTD_MAGIC):
            raise CannotRun(f"layer {name} is zstd-compressed; this Python cannot read it")
        if head.startswith(GZIP_MAGIC):
            inner = _Peek(gzip.GzipFile(fileobj=peek), 512)
            if _is_tar(inner.head):
                with tarfile.open(fileobj=inner, mode="r|") as layer:
                    self.scan_layer(name, layer)
                return
            found, total, _ = _markers_in(inner, MAX_NESTED_BYTES)
            self.report.bytes += total
            self._markers("blob", name, found)
            return
        if _is_tar(head):
            with tarfile.open(fileobj=peek, mode="r|") as layer:
                self.scan_layer(name, layer)
            return
        found, total, _ = _markers_in(peek)
        self.report.bytes += total
        self._markers("blob", name, found)


def scan_image(image: str, *, timeout: float = 1800) -> Report:
    """`docker save IMAGE`, scanned as it streams. The image must be present locally."""
    try:
        inspect = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                                 capture_output=True, text=True, timeout=120)
    except FileNotFoundError as error:
        raise CannotRun("the docker CLI is not on PATH") from error
    if inspect.returncode:
        raise CannotRun(f"image {image!r} is not present locally: {inspect.stderr.strip()[:300]}")
    process = subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    scanner = Scanner(image)
    try:
        scanner.scan_saved(process.stdout)
        # Drain anything the tar reader did not consume, so `docker save` can finish.
        while process.stdout.read(CHUNK):
            pass
        stderr = process.stderr.read().decode("utf-8", "replace")
        code = process.wait(timeout=timeout)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    if code:
        raise CannotRun(f"`docker save {image}` exited {code}: {stderr.strip()[:500]}")
    return scanner.report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("images", nargs="+", metavar="IMAGE")
    parser.add_argument("--expect-board", action="store_true",
                        help="require the board bundle and its source map to have been read (product, ops)")
    args = parser.parse_args(argv)
    status = 0
    for image in args.images:
        try:
            report = scan_image(image)
        except CannotRun as error:
            print(f"agent_sdk_absence: cannot run on {image}: {error}", file=sys.stderr)
            status = max(status, 2)
            continue
        print(report.summary())
        failed = report.controls(args.expect_board)
        for line in failed:
            print(f"  control failed: {line}", file=sys.stderr)
        for finding in report.findings:
            print(f"  FOUND {finding}")
        if report.findings:
            status = max(status, 1)
        if failed:
            status = 2
    verdict = {0: "clean", 1: "CARRIES claude-agent-sdk", 2: "CANNOT STATE A RESULT"}[status]
    print(f"agent_sdk_absence: {verdict} ({len(args.images)} images)")
    return status


if __name__ == "__main__":
    sys.exit(main())
