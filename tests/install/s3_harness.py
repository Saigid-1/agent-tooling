"""Contract harness for order S3 (docs/work/orders/S3-manifest-installer.md).

The tests drive the `kp-agent-install` console script that sits next to the test
interpreter, as a subprocess, and read only what it prints and renders. Nothing
here imports `kp_agent_tooling`: the order's CLI surface, rendered runtime root
and `deploy/compose.yaml` role constraints are the contract, not a module.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import tomllib
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = REPO_ROOT / "deploy" / "compose.yaml"
# Deliberately not resolved: a venv's bin/python is a symlink to the base
# interpreter, and the console script lives beside the venv entry.
INSTALLER = Path(sys.executable).parent / "kp-agent-install"

# T12b (B2, role-pinned install tests): `runtime_install` SERVICES/COMPONENTS gain `indexer`, the one drainer, a
# tooling-image role selected whenever capture or board is (docs/work/orders/T12b-one-indexer-outbox.md).
ALL_COMPONENTS = ("tooling", "refresh", "capture", "board", "indexer", "telemetry")
TABLE_PROFILES = ("refresh", "capture", "board", "indexer", "telemetry")
TELEMETRY_SERVICES = frozenset({"tempo", "collector"})
ROLE_SERVICES = ("tooling", "refresh", "capture", "board", "indexer", "tempo", "collector")
TOOLING_ROLES = ("tooling", "refresh", "capture", "board", "indexer")
SERVE_TAIL = ["kp-agent-tooling", "--config", "/config/navigation.json", "serve"]
STATE_MARKER = "ops-tooling-state-v1"
PLAN_HASH = re.compile(r"(?:sha256:)?[0-9a-f]{64}")
DIGEST_FORM = re.compile(r"(?:[^\s@]+@)?sha256:[0-9a-f]{64}")

DIGEST_REGISTRY = ("ghcr.io/saigid-1/agent-tooling@sha256:"
                   + hashlib.sha256(b"s3-contract-image-a").hexdigest())
DIGEST_BARE = "sha256:" + hashlib.sha256(b"s3-contract-image-b").hexdigest()

BASE_UID, BASE_GID = 4242, 4343
BASE_BOARD_PORT = 34871
BASE_PROJECT = "s3-contract-a"

FIXTURE_FILES = {
    "README.md": "S3 contract fixture repository.\nneedle: kp-s3-fixture-needle-5d1c\n",
    "src/hello.py": "def hello():\n    return 'kp-s3-fixture-needle-5d1c'\n",
}
SECOND_FIXTURE_FILES = {"NOTES.md": "second fixture repository\n"}

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "S3 Contract",
    "GIT_AUTHOR_EMAIL": "s3-contract@example.invalid",
    "GIT_COMMITTER_NAME": "S3 Contract",
    "GIT_COMMITTER_EMAIL": "s3-contract@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
}


# --------------------------------------------------------------------------- git


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], env=dict(os.environ, **_GIT_ENV),
                          capture_output=True, text=True)
    if proc.returncode:
        raise RuntimeError(f"git {args} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def make_repo(path: Path, files: dict[str, str] | None = None, *, commit: bool = True) -> str | None:
    """Create a small Git repository; return its HEAD commit (None when uncommitted)."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    if not commit:
        return None
    # Files older than the index keep later `git status` calls from rewriting it
    # (racy-clean refresh), so a read-only consumer leaves the tree byte-identical.
    past = time.time() - 300
    for rel, text in (files or FIXTURE_FILES).items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        os.utime(target, (past, past))
    git(path, "add", "-A")
    git(path, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "S3 contract fixture")
    git(path, "status", "--porcelain")
    return git(path, "rev-parse", "HEAD")


# ---------------------------------------------------------------------- snapshots


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot(*paths: Path) -> dict[str, tuple]:
    """Byte-and-mode inventory of each tree, never following symlinks."""
    out: dict[str, tuple] = {}

    def visit(p: Path) -> None:
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            out[str(p)] = ("absent",)
            return
        mode = stat.S_IMODE(st.st_mode)
        if stat.S_ISLNK(st.st_mode):
            out[str(p)] = ("link", os.readlink(p))
        elif stat.S_ISDIR(st.st_mode):
            out[str(p)] = ("dir", mode)
            for child in sorted(os.listdir(p)):
                visit(p / child)
        elif stat.S_ISREG(st.st_mode):
            out[str(p)] = ("file", mode, sha256_file(p))
        else:
            out[str(p)] = ("other", mode)

    for p in paths:
        visit(Path(p))
    return out


def snapshot_diff(before: dict, after: dict) -> list[str]:
    changed = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changed.append(f"{key}: {before.get(key, ('absent',))} -> {after.get(key, ('absent',))}")
    return changed


def tree_entries(root: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """(files, dirs, links) under root, excluding root itself, not following links."""
    files, dirs, links = [], [], []
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(current)
        for name in list(dirnames):
            p = base / name
            (links if p.is_symlink() else dirs).append(p)
        for name in filenames:
            p = base / name
            (links if p.is_symlink() else files).append(p)
    return sorted(files), sorted(dirs), sorted(links)


# ---------------------------------------------------------------- the installer


def hermetic_env(home: Path, tmp: Path) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(tmp) + "/",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    for key in ("USER", "LOGNAME", "LC_ALL"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def explain(proc: subprocess.CompletedProcess) -> str:
    return (f"argv={proc.args!r}\nexit={proc.returncode}\n"
            f"stdout={proc.stdout[-3000:]}\nstderr={proc.stderr[-3000:]}")


@dataclass
class World:
    """One isolated installer universe: an empty runtime root, a fake HOME, a cwd,
    a private TMPDIR for the installer and committed fixture repositories."""

    base: Path
    root: Path
    home: Path
    cwd: Path
    itmp: Path
    repos_dir: Path
    repo: Path
    head: str
    repo_b: Path
    image: str = DIGEST_REGISTRY
    repos: dict = field(default_factory=dict)
    components: tuple = ("tooling", "board")
    uid: int = BASE_UID
    gid: int = BASE_GID
    board_port: int = BASE_BOARD_PORT
    project: str = BASE_PROJECT

    @classmethod
    def create(cls, base: Path, **overrides) -> "World":
        base = Path(base).resolve()
        root, home, cwd, itmp, repos_dir = (base / "root", base / "home", base / "cwd",
                                            base / "itmp", base / "repos")
        for d in (root, home, cwd, itmp, repos_dir):
            d.mkdir(parents=True)
        # Capture's native transcript roots have no CLI argument; give the fake
        # HOME the conventional (empty) Claude and Codex roots.
        (home / ".claude" / "projects").mkdir(parents=True)
        (home / ".codex" / "sessions").mkdir(parents=True)
        repo = repos_dir / "fixture"
        head = make_repo(repo, FIXTURE_FILES)
        repo_b = repos_dir / "second"
        make_repo(repo_b, SECOND_FIXTURE_FILES)
        world = cls(base=base, root=root, home=home, cwd=cwd, itmp=itmp, repos_dir=repos_dir,
                    repo=repo, head=head, repo_b=repo_b)
        world.repos = {"fixture": repo}
        return replace(world, **overrides) if overrides else world

    # -- argv ---------------------------------------------------------------
    def args(self, **over) -> list[str]:
        root = over.get("root", self.root)
        image = over.get("image", self.image)
        repos = over.get("repos", self.repos)
        components = over.get("components", self.components)
        if not isinstance(components, str):
            components = ",".join(components)
        argv = ["--runtime-root", str(root), "--image", image]
        for key, path in repos.items():
            argv += ["--repository", f"{key}={path}"]
        argv += ["--components", components,
                 "--uid", str(over.get("uid", self.uid)),
                 "--gid", str(over.get("gid", self.gid)),
                 "--board-port", str(over.get("board_port", self.board_port)),
                 "--project-name", over.get("project", self.project)]
        return argv

    # -- runs ---------------------------------------------------------------
    def run(self, action: str, argv: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
        return subprocess.run([str(INSTALLER), action, *argv], cwd=self.cwd,
                              env=hermetic_env(self.home, self.itmp), capture_output=True,
                              text=True, timeout=timeout, umask=0o022, stdin=subprocess.DEVNULL)

    def plan(self, argv: list[str] | None = None) -> tuple[subprocess.CompletedProcess, dict | None]:
        proc = self.run("plan", self.args() if argv is None else argv)
        try:
            value = json.loads(proc.stdout)
        except ValueError:
            value = None
        return proc, value

    def plan_hash(self, argv: list[str] | None = None) -> str:
        proc, value = self.plan(argv)
        assert proc.returncode == 0, "plan refused valid inputs\n" + explain(proc)
        assert isinstance(value, dict), "plan stdout is not a JSON object\n" + explain(proc)
        digest = value.get("plan_sha256")
        assert isinstance(digest, str) and PLAN_HASH.fullmatch(digest), (
            "plan JSON has no plan_sha256 digest\n" + explain(proc))
        return digest

    def apply(self, argv: list[str] | None = None, expected: str | None = None) -> subprocess.CompletedProcess:
        argv = self.args() if argv is None else list(argv)
        if expected is not None:
            argv += ["--expected-plan-sha256", expected]
        return self.run("apply", argv)

    def apply_ok(self, argv: list[str] | None = None) -> subprocess.CompletedProcess:
        digest = self.plan_hash(argv)
        proc = self.apply(argv, digest)
        assert proc.returncode == 0, "apply refused its own reviewed plan\n" + explain(proc)
        return proc

    def surfaces(self) -> list[Path]:
        """Every location this world can observe for stray writes."""
        return [self.base]


# ------------------------------------------------------------- rendered files


def compose_files(root: Path) -> list[Path]:
    """compose.yaml first, then every rendered compose overlay, in name order."""
    base = root / "compose.yaml"
    overlays = sorted(p for p in root.iterdir()
                      if p.is_file() and p.name != "compose.yaml"
                      and re.fullmatch(r"compose\..+\.ya?ml", p.name))
    return [base, *overlays]


def json_strings(value) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            out.append(str(k))
            out.extend(json_strings(v))
    elif isinstance(value, list):
        for v in value:
            out.extend(json_strings(v))
    elif isinstance(value, str):
        out.append(value)
    return out


def receipt_pairs(receipt, names: set[str]) -> dict[str, set[str]]:
    """For each path name, the digests the receipt associates with it: either
    {name: digest-or-object} or an object that holds the name and the digest."""
    found: dict[str, set[str]] = {}

    def walk(v) -> None:
        if isinstance(v, dict):
            for k, child in v.items():
                if k in names:
                    found.setdefault(k, set()).update(json_strings(child))
            strings = {x for x in v.values() if isinstance(x, str)}
            for name in names & strings:
                found.setdefault(name, set()).update(strings)
            for child in v.values():
                walk(child)
        elif isinstance(v, list):
            for child in v:
                walk(child)

    walk(receipt)
    return found


def command_vectors(value) -> list[list[str]]:
    found: list[list[str]] = []

    def walk(v) -> None:
        if isinstance(v, dict):
            cmd, args = v.get("command"), v.get("args")
            if isinstance(cmd, str) and isinstance(args, list) and all(isinstance(a, str) for a in args):
                found.append([cmd, *args])
            elif isinstance(cmd, list) and cmd and all(isinstance(a, str) for a in cmd):
                found.append(list(cmd))
            for child in v.values():
                walk(child)
        elif isinstance(v, list):
            for child in v:
                walk(child)

    walk(value)
    return found


_TTY = re.compile(r"-[A-Za-z]*t[A-Za-z]*")


def serve_exec(vectors: list[list[str]]) -> tuple[list[str], str] | None:
    """The argv `docker exec -i <container> kp-agent-tooling --config
    /config/navigation.json serve`, and its container, if any vector is that."""
    for argv in vectors:
        if Path(argv[0]).name != "docker":
            continue
        for i, token in enumerate(argv):
            if token != "exec" or i + 2 >= len(argv):
                continue
            if argv[i + 1] not in ("-i", "--interactive"):
                continue
            if argv[i + 3:] != SERVE_TAIL:
                continue
            return argv, argv[i + 2]
    return None


def has_tty_flag(argv: list[str]) -> bool:
    return any(tok == "--tty" or _TTY.fullmatch(tok) for tok in argv[1:])


def read_snippets(root: Path) -> dict[str, object]:
    return {
        "host/claude-mcp.json": json.loads((root / "host" / "claude-mcp.json").read_text()),
        "host/codex-mcp.toml": tomllib.loads((root / "host" / "codex-mcp.toml").read_text()),
    }


# ------------------------------------------------------ compose configuration


def docker_env(home: Path) -> dict[str, str]:
    env = hermetic_env(home, Path(os.environ.get("TMPDIR", "/tmp")).resolve())
    for key, value in os.environ.items():
        if key.startswith("DOCKER_"):
            env[key] = value
    env.setdefault("DOCKER_CONFIG", str(Path.home() / ".docker"))
    return env


def docker_cli() -> str | None:
    docker = shutil.which("docker")
    if not docker:
        return None
    probe = subprocess.run([docker, "compose", "version"], capture_output=True, text=True,
                           env=docker_env(Path.home()))
    return docker if probe.returncode == 0 else None


def declared_profiles(files: list[Path]) -> list[str]:
    profiles = set(TABLE_PROFILES)
    for f in files:
        raw = yaml.safe_load(f.read_text()) or {}
        for svc in (raw.get("services") or {}).values():
            profiles.update((svc or {}).get("profiles") or [])
    return sorted(profiles)


def rendered_config(world: World) -> tuple[dict, str]:
    """The merged, interpolated configuration of every rendered compose file with
    every profile enabled, and the name of the instrument that produced it."""
    files = compose_files(world.root)
    profiles = declared_profiles(files)
    forced = os.environ.get("AGENT_TOOLING_TEST_COMPOSE_INSTRUMENT", "").strip().lower()
    docker = None if forced == "yaml" else docker_cli()
    if docker:
        argv = [docker, "compose", "--project-directory", str(world.root), "-p", world.project]
        for f in files:
            argv += ["-f", str(f)]
        for p in profiles:
            argv += ["--profile", p]
        argv += ["config", "--format", "json"]
        proc = subprocess.run(argv, cwd=world.root, env=docker_env(world.home),
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, "docker compose config rejected the rendered root\n" + explain(proc)
        return json.loads(proc.stdout), "docker compose config"
    reason = "forced by AGENT_TOOLING_TEST_COMPOSE_INSTRUMENT=yaml" if forced == "yaml" else "docker compose CLI absent"
    label = f"YAML parse fallback ({reason}); not docker compose config"
    warnings.warn(f"S3 P4 instrument: {label}", UserWarning)
    return yaml_compose_config(files, world.root / ".env", world.root, profiles), label


# A deliberately small model of Compose's merge and interpolation rules, used
# only when the docker CLI is absent. It is labelled wherever it is used.
_VAR = re.compile(r"\$(?:(?P<escape>\$)|\{(?P<braced>[^}]*)\}|(?P<named>[A-Za-z_][A-Za-z0-9_]*))")


def interpolate(text: str, env: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        if m.group("escape"):
            return "$"
        if m.group("named"):
            return env.get(m.group("named"), "")
        parsed = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?+])(.*))?", m.group("braced"), re.S)
        if not parsed:
            raise ValueError(f"invalid interpolation ${{{m.group('braced')}}}")
        name, op, arg = parsed.groups()
        value = env.get(name)
        if op is None:
            return value or ""
        if op == ":-":
            return value if value else arg
        if op == "-":
            return value if value is not None else arg
        if op in (":?", "?"):
            if (op == ":?" and not value) or value is None:
                raise ValueError(f"required variable {name} is missing: {arg}")
            return value
        if op == ":+":
            return arg if value else ""
        return arg if value is not None else ""

    return _VAR.sub(sub, text)


def read_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            quote, value = value[0], value[1:-1]
            if quote == '"':
                value = interpolate(value.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\"), env)
        else:
            value = interpolate(re.sub(r"\s+#.*$", "", value), env)
        env[key] = value
    return env


def _interp_tree(value, env):
    if isinstance(value, dict):
        return {k: _interp_tree(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [_interp_tree(v, env) for v in value]
    if isinstance(value, str):
        return interpolate(value, env)
    return value


def _normalize_volume(v, project_dir: Path, env: dict[str, str]) -> dict:
    if isinstance(v, str):
        parts = v.split(":")
        if len(parts) == 1:
            out = {"type": "volume", "target": parts[0]}
        else:
            source, target = parts[0], parts[1]
            mode = parts[2] if len(parts) > 2 else ""
            kind = "bind" if source.startswith(("/", ".", "~")) else "volume"
            out = {"type": kind, "source": source, "target": target}
            if kind == "bind":
                out["bind"] = {}
            if "ro" in mode.split(","):
                out["read_only"] = True
    else:
        out = json.loads(json.dumps(v))
    if out.get("type") == "bind" and isinstance(out.get("source"), str):
        source = out["source"]
        if source.startswith("~"):
            source = env.get("HOME", "") + source[1:]
        if not os.path.isabs(source):
            source = os.path.normpath(project_dir / source)
        out["source"] = source
    return out


def _normalize_port(p) -> dict:
    if isinstance(p, int):
        return {"host_ip": "", "published": "", "target": p}
    if isinstance(p, str):
        spec, _, proto = p.partition("/")
        parts = spec.rsplit(":", 2)
        if len(parts) == 3:
            host_ip, published, target = parts
        elif len(parts) == 2:
            host_ip, (published, target) = "", parts
        else:
            host_ip, published, target = "", "", parts[0]
        return {"host_ip": host_ip, "published": published,
                "target": int(target) if target.isdigit() else target, "protocol": proto or "tcp"}
    out = dict(p)
    out.setdefault("host_ip", "")
    out["published"] = str(out.get("published", ""))
    return out


def _normalize_service(svc: dict, project_dir: Path, env: dict[str, str]) -> dict:
    svc = dict(svc or {})
    for key in ("command", "entrypoint"):
        if isinstance(svc.get(key), str):
            svc[key] = shlex.split(svc[key])
    if "volumes" in svc:
        svc["volumes"] = [_normalize_volume(v, project_dir, env) for v in svc["volumes"] or []]
    if "ports" in svc:
        svc["ports"] = [_normalize_port(p) for p in svc["ports"] or []]
    for key in ("environment", "labels"):
        if isinstance(svc.get(key), list):
            svc[key] = dict(item.split("=", 1) if "=" in item else (item, "") for item in svc[key])
    return svc


def _merge_service(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        current = out.get(key)
        if key == "volumes":
            by_target = {v.get("target"): v for v in current or []}
            for v in value:
                by_target[v.get("target")] = v
            out[key] = list(by_target.values())
        elif key in ("ports", "expose", "dns", "dns_search", "tmpfs"):
            out[key] = list(current or []) + list(value or [])
        elif key in ("cap_add", "cap_drop", "security_opt"):
            out[key] = list(dict.fromkeys([*(current or []), *(value or [])]))
        elif isinstance(value, dict) and isinstance(current, dict) and key != "healthcheck":
            out[key] = _merge_service(current, value)
        else:
            out[key] = value
    return out


def yaml_compose_config(files: list[Path], env_file: Path, project_dir: Path, profiles: list[str]) -> dict:
    env = read_dotenv(env_file)
    services: dict[str, dict] = {}
    for f in files:
        raw = _interp_tree(yaml.safe_load(f.read_text()) or {}, env)
        for name, svc in (raw.get("services") or {}).items():
            svc = _normalize_service(svc, project_dir, env)
            services[name] = _merge_service(services[name], svc) if name in services else svc
    active = set(profiles)
    services = {name: svc for name, svc in services.items()
                if not svc.get("profiles") or active & set(svc["profiles"])}
    return {"services": services}


def binds(service: dict) -> list[dict]:
    return [v for v in service.get("volumes") or [] if v.get("type") == "bind"]


ABSENT = "absent"


def declared_binds(root: Path) -> dict[tuple[str, str], dict]:
    """Bind declarations exactly as written in the rendered compose files (the
    manifest copy and every overlay), keyed by (service, container target); a later
    file replaces an earlier declaration of the same target, as Compose merges.

    Each value records `syntax` (long|short), `file`, and the literal
    `create_host_path` (ABSENT when not written). This is the authority for a
    property whose default Compose's normalized output does not print stably:
    releases differ on which create_host_path value they omit.
    """
    env = read_dotenv(root / ".env")
    declared: dict[tuple[str, str], dict] = {}
    for f in compose_files(root):
        raw = yaml.safe_load(f.read_text()) or {}
        for name, svc in (raw.get("services") or {}).items():
            for entry in (svc or {}).get("volumes") or []:
                if isinstance(entry, str):
                    parts = interpolate(entry, env).split(":")
                    if len(parts) < 2 or not parts[0].startswith(("/", ".", "~")):
                        continue  # a named or anonymous volume, not a bind
                    declared[(name, parts[1])] = {"syntax": "short", "file": f.name,
                                                  "create_host_path": ABSENT}
                elif isinstance(entry, dict) and entry.get("type") == "bind":
                    options = entry.get("bind")
                    value = options.get("create_host_path", ABSENT) if isinstance(options, dict) else ABSENT
                    declared[(name, interpolate(str(entry.get("target", "")), env))] = {
                        "syntax": "long", "file": f.name, "create_host_path": value}
    return declared


def home_roots(*homes: Path) -> set[str]:
    import pwd
    roots = {"/", "/Users", "/home", "/root", "/var/root", str(Path.home()),
             os.path.expanduser("~"), pwd.getpwuid(os.getuid()).pw_dir}
    roots.update(str(h) for h in homes)
    return {os.path.normpath(r) for r in roots if r}


# ----------------------------------------------------------------- MCP stdio


class StdioSession:
    """Newline-delimited JSON-RPC over a child's stdio (MCP stdio transport)."""

    def __init__(self, argv: list[str], env: dict[str, str]):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=env)
        self.lines: queue.Queue = queue.Queue()
        self.stderr: list[str] = []
        self.noise: list[str] = []
        threading.Thread(target=self._pump_out, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()

    def _pump_out(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def _pump_err(self) -> None:
        for line in self.proc.stderr:
            self.stderr.append(line.decode(errors="replace"))

    def _send(self, message: dict) -> None:
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, **({"params": params} if params else {})})

    def request(self, rid: int, method: str, params: dict, timeout: float = 120) -> dict:
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no MCP response to {method} within {timeout}s; "
                                     f"stderr={''.join(self.stderr)[-2000:]}")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise AssertionError(f"MCP server closed stdout before answering {method}; "
                                     f"exit={self.proc.poll()} stderr={''.join(self.stderr)[-2000:]}")
            try:
                message = json.loads(line)
            except ValueError:
                self.noise.append(line.decode(errors="replace"))
                continue
            if message.get("id") == rid and ("result" in message or "error" in message):
                return message

    def close(self) -> None:
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
            self.proc.wait(timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            self.proc.kill()
            self.proc.wait(timeout=15)
