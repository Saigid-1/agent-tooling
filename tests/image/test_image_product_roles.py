"""S2 P1 (one image carries every role) and the `product`/`agents` content surface."""
from __future__ import annotations

import pytest

from image_harness import declared_entry_points, image_env, image_for, run_once, sh, unresolved

pytestmark = pytest.mark.image

NODE_PTY_SCRIPT = r"""
const { createRequire } = require("module");
const load = createRequire("/app/dist/cli.js");
let pty;
try { pty = load("node-pty"); } catch (error) { console.log("LOADFAIL " + error.message); process.exit(3); }
if (typeof pty.spawn !== "function") { console.log("NOSPAWN"); process.exit(4); }
let out = "";
const timer = setTimeout(() => { console.log("TIMEOUT " + JSON.stringify(out)); process.exit(5); }, 20000);
const child = pty.spawn("/bin/sh", ["-c", "echo node-pty-ok; sleep 0.2"], { name: "xterm", cols: 80, rows: 24, cwd: "/", env: process.env });
child.onData((data) => { out += data; });
child.onExit((event) => {
  setTimeout(() => {
    clearTimeout(timer);
    console.log("EXIT " + event.exitCode + " " + JSON.stringify(out));
    process.exit(out.includes("node-pty-ok") && event.exitCode === 0 ? 0 : 6);
  }, 300);
});
"""

# What the current `full` target carries (deploy/tooling/Dockerfile at the order's base),
# which `product` must keep: toolchain, Serena, semantic and telemetry packages.
FULL_EXECUTABLES_ON_PATH = [
    "node", "npm", "git", "tooling-container", "git-credential-file",
    "scip-python", "scip-typescript", "pyright-langserver", "typescript-language-server",
]
FULL_FILES = [
    "/opt/toolchain/node_modules/.bin/scip-python",
    "/opt/toolchain/node_modules/.bin/scip-typescript",
    "/opt/toolchain/node_modules/.bin/pyright-langserver",
    "/opt/toolchain/node_modules/.bin/typescript-language-server",
    "/usr/local/share/ops-tooling/serena.yml",
]
FULL_PYTHON_MODULES = [
    "kp_agent_tooling", "torch", "sentence_transformers", "transformers",
    "opentelemetry.sdk.trace", "opentelemetry.exporter.otlp.proto.http",
]
FULL_OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}

FULL_CONTENT_SCRIPT = r"""
for f in "$@"; do [ -e "$f" ] || echo "ABSENT $f"; done
if [ -x /opt/serena/bin/python ]; then
  /opt/serena/bin/python -c 'import serena' >/dev/null 2>&1 || echo "SERENA-IMPORT-FAILED"
else
  echo "ABSENT /opt/serena/bin/python"
fi
python - <<'PY' || echo "PYTHON-FAILED"
import importlib
for name in %s:
    try:
        importlib.import_module(name)
    except Exception as error:
        print("IMPORT-FAILED", name, type(error).__name__)
PY
echo PROBE-DONE
""" % repr(FULL_PYTHON_MODULES)


def test_product_resolves_every_declared_entry_point_on_path():
    image = image_for("product")
    missing = unresolved(image, declared_entry_points())
    assert not missing, "declared [project.scripts] entry points that do not resolve in product:\n" + "\n".join(missing)


def test_product_kanban_help_exits_zero():
    image = image_for("product")
    result = sh(image, "kanban --help", timeout=120)
    assert result.returncode == 0, f"`kanban --help` exited {result.returncode}: {(result.stdout + result.stderr)[-1500:]}"


def test_product_node_pty_loads_for_the_board():
    image = image_for("product")
    result = run_once(image, ["-e", NODE_PTY_SCRIPT], entrypoint="node", timeout=120)
    assert result.returncode == 0, f"node-pty did not load and run from /app/dist/cli.js: exit {result.returncode}: {(result.stdout + result.stderr)[-1500:]}"


def test_product_carries_what_full_carries():
    image = image_for("product")
    problems = unresolved(image, FULL_EXECUTABLES_ON_PATH)
    probe = sh(image, FULL_CONTENT_SCRIPT, *FULL_FILES, timeout=600)
    if "PROBE-DONE" not in probe.stdout:
        problems.append(f"content probe did not finish: exit {probe.returncode}: {probe.stderr.strip()[-500:]}")
    problems += [line for line in probe.stdout.splitlines() if line and line != "PROBE-DONE"]
    env = image_env(image)
    problems += [f"ENV {key}={env.get(key)!r}, expected {value!r}" for key, value in FULL_OFFLINE_ENV.items()
                 if env.get(key) != value]
    assert not problems, "product lacks what full carries today:\n" + "\n".join(problems)


def test_agents_carries_the_product_roles():
    image = image_for("agents")
    missing = unresolved(image, [*declared_entry_points(), "kanban", "tooling-container"])
    assert not missing, "agents is not product plus the agent CLIs:\n" + "\n".join(missing)
