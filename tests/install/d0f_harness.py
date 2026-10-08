"""Contract harness for order D0f, F4 (docs/work/orders/D0f-board-without-agent-sdk.md), image-marked tests.

Public surfaces only, as tests/install/t9b_harness.py: the `kp-agent-install` console script, the
rendered Compose project driven with `docker compose --project-directory "$root"`, `docker inspect`,
`docker diff`, `docker logs`, and processes run in role containers with `docker exec`. The board is
driven through its own HTTP interface, from inside its container (see Board below). No implementation
module is imported.

One test overlay, outside the runtime root, joins the rendered COMPOSE_FILE list. It changes the board
role only in what the action reaches: the board joins an internal network (no route to the internet)
shared with a stand-in npm registry, loses its published port (an internal network publishes none),
and gets `npm_config_registry` pointing at the stand-in (the S5 seam). The stand-in registry runs the
image under test's own python3, read-only, and records every request it receives. It serves, as
reconciled at the meet: @anthropic-ai/claude-agent-sdk at the lockfile's pin and
ai-sdk-provider-claude-code at its pin as published, byte for byte (the install action checks every
package at a version the board's lockfile pins against the lockfile's integrity), with their
dependencies at the lockfile's versions, all read from npm's content-addressed cache by the lockfile's
integrity (npm ci fills it; nothing is fetched from the internet); and a stand-in SDK, no Anthropic
code, at an older version and at a newer `latest`. The SDK's platform packages (its native binary) are
not served; npm skips them as optional.

Every Compose project, container, network and volume these tests create is named `d0f-test-...` (the
arm's Docker rule: `d0f-` only). Each project lives in a never-reused directory under TMPDIR and is
taken down with its volumes.

Seams (one place here; the Kanban tests carry the same spellings in
apps/kanban/test/integration/d0f/d0f-seams.ts), reconciled with FEATURE's (the FEATURE arm's seams):
  S2 status query  runtime.getClaudeAgentSdkStatus. The tests read {installed, version, defaultVersion,
                   notice, noticeSha256} (parse_status): installed <- state ("installed" True,
                   "not_installed" False, any other state kept as that string), version <-
                   installed.sdkVersion (equal to provider.sdkVersion when installed), defaultVersion <-
                   pinned.sdkVersion, notice <- notice.text, noticeSha256 <- notice.sha256
  S3 install       runtime.installClaudeAgentSdk {acknowledgedNoticeSha256, sdkVersion?} (install_input:
                   {acknowledged: true, noticeSha256, version?}) -> {ok: true, code: "installed"}
  S5 fetch route   npm, honouring npm_config_registry in the board's environment
  S6 location      FEATURE: /state/kanban/kanban/optional-packages/claude-agent-sdk; the tests look
                   anywhere under /state for node_modules/@anthropic-ai/claude-agent-sdk/package.json
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from s3_harness import World, docker_env, explain, read_dotenv
from t7a_harness import Runtime, free_port, required_docker, resolve_image
from t9b_harness import PROCESSES, exec_python, install, required_image

PREFIX = "d0f-test"
ROOT = Path(__file__).resolve().parents[2]
LOCKFILE = ROOT / "apps" / "kanban" / "package-lock.json"
SDK = "@anthropic-ai/claude-agent-sdk"
PROVIDER = "ai-sdk-provider-claude-code"
STATUS_QUERY = "runtime.getClaudeAgentSdkStatus"
INSTALL_MUTATION = "runtime.installClaudeAgentSdk"
REGISTRY_ENV = "npm_config_registry"
INSTALLED_SUFFIX = "node_modules/@anthropic-ai/claude-agent-sdk/package.json"
REGISTRY_SERVICE = "d0f-registry"
REGISTRY_PORT = 4873
BOARD_PORT = 3486
# Files Docker itself writes into a container's own filesystem.
DOCKER_MANAGED = {"/etc/hosts", "/etc/hostname", "/etc/resolv.conf", "/etc/mtab", "/.dockerenv"}


def lockfile_pin(name: str) -> str:
    packages = json.loads(LOCKFILE.read_text()).get("packages") or {}
    version = (packages.get(f"node_modules/{name}") or {}).get("version")
    assert version, f"apps/kanban/package-lock.json pins no {name}: 'the version the lockfile pins' is unreadable"
    return version


def stand_in_versions() -> dict[str, str]:
    major, minor, patch = (int(part) for part in lockfile_pin(SDK).split("."))
    return {"pin": f"{major}.{minor}.{patch}", "older": f"{major}.{minor}.{patch - 1 if patch else 1}",
            "latest": f"{major}.{minor}.{patch + 871}"}


# ------------------------------------------------------------- stand-in registry

SDK_SOURCE = """\
// D0f test stand-in for @anthropic-ai/claude-agent-sdk: no Anthropic code, no model call.
export const D0F_STAND_IN = true;
export function query() {
\tconst iterator = (async function* () {
\t\tthrow new Error("d0f stand-in claude-agent-sdk: entry point reached; no model call is made");
\t})();
\treturn Object.assign(iterator, { interrupt: async () => {}, close: () => {} });
}
export function tool(name, description, inputSchema, handler) {
\treturn { name, description, inputSchema, handler };
}
export function createSdkMcpServer(options) {
\treturn { type: "sdk", name: options?.name, instance: {} };
}
"""

SERVER = r'''
import http.server, json, sys, time, urllib.parse
INDEX = json.load(open("/registry/index.json"))

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, kind):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        with open("/log/requests.jsonl", "a") as log:
            log.write(json.dumps({"method": "GET", "path": self.path, "at": time.time()}) + "\n")
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/-/d0f-tarball/"):
            name, version = (urllib.parse.unquote(p) for p in path[len("/-/d0f-tarball/"):].rsplit("/", 1))
            entry = INDEX.get(name, {}).get("versions", {}).get(version[:-4] if version.endswith(".tgz") else version)
            if not entry:
                return self._send(404, b'{"error":"not found"}', "application/json")
            return self._send(200, open("/registry/" + entry["file"], "rb").read(), "application/octet-stream")
        name = urllib.parse.unquote(path[1:])
        package = INDEX.get(name)
        if not package:
            return self._send(404, b'{"error":"not found"}', "application/json")
        host = self.headers.get("Host") or "%s:%d" % (self.server.server_name, self.server.server_port)
        versions = {}
        for version, entry in package["versions"].items():
            versions[version] = dict(entry["manifest"], _id=name + "@" + version, dist={
                "tarball": "http://%s/-/d0f-tarball/%s/%s.tgz" % (host, urllib.parse.quote(name, safe=""), version),
                "shasum": entry["shasum"], "integrity": entry["integrity"]})
        body = json.dumps({"name": name, "dist-tags": {"latest": package["latest"]}, "versions": versions})
        return self._send(200, body.encode(), "application/json")

http.server.ThreadingHTTPServer(("0.0.0.0", @PORT@), Handler).serve_forever()
'''.replace("@PORT@", str(REGISTRY_PORT))


def _tarball(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(f"package/{name}")
            info.size, info.mode, info.mtime = len(content), 0o644, 0
            archive.addfile(info, io.BytesIO(content))
    return gzip.compress(buffer.getvalue(), mtime=0)


def _npm_cache() -> Path:
    """npm's cache directory: npm_config_cache, else `npm config get cache`, else ~/.npm."""
    configured = os.environ.get("npm_config_cache") or os.environ.get("NPM_CONFIG_CACHE")
    if configured:
        return Path(configured)
    npm = shutil.which("npm")
    if npm:
        proc = subprocess.run([npm, "config", "get", "cache"], capture_output=True, text=True, timeout=60)
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip())
    return Path.home() / ".npm"


def _tarball_manifest(tarball: bytes) -> dict:
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as archive:
        for member in archive:
            if member.isfile() and member.name.split("/", 1)[-1] == "package.json" and member.name.count("/") == 1:
                handle = archive.extractfile(member)
                if handle is not None:
                    return json.loads(handle.read())
    raise AssertionError("a published tarball without package.json")


def published_closure() -> dict[str, list[tuple[str, str, bytes, dict]]]:
    """The packages the action fetches at the lockfile's versions, as published: the SDK and the provider
    at their pins and every package their lockfile entries depend on (dependencies, peer and optional
    dependencies; every lockfile entry of each name), except the SDK's platform packages. Each tarball is
    read from npm's content-addressed cache by the lockfile's integrity and checked against it.
    Returns name -> [(version, integrity, tarball, manifest)]."""
    packages = json.loads(LOCKFILE.read_text()).get("packages") or {}
    by_name: dict[str, list[dict]] = {}
    for path, entry in packages.items():
        if "node_modules/" in path:
            by_name.setdefault(path.rsplit("node_modules/", 1)[1], []).append(entry)
    cache = _npm_cache() / "_cacache" / "content-v2" / "sha512"
    closure: dict[str, list[tuple[str, str, bytes, dict]]] = {}
    queue, seen, missing = [SDK, PROVIDER], set(), []
    while queue:
        name = queue.pop()
        if name in seen or name.startswith(SDK + "-"):
            continue
        seen.add(name)
        for entry in by_name.get(name, []):
            for key in ("dependencies", "peerDependencies", "optionalDependencies"):
                queue.extend(entry.get(key) or {})
            version, integrity = entry.get("version"), entry.get("integrity") or ""
            algorithm, _, digest = integrity.partition("-")
            if algorithm != "sha512" or not version:
                missing.append(f"{name}@{version} (integrity {integrity or 'none'})")
                continue
            hexdigest = base64.b64decode(digest).hex()
            blob = cache / hexdigest[:2] / hexdigest[2:4] / hexdigest[4:]
            if not blob.is_file() or hashlib.sha512(blob.read_bytes()).hexdigest() != hexdigest:
                missing.append(f"{name}@{version}")
                continue
            tarball = blob.read_bytes()
            versions = closure.setdefault(name, [])
            if all(v != version for v, *_ in versions):
                versions.append((version, integrity, tarball, _tarball_manifest(tarball)))
    pins = [(n, lockfile_pin(n)) for n in (SDK, PROVIDER)]
    absent = [f"{n}@{v}" for n, v in pins if all(version != v for version, *_ in closure.get(n, []))]
    assert not absent and not missing, (
        f"npm's cache ({cache}) lacks the published tarballs the action fetches at the lockfile's versions: "
        f"{absent + missing}; run npm ci in apps/kanban (or npm cache add each)")
    return closure


def write_registry(directory: Path) -> dict[str, str]:
    """The stand-in registry's files: index.json, the tarballs and server.py. Returns the SDK versions."""
    directory.mkdir(parents=True, exist_ok=True)
    versions = stand_in_versions()
    index: dict = {}

    def store(name: str, version: str, manifest: dict, tarball: bytes, latest: str, integrity: str) -> None:
        file = f"{hashlib.sha256((name + version).encode()).hexdigest()[:16]}.tgz"
        (directory / file).write_bytes(tarball)
        entry = index.setdefault(name, {"latest": latest, "versions": {}})
        entry["versions"][version] = {
            "manifest": manifest, "file": file, "shasum": hashlib.sha1(tarball).hexdigest(),
            "integrity": integrity}

    def add(name: str, version: str, manifest: dict, files: dict[str, bytes], latest: str) -> None:
        manifest = {"name": name, "version": version, "type": "module", "license": "UNLICENSED", **manifest}
        tarball = _tarball({"package.json": (json.dumps(manifest, indent=2) + "\n").encode(), **files})
        store(name, version, manifest, tarball, latest,
              "sha512-" + base64.b64encode(hashlib.sha512(tarball).digest()).decode())

    for name, found in published_closure().items():
        latest = versions["latest"] if name == SDK else found[0][0]
        for version, integrity, tarball, manifest in found:
            store(name, version, manifest, tarball, latest, integrity)
    for label in ("older", "latest"):
        add(SDK, versions[label], {"main": "sdk.mjs", "exports": {".": "./sdk.mjs", "./package.json": "./package.json"},
                                   "description": "D0f test stand-in; no Anthropic code."},
            {"sdk.mjs": SDK_SOURCE.encode(), "README.md": b"D0f test stand-in.\n"}, versions["latest"])
    (directory / "index.json").write_text(json.dumps(index, indent=1))
    (directory / "server.py").write_text(SERVER)
    for path in directory.iterdir():
        path.chmod(0o644)
    directory.chmod(0o755)
    return versions


# --------------------------------------------------------------------- projects


def fresh_directory(label: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"{PREFIX}-{label}-")).resolve()


def image_world(label: str, components) -> tuple[World, str]:
    """t9b_harness.image_world with a `d0f-test-*` project."""
    docker = required_docker()
    ref = required_image()
    run_id = uuid.uuid4().hex[:8]
    world = World.create(fresh_directory(f"{label}-{run_id}"), components=tuple(components),
                         uid=os.getuid() or 10001, gid=os.getgid() or 10001, board_port=free_port(),
                         project=f"{PREFIX}-{label}-{run_id}")
    return replace(world, image=resolve_image(docker, ref, docker_env(world.home))), docker


class Project(Runtime):
    """A rendered, installed root with the D0f overlay; taken down with its volumes and network."""

    def __init__(self, docker: str, world: World):
        super().__init__(docker, world)
        assert self.project.startswith(PREFIX + "-"), self.project
        self.registry_dir = world.base / "d0f-registry"
        self.registry_log = world.base / "d0f-registry-log"
        self.versions: dict[str, str] = {}

    def add_overlay(self) -> None:
        self.versions = write_registry(self.registry_dir)
        self.registry_log.mkdir(mode=0o777, exist_ok=True)
        self.registry_log.chmod(0o777)
        rendered = read_dotenv(self.root / ".env").get("COMPOSE_FILE", "")
        assert rendered, "the rendered .env names no COMPOSE_FILE"
        overlay = self.world.base / "compose.d0f-test.yaml"
        # Written as text: `!reset` (Compose's tag to drop the published port) has no yaml.safe_dump form.
        overlay.write_text(f"""\
services:
  board:
    networks: [d0f]
    ports: !reset []
    environment:
      {REGISTRY_ENV}: http://{REGISTRY_SERVICE}:{REGISTRY_PORT}/
      npm_config_update_notifier: "false"
      npm_config_audit: "false"
      npm_config_fund: "false"
  {REGISTRY_SERVICE}:
    profiles: [board]
    image: {self.world.image}
    pull_policy: never
    user: "{self.world.uid}:{self.world.gid}"
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    networks: [d0f]
    entrypoint: [python3, /registry/server.py]
    volumes:
      - {{type: bind, source: "{self.registry_dir}", target: /registry, read_only: true}}
      - {{type: bind, source: "{self.registry_log}", target: /log}}
networks:
  d0f:
    internal: true
""")
        self.env["COMPOSE_FILE"] = rendered + os.pathsep + str(overlay)

    def rendered_service(self, name: str) -> dict:
        """A service as `docker compose config` renders the root itself, without the test overlay."""
        env = dict(self.env)
        env.pop("COMPOSE_FILE", None)
        proc = subprocess.run([self.docker, "compose", "--project-directory", str(self.root), "config",
                               "--format", "json"], cwd=self.root, env=env, capture_output=True, text=True,
                              timeout=120)
        assert proc.returncode == 0, "docker compose config rejected the rendered root\n" + explain(proc)
        return (json.loads(proc.stdout).get("services") or {}).get(name) or {}

    def up(self, *services: str) -> None:
        up = self.compose("up", "-d", "--no-deps", "--wait", "--wait-timeout", "300", *services, timeout=900)
        if up.returncode != 0:
            logs = {s: self.logs(c)[-2500:] for s in services if (c := self.container(s))}
            pytest.fail(f"`docker compose up {' '.join(services)}` failed\n" + explain(up) +
                        f"\nlogs={json.dumps(logs, indent=1)}", pytrace=False)

    def requests(self) -> list[dict]:
        path = self.registry_log / "requests.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def down(self) -> list[str]:
        self.started = True
        remaining = super().down()
        label = f"label=com.docker.compose.project={self.project}"
        for name in self.docker_run("volume", "ls", "-q", "--filter", label).stdout.split():
            self.docker_run("volume", "rm", "-f", name)
        for name in self.docker_run("volume", "ls", "-q", "--filter", f"name={self.project}").stdout.split():
            if name.startswith(self.project):
                self.docker_run("volume", "rm", "-f", name)
        return remaining


def installed_world(label: str, components) -> tuple[World, Project]:
    world, docker = image_world(label, components)
    install(world)
    project = Project(docker, world)
    project.add_overlay()
    return world, project


# ------------------------------------------------------------------------ board

CLIENT = r'''
import http.client, json, os, sys, urllib.parse
passcode, calls = sys.argv[1], json.loads(sys.argv[2])
host = urllib.parse.urlsplit(os.environ.get("KANBAN_PUBLIC_ORIGIN") or "http://127.0.0.1:@PORT@").netloc
def request(method, path, body=None, cookie=None):
    connection = http.client.HTTPConnection("127.0.0.1", @PORT@, timeout=900)
    headers = {"Host": host}
    if cookie:
        headers["Cookie"] = cookie
    data = None
    if body is not None:
        data = json.dumps(body)
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=data, headers=headers)
    response = connection.getresponse()
    return response.status, response.getheader("set-cookie") or "", response.read().decode("utf-8", "replace")
status, _, text = request("GET", "/api/passcode/status")
cookie = None
if not (status == 200 and json.loads(text).get("required") is False):
    status, header, text = request("POST", "/api/passcode/verify", {"passcode": passcode})
    cookie = header.split(";", 1)[0]
results = []
for kind, procedure, value in calls:
    if kind == "get":
        status, _, text = request("GET", procedure, cookie=cookie)
    elif kind == "query":
        path = "/api/trpc/" + procedure
        if value is not None:
            path += "?input=" + urllib.parse.quote(json.dumps(value))
        status, _, text = request("GET", path, cookie=cookie)
    else:
        status, _, text = request("POST", "/api/trpc/" + procedure, value if value is not None else {}, cookie=cookie)
    results.append({"status": status, "text": text})
print(json.dumps(results))
'''.replace("@PORT@", str(BOARD_PORT))


class Board:
    """The board's HTTP interface, called from inside its own container (the overlay gives it no published
    port): `python3 -` in the board container, past the passcode gate with the passcode its log printed."""

    def __init__(self, project: Project):
        self.project = project

    def cid(self) -> str:
        cid = self.project.container("board")
        assert cid, f"no board container; compose ps: {self.project.services()}"
        return cid

    def passcode(self) -> str:
        found = re.findall(r"passcode:\s*([A-Za-z0-9]+)", self.project.logs(self.cid()))
        return found[-1] if found else ""

    def calls(self, calls: list[tuple[str, str, object]]) -> list[dict]:
        proc = self.project.docker_run("exec", "-i", self.cid(), "python3", "-", self.passcode(), json.dumps(calls),
                                       input=CLIENT, timeout=1200)
        assert proc.returncode == 0, "the in-container board client did not run\n" + explain(proc)
        results = json.loads(proc.stdout.strip().splitlines()[-1])
        for result in results:
            try:
                result["json"] = json.loads(result["text"])
            except ValueError:
                result["json"] = None
        return results

    def call(self, kind: str, procedure: str, value=None) -> dict:
        return self.calls([(kind, procedure, value)])[0]

    def serving(self, timeout: float = 120.0) -> int:
        deadline = time.monotonic() + timeout
        status = 0
        while time.monotonic() < deadline:
            try:
                status = self.call("get", "/")["status"]
            except AssertionError:
                status = 0
            if status == 200:
                return status
            time.sleep(2)
        return status

    def status(self) -> dict:
        result = self.call("query", STATUS_QUERY)
        data = ((result.get("json") or {}).get("result") or {}).get("data")
        assert result["status"] == 200 and isinstance(data, dict), (
            f"{STATUS_QUERY} did not answer: {result['status']} {result['text'][:800]}")
        return parse_status(data)

    def install_acknowledged(self, version: str | None = None) -> dict:
        status = self.status()
        value = install_input(acknowledged=True, notice_sha256=status["noticeSha256"], version=version)
        return self.call("mutation", INSTALL_MUTATION, value)

    def server_pids(self) -> list[int]:
        """The board server processes: `node /app/dist/cli.js` that are not its hook commands."""
        proc = exec_python(self.project, self.cid(), PROCESSES, timeout=60)
        assert proc.returncode == 0, explain(proc)
        rows = json.loads(proc.stdout.strip().splitlines()[-1])
        return sorted(row["pid"] for row in rows if "/app/dist/cli.js" in row["argv"]
                      and row["argv"][row["argv"].index("/app/dist/cli.js") + 1:][:1] != ["hooks"])


STATES = ("not_installed", "installing", "installed", "unavailable")


def parse_status(data: dict) -> dict:
    """S2 as the tests read it, from FEATURE's answer (see the seam map above); asserts what it lacks."""
    problems = []
    state = data.get("state")
    if state not in STATES:
        problems.append(f"state: one of {' | '.join(STATES)}")
    pinned, notice, provider = (data.get(k) if isinstance(data.get(k), dict) else {} for k in ("pinned", "notice", "provider"))
    installed = data.get("installed")
    if not (installed is None or (isinstance(installed, dict) and isinstance(installed.get("sdkVersion"), str))):
        problems.append("installed: null | {sdkVersion: string}")
    for label, value in (("pinned.sdkVersion", pinned.get("sdkVersion")), ("notice.text", notice.get("text")),
                         ("notice.sha256", notice.get("sha256"))):
        if not isinstance(value, str):
            problems.append(f"{label}: string")
    if not isinstance(provider.get("registered"), bool):
        problems.append("provider.registered: boolean")
    version = installed.get("sdkVersion") if isinstance(installed, dict) else None
    if state == "installed" and (provider.get("registered") is not True or provider.get("sdkVersion") != version):
        problems.append(f"state installed: the provider registered with the recorded SDK version "
                        f"(registered {provider.get('registered')}, loaded {provider.get('sdkVersion')}, recorded {version})")
    if state == "not_installed" and version is not None:
        problems.append(f"state not_installed with an install record at {version}")
    assert not problems, f"{STATUS_QUERY} answered {json.dumps(data)[:1500]}; it lacks {', '.join(problems)}"
    return {"installed": True if state == "installed" else False if state == "not_installed" else state,
            "version": version, "defaultVersion": pinned["sdkVersion"], "notice": notice["text"],
            "noticeSha256": notice["sha256"], "state": state}


def install_input(*, acknowledged: bool | None = None, notice_sha256: str | None = None,
                  version: str | None = None) -> dict:
    """S3's input: FEATURE's request has no flag; the digest of the shown notice is the acknowledgement."""
    value: dict = {}
    if acknowledged is True:
        value["acknowledgedNoticeSha256"] = notice_sha256 or ""
    elif acknowledged is False:
        value["acknowledgedNoticeSha256"] = None
    if version:
        value["sdkVersion"] = version
    return value


def install_ok(result: dict) -> bool:
    data = ((result.get("json") or {}).get("result") or {}).get("data")
    return (result.get("status") == 200 and isinstance(data, dict) and data.get("ok") is True
            and data.get("code") == "installed")


# ------------------------------------------------------------------ observations


def installed_sdks(state: Path) -> list[tuple[str, str]]:
    """(path relative to /state, version) of every installed SDK package.json under the host state bind."""
    found = []
    for path in state.rglob("package.json"):
        relative = path.relative_to(state).as_posix()
        if relative.endswith(INSTALLED_SUFFIX):
            try:
                version = json.loads(path.read_text()).get("version")
            except (OSError, ValueError):
                version = None
            found.append((relative, str(version)))
    return sorted(found)


def tree(state: Path) -> set[str]:
    return {path.relative_to(state).as_posix() for path in state.rglob("*")}


NPM_PROCESSES = r'''
import json, os
rows = []
for name in os.listdir("/proc"):
    if not name.isdigit():
        continue
    try:
        argv = open("/proc/%s/cmdline" % name, "rb").read().split(b"\0")
    except OSError:
        continue
    words = [w.decode("utf-8", "replace") for w in argv if w]
    if any(os.path.basename(w) in ("npm", "npm-cli.js", "npx", "npx-cli.js") for w in words):
        rows.append(words)
print(json.dumps(rows))
'''


def npm_processes(project: Project, cid: str) -> list[list[str]] | None:
    proc = exec_python(project, cid, NPM_PROCESSES, timeout=60)
    if proc.returncode != 0:
        return None
    return json.loads(proc.stdout.strip().splitlines()[-1])


def writable_mounts_outside_state(project: Project, cid: str) -> list[str]:
    proc = project.docker_run("inspect", "--format", "{{json .Mounts}}", cid)
    assert proc.returncode == 0, explain(proc)
    outside = []
    for mount in json.loads(proc.stdout) or []:
        destination = mount.get("Destination", "")
        if mount.get("RW") and not (destination == "/state" or destination.startswith("/state/")):
            outside.append(f"{destination} ({mount.get('Type')} {mount.get('Source') or mount.get('Name')})")
    return outside


def diff_outside_state(project: Project, cid: str) -> list[str]:
    proc = project.docker_run("diff", cid, timeout=900)
    assert proc.returncode == 0, explain(proc)
    changed = []
    for line in proc.stdout.splitlines():
        kind, _, path = line.partition(" ")
        if path in DOCKER_MANAGED or path == "/state" or path.startswith("/state/"):
            continue
        changed.append(line)
    return changed
