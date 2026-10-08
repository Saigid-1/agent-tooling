"""D0b R7 (image-marked): no CLI and no SDK in any pushed image, checked on the pushed digest.

Order: docs/work/orders/D0b-release-workflow.md, R7, run at R6 step 2 on each runner. Each published target
(runtime, product, ops, opencode) is named by its image-suite variable, which must be a digest reference
`<registry>/<name>@sha256:<64 hex>` present locally (pulled): a tag is not what was pushed, so it FAILS.

Per digest, two checks:
1. D0f's reusable falsifier, `tests/image/agent_sdk_absence.py` `scan_image(image) -> Report`: every layer
   (files a later layer deletes included, gzip streams and zip members) holds no claude-agent-sdk file,
   source map or marker, with its instrument controls held (`--expect-board` for product, ops, opencode).
2. A file-system listing of every layer of the digest (`docker save`, names only, whiteouts kept): no path
   under an `@anthropic-ai/claude-code` or `@openai/codex` package (or their platform packages) and no file
   named `claude` or `codex`; and, in a container of the digest, no `claude` or `codex` on PATH.
   The listing's positive controls: the build-identity file, an npm package directory (`node-pty`) in the
   board targets, the Python package in runtime, and for opencode its own package. On PATH, a binary the
   target must carry (kp-agent-tooling, kanban, opencode) resolves.
For opencode (O3's positive controls, so the check is not vacuous for the optional target): `opencode` on
PATH answers O3's pin with no network, and its upstream LICENSE is at its path with O3's recorded sha256.

Exit 2 of the check ("cannot state a result") and any unreadable layer are failures, never skips.
"""
from __future__ import annotations

import gzip
import io
import re
import subprocess
import tarfile
from dataclasses import dataclass, field

import pytest

from agent_sdk_absence import CannotRun, scan_image
from d0b_seams import BOARD_TARGETS, IMAGE_VARIABLES, PUBLISHED_TARGETS
from image_harness import required, resolve_on_path, run_once, sh
from opencode_pin import OPENCODE_LICENSE_PATH, OPENCODE_LICENSE_SHA256, OPENCODE_VERSION

pytestmark = pytest.mark.image

DIGEST_REFERENCE = re.compile(r"[^\s@/]+(?:/[^\s@/]+)+@sha256:[0-9a-f]{64}")
CLI_PACKAGE = re.compile(r"(?:^|/)@(?:anthropic-ai/claude-code|openai/codex)(?:-[A-Za-z0-9_.-]+)?(?:/|$)")
CLI_FILE = re.compile(r"(?:^|/)(?:\.wh\.)?(?:claude|codex)$")
IDENTITY = "usr/local/share/agent-tooling-build-identity.json"
LISTING_CONTROLS = {
    "runtime": re.compile(r"(?:^|/)site-packages/kp_agent_tooling/__init__\.py$"),
    "product": re.compile(r"(?:^|/)app/node_modules/node-pty/package\.json$"),
    "ops": re.compile(r"(?:^|/)app/node_modules/node-pty/package\.json$"),
    "opencode": re.compile(r"(?:^|/)opt/opencode/node_modules/opencode-ai/package\.json$"),
}
PATH_CONTROL = {"runtime": "kp-agent-tooling", "product": "kanban", "ops": "kanban", "opencode": "opencode"}


def pushed_digest(target: str) -> str:
    """The target's image as a digest reference; FAILS on an unset variable or any other reference."""
    variable = IMAGE_VARIABLES[target]
    ref = required(variable)
    if not DIGEST_REFERENCE.fullmatch(ref):
        pytest.fail(f"{variable}={ref!r} is not a digest reference <registry>/<name>@sha256:<64 hex>: R7 checks what "
                    "was pushed, by digest (D0b R6 step 2)", pytrace=False)
    return ref


# ----------------------------------------------------------------- the layer listing


class _Prefixed(io.RawIOBase):
    """A stream that first yields `head`, then the rest of `raw` (a peeked stream, re-joined)."""

    def __init__(self, head: bytes, raw):
        self._head, self._raw = head, raw

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if self._head:
            n = min(len(buffer), len(self._head))
            buffer[:n], self._head = self._head[:n], self._head[n:]
            return n
        data = self._raw.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)


@dataclass
class Listing:
    layers: int = 0
    names: list = field(default_factory=list)    # (layer blob, path, is a directory)
    unreadable: list = field(default_factory=list)


def _read_exactly(stream, size: int) -> bytes:
    parts, total = [], 0
    while total < size:
        data = stream.read(size - total)
        if not data:
            break
        parts.append(data)
        total += len(data)
    return b"".join(parts)


def layer_listing(ref: str, timeout: float = 3600) -> Listing:
    """Every path in every layer of `docker save REF`, read as a stream; contents are not kept. A gzip layer is
    decompressed; a zstd layer, which the standard library cannot read, is reported unreadable."""
    process = subprocess.Popen(["docker", "save", ref], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    listing = Listing()
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as outer:
            for member in outer:
                if not member.isfile():
                    continue
                blob = outer.extractfile(member)
                head = _read_exactly(blob, 512)
                stream = _Prefixed(head, blob)
                if head[:2] == b"\x1f\x8b":
                    unzipped = gzip.GzipFile(fileobj=io.BufferedReader(stream, 1 << 20))
                    try:
                        head = _read_exactly(unzipped, 512)
                    except (OSError, EOFError) as error:
                        listing.unreadable.append(f"{member.name}: {error}")
                        continue
                    stream = _Prefixed(head, unzipped)
                elif head[:4] == b"\x28\xb5\x2f\xfd":
                    listing.unreadable.append(f"{member.name}: zstd layer")
                    continue
                if len(head) < 262 or head[257:262] != b"ustar":
                    continue  # a config or manifest blob, not a layer
                listing.layers += 1
                try:
                    with tarfile.open(fileobj=io.BufferedReader(stream, 1 << 20), mode="r|") as layer:
                        for entry in layer:
                            listing.names.append((member.name, entry.name.removeprefix("./"), entry.isdir()))
                except (tarfile.TarError, OSError, EOFError) as error:
                    listing.unreadable.append(f"{member.name}: {error}")
        while process.stdout.read(1 << 20):
            pass
        code = process.wait(timeout=timeout)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    if code:
        pytest.fail(f"`docker save {ref}` exited {code}: {process.stderr.read().decode('utf-8', 'replace')[:500]}",
                    pytrace=False)
    return listing


# ----------------------------------------------------------------- tests


@pytest.mark.parametrize("target", PUBLISHED_TARGETS)
def test_r7_scan_image_finds_no_agent_sdk_in_the_pushed_digest(target):
    """GREEN-IF the target's variable is a digest reference present locally, and D0f's `scan_image` on it states a
    result (no CannotRun) with every instrument control held (the identity file read; for product, ops and opencode
    the board bundle and its source map read) and no finding of claude-agent-sdk in any layer."""
    ref = pushed_digest(target)
    try:
        report = scan_image(ref)
    except CannotRun as error:
        pytest.fail(f"the check cannot state a result for {target} ({ref}): {error}", pytrace=False)
    failed = report.controls(target in BOARD_TARGETS)
    assert not failed, f"{target}: instrument controls failed, so a clean result proves nothing: {failed}"
    assert not report.findings, f"{target} ({ref}) carries claude-agent-sdk:\n" + "\n".join(map(str, report.findings[:40]))


@pytest.mark.parametrize("target", PUBLISHED_TARGETS)
def test_r7_no_claude_code_or_codex_file_in_any_layer_of_the_pushed_digest(target):
    """GREEN-IF the target's variable is a digest reference, every layer of `docker save` of it is read (none zstd or
    torn), the listing holds its positive controls (the build-identity file, and the target's known package path),
    and no path in any layer is under an `@anthropic-ai/claude-code` or `@openai/codex` package (or a platform
    package of either) or is a file named `claude` or `codex` (or its whiteout)."""
    ref = pushed_digest(target)
    listing = layer_listing(ref)
    assert not listing.unreadable, f"layers of {ref} that could not be listed: {listing.unreadable}"
    paths = [path for _, path, _ in listing.names]
    assert listing.layers and IDENTITY in paths, \
        f"positive control: {listing.layers} layers listed and the build-identity file {'' if IDENTITY in paths else 'not '}seen"
    assert any(LISTING_CONTROLS[target].search(p) for p in paths), \
        f"positive control: no path matching {LISTING_CONTROLS[target].pattern} in {target}, so an absence proves nothing"
    found = [f"{layer}: {path}" for layer, path, is_dir in listing.names
             if CLI_PACKAGE.search(path) or (CLI_FILE.search(path) and not is_dir)]
    assert not found, f"{target} ({ref}) carries an agent CLI in its layers:\n" + "\n".join(found[:40])


@pytest.mark.parametrize("target", PUBLISHED_TARGETS)
def test_r7_no_claude_or_codex_on_path_of_the_pushed_digest(target):
    """GREEN-IF, in a container of the pushed digest (its own user, no network), the target's own binary
    (kp-agent-tooling for runtime, kanban for product and ops, opencode for opencode) resolves on PATH, and neither
    `claude` nor `codex` does."""
    ref = pushed_digest(target)
    resolved = resolve_on_path(ref, ["claude", "codex", PATH_CONTROL[target]])
    assert resolved[PATH_CONTROL[target]].startswith("OK "), \
        f"positive control: {PATH_CONTROL[target]} does not resolve on PATH: {resolved[PATH_CONTROL[target]]}"
    present = [line for name, line in resolved.items() if name in ("claude", "codex") and not line.startswith("MISSING ")]
    assert not present, f"{target} ({ref}) has an agent CLI on PATH: {present}"


def test_r7_opencode_digest_answers_its_pin_offline_and_carries_its_licence():
    """GREEN-IF the opencode variable is a digest reference; `opencode --version` in a container of it with no
    network exits 0 and prints O3's pinned version; and its upstream LICENSE is at O3's path with O3's sha256."""
    ref = pushed_digest("opencode")
    result = run_once(ref, ["--version"], entrypoint="opencode", network="none", timeout=180)
    output = (result.stdout + result.stderr).strip()
    assert result.returncode == 0, f"`opencode --version` exited {result.returncode}: {output[-800:]}"
    assert re.search(rf"(?<![\w.]){re.escape(OPENCODE_VERSION)}(?![\w.])", output), \
        f"`opencode --version` answered {output[:300]!r}; O3 pins {OPENCODE_VERSION!r}"
    digest = sh(ref, f"sha256sum {OPENCODE_LICENSE_PATH}", timeout=120)
    assert digest.returncode == 0 and digest.stdout.split()[:1] == [OPENCODE_LICENSE_SHA256], \
        f"{OPENCODE_LICENSE_PATH} in {ref}: exit {digest.returncode}, {digest.stdout.strip()[:120]!r} {digest.stderr.strip()[:200]!r}"
