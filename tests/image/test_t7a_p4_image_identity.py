"""T7a P4 (image-marked): the image knows its revision.

Order: docs/work/orders/T7a-role-readiness.md, P4.
In an image built with `--build-arg SOURCE_REVISION=<40-hex>`, `tooling.identity` reports:
- `status: known`;
- `server_build_revision` equal to that revision and to the image label;
- an origin that names an image or package build declaration.
In an image built without it, `status` is `unknown`. A new revision still rebuilds
only the final stages.
Falsifier: a null or differing revision in a labelled image, a known revision in an
unlabelled one, or a revision change that invalidates the shared stages.

Environment (a test FAILS, never skips, when its variable is unset):
- AGENT_TOOLING_TEST_IMAGE: a product image built with SOURCE_REVISION=<40 hex>;
- AGENT_TOOLING_TEST_SOURCE_REVISION (optional): that build argument; when set it must
  equal the label;
- AGENT_TOOLING_TEST_IMAGE_UNREVISIONED: a product image built from the same tree
  without SOURCE_REVISION (its revision label is the Dockerfile default).

`tooling.identity` is called over MCP stdio exactly as the tooling role serves it: the
image's own entrypoint with `kp-agent-tooling --config /config/navigation.json serve`,
read-only, no network, all capabilities dropped, the invoking user's IDs, with a
private /state (the state marker and the directories the configuration names) and a
read-only /config.

Readings (repeated in the arm report under AMBIGUITY):
- "an origin that names an image or package build declaration": `server_build_origin`
  contains "image" or "package", and "build" or "declaration" (the existing
  `package-build-declaration` qualifies; `git-checkout` does not);
- "rebuilds only the final stages" is checked on deploy/Dockerfile itself: no stage
  that another stage builds on, copies from or mounts from declares or uses
  SOURCE_REVISION, and no FROM line uses it. No image is built by this suite.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from image_harness import REVISION, REVISION_LABEL, image_labels, remove, required, source_root

_INSTALL = Path(__file__).resolve().parents[1] / "install"
if str(_INSTALL) not in sys.path:
    sys.path.insert(0, str(_INSTALL))

from s3_harness import StdioSession  # noqa: E402

pytestmark = pytest.mark.image

UNREVISIONED = "AGENT_TOOLING_TEST_IMAGE_UNREVISIONED"
HEX40 = re.compile(r"[0-9a-f]{40}")


def _identity(image: str) -> dict:
    """`tooling.identity` from the image's own serve path, over MCP stdio."""
    base = Path(tempfile.mkdtemp(prefix="t7a-test-p4-")).resolve()
    state, config = base / "state", base / "config"
    for directory in (state, state / "snapshots", state / "search", state / "delivery", state / "tmp", config):
        directory.mkdir(mode=0o700, parents=True)
        directory.chmod(0o700)
    (state / ".ops-tooling-volume").write_text("ops-tooling-state-v1\n")
    (config / "navigation.json").write_text(json.dumps({
        "schema_version": "ops.agent-tooling.v1", "repos": {}, "snapshot_registry": "/state/snapshots",
        "navigation_registry_path": "/state/search", "delivery_root": "/state/delivery",
        "enabled_tools": ["tooling.identity"]}))
    name = f"t7a-test-p4-{uuid.uuid4().hex[:10]}"
    argv = ["docker", "run", "--rm", "-i", "--pull", "never", "--name", name, "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/state", "-e", "TMPDIR=/state/tmp",
            "--mount", f"type=bind,source={state},target=/state",
            "--mount", f"type=bind,source={config},target=/config,readonly",
            image, "kp-agent-tooling", "--config", "/config/navigation.json", "serve"]
    session = StdioSession(argv, dict(os.environ))
    try:
        init = session.request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                 "clientInfo": {"name": "t7a-p4", "version": "1"}})
        assert "result" in init, f"initialize failed: {init}"
        session.notify("notifications/initialized")
        called = session.request(2, "tools/call", {"name": "tooling.identity", "arguments": {}})
        result = called.get("result") or {}
        assert "error" not in called and not result.get("isError"), f"tooling.identity failed: {called}"
        value = result.get("structuredContent")
        return value if value is not None else json.loads(result["content"][0]["text"])
    finally:
        session.close()
        remove(name)


def _names_a_build_declaration(origin) -> bool:
    return isinstance(origin, str) and re.search(r"image|package", origin, re.I) is not None \
        and re.search(r"build|declar", origin, re.I) is not None


def test_labelled_image_reports_its_revision():
    """GREEN-IF tooling.identity in a labelled image is known, equals the label, and names a build declaration."""
    image = required("AGENT_TOOLING_TEST_IMAGE")
    label = image_labels(image).get(REVISION_LABEL)
    assert isinstance(label, str) and HEX40.fullmatch(label), (
        f"precondition: {image} must be built with SOURCE_REVISION=<40 hex>; its {REVISION_LABEL} is {label!r}")
    declared = os.environ.get(REVISION, "").strip()
    if declared:
        assert declared == label, f"{REVISION}={declared} differs from the image label {label}"
    identity = _identity(image)
    assert identity.get("status") == "known", f"a labelled image reports status {identity.get('status')!r}: {identity}"
    assert identity.get("server_build_revision") == label, (
        f"server_build_revision {identity.get('server_build_revision')!r} differs from the label {label}: {identity}")
    assert _names_a_build_declaration(identity.get("server_build_origin")), (
        f"origin {identity.get('server_build_origin')!r} names no image or package build declaration: {identity}")


def test_unlabelled_image_reports_unknown():
    """GREEN-IF an image built without SOURCE_REVISION reports status unknown and no revision."""
    image = required(UNREVISIONED)
    label = image_labels(image).get(REVISION_LABEL)
    assert not (isinstance(label, str) and HEX40.fullmatch(label)), (
        f"precondition: {UNREVISIONED} must be built without SOURCE_REVISION; its {REVISION_LABEL} is {label!r}")
    identity = _identity(image)
    assert identity.get("status") == "unknown", f"an unlabelled image reports {identity.get('status')!r}: {identity}"
    assert identity.get("server_build_revision") is None, identity


# ------------------------------------------------------------------- build file


def _logical_lines(text: str) -> list[str]:
    lines, current = [], ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if stripped.startswith("#"):
            continue
        current = f"{current} {stripped}" if current else stripped
        if current.endswith("\\"):
            current = current[:-1].rstrip()
            continue
        lines.append(current)
        current = ""
    if current:
        lines.append(current)
    return lines


_FROM = re.compile(r"(?i)^FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?\s*$")
_USES_REVISION = re.compile(r"\bSOURCE_REVISION\b")


def stages(text: str) -> list[dict]:
    found = []
    for line in _logical_lines(text):
        match = _FROM.match(line)
        if match:
            found.append({"name": (match.group(2) or f"#{len(found)}").lower(), "base": match.group(1).lower(),
                          "from_line": line, "body": []})
        elif found:
            found[-1]["body"].append(line)
    return found


def dependencies(stage: dict, names: set[str]) -> set[str]:
    deps = {stage["base"]} & names
    for line in stage["body"]:
        deps |= {m.lower() for m in re.findall(r"--from=([A-Za-z0-9_.-]+)", line)} & names
        deps |= {m.lower() for m in re.findall(r"\bfrom=([A-Za-z0-9_.-]+)", line)} & names
    return deps


def test_revision_is_declared_only_in_final_stages():
    """GREEN-IF no stage that another stage depends on declares or uses SOURCE_REVISION, and no FROM uses it."""
    dockerfile = source_root() / "deploy" / "Dockerfile"
    assert dockerfile.is_file(), f"{dockerfile} does not exist"
    found = stages(dockerfile.read_text())
    names = {stage["name"] for stage in found}
    assert "product" in names, f"deploy/Dockerfile has no product stage: {sorted(names)}"
    shared = set()
    for stage in found:
        shared |= dependencies(stage, names - {stage["name"]})
    product = next(stage for stage in found if stage["name"] == "product")
    assert any(_USES_REVISION.search(line) for line in product["body"]), (
        "precondition: the product stage declares SOURCE_REVISION (it is labelled with it)")
    offenders = [f"{stage['name']}: {line}" for stage in found if stage["name"] in shared
                 for line in stage["body"] if _USES_REVISION.search(line)]
    offenders += [stage["from_line"] for stage in found if _USES_REVISION.search(stage["from_line"])]
    assert not offenders, ("SOURCE_REVISION reaches a shared stage, so a new revision rebuilds stages other "
                           "than the final ones:\n" + "\n".join(offenders))
