"""D0f F1: the instrument behind the per-image check (tests/image/agent_sdk_absence.py).

Not image-marked: it runs in the default suite on synthetic saved images, so a clean
image result from the image tests (and from D0b's publish job) rests on an instrument
shown here to see each kind of carrying it claims to see, and to stay silent on the
package's name alone.
"""
from __future__ import annotations

import gzip
import io
import json
import tarfile
import zipfile

import pytest

from agent_sdk_absence import (
    BOARD_BUNDLE, BOARD_MAP, CHUNK, IDENTITY_PATH, MARKERS, CannotRun, Scanner,
)

IDENTITY = json.dumps({"schema_version": "agent-tooling.build-identity.v1", "source_revision": "x"}).encode()
CLEAN_MAP = json.dumps({"version": 3, "sources": ["../src/cli.ts", "../node_modules/ai/dist/index.mjs"],
                        "sourcesContent": ["export {}", "export {}"]}).encode()
SDK_SOURCE = "../node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs"


def _tar(entries: list[tuple]) -> bytes:
    """A tar from (path, bytes) regular files and (path, None, target) symlinks."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for entry in entries:
            info = tarfile.TarInfo(entry[0])
            if entry[1] is None:
                info.type, info.linkname = tarfile.SYMTYPE, entry[2]
                archive.addfile(info)
            else:
                info.size = len(entry[1])
                archive.addfile(info, io.BytesIO(entry[1]))
    return buffer.getvalue()


def _saved(*layers: bytes, compress: bool = True, config: bytes = b'{"architecture":"arm64"}') -> bytes:
    """A `docker save` stream in OCI layout: one blob per layer, a config and an index."""
    blobs = [(f"blobs/sha256/{index:064x}", gzip.compress(layer) if compress else layer)
             for index, layer in enumerate(layers, start=1)]
    blobs.append((f"blobs/sha256/{0xC0FF:064x}", config))
    return _tar([("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'), ("index.json", b'{"manifests":[]}'), *blobs])


def _base(*extra) -> list[tuple]:
    """A clean image: the identity file, the board bundle and its map."""
    return [("usr/local/share/agent-tooling-build-identity.json", IDENTITY),
            ("app/dist/cli.js", b"#!/usr/bin/env node\nconsole.log('board')\n"),
            ("app/dist/cli.js.map", CLEAN_MAP), *extra]


def _scan(data: bytes):
    return Scanner("synthetic").scan_saved(io.BytesIO(data))


def _kinds(report) -> set[tuple[str, str]]:
    return {(finding.kind, finding.path) for finding in report.findings}


def test_a_clean_image_has_no_findings_and_every_control_holds():
    report = _scan(_saved(_tar(_base())))
    assert report.findings == []
    assert report.controls(expect_board=True) == [], report.controls(expect_board=True)
    assert report.layers == 1 and report.files == 3 and report.maps_parsed == 1


@pytest.mark.parametrize("compress", [True, False], ids=["gzip-layer", "plain-layer"])
def test_each_marker_in_the_bundle_is_a_finding(compress):
    for marker in MARKERS:
        bundle = b"var a=1;" + marker + b";var b=2;"
        report = _scan(_saved(_tar(_base(("app/dist/extra.js", bundle))), compress=compress))
        assert ("marker", "/app/dist/extra.js") in _kinds(report), (marker, report.findings)


def test_a_source_map_listing_the_sdk_source_path_is_a_finding_without_its_content():
    sdk_map = json.dumps({"version": 3, "sources": ["../src/cli.ts", SDK_SOURCE]}).encode()
    report = _scan(_saved(_tar([("usr/local/share/agent-tooling-build-identity.json", IDENTITY),
                                ("app/dist/cli.js", b"x"), ("app/dist/cli.js.map", sdk_map)])))
    assert ("source-map", BOARD_MAP) in _kinds(report), report.findings
    assert any(SDK_SOURCE in finding.detail for finding in report.findings)


@pytest.mark.parametrize("path", [
    "app/node_modules/@anthropic-ai/claude-agent-sdk/package.json",
    "opt/x/node_modules/@anthropic-ai/claude-agent-sdk-linux-arm64/claude",
    "state-seed/lib/node_modules/@anthropic-ai/claude-agent-sdk/LICENSE.md",
])
def test_a_file_of_the_package_is_a_finding_even_without_a_marker(path):
    report = _scan(_saved(_tar(_base((path, b'{"name":"irrelevant"}')))))
    assert ("package-file", "/" + path) in _kinds(report), report.findings


def test_a_file_a_later_layer_deletes_is_still_read():
    first = _tar(_base(("app/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs", b"x" + MARKERS[0])))
    second = _tar([("app/node_modules/@anthropic-ai/.wh.claude-agent-sdk", b"")])
    report = _scan(_saved(first, second))
    assert ("marker", "/app/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs") in _kinds(report)
    assert report.layers == 2


def test_an_npm_cache_tarball_is_read_inside():
    package = gzip.compress(_tar([("package/package.json", b'{"name":"x"}'),
                                  ("package/sdk.mjs", b"let q=1;" + MARKERS[1])]))
    report = _scan(_saved(_tar(_base(("state/.npm/_cacache/content-v2/sha512/ab/cd/ef", package)))))
    assert ("marker", "/state/.npm/_cacache/content-v2/sha512/ab/cd/ef!package/sdk.mjs") in _kinds(report), \
        report.findings


def test_a_zip_member_is_read_inside():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("vendor/sdk.mjs", b"let q=1;" + MARKERS[2])
    report = _scan(_saved(_tar(_base(("usr/lib/vendor.whl", buffer.getvalue())))))
    assert ("marker", "/usr/lib/vendor.whl!vendor/sdk.mjs") in _kinds(report), report.findings


def test_a_marker_across_a_read_boundary_is_found():
    marker = MARKERS[1]
    content = b"a" * (CHUNK - len(marker) // 2) + marker + b"b" * 100
    report = _scan(_saved(_tar(_base(("app/dist/big.js", content)))))
    assert ("marker", "/app/dist/big.js") in _kinds(report), report.findings


def test_naming_the_package_or_linking_to_state_is_not_carrying_it():
    """An install action, its notice and NOTICE name the package; a link points at /state."""
    named = (b"npm install --prefix /state/x @anthropic-ai/claude-agent-sdk@0.2.128\n"
             b"We neither warrant nor license your use of @anthropic-ai/claude-agent-sdk.\n"
             b"import('../node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs')\n")
    report = _scan(_saved(_tar(_base(
        ("app/dist/install.js", named),
        ("app/NOTICE", b"claude-agent-sdk is not redistributed"),
        ("app/node_modules/@anthropic-ai/claude-agent-sdk", None, "/state/claude/node_modules/x"),
    ))))
    assert report.findings == [], report.findings


def test_a_layer_config_or_history_string_is_read():
    report = _scan(_saved(_tar(_base()), config=b'{"history":[{"created_by":"' + MARKERS[3] + b'"}]}'))
    assert any(finding.kind == "marker" and finding.layer == "blob" for finding in report.findings)


def test_controls_fail_when_the_identity_or_the_board_was_not_read():
    report = _scan(_saved(_tar([("app/other.txt", b"x")])))
    failed = report.controls(expect_board=True)
    assert any(IDENTITY_PATH in line for line in failed), failed
    assert any(BOARD_BUNDLE in line for line in failed) and any(BOARD_MAP in line for line in failed), failed
    assert report.controls(expect_board=False) and not any(BOARD_BUNDLE in line
                                                           for line in report.controls(expect_board=False))


def test_a_map_without_the_board_entry_fails_the_board_control():
    other = json.dumps({"version": 3, "sources": ["../src/other.ts"]}).encode()
    report = _scan(_saved(_tar([("usr/local/share/agent-tooling-build-identity.json", IDENTITY),
                                ("app/dist/cli.js", b"x"), ("app/dist/cli.js.map", other)])))
    assert any("src/cli.ts" in line for line in report.controls(expect_board=True))


def test_a_zstd_layer_is_cannot_run_not_clean():
    with pytest.raises(CannotRun):
        _scan(_tar([("blobs/sha256/" + "a" * 64, b"\x28\xb5\x2f\xfd" + b"\0" * 600)]))
