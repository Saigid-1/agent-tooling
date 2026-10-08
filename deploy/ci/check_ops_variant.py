#!/usr/bin/env python3
"""Check the `product` / `ops` split of deploy/Dockerfile against built images.

    python deploy/ci/check_ops_variant.py --product IMAGE --ops IMAGE

The images default to $AGENT_TOOLING_TEST_IMAGE and $AGENT_TOOLING_TEST_IMAGE_OPS.
Order T6a, P1 (docs/work/orders/T6a-images-and-ci.md):

- product: every core [project.scripts] entry point resolves; kp_agent_tooling_ops
  is not importable, not installed and registers no tool provider.
- ops: every core and extension entry point resolves; the extension, its tool
  provider and its packaged assets are installed.
- `kp-agent-tooling --config … serve` (MCP over stdio, through the image's own
  entry) lists the extension tools when the configuration enables them, and only
  core tools when it does not; product lists no extension tool either way.

Entry-point names and assets are read from this checkout, so build the images from
it. Exits 1 on the first failed property, 2 when the check cannot run. Every
container it starts is labelled agent-tooling-ci-check=ops-variant and removed.
Uses the standard library only (Python 3.11+).
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE_PYPROJECT = ROOT / "packages/tooling/pyproject.toml"
EXTENSION_PYPROJECT = ROOT / "extensions/ops/pyproject.toml"
EXTENSION_ASSETS = ROOT / "extensions/ops/src/kp_agent_tooling_ops/assets"
LABEL = "agent-tooling-ci-check=ops-variant"
USER = "10001:10001"
PROTOCOL_VERSION = "2025-06-18"
RESPONSE_TIMEOUT = 180

# Tools the extension's provider lists itself (kp_agent_tooling_ops ... ops_tools.OPS_TOOLS).
EXTENSION_TOOLS = ["lifecycle.evidence", "verification.guide", "verification.packet",
                   "verification.finding", "verification.plan", "verification.handoff",
                   "verification.observations", "knowledge.rationale", "verification.review"]
CORE_TOOLS = ["tooling.identity", "navigation.source", "navigation.paths", "navigation.search",
              "navigation.manifest", "navigation.search_page", "navigation.imports",
              "navigation.dependencies", "delivery.read"]

RESOLVE_SCRIPT = r"""
for n in "$@"; do
  p=$(command -v "$n" 2>/dev/null) || { echo "MISSING $n"; continue; }
  [ -f "$p" ] && [ -x "$p" ] || { echo "NOTEXEC $n $p"; continue; }
  first=$(head -n 1 "$p" | head -c 512)
  case "$first" in
    '#!'*) interp=${first#??}; interp=${interp# }; interp=${interp%% *}
           [ -x "$interp" ] || { echo "BADINTERP $n $interp"; continue; } ;;
  esac
  echo "OK $n"
done
"""

INSTALL_PROBE = r"""
import importlib.metadata as metadata, importlib.resources as resources, importlib.util as util, json, sys
report = {"core": util.find_spec("kp_agent_tooling") is not None,
          "extension_importable": util.find_spec("kp_agent_tooling_ops") is not None,
          "providers": sorted(e.name + "=" + e.value for e in metadata.entry_points(group="kp_agent_tooling.tools"))}
try:
    report["extension_version"] = metadata.version("kp-agent-tooling-ops")
except metadata.PackageNotFoundError:
    report["extension_version"] = None
if report["extension_importable"]:
    assets = resources.files("kp_agent_tooling_ops") / "assets"
    report["assets"] = sorted(p.name for p in assets.iterdir()) if assets.is_dir() else []
print(json.dumps(report))
"""


class Failed(Exception):
    """A P1 property does not hold."""


class CannotRun(Exception):
    """The check could not establish what it needs."""


def scripts(pyproject: Path) -> list[str]:
    return sorted(tomllib.loads(pyproject.read_text())["project"]["scripts"])


def docker(*args: str, timeout: float = 300, **kw) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, **kw)
    except FileNotFoundError:
        raise CannotRun("the docker CLI is not on PATH") from None


def run_once(image: str, entrypoint: str, *args: str, mounts: tuple[str, ...] = ()) -> subprocess.CompletedProcess:
    name = f"agent-tooling-ci-check-{uuid.uuid4().hex[:10]}"
    try:
        return docker("run", "--rm", "--pull", "never", "--network", "none", "--name", name,
                      "--label", LABEL, *mounts, "--entrypoint", entrypoint, image, *args)
    finally:
        docker("rm", "-f", name, timeout=120)


def require_image(image: str) -> None:
    if docker("image", "inspect", image, timeout=120).returncode:
        raise CannotRun(f"image {image!r} is not present locally; build it first")


def resolve(image: str, names: list[str]) -> dict[str, str]:
    """Each name's PATH resolution in image, as the image's default user sees it."""
    result = run_once(image, "/bin/sh", "-c", RESOLVE_SCRIPT, "sh", *names)
    seen = {line.split()[1]: line for line in result.stdout.splitlines() if len(line.split()) >= 2}
    absent = [name for name in names if name not in seen]
    if absent:
        raise CannotRun(f"{image}: PATH probe gave no answer for {absent} "
                        f"(exit {result.returncode}: {result.stderr.strip()[:300]})")
    return seen


def unresolved(image: str, names: list[str]) -> list[str]:
    return [line for line in resolve(image, names).values() if not line.startswith("OK ")]


def install_report(image: str) -> dict:
    result = run_once(image, "python", "-c", INSTALL_PROBE)
    if result.returncode:
        raise Failed(f"{image}: install probe exited {result.returncode}: {result.stderr.strip()[-600:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def list_tools(image: str, enabled_tools: list[str] | None) -> list[str]:
    """tools/list from `kp-agent-tooling --config /config/navigation.json serve` in image."""
    with tempfile.TemporaryDirectory(prefix="ops-variant-") as scratch:
        scratch = Path(scratch).resolve()
        state, config = scratch / "state", scratch / "config"
        for directory in (state, state / "snapshots", state / "search", state / "tmp"):
            directory.mkdir()
            directory.chmod(0o777)
        (state / ".ops-tooling-volume").write_text("ops-tooling-state-v1\n")
        navigation = {"schema_version": "ops.agent-tooling.v1", "repos": {},
                      "snapshot_registry": "/state/snapshots", "navigation_registry_path": "/state/search"}
        if enabled_tools is not None:
            navigation["enabled_tools"] = enabled_tools
        config.mkdir()
        (config / "navigation.json").write_text(json.dumps(navigation))
        for path in (state / ".ops-tooling-volume", config / "navigation.json"):
            path.chmod(0o644)
        config.chmod(0o755)
        name = f"agent-tooling-ci-check-{uuid.uuid4().hex[:10]}"
        argv = ["docker", "run", "--rm", "-i", "--pull", "never", "--network", "none", "--name", name,
                "--label", LABEL, "--user", USER, "--read-only", "--cap-drop", "ALL",
                "-v", f"{state}:/state", "-v", f"{config}:/config:ro",
                image, "kp-agent-tooling", "--config", "/config/navigation.json", "serve"]
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, bufsize=1)
        lines: queue.Queue = queue.Queue()

        def pump():
            for raw in process.stdout:
                lines.put(raw)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()

        def send(message: dict) -> None:
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

        def reply(request_id: int) -> dict:
            deadline = time.monotonic() + RESPONSE_TIMEOUT
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Failed(f"{image}: no reply to request {request_id} within {RESPONSE_TIMEOUT}s")
                try:
                    raw = lines.get(timeout=remaining)
                except queue.Empty:
                    continue
                if raw is None:
                    raise Failed(f"{image}: serve exited before replying to request {request_id}: "
                                 f"{process.stderr.read()[-1500:]}")
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if message.get("id") == request_id:
                    if "error" in message:
                        raise Failed(f"{image}: request {request_id} failed: {message['error']}")
                    return message["result"]

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                             "clientInfo": {"name": "check_ops_variant", "version": "1"}}})
            reply(1)
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            return [tool["name"] for tool in reply(2)["tools"]]
        finally:
            try:
                process.stdin.close()
                process.wait(timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
            docker("rm", "-f", name, timeout=120)
            # On a Linux host the server writes /state as USER with private modes, which the
            # host user cannot delete; remove those files as USER before the scratch cleanup.
            # If this fails the cleanup still fails loudly; raising here would mask a check failure.
            run_once(image, "find", "/state", "-mindepth", "1", "-depth",
                     "-user", USER.split(":")[0], "-delete",
                     mounts=("-v", f"{state}:/state", "--user", USER))


def check(product: str, ops: str) -> list[str]:
    core_scripts, extension_scripts = scripts(CORE_PYPROJECT), scripts(EXTENSION_PYPROJECT)
    if not core_scripts or not extension_scripts:
        raise CannotRun("no [project.scripts] read from the core or extension pyproject.toml")
    assets = sorted(p.name for p in EXTENSION_ASSETS.iterdir())
    for image in (product, ops):
        require_image(image)
    passed = []

    missing = unresolved(product, core_scripts)
    if missing:
        raise Failed("product: core entry points do not resolve:\n" + "\n".join(missing))
    passed.append(f"product resolves all {len(core_scripts)} core entry points")
    report = install_report(product)
    if not report["core"]:
        raise Failed("product: positive control failed, kp_agent_tooling is not importable")
    leaked = {key: report[key] for key in ("extension_importable", "extension_version", "providers") if report[key]}
    if leaked:
        raise Failed(f"product carries extension code: {leaked}")
    present = [line for line in resolve(product, extension_scripts).values() if not line.startswith("MISSING ")]
    if present:
        raise Failed("product carries extension console scripts: " + ", ".join(present))
    passed.append("product: kp_agent_tooling_ops not importable, not installed, no tool provider, "
                  f"none of the {len(extension_scripts)} extension console scripts")

    missing = unresolved(ops, core_scripts + extension_scripts)
    if missing:
        raise Failed("ops: entry points do not resolve:\n" + "\n".join(missing))
    passed.append(f"ops resolves all {len(core_scripts)} core and {len(extension_scripts)} extension entry points")
    report = install_report(ops)
    expected_version = tomllib.loads(EXTENSION_PYPROJECT.read_text())["project"]["version"]
    if not (report["core"] and report["extension_importable"] and report["extension_version"] == expected_version):
        raise Failed(f"ops: the extension {expected_version} is not installed and importable: {report}")
    if not any(provider.startswith("ops=") for provider in report["providers"]):
        raise Failed(f"ops: no `ops` provider in kp_agent_tooling.tools: {report['providers']}")
    if report.get("assets") != assets:
        raise Failed(f"ops: packaged assets {report.get('assets')} differ from the extension's {assets}")
    passed.append(f"ops: extension {expected_version} importable, provider {report['providers']}, assets {assets}")

    enabled = CORE_TOOLS[:2] + EXTENSION_TOOLS
    listed = list_tools(ops, enabled)
    if sorted(listed) != sorted(enabled):
        raise Failed(f"ops serve with the extension tools enabled listed {sorted(listed)}; expected {sorted(enabled)}")
    passed.append(f"ops serve lists the {len(EXTENSION_TOOLS)} extension tools its configuration enables")
    listed = list_tools(ops, CORE_TOOLS)
    if sorted(listed) != sorted(CORE_TOOLS):
        raise Failed(f"ops serve with only core tools enabled listed {sorted(listed)}; expected {sorted(CORE_TOOLS)}")
    passed.append(f"ops serve lists only the {len(CORE_TOOLS)} core tools when no extension tool is enabled")
    listed = list_tools(ops, None)
    if not set(EXTENSION_TOOLS + CORE_TOOLS) <= set(listed):
        raise Failed(f"ops serve with every tool enabled lacks {sorted(set(EXTENSION_TOOLS + CORE_TOOLS) - set(listed))}")
    passed.append(f"ops serve with no enabled_tools lists {len(listed)} tools, the extension's among them")
    listed = list_tools(product, enabled)
    if sorted(listed) != sorted(CORE_TOOLS[:2]):
        raise Failed(f"product serve with extension tools configured listed {sorted(listed)}; expected {sorted(CORE_TOOLS[:2])}")
    passed.append("product serve lists only core tools when extension tools are configured")
    return passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--product", default=os.environ.get("AGENT_TOOLING_TEST_IMAGE", ""))
    parser.add_argument("--ops", default=os.environ.get("AGENT_TOOLING_TEST_IMAGE_OPS", ""))
    args = parser.parse_args(argv)
    if not args.product or not args.ops:
        print("check_ops_variant: name both images (--product/--ops or AGENT_TOOLING_TEST_IMAGE"
              "/AGENT_TOOLING_TEST_IMAGE_OPS)", file=sys.stderr)
        return 2
    try:
        passed = check(args.product, args.ops)
    except CannotRun as error:
        print(f"check_ops_variant: cannot run: {error}", file=sys.stderr)
        return 2
    except Failed as error:
        print(f"check_ops_variant: FAILED: {error}", file=sys.stderr)
        return 1
    for line in passed:
        print("ok  " + line)
    print(f"check_ops_variant: {len(passed)} properties hold for product={args.product} ops={args.ops}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
