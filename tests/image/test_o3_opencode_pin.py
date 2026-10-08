"""O3 Q3 and Q4 in the source tree (default suite; no Docker).

The pin in tests/image/opencode_pin.py is the one the build installs: deploy/Dockerfile's
`ARG OPENCODE_VERSION` in the `opencode` stage, deploy/image/opencode/package.json and every
opencode package in its lockfile carry it. The committed LICENSE is the upstream file at the
pinned tag (sha256 recorded in the pin module), and NOTICE and the public-readiness audit name
the package, the version and its licence before the image exists.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from opencode_pin import (
    OPENCODE_LICENSE_BYTES, OPENCODE_LICENSE_PATH, OPENCODE_LICENSE_SHA256, OPENCODE_PACKAGE, OPENCODE_TARGET,
    OPENCODE_VERSION,
)

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = ROOT / "deploy" / "image" / "opencode"


def _stage(dockerfile: str, name: str) -> tuple[str, str]:
    """(base, body) of the Dockerfile stage `name`: its FROM base and its text up to the next FROM."""
    match = re.search(rf"(?m)^FROM (\S+) AS {re.escape(name)}\n((?:(?!^FROM ).*\n?)*)", dockerfile)
    assert match, f"deploy/Dockerfile has no stage {name!r}"
    return match.group(1), match.group(2)


def test_dockerfile_stage_pins_the_constant_on_product_base():
    base, body = _stage((ROOT / "deploy" / "Dockerfile").read_text(), OPENCODE_TARGET)
    assert base == "product-base", f"the {OPENCODE_TARGET} stage is FROM {base}, not product-base"
    pins = re.findall(r"(?m)^ARG OPENCODE_VERSION=(\S+)$", body)
    assert pins == [OPENCODE_VERSION], pins


def test_package_json_and_lockfile_pin_the_constant():
    manifest = json.loads((PACKAGE_DIR / "package.json").read_text())
    assert manifest.get("private") is True
    assert manifest.get("dependencies") == {OPENCODE_PACKAGE: OPENCODE_VERSION}, manifest.get("dependencies")
    lock = json.loads((PACKAGE_DIR / "package-lock.json").read_text())
    packages = lock["packages"]
    assert packages[""]["dependencies"] == {OPENCODE_PACKAGE: OPENCODE_VERSION}
    assert packages[f"node_modules/{OPENCODE_PACKAGE}"]["version"] == OPENCODE_VERSION
    locked = {path: entry.get("version") for path, entry in packages.items() if path}
    assert all(path.startswith("node_modules/opencode-") for path in locked), sorted(locked)
    assert set(locked.values()) == {OPENCODE_VERSION}, locked
    linux = sorted(path for path in locked if path.startswith("node_modules/opencode-linux-"))
    assert "node_modules/opencode-linux-x64" in linux and "node_modules/opencode-linux-arm64" in linux, linux


def test_committed_license_is_the_recorded_upstream_file():
    data = (PACKAGE_DIR / "LICENSE").read_bytes()
    assert (len(data), hashlib.sha256(data).hexdigest()) == (OPENCODE_LICENSE_BYTES, OPENCODE_LICENSE_SHA256)
    assert data.startswith(b"MIT License\n")


def _flat(path: Path) -> str:
    return re.sub(r"\s+", " ", path.read_text())


def test_notice_names_the_cli_its_platform_packages_and_their_licence_basis():
    notice = _flat(ROOT / "NOTICE")
    start = notice.index("OpenCode CLI (opencode image only)")
    section = notice[start:notice.index("3. Sidecar images", start)]
    assert f'{OPENCODE_PACKAGE} {OPENCODE_VERSION} Licence field: "MIT"' in section, section
    assert f"tag v{OPENCODE_VERSION} is the MIT License" in section
    assert f"opencode-linux-* {OPENCODE_VERSION}" in section
    assert "Their npm metadata carries no licence field" in section
    assert (f"Their licence basis is the upstream repository's MIT LICENSE at tag v{OPENCODE_VERSION}, "
            "by the maintainer's decision of 2026-10-06") in section
    assert OPENCODE_LICENSE_PATH in section


def test_audit_carries_exactly_one_opencode_row_with_the_decision():
    rows = [line for line in (ROOT / "docs" / "work" / "PUBLIC-READINESS-AUDIT.md").read_text().splitlines()
            if line.startswith("|") and OPENCODE_PACKAGE in line]
    assert len(rows) == 1, rows
    row = rows[0]
    assert f"pinned {OPENCODE_VERSION}" in row and "npm licence field: `MIT`" in row
    assert "no licence field" in row and "DECIDED 2026-10-06" in row and OPENCODE_LICENSE_PATH in row
