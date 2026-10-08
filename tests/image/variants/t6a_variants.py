"""Harness for the T6a P1 variant tests (``tests/image/variants``).

Contract: ``docs/work/orders/T6a-images-and-ci.md``, P1 ("one tree yields both
variants") and the interface lines "`ops` is `product` plus the extension installed
from `extensions/ops`, with its OPS assets and console scripts" and "`product` and
`agents` contain no `kp_agent_tooling_ops`". The suite reads image references from the
environment; it never builds an image and never reads an implementation.

Environment (a test whose variable is unset FAILS; nothing here skips):

- ``AGENT_TOOLING_TEST_IMAGE``         the ``product`` image
- ``AGENT_TOOLING_TEST_IMAGE_OPS``     the ``ops`` image
- ``AGENT_TOOLING_TEST_IMAGE_AGENTS``  the ``agents`` image (extension-absence tests only)

The declared entry points are read at test time from ``packages/tooling/pyproject.toml``
(core) and ``extensions/ops/pyproject.toml`` (extension): ``[project.scripts]``,
``[project.gui-scripts]`` and every ``[project.entry-points.<group>]`` table. The OPS
assets are the extension's declared ``[tool.setuptools.package-data]`` files.

Every container this harness starts runs with ``--network none``, carries the label
``agent-tooling-image-test=t6a`` and is force-removed by name afterwards.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_PYPROJECT = REPO_ROOT / "packages/tooling/pyproject.toml"
EXTENSION_PYPROJECT = REPO_ROOT / "extensions/ops/pyproject.toml"

PRODUCT = "AGENT_TOOLING_TEST_IMAGE"
OPS = "AGENT_TOOLING_TEST_IMAGE_OPS"
AGENTS = "AGENT_TOOLING_TEST_IMAGE_AGENTS"
VARIABLE = {"product": PRODUCT, "ops": OPS, "agents": AGENTS}

EXTENSION_PACKAGE = "kp_agent_tooling_ops"
EXTENSION_DISTRIBUTION = "kp-agent-tooling-ops"
SCRIPT_GROUPS = ("console_scripts", "gui_scripts")

LABEL = "agent-tooling-image-test=t6a"
PROTOCOL_VERSION = "2025-06-18"
RESPONSE_TIMEOUT = 180

# One configuration enables core and extension operations; the other only core ones.
# verification.guide is the order's example of an extension tool.
CORE_TOOLS = ("tooling.identity", "delivery.read")
EXTENSION_TOOLS = ("verification.guide", "verification.finding")
CONFIGS = {
    "extension": {"schema_version": "ops.agent-tooling.v1", "repos": {},
                  "enabled_tools": [*CORE_TOOLS, *EXTENSION_TOOLS]},
    "core": {"schema_version": "ops.agent-tooling.v1", "repos": {},
             "enabled_tools": list(CORE_TOOLS)},
}
# Calls made in every serve session, after tools/list.
SESSION_CALLS = ("tooling.identity",)


# --------------------------------------------------------------------------
# Environment and the declared contract


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.fail(f"{name} is unset. The T6a variant suite requires it and fails rather than "
                    f"skips (docs/work/orders/T6a-images-and-ci.md, TEST write scope).", pytrace=False)
    return value


def image_for(variant: str) -> str:
    return required(VARIABLE[variant])


@dataclass(frozen=True)
class EntryPoint:
    group: str
    name: str
    value: str

    @property
    def key(self) -> str:
        return f"{self.group}:{self.name}"


def _project(pyproject: Path) -> dict:
    if not pyproject.is_file():
        pytest.fail(f"{pyproject.relative_to(REPO_ROOT)} is absent; the declared entry points "
                    "cannot be read", pytrace=False)
    return tomllib.loads(pyproject.read_text())


def declared_entry_points(pyproject: Path) -> list[EntryPoint]:
    project = _project(pyproject)["project"]
    found = [EntryPoint("console_scripts", name, value) for name, value in project.get("scripts", {}).items()]
    found += [EntryPoint("gui_scripts", name, value) for name, value in project.get("gui-scripts", {}).items()]
    for group, table in (project.get("entry-points") or {}).items():
        found += [EntryPoint(group, name, value) for name, value in table.items()]
    assert found, f"{pyproject.relative_to(REPO_ROOT)} declares no entry point"
    return sorted(found, key=lambda e: (e.group, e.name))


def core_entry_points() -> list[EntryPoint]:
    return declared_entry_points(CORE_PYPROJECT)


def extension_entry_points() -> list[EntryPoint]:
    return declared_entry_points(EXTENSION_PYPROJECT)


def extension_script_names() -> list[str]:
    return [e.name for e in extension_entry_points() if e.group in SCRIPT_GROUPS]


def extension_assets() -> dict[str, str]:
    """``relative path -> sha256`` of every file the extension declares as package data."""
    data = _project(EXTENSION_PYPROJECT)
    setuptools = data.get("tool", {}).get("setuptools", {})
    where = (setuptools.get("packages", {}).get("find", {}).get("where") or ["."])[0]
    patterns = (setuptools.get("package-data") or {}).get(EXTENSION_PACKAGE) or []
    package_dir = EXTENSION_PYPROJECT.parent / where / EXTENSION_PACKAGE
    found = {}
    for pattern in patterns:
        for path in sorted(package_dir.glob(pattern)):
            if path.is_file():
                found[path.relative_to(package_dir).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert found, (f"extensions/ops/pyproject.toml declares no {EXTENSION_PACKAGE} package data "
                   f"that exists under {package_dir.relative_to(REPO_ROOT)}")
    return found


# --------------------------------------------------------------------------
# Docker


def docker(*args: str, timeout: float = 300) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        pytest.fail("docker CLI not found; the image suite requires Docker", pytrace=False)


def _name(kind: str) -> str:
    return f"agent-tooling-image-test-t6a-{kind}-{uuid.uuid4().hex[:10]}"


def remove(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=120,
                   stdin=subprocess.DEVNULL)


def require_image(image: str) -> None:
    result = docker("image", "inspect", "--format", "{{.Id}}", image, timeout=120)
    if result.returncode:
        pytest.fail(f"image {image!r} is not present locally: {result.stderr.strip()}", pytrace=False)


def sh(image: str, script: str, *args: str, user: str | None = None,
       timeout: float = 600) -> subprocess.CompletedProcess:
    """Run ``/bin/sh -c script`` once in the image; the image's own entry is bypassed."""
    require_image(image)
    name = _name("probe")
    argv = ["run", "--rm", "--pull", "never", "--name", name, "--label", LABEL, "--network", "none",
            "--entrypoint", "/bin/sh"]
    if user is not None:
        argv += ["--user", user]
    argv += [image, "-c", script, "sh", *args]
    try:
        return docker(*argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        pytest.fail(f"a probe in {image!r} did not finish within {timeout}s", pytrace=False)
    finally:
        remove(name)


# Runs the Python program in $1 with the first interpreter found: python3 or python on
# PATH, else the interpreter named by the kp-agent-tooling script's `#!` line.
PYTHON_LAUNCH = r"""
probe=$1; shift
for c in python3 python; do
  if i=$(command -v "$c" 2>/dev/null); then exec "$i" -c "$probe" "$@"; fi
done
if p=$(command -v kp-agent-tooling 2>/dev/null); then
  i=$(head -n 1 "$p" | sed -e 's/^#![[:space:]]*//' -e 's/[[:space:]].*$//')
  if [ -x "$i" ]; then exec "$i" -c "$probe" "$@"; fi
fi
echo "T6A-NO-PYTHON"
exit 97
"""

# Shared by the probes below: `#!` parsing and the interpreter serving kp-agent-tooling.
PROBE_PRELUDE = r'''
import json, os, re, shutil, subprocess, sys

def shebang(path):
    with open(path, "rb") as handle:
        first = handle.readline(512)
    if not first.startswith(b"#!"):
        return None
    parts = first[2:].decode(errors="replace").split()
    if not parts:
        return ""
    if os.path.basename(parts[0]) == "env":
        names = [part for part in parts[1:] if not part.startswith("-")]
        if not names:
            return parts[0]
        return shutil.which(names[0]) or ("MISSING:" + names[0])
    return parts[0]

def python_like(path):
    return bool(path) and re.fullmatch(r"(python|pypy)[0-9.]*", os.path.basename(path)) is not None

def usable(path):
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)

def serving_interpreter():
    """The interpreter of the kp-agent-tooling script, when it is a usable Python."""
    script = shutil.which("kp-agent-tooling")
    if script:
        try:
            interpreter = shebang(script)
        except OSError:
            interpreter = None
        if python_like(interpreter) and usable(interpreter):
            return interpreter
    return None

def run(argv, *payload):
    proc = subprocess.run([*argv, *payload], capture_output=True, text=True, timeout=600, cwd="/")
    lines = [line for line in proc.stdout.splitlines() if line.startswith("T6A-JSON ")]
    if not lines:
        return None, f"exit {proc.returncode}; stdout={proc.stdout[-300:]!r}; stderr={proc.stderr[-600:]!r}"
    return json.loads(lines[-1][len("T6A-JSON "):]), None
'''

ENTRY_POINT_LOADER = r'''
import importlib.metadata as metadata, json, sys
out = {}
for group, name, value in json.loads(sys.argv[1]):
    key = group + ":" + name
    want = value.replace(" ", "")
    found = [e for e in metadata.entry_points(group=group) if e.name == name]
    if not found:
        out[key] = "NOT-INSTALLED (no entry point of that name in group " + group + ")"
        continue
    match = [e for e in found if e.value.replace(" ", "") == want]
    if not match:
        out[key] = "WRONG-VALUE installed " + repr(sorted({e.value for e in found})) + ", declared " + repr(value)
        continue
    try:
        match[0].load()
        out[key] = "OK"
    except BaseException as error:
        out[key] = ("LOAD-FAILED " + type(error).__name__ + ": " + str(error))[:500]
print("T6A-JSON " + json.dumps(out))
'''

ENTRY_POINT_PROBE = PROBE_PRELUDE + r'''
spec = json.loads(sys.argv[1])
serving = serving_interpreter() or sys.executable
results, batches = {}, {}
for group, name, value in spec:
    key = group + ":" + name
    interpreter = serving
    if group in ("console_scripts", "gui_scripts"):
        path = shutil.which(name)
        if not path:
            results[key] = "NOT-ON-PATH"
            continue
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            results[key] = "NOT-EXECUTABLE " + path
            continue
        try:
            named = shebang(path)
        except OSError as error:
            results[key] = "UNREADABLE " + path + " " + str(error)
            continue
        if named is not None:
            if not usable(named):
                results[key] = "BAD-INTERPRETER " + path + " -> " + repr(named)
                continue
            if python_like(named):
                interpreter = named
    batches.setdefault(interpreter, []).append([group, name, value])
for interpreter, triples in batches.items():
    loaded, error = run([interpreter, "-c", __LOADER__], json.dumps(triples))
    for group, name, value in triples:
        key = group + ":" + name
        results[key] = (loaded or {}).get(key) or ("LOADER-FAILED in " + interpreter + ": " + str(error))
print("T6A-JSON " + json.dumps({"serving": serving, "results": results}))
'''.replace("__LOADER__", repr(ENTRY_POINT_LOADER))

EXTENSION_PRESENCE_CHECK = r'''
import importlib.metadata as metadata, importlib.util, json
package, distribution = "kp_agent_tooling_ops", "kp-agent-tooling-ops"
out = {"importable": None, "distribution": None, "entry_points": [], "core_importable": None}
try:
    core = importlib.util.find_spec("kp_agent_tooling")
    out["core_importable"] = None if core is None else (core.origin or "namespace")
except BaseException:
    pass
try:
    spec = importlib.util.find_spec(package)
    out["importable"] = None if spec is None else (spec.origin or repr(list(spec.submodule_search_locations or [])))
except BaseException as error:
    out["importable"] = None
    out["find_spec_error"] = type(error).__name__ + ": " + str(error)
try:
    out["distribution"] = metadata.distribution(distribution).version
except metadata.PackageNotFoundError:
    pass
for entry in metadata.entry_points():
    if entry.value.split(":")[0].split(".")[0] == package:
        out["entry_points"].append(entry.group + ":" + entry.name + " = " + entry.value)
print("T6A-JSON " + json.dumps(out))
'''

EXTENSION_PRESENCE_PROBE = PROBE_PRELUDE + r'''
interpreters = []
for candidate in [serving_interpreter(), sys.executable] + [shutil.which(n) for n in ("python", "python3")] + \
        [shutil.which("python3.%d" % minor) for minor in range(8, 16)]:
    if candidate and usable(candidate) and os.path.realpath(candidate) not in [os.path.realpath(i) for i in interpreters]:
        interpreters.append(candidate)
report = {}
for interpreter in interpreters:
    seen, error = run([interpreter, "-c", __CHECK__])
    report[interpreter] = seen if seen is not None else {"probe_error": error}
print("T6A-JSON " + json.dumps(report))
'''.replace("__CHECK__", repr(EXTENSION_PRESENCE_CHECK))

ASSET_CHECK = r'''
import hashlib, importlib.resources, json, sys
out = {}
for relative in json.loads(sys.argv[1]):
    try:
        data = importlib.resources.files("kp_agent_tooling_ops").joinpath(relative).read_bytes()
        out[relative] = hashlib.sha256(data).hexdigest()
    except BaseException as error:
        out[relative] = "UNREADABLE " + type(error).__name__ + ": " + str(error)[:300]
print("T6A-JSON " + json.dumps(out))
'''

ASSET_PROBE = PROBE_PRELUDE + r'''
serving = serving_interpreter()
if serving is None:
    print("T6A-JSON " + json.dumps({"error": "kp-agent-tooling is not on PATH with a usable Python #! interpreter"}))
else:
    seen, error = run([serving, "-c", __CHECK__], sys.argv[1])
    print("T6A-JSON " + json.dumps({"serving": serving, "assets": seen, "error": error}))
'''.replace("__CHECK__", repr(ASSET_CHECK))


def run_python_probe(image: str, program: str, *args: str, user: str | None = None) -> dict:
    result = sh(image, PYTHON_LAUNCH, program, *args, user=user)
    lines = [line for line in result.stdout.splitlines() if line.startswith("T6A-JSON ")]
    if not lines:
        pytest.fail(f"the probe did not report in {image!r}: exit {result.returncode}\n"
                    f"stdout: {result.stdout[-1500:]}\nstderr: {result.stderr[-1500:]}", pytrace=False)
    return json.loads(lines[-1][len("T6A-JSON "):])


def entry_point_report(image: str, declared: list[EntryPoint]) -> dict:
    """``{"serving": interpreter, "results": {group:name -> "OK" | reason}}``."""
    spec = json.dumps([[e.group, e.name, e.value] for e in declared])
    return run_python_probe(image, ENTRY_POINT_PROBE, spec)


def unresolved_entry_points(image: str, declared: list[EntryPoint]) -> list[str]:
    report = entry_point_report(image, declared)
    results = report.get("results") or {}
    return [f"{e.key} = {e.value}: {results.get(e.key, 'NOT-PROBED')}"
            for e in declared if results.get(e.key) != "OK"]


def extension_presence(image: str) -> dict:
    """Per Python interpreter in the image: is the extension importable or installed?"""
    return run_python_probe(image, EXTENSION_PRESENCE_PROBE)


def extension_asset_digests(image: str, relatives: list[str]) -> dict:
    return run_python_probe(image, ASSET_PROBE, json.dumps(relatives))


FILES_SCRIPT = r"""
find / -xdev \( -name '*kp_agent_tooling*' -o -name '*kp-agent-tooling*' \) -print 2>/dev/null \
  | sed 's/^/FILE /'
for n in "$@"; do
  if p=$(command -v "$n" 2>/dev/null); then echo "SCRIPT $n $p"; fi
done
echo T6A-FILES-DONE
"""


def _extension_named(path: str) -> bool:
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return "kp_agent_tooling_ops" in name or "kp-agent-tooling-ops" in name


def extension_files(image: str) -> tuple[list[str], list[str]]:
    """``(extension, core)``: paths on the image root filesystem named for the extension
    (plus extension console scripts on PATH), and the core ``kp_agent_tooling`` package
    directories the same search found. The core finds show the search reached the
    installed packages; without them an empty extension list proves nothing. Runs as
    root so no directory is unreadable."""
    result = sh(image, FILES_SCRIPT, *extension_script_names(), user="0:0")
    if "T6A-FILES-DONE" not in result.stdout:
        pytest.fail(f"the file probe did not finish in {image!r}: exit {result.returncode}\n"
                    f"stderr: {result.stderr[-1500:]}", pytrace=False)
    extension, core = [], []
    for line in result.stdout.splitlines():
        if line.startswith("SCRIPT "):
            extension.append(line)
        elif line.startswith("FILE "):
            path = line[len("FILE "):]
            if _extension_named(path):
                extension.append(line)
            elif path.rstrip("/").rsplit("/", 1)[-1] == "kp_agent_tooling":
                core.append(path)
    return extension, core


# --------------------------------------------------------------------------
# kp-agent-tooling ... serve over stdio MCP, inside the image

SERVE_SCRIPT = ('printf "%s" "$T6A_CONFIG" > /t6a/navigation.json '
                '&& exec kp-agent-tooling --config /t6a/navigation.json serve')


@dataclass
class ServeSession:
    image: str
    config_name: str
    argv: list
    tools: list | None = None
    calls: dict = field(default_factory=dict)
    error: str | None = None
    stderr_tail: str = ""

    def describe(self) -> str:
        return (f"image: {self.image}\nconfig ({self.config_name}): {json.dumps(CONFIGS[self.config_name])}\n"
                f"session error: {self.error}\nserver stderr:\n{self.stderr_tail}")

    def names(self) -> list[str]:
        if self.tools is None:
            pytest.fail("tools/list did not answer\n" + self.describe(), pytrace=False)
        return [tool.get("name") for tool in self.tools]

    def result(self, name: str) -> dict:
        if name not in self.calls:
            pytest.fail(f"tools/call {name} did not answer\n" + self.describe(), pytrace=False)
        return self.calls[name]


_SESSIONS: dict[tuple[str, str], ServeSession] = {}


def serve(image: str, config_name: str) -> ServeSession:
    """``kp-agent-tooling --config <config> serve`` in the image, spoken to over stdio.

    The session asks initialize, tools/list (all pages), then tools/call for each of
    SESSION_CALLS. Results are cached per image and configuration.
    """
    key = (image, config_name)
    if key not in _SESSIONS:
        require_image(image)
        _SESSIONS[key] = _serve(image, config_name)
    return _SESSIONS[key]


def _call_payload(message: dict):
    result = message.get("result") or {}
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    for item in result.get("content") or []:
        if item.get("type") == "text":
            try:
                return json.loads(item["text"])
            except ValueError:
                return {"unparsed_text": item["text"][:2000]}
    return {"raw_result": result}


def _serve(image: str, config_name: str) -> ServeSession:
    name = _name(f"serve-{config_name}")
    argv = ["docker", "run", "--rm", "-i", "--pull", "never", "--name", name, "--label", LABEL,
            "--network", "none", "--tmpfs", "/t6a:rw,exec,mode=1777",
            "-e", "T6A_CONFIG=" + json.dumps(CONFIGS[config_name]), "-e", "TMPDIR=/t6a",
            "--entrypoint", "/bin/sh", image, "-c", SERVE_SCRIPT]
    session = ServeSession(image=image, config_name=config_name, argv=argv)
    lines: queue.Queue = queue.Queue()
    stderr_chunks: list[str] = []
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, bufsize=1)

    def pump_stdout():
        for raw in process.stdout:
            lines.put(raw)
        lines.put(None)

    def pump_stderr():
        for raw in process.stderr:
            stderr_chunks.append(raw)

    threading.Thread(target=pump_stdout, daemon=True).start()
    threading.Thread(target=pump_stderr, daemon=True).start()

    def send(message):
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def reply(request_id):
        deadline = time.monotonic() + RESPONSE_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f"no response to request {request_id} within {RESPONSE_TIMEOUT}s")
            try:
                raw = lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if raw is None:
                raise RuntimeError(f"the server exited before answering request {request_id}")
            try:
                message = json.loads(raw)
            except ValueError:
                raise RuntimeError(f"the server wrote non-JSON to stdout: {raw[:300]!r}") from None
            if isinstance(message, dict) and message.get("id") == request_id:
                return message

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                         "clientInfo": {"name": "t6a-variant-test", "version": "1"}}})
        answer = reply(1)
        if "error" in answer:
            raise RuntimeError(f"initialize failed: {answer['error']}")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        collected, cursor, request_id = [], None, 2
        while True:
            send({"jsonrpc": "2.0", "id": request_id, "method": "tools/list",
                  "params": {"cursor": cursor} if cursor else {}})
            answer = reply(request_id)
            request_id += 1
            if "error" in answer:
                raise RuntimeError(f"tools/list failed: {answer['error']}")
            collected += answer["result"]["tools"]
            cursor = answer["result"].get("nextCursor")
            if not cursor:
                break
        session.tools = collected
        for tool in SESSION_CALLS:
            send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                  "params": {"name": tool, "arguments": {}}})
            answer = reply(request_id)
            request_id += 1
            session.calls[tool] = ({"jsonrpc_error": answer["error"]} if "error" in answer
                                   else _call_payload(answer))
    except (RuntimeError, OSError, KeyError, TypeError) as failure:
        session.error = f"{type(failure).__name__}: {failure}"
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=30)
        remove(name)
    time.sleep(0.1)
    session.stderr_tail = "".join(stderr_chunks)[-4000:]
    return session
