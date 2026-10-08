"""Scratch environments, frozen configurations and a stdio MCP client for the T1
boundary tests.

Every runtime observation goes through an installed distribution, never through the
test process's own imports:

* A dependency farm links every top-level entry of the running interpreter's
  site-packages, except `.pth` files, editable finders, pip, and every
  `kp-agent-tooling*` distribution (so neither the core nor the extension leaks in).
* A scratch venv under TMPDIR sees the farm through one `.pth` line; pip then installs
  copies of `packages/tooling` (core only) or `packages/tooling` + `extensions/ops`
  (core + extension) into it, non-editable, without dependencies or network.
* `kp-agent-tooling --config <config> serve` runs from that venv; the client speaks
  newline-delimited JSON-RPC over its stdio: initialize, notifications/initialized,
  tools/list. An import recorder installed in the scratch venv (a meta-path finder
  loaded first by `00_t1_boundary_import_audit.pth`, active only when
  T1_BOUNDARY_IMPORT_LOG is set) logs every module that process finds and loads,
  through `import` statements and importlib/entry-point loading alike.
  (-X importtime and the `import` audit event both miss importlib.import_module,
  which is how entry points load, so neither is used.)

The configurations are frozen in fixtures/pre_move_tools_list.json with placeholders.
`python tests/boundary/t1_harness.py capture` rebuilds that fixture; it refuses to run
unless packages/, deploy/, extensions/ and scripts/ equal the order's merge base, which
is the pre-move code.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if str(Path(__file__).resolve().parent) not in {str(Path(p).resolve()) for p in sys.path if p}:
    sys.path.insert(0, str(Path(__file__).resolve().parent))  # run as a script (capture)
from t1_contract import (  # noqa: E402
    CORE_PROJECT, EXTENSION_DISTRIBUTION, EXTENSION_PACKAGE, FAKE_GATEWAY_CATALOG, PRE_MOVE_PRODUCT_SHA256, PRE_MOVE_SURFACE,
    PRODUCT_PATHS, REPO_ROOT, is_extension_tool_name)

PROTOCOL_VERSION = "2025-06-18"
RESPONSE_TIMEOUT = 180
CORE_LIFECYCLE_MODULE = "kp_agent_tooling._impl.service.knowledge_lifecycle"
# Interface surface: each MOVE module lives at kp_agent_tooling_ops.<same relative path>.
EXTENSION_LIFECYCLE_MODULE = EXTENSION_PACKAGE + "._impl.service.knowledge_lifecycle"

CONFIG_NAMES = ("core-implicit", "core-explicit", "image-example", "extension-rich",
                "gateway", "portable")

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "T1 Boundary", "GIT_AUTHOR_EMAIL": "t1-boundary@example.invalid",
    "GIT_COMMITTER_NAME": "T1 Boundary", "GIT_COMMITTER_EMAIL": "t1-boundary@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
}


class HarnessError(RuntimeError):
    """The harness could not establish the environment or observation it needs."""


@dataclass
class Unavailable:
    """An environment the tests need but could not establish; `need` turns it into a FAILURE."""
    reason: str


def need(value):
    if isinstance(value, Unavailable):
        import pytest
        pytest.fail(value.reason, pytrace=False)
    return value


def explain(proc: subprocess.CompletedProcess) -> str:
    return (f"argv={proc.args!r}\nexit={proc.returncode}\n"
            f"stdout={(proc.stdout or '')[-4000:]}\nstderr={(proc.stderr or '')[-4000:]}")


def base_env(tmp: Path, *, home: Path | None = None, path_prefix: Path | None = None) -> dict:
    """A scrubbed environment: no PYTHONPATH/PYTHONHOME/VIRTUAL_ENV, no user site."""
    env = {
        "PATH": (str(path_prefix) + os.pathsep if path_prefix else "") + os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home) if home else os.environ.get("HOME", str(tmp)),
        "TMPDIR": str(tmp) + "/",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for key in ("USER", "LOGNAME", "LC_ALL"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


# ------------------------------------------------------------------ environments


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _excluded_top_level(name: str) -> bool:
    lowered = _normalized(name)
    return (name.endswith(".pth") or name.startswith("__editable__") or name == "__pycache__"
            or lowered.startswith("kp-agent-tooling") or lowered == "pip"
            or re.fullmatch(r"pip-[^/]*\.(?:dist|egg)-info", name) is not None)


def build_dependency_farm(dest: Path) -> Path:
    """Link the running interpreter's third-party packages, minus the products under test."""
    import importlib.metadata as metadata
    sites = []
    for key in ("purelib", "platlib"):
        site = Path(sysconfig.get_paths()[key]).resolve()
        if site.is_dir() and site not in sites:
            sites.append(site)
    product_files = set()
    for dist in metadata.distributions():
        name = _normalized(dist.metadata.get("Name") or "")
        if name.startswith("kp-agent-tooling"):
            for entry in dist.files or ():
                product_files.add(Path(entry).parts[0])
    dest.mkdir(parents=True)
    for site in sites:
        for entry in sorted(site.iterdir()):
            if _excluded_top_level(entry.name) or entry.name in product_files:
                continue
            link = dest / entry.name
            if not link.exists():
                link.symlink_to(entry)
    for needed in ("mcp", "jsonschema", "yaml"):
        if not any((dest / candidate).exists() for candidate in (needed, needed + ".py")):
            raise HarnessError(f"the running interpreter lacks {needed!r}; the scratch venv needs it")
    # setuptools (the build backend) is linked when the running interpreter has it; a
    # Python 3.14 venv does not, and build_environment then installs it into the scratch venv.
    return dest


def ensure_setuptools(python: Path, tmp: Path) -> str:
    """The scratch venv's build backend: present (from the farm), else installed into it."""
    probe = subprocess.run([str(python), "-c", "import setuptools"], capture_output=True, text=True,
                           timeout=120, env=base_env(tmp))
    if probe.returncode == 0:
        return "present"
    argv = [sys.executable, "-m", "pip", "--python", str(python), "install", "--no-cache-dir",
            "--disable-pip-version-check", "--only-binary", ":all:", "setuptools"]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=900, env=base_env(tmp), cwd=str(tmp))
    if proc.returncode:
        raise HarnessError("the scratch venv has no setuptools (the running interpreter lacks it) and pip could "
                           "not install it into the scratch venv\n" + explain(proc))
    return "installed"


def _copy_project(project: Path, dest: Path) -> Path:
    shutil.copytree(project, dest, symlinks=False, ignore=shutil.ignore_patterns(
        "build", "dist", "*.egg-info", "__pycache__", ".pytest_cache", "*.pyc"))
    return dest


@dataclass
class Environment:
    root: Path
    python: Path
    bin: Path
    tmp: Path
    with_extension: bool
    install_log: str = ""

    def env(self, *, home: Path | None = None, **extra) -> dict:
        return dict(base_env(self.tmp, home=home, path_prefix=self.bin), **extra)

    def run(self, argv: list, *, timeout: int = 300, home: Path | None = None,
            cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=timeout,
                              env=self.env(home=home), cwd=str(cwd or self.tmp),
                              stdin=subprocess.DEVNULL)

    def python_json(self, code: str, *args, timeout: int = 300):
        proc = self.run([self.python, "-c", code, *args], timeout=timeout)
        if proc.returncode:
            raise HarnessError("scratch interpreter snippet failed\n" + explain(proc))
        return json.loads(proc.stdout)


def build_environment(dest: Path, farm: Path, projects: list[Path], *, with_extension: bool) -> Environment:
    """A scratch venv holding non-editable installs of copies of `projects`."""
    dest.mkdir(parents=True)
    tmp = dest / "tmp"
    tmp.mkdir()
    venv_dir = dest / "venv"
    proc = subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv_dir)],
                          capture_output=True, text=True, timeout=300, env=base_env(tmp))
    if proc.returncode:
        raise HarnessError("could not create a scratch venv\n" + explain(proc))
    python = venv_dir / "bin" / "python"
    proc = subprocess.run([str(python), "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
                          capture_output=True, text=True, timeout=120, env=base_env(tmp))
    if proc.returncode:
        raise HarnessError("scratch venv interpreter does not start\n" + explain(proc))
    purelib = Path(proc.stdout.strip())
    (purelib / "t1_boundary_dependencies.pth").write_text(str(farm) + "\n")
    (purelib / "t1_boundary_import_audit.py").write_text(IMPORT_RECORDER)
    (purelib / "00_t1_boundary_import_audit.pth").write_text("import t1_boundary_import_audit\n")
    ensure_setuptools(python, tmp)
    copies = []
    for project in projects:
        relative = project.resolve().relative_to(REPO_ROOT)
        copies.append(_copy_project(project, dest / "sources" / relative))
    argv = [sys.executable, "-m", "pip", "--python", str(python), "install", "--no-deps",
            "--no-build-isolation", "--no-index", "--no-cache-dir", "--disable-pip-version-check",
            *[str(c) for c in copies]]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=900, env=base_env(tmp),
                          cwd=str(tmp))
    if proc.returncode:
        raise HarnessError("pip could not install " + ", ".join(str(p) for p in projects)
                           + " into the scratch venv\n" + explain(proc))
    environment = Environment(root=dest, python=python, bin=venv_dir / "bin", tmp=tmp,
                              with_extension=with_extension, install_log=proc.stdout[-2000:])
    if not (environment.bin / "kp-agent-tooling").is_file():
        raise HarnessError("the installed core has no kp-agent-tooling console script")
    return environment


IMPORT_RECORDER = r'''"""T1 boundary instrument: log every module this process finds and loads.

Active only when T1_BOUNDARY_IMPORT_LOG names a file. It asks the remaining meta-path
finders for the spec, logs the name when one is found, and returns that same spec.
"""
import os
import sys

_path = os.environ.get("T1_BOUNDARY_IMPORT_LOG")


class _Recorder:
    def __init__(self, handle):
        self.handle = handle

    def find_spec(self, fullname, path=None, target=None):
        for finder in list(sys.meta_path):
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                self.handle.write(fullname + "\n")
                return spec
        return None


if _path:
    sys.meta_path.insert(0, _Recorder(open(_path, "a", buffering=1)))
'''


PRESENCE_PROBE = r"""
import importlib.metadata, importlib.util, json, sys
def dist(name):
    try:
        return importlib.metadata.distribution(name).version
    except importlib.metadata.PackageNotFoundError:
        return None
print(json.dumps({'core': dist('kp-agent-tooling'), 'extension': dist(sys.argv[1]),
                  'extension_package': importlib.util.find_spec(sys.argv[2]) is not None}))
"""


def presence(environment: Environment) -> dict:
    return environment.python_json(PRESENCE_PROBE, EXTENSION_DISTRIBUTION, EXTENSION_PACKAGE)


# ---------------------------------------------------------------- fake gateway


class FakeGateway:
    """A loopback HTTP JSON-RPC endpoint that answers initialize and tools/list only."""

    def __init__(self, catalog: dict):
        self.catalog = catalog
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                request = json.loads(self.rfile.read(length) or b"{}")
                method = request.get("method")
                if method == "initialize":
                    result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                              "serverInfo": {"name": "t1-fake-gateway", "version": "1"}}
                    body = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
                elif method == "tools/list":
                    body = {"jsonrpc": "2.0", "id": request.get("id"), "result": gateway.catalog}
                else:
                    body = {"jsonrpc": "2.0", "id": request.get("id"),
                            "error": {"code": -32601, "message": "not available"}}
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/api/mcp"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()


# ------------------------------------------------------------------ the world


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env=dict(os.environ, **_GIT_ENV))
    if proc.returncode:
        raise HarnessError(f"git {args} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo(path: Path, files: dict[str, str]) -> str:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    past = time.time() - 300
    for rel, text in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        os.utime(target, (past, past))
    git(path, "add", "-A")
    git(path, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "T1 boundary fixture")
    git(path, "status", "--porcelain")
    return git(path, "rev-parse", "HEAD")


PRODUCT_FILES = {
    "README.md": "T1 boundary fixture repository.\n",
    "src/app.py": "def answer():\n    return 42\n",
    "evidence/summary.json": json.dumps({"elapsed_ms": 1}) + "\n",
    "evidence/report.md": "# Report\nfixture\n",
    ".serena/project.yml": "read_only: true\n",
}


def substitute(value, values: dict):
    if isinstance(value, str):
        for placeholder, replacement in values.items():
            if placeholder in value:
                value = value.replace(placeholder, replacement)
        return value
    if isinstance(value, list):
        return [substitute(v, values) for v in value]
    if isinstance(value, dict):
        return {substitute(k, values): substitute(v, values) for k, v in value.items()}
    return value


def write_private_json(path: Path, value) -> str:
    path.write_text(json.dumps(value, indent=1, sort_keys=True))
    path.chmod(0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class World:
    """Repositories, directories and auxiliary files the frozen configs point at."""
    root: Path
    values: dict = field(default_factory=dict)

    def write_config(self, name: str, template: dict) -> Path:
        path = self.root / "configs" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = substitute(template, self.values)
        leftover = re.findall(r"@[A-Z_0-9]+@", json.dumps(rendered))
        if leftover:
            raise HarnessError(f"config {name} has unresolved placeholders {sorted(set(leftover))}")
        write_private_json(path, rendered)
        return path


def make_world(root: Path, aux: dict, *, gateway_url: str | None = None) -> World:
    root = root.resolve()
    root.mkdir(parents=True)
    world = World(root=root)
    values = world.values
    for key, name, files in (("PRODUCT", "product", PRODUCT_FILES),
                             ("ATS", "ats", {"README.md": "ats fixture\n"}),
                             ("CORE", "core", {"README.md": "core fixture\n"})):
        repo = root / "repos" / name
        values[f"@REV_{key}@"] = make_repo(repo, files)
        values[f"@REPO_{key}@"] = str(repo)
    for key, name in (("DELIVERY", "delivery"), ("SNAPSHOTS", "snapshots"), ("SEARCH", "search")):
        directory = root / "state" / name
        directory.mkdir(parents=True, mode=0o700)
        values[f"@{key}@"] = str(directory)
    values["@GATEWAY_URL@"] = gateway_url or "http://127.0.0.1:9/unconfigured"
    aux_dir = root / "aux"
    aux_dir.mkdir()
    local = aux_dir / "local-knowledge.json"
    write_private_json(local, substitute(aux["local_knowledge_catalog"], values))
    values["@LOCAL_KNOWLEDGE@"] = str(local)
    return world


def provision_portable(world: World, aux: dict, environment: Environment, lifecycle_module: str) -> None:
    """Write the portable knowledge runtime; its lifecycle ledger is initialized by
    `lifecycle_module` inside `environment` (pre-move: the core's module; post-move:
    the extension's module at the same relative path)."""
    values = world.values
    aux_dir = world.root / "aux"
    catalog = aux_dir / "portable-catalog.json"
    write_private_json(catalog, substitute(aux["portable_catalog"], values))
    corpus = aux_dir / "portable-corpus.json"
    values["@PORTABLE_CORPUS_SHA256@"] = write_private_json(corpus, aux["portable_corpus"])
    values["@PORTABLE_CATALOG@"] = str(catalog)
    values["@PORTABLE_CORPUS@"] = str(corpus)
    ledger = aux_dir / "lifecycle" / "lifecycle.sqlite3"
    init = ("import importlib, json, sys\n"
            "importlib.import_module(sys.argv[1]).LifecycleLedger.initialize(sys.argv[2])\n"
            "print(json.dumps('ok'))\n")
    environment.python_json(init, lifecycle_module, str(ledger))
    values["@LIFECYCLE@"] = str(ledger)
    runtime = aux_dir / "portable-runtime.json"
    write_private_json(runtime, substitute(aux["portable_runtime"], values))
    values["@PORTABLE_KNOWLEDGE@"] = str(runtime)


# ------------------------------------------------------------- stdio listing


@dataclass
class Listing:
    argv: list
    tools: list | None
    error: str | None
    loaded_modules: list
    stderr_tail: str

    def names(self) -> list[str]:
        return [tool["name"] for tool in self.tools or ()]


def _loaded_modules(log: Path) -> list[str]:
    return [line.strip() for line in log.read_text().splitlines() if line.strip()] if log.exists() else []


def list_tools_over_stdio(environment: Environment, config: Path, *, label: str) -> Listing:
    """Run `kp-agent-tooling --config <config> serve` and ask it tools/list over stdio."""
    argv = [str(environment.bin / "kp-agent-tooling"), "--config", str(config), "serve"]
    stem = environment.tmp / f"serve-{label}-{os.getpid()}-{time.monotonic_ns()}"
    stderr_path, import_log = stem.with_suffix(".stderr"), stem.with_suffix(".imports")
    lines: queue.Queue = queue.Queue()
    error = None
    tools = None
    with stderr_path.open("w") as stderr:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                   env=environment.env(T1_BOUNDARY_IMPORT_LOG=str(import_log)),
                                   cwd=str(environment.tmp), text=True, bufsize=1)

        def pump():
            for raw in process.stdout:
                lines.put(raw)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()

        def send(message):
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

        def reply(request_id):
            deadline = time.monotonic() + RESPONSE_TIMEOUT
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HarnessError(f"no response to request {request_id} within {RESPONSE_TIMEOUT}s")
                try:
                    raw = lines.get(timeout=remaining)
                except queue.Empty:
                    continue
                if raw is None:
                    raise HarnessError(f"server exited before answering request {request_id}")
                try:
                    message = json.loads(raw)
                except ValueError:
                    raise HarnessError(f"server wrote non-JSON to stdout: {raw[:300]!r}") from None
                if isinstance(message, dict) and message.get("id") == request_id:
                    return message

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                             "clientInfo": {"name": "t1-boundary", "version": "1"}}})
            answer = reply(1)
            if "error" in answer:
                raise HarnessError(f"initialize failed: {answer['error']}")
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            collected, cursor, request_id = [], None, 2
            while True:
                send({"jsonrpc": "2.0", "id": request_id, "method": "tools/list",
                      "params": {"cursor": cursor} if cursor else {}})
                answer = reply(request_id)
                if "error" in answer:
                    raise HarnessError(f"tools/list failed: {answer['error']}")
                collected += answer["result"]["tools"]
                cursor = answer["result"].get("nextCursor")
                request_id += 1
                if not cursor:
                    break
            tools = collected
        except (HarnessError, OSError, KeyError, TypeError) as failure:
            error = f"{type(failure).__name__}: {failure}"
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=20)
    stderr_text = stderr_path.read_text(errors="replace")
    return Listing(argv=argv, tools=tools, error=error, loaded_modules=_loaded_modules(import_log),
                   stderr_tail=stderr_text[-4000:])


# -------------------------------------------------------------- the fixture


def initial_templates(image_example: dict) -> dict:
    product = {"product": {"path": "@REPO_PRODUCT@", "revision": "@REV_PRODUCT@"}}
    state = {"delivery_root": "@DELIVERY@", "snapshot_registry": "@SNAPSHOTS@"}
    image = copy.deepcopy(image_example)
    replacements = {"/workspace/repo": "@REPO_PRODUCT@", "REPLACE_WITH_FULL_COMMIT": "@REV_PRODUCT@",
                    "/state/snapshots": "@SNAPSHOTS@", "/state/search": "@SEARCH@"}
    image = substitute(image, replacements)
    return {
        "core-implicit": {"schema_version": "ops.agent-tooling.v1", "repos": product,
                          "local_knowledge_config": "@LOCAL_KNOWLEDGE@",
                          "navigation_registry_path": "@SEARCH@", **state},
        "image-example": image,
        "extension-rich": {"schema_version": "ops.agent-tooling.v1",
                           "repos": {"ats": {"path": "@REPO_ATS@", "revision": "@REV_ATS@"},
                                     "core": {"path": "@REPO_CORE@", "revision": "@REV_CORE@"},
                                     **product},
                           "evidence_repo": "@REPO_PRODUCT@", "evidence_revision": "@REV_PRODUCT@",
                           "evidence": {"worker-summary": "evidence/summary.json",
                                        "worker-report": "evidence/report.md"},
                           "review_contracts": {"contract-alpha": {"specification": {}},
                                                "contract-beta": {"specification": {}}},
                           **state},
        "gateway": {"schema_version": "ops.agent-tooling.v1", "repos": product,
                    "gateway_endpoint": "@GATEWAY_URL@", **state},
        "portable": {"schema_version": "ops.agent-tooling.v1", "repos": product,
                     "portable_knowledge_config": "@PORTABLE_KNOWLEDGE@", **state},
    }


AUX_TEMPLATES = {
    "local_knowledge_catalog": {
        "repositories": {"product": {"path": "@REPO_PRODUCT@"}},
        "platforms": {"product": {"sources": {"product": {"revision": "@REV_PRODUCT@"}}}},
        "scip_indexes": {}},
    "portable_catalog": {
        "schema_version": "ops.knowledge-config.v1",
        "repositories": {"product": {"path": "@REPO_PRODUCT@", "ref": "@REV_PRODUCT@",
                                     "default_branch_ref": "refs/heads/main", "corpus_scope": "product",
                                     "tenant_ids": ["tenant-a"], "capabilities": {},
                                     "artifacts": {"README.md": {"owner": "fixture", "status": "maintained"}}}}},
    "portable_corpus": {"schema_version": "agent-tooling.document-corpus.v1", "chunks": [], "vectors": []},
    "portable_runtime": {
        "schema_version": "agent-tooling.knowledge-runtime.v1", "tenant_id": "tenant-a",
        "lifecycle_path": "@LIFECYCLE@", "catalog_path": "@PORTABLE_CATALOG@",
        "documents": {"path": "@PORTABLE_CORPUS@", "sha256": "@PORTABLE_CORPUS_SHA256@",
                      "repositories": {"product": "@REPO_PRODUCT@"}},
        "embedding": {}},
}


def product_tree_sha256(revision: str = "HEAD") -> str:
    """The content hash of a commit's product tree: sha256 of `git ls-tree -r` over PRODUCT_PATHS."""
    listing = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-tree", "-r", revision, "--", *PRODUCT_PATHS],
                             capture_output=True, check=True).stdout
    return hashlib.sha256(listing).hexdigest()


def _require_merge_base_tree() -> str:
    """The product tree (packages, deploy, extensions, scripts) must be the merge base's: committed at HEAD
    with the content hash PRE_MOVE_PRODUCT_SHA256, with no change or untracked file in the working tree.

    That tree is in the private history only, so this capture cannot be regenerated from the public history
    as it stands."""
    git_c = ["git", "-C", str(REPO_ROOT)]
    changed = subprocess.run([*git_c, "diff", "--name-only", "HEAD", "--", *PRODUCT_PATHS],
                             capture_output=True, text=True, check=True).stdout.strip()
    untracked = subprocess.run([*git_c, "ls-files", "--others", "--exclude-standard", "--", *PRODUCT_PATHS],
                               capture_output=True, text=True, check=True).stdout.strip()
    digest = product_tree_sha256()
    if changed or untracked or digest != PRE_MOVE_PRODUCT_SHA256:
        raise HarnessError("capture needs the merge-base product tree, content sha256 "
                           f"{PRE_MOVE_PRODUCT_SHA256}; HEAD's is {digest}:\n{changed}\n{untracked}")
    return digest


def capture(out: Path) -> dict:
    product_sha256 = _require_merge_base_tree()
    image_path = REPO_ROOT / "deploy" / "tooling" / "image" / "config.example.json"
    image_raw = image_path.read_bytes()
    templates = initial_templates(json.loads(image_raw))
    gateway_catalog = json.loads(FAKE_GATEWAY_CATALOG.read_text())
    with tempfile.TemporaryDirectory(prefix="t1-capture-") as scratch:
        scratch = Path(scratch).resolve()
        farm = build_dependency_farm(scratch / "farm")
        environment = build_environment(scratch / "merge-base", farm, [CORE_PROJECT], with_extension=False)
        with FakeGateway(gateway_catalog) as gateway:
            world = make_world(scratch / "world", AUX_TEMPLATES, gateway_url=gateway.url)
            provision_portable(world, AUX_TEMPLATES, environment, CORE_LIFECYCLE_MODULE)
            results = {}
            for name in ("core-implicit", "image-example", "extension-rich", "gateway", "portable"):
                results[name] = list_tools_over_stdio(environment, world.write_config(name, templates[name]),
                                                      label=name)
                if results[name].error:
                    raise HarnessError(f"merge-base listing for {name} failed: {results[name].error}\n"
                                       + results[name].stderr_tail)
            core_names = [n for n in results["core-implicit"].names() if not is_extension_tool_name(n)]
            templates["core-explicit"] = dict(templates["core-implicit"], enabled_tools=core_names)
            results["core-explicit"] = list_tools_over_stdio(
                environment, world.write_config("core-explicit", templates["core-explicit"]), label="core-explicit")
            if results["core-explicit"].error:
                raise HarnessError("merge-base listing for core-explicit failed: " + results["core-explicit"].error)
        versions = environment.python_json(
            "import importlib.metadata as m, json, sys;"
            "print(json.dumps({'python': sys.version.split()[0], 'mcp': m.version('mcp'),"
            " 'jsonschema': m.version('jsonschema'), 'kp-agent-tooling': m.version('kp-agent-tooling')}))")
    document = {
        "schema_version": "t1-boundary.pre-move-tools-list.v1",
        "order": "docs/work/orders/T1-extension-seam.md",
        "pre_move_product_sha256": product_sha256,
        "captured_by": "python tests/boundary/t1_harness.py capture",
        "server_argv": ["kp-agent-tooling", "--config", "<rendered config>", "serve"],
        "protocol_version": PROTOCOL_VERSION,
        "versions": versions,
        "image_example_source": {"path": str(image_path.relative_to(REPO_ROOT)),
                                 "sha256": hashlib.sha256(image_raw).hexdigest()},
        "aux_templates": AUX_TEMPLATES,
        "configs": {name: {"template": templates[name], "tools": results[name].tools}
                    for name in CONFIG_NAMES},
    }
    out.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
    return document


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["capture"])
    parser.add_argument("--out", type=Path, default=PRE_MOVE_SURFACE)
    args = parser.parse_args(argv)
    document = capture(args.out)
    for name, row in document["configs"].items():
        print(f"{name}: {len(row['tools'])} tools")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
