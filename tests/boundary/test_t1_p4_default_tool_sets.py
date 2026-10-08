"""P4 — defaults are core-only (docs/work/orders/T1-extension-seam.md).

"Every default enabled-tool list contains only core tools." Falsifier: an extension tool
in any default. The order names the defaults: the installer (runtime_install.ENABLED_TOOLS),
workspace_setup, kp-agent-setup and the image example config (deploy/*/config.example.json);
"verification.guide is removed from all defaults".

Runtime defaults are read from the core installed alone in a scratch venv (conftest
core_env), through its console scripts or the named module, never from this process.
Each default must be an explicit, non-empty list of names: a default that enables
"everything" would enable extension tools whenever the extension is installed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from t1_contract import REPO_ROOT, is_extension_tool_name
from t1_harness import PRODUCT_FILES, make_repo, need


def extension_tools_in(label, enabled):
    assert isinstance(enabled, list) and enabled and all(isinstance(n, str) for n in enabled), (
        f"{label}: expected an explicit non-empty enabled_tools list of names, got {enabled!r}")
    return [name for name in enabled if is_extension_tool_name(name)]


def test_p4_installer_enabled_tools_default_is_core_only(core_env):
    env = need(core_env)
    enabled = env.python_json(
        "import json; from kp_agent_tooling._impl.runtime_install import ENABLED_TOOLS;"
        "print(json.dumps(list(ENABLED_TOOLS)))")
    offending = extension_tools_in("runtime_install.ENABLED_TOOLS", enabled)
    assert not offending, f"runtime_install.ENABLED_TOOLS enables extension tools {offending}: {enabled}"


def test_p4_installer_rendered_navigation_config_is_core_only(core_env, tmp_path):
    env = need(core_env)
    base = tmp_path.resolve()
    home, root = base / "home", base / "runtime-root"
    home.mkdir()
    root.mkdir()
    repo = base / "repos" / "product"
    make_repo(repo, PRODUCT_FILES)
    argv = ["--runtime-root", root, "--image", "sha256:" + hashlib.sha256(b"t1-boundary").hexdigest(),
            "--repository", f"product={repo}", "--components", "tooling",
            "--uid", str(os.getuid()), "--gid", str(os.getgid()),
            "--board-port", "34871", "--project-name", "t1-boundary"]
    planned = env.run([env.bin / "kp-agent-install", "plan", *argv], home=home)
    assert planned.returncode == 0, f"kp-agent-install plan refused\n{planned.stdout}\n{planned.stderr}"
    digest = json.loads(planned.stdout)["plan_sha256"]
    applied = env.run([env.bin / "kp-agent-install", "apply", *argv, "--expected-plan-sha256", digest],
                      home=home)
    assert applied.returncode == 0, f"kp-agent-install apply refused\n{applied.stdout}\n{applied.stderr}"
    rendered = json.loads((root / "config" / "navigation.json").read_text())
    offending = extension_tools_in("installer config/navigation.json", rendered.get("enabled_tools"))
    assert not offending, (f"the installer's rendered config/navigation.json enables extension tools "
                           f"{offending}: {rendered.get('enabled_tools')}")


WORKSPACE_PLANS = r"""
import json, sys
from kp_agent_tooling._impl.workspace_setup import plan
requests = json.loads(sys.argv[1])
print(json.dumps({label: plan(request)['config'].get('enabled_tools') for label, request in requests.items()}))
"""


def _setup_world(base: Path):
    repo = base / "repos" / "product"
    make_repo(repo, PRODUCT_FILES)  # includes a reviewed .serena/project.yml (read_only: true)
    compiler = base / "typescript.js"
    compiler.write_text("// stand-in compiler file for configuration planning\n")
    (base / "serena-home").mkdir()
    return repo, compiler


def test_p4_workspace_setup_plans_are_core_only_for_every_supported_capability_set(core_env, tmp_path):
    env = need(core_env)
    base = tmp_path.resolve()
    repo, compiler = _setup_world(base)
    executable = str(env.bin / "kp-agent-tooling")
    request = {"schema_version": "ops.workspace-setup.v1", "repo_key": "product", "repository": str(repo),
               "output_root": str(base / "setup-out"), "launcher": {"command": executable, "args": []}}
    serena = {"command": executable, "python": str(env.python), "runtime_home": str(base / "serena-home")}
    typescript = {"node": executable, "module": str(compiler),
                  "sha256": hashlib.sha256(compiler.read_bytes()).hexdigest()}
    requests = {
        "source-navigation": dict(request, capabilities=["source-navigation"]),
        "+serena": dict(request, capabilities=["source-navigation", "serena"], serena=serena),
        "+typescript": dict(request, capabilities=["source-navigation", "typescript"], typescript=typescript),
        "+serena+typescript": dict(request, capabilities=["source-navigation", "serena", "typescript"],
                                   serena=serena, typescript=typescript),
    }
    plans = env.python_json(WORKSPACE_PLANS, json.dumps(requests))
    offending = {label: extension_tools_in(f"workspace_setup.plan[{label}]", enabled)
                 for label, enabled in plans.items()}
    offending = {label: names for label, names in offending.items() if names}
    assert not offending, f"workspace_setup plans enable extension tools: {offending}\nplans: {plans}"


def test_p4_kp_agent_setup_plan_is_core_only(core_env, tmp_path):
    env = need(core_env)
    base = tmp_path.resolve()
    repo, _ = _setup_world(base)
    proc = env.run([env.bin / "kp-agent-setup", "plan", "--repository", repo, "--repo-key", "product",
                    "--output-root", base / "setup-out", "--launcher-command", env.bin / "kp-agent-tooling"])
    assert proc.returncode == 0, f"kp-agent-setup plan failed\n{proc.stdout}\n{proc.stderr}"
    enabled = json.loads(proc.stdout)["config"].get("enabled_tools")
    offending = extension_tools_in("kp-agent-setup plan", enabled)
    assert not offending, f"kp-agent-setup's planned config enables extension tools {offending}: {enabled}"


def _enabled_lists(value, where="$"):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "enabled_tools":
                yield where + ".enabled_tools", child
            else:
                yield from _enabled_lists(child, f"{where}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _enabled_lists(child, f"{where}[{index}]")


def test_p4_shipped_example_configs_are_core_only():
    examples = sorted((REPO_ROOT / "deploy").glob("**/config.example.json"))
    assert examples, "no deploy/**/config.example.json found; the order names the image example config"
    shipped = sorted({*examples, *(REPO_ROOT / "deploy").glob("**/*.json"),
                      *(REPO_ROOT / "examples").glob("**/*.json"),
                      *(REPO_ROOT / "packages" / "tooling" / "src").glob("**/*.json")})
    offending, seen = {}, []
    for path in shipped:
        document = json.loads(path.read_text())
        lists = list(_enabled_lists(document))
        if path in examples:
            assert lists, f"{path.relative_to(REPO_ROOT)} declares no enabled_tools list"
        for where, enabled in lists:
            label = f"{path.relative_to(REPO_ROOT)}:{where}"
            seen.append(label)
            names = extension_tools_in(label, enabled)
            if names:
                offending[label] = names
    assert not offending, (f"shipped example configurations enable extension tools: {offending} "
                           f"(checked {len(seen)} enabled_tools lists: {seen})")
