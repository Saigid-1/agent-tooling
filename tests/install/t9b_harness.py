"""Contract harness for order T9b (docs/work/orders/T9b-memory-store-volume.md, with
Amendments 1-4; each supersedes the earlier text where they differ).

Public surfaces only:
- the `kp-agent-install` console script beside the test interpreter (S3 harness);
- the rendered Compose project, driven with `docker compose --project-directory "$root"`
  as docs/DOCKER.md writes it, and its resolved configuration (`docker compose config`);
- `docker inspect` of the role containers and processes run in them with `docker exec`;
- the project's volume `<project>_memory`, read by a throwaway inspection container of the
  same image that mounts it read-only (it never opens a store file in place: databases are
  copied to a private tmpfs first);
- `kp-agent-install prepare` (Amendment 4), its report, the receipt, `verify`, docs/DOCKER.md and
  deploy/examples.
No implementation module is imported.

Image-marked tests read AGENT_TOOLING_TEST_IMAGE and FAIL (never skip) when it is unset.
Every Compose project, container and volume they create is named `t9b-test-...`; each
project lives in a never-reused directory under TMPDIR and is taken down with its volumes.

Readings the order leaves open (repeated in the arm report under AMBIGUITY):
- the volume is the one the resolved Compose configuration mounts at /state/memory; its
  Compose key is `memory`, so its name is `<project>_memory` (Amendment 2);
- a pre-T9b root holds its stores on the host bind: `state/memory/*`, `state/registry`,
  `state/assistant` (store files only: `store/`, `launches/`) and `state/desk-memory`; they
  migrate to `/state/memory/`, `/state/memory/registry`, `/state/memory/assistant` and
  `/state/memory/desk-memory`, so to the volume's root, `registry/`, `assistant/` and
  `desk-memory/` (Amendment 2, P3);
- "every file" is every regular file of a store except SQLite's sidecars (`-wal`, `-shm`,
  `-journal`), which are never copied: a sidecar in the volume with the bytes of its source
  sidecar is a copied sidecar (Amendment 1, P3);
- a database is equal when `quick_check` is ok and each ordinary table's rows, in primary-key
  order (rowid order without one), digest the same as the source's (Amendment 1, P3);
- "owner and mode preserved": every copied file is owned by AGENT_UID:AGENT_GID (the owner
  of every source file here) and keeps its source mode; directory modes are not compared;
- "the host directory as it was": the same regular files at the same paths with the same
  bytes and modes; an `-shm` sidecar, or an empty `-wal`, that SQLite creates or removes
  when the database is opened is not a change;
- "the volume as it was": when the volume did not exist before the step, it may exist
  afterwards only if it holds no entry;
- a refusal is an exit other than 0 that does not report success and is not a Python
  traceback (a crash is not a refusal); a `prepare` refusal also prints `refused` and its
  named reason; `prepared` is an exit 0 whose output says `prepared`;
- "the receipt records the migration": receipt.json names each renamed directory,
  `<name>.migrated-<plan_sha256[:12]>`, the plan_sha256 of the receipt `prepare` ran on;
- a `prepare` refusal may record its outcome in the receipt ("records the outcome in the
  receipt"), so a refusal is not required to leave receipt.json byte-identical.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
import yaml

from s3_harness import INSTALLER, World, compose_files, docker_env, explain, hermetic_env
from t7a_harness import Runtime, free_port, required_docker, resolve_image

IMAGE_VARIABLE = "AGENT_TOOLING_TEST_IMAGE"
PREFIX = "t9b-test"
STATE_TARGET = "/state"
STORE_TARGET = "/state/memory"
VOLUME_KEY = "memory"
MIGRATED = ".migrated-"
# Every service of the manifest that mounts /state (deploy/compose.yaml). T12b: the `indexer` role mounts it too
# (the memory-volume rule applies to it; docs/work/orders/T12b-one-indexer-outbox.md, B2).
STATE_ROLES = ("tooling", "refresh", "capture", "board", "indexer")
# Pre-T9b store directories under the runtime root's state/, and where each one lands in
# the volume (relative to /state/memory).
LEGACY = {"memory": "", "registry": "registry", "assistant": "assistant", "desk-memory": "desk-memory"}
# State directories the installer renders, which stay on the host bind.
BIND_STATE = ("/state/tmp", "/state/snapshots", "/state/search", "/state/delivery", "/state/models")
TRACEBACK = "Traceback (most recent call last)"


def store_volume_name(project: str) -> str:
    return f"{project}_{VOLUME_KEY}"


# ---------------------------------------------------------------- worlds


def required_image() -> str:
    ref = os.environ.get(IMAGE_VARIABLE, "").strip()
    if not ref:
        pytest.fail(f"{IMAGE_VARIABLE} is unset. This T9b test is image-marked and needs a built "
                    "product image; this is a failure, not a skip.", pytrace=False)
    return ref


def fresh_directory(label: str) -> Path:
    """A never-reused directory under TMPDIR (Docker Desktop has served a stale missing bind
    source for a reused path; see tests/install/test_p5_empty_root_image.py)."""
    return Path(tempfile.mkdtemp(prefix=f"{PREFIX}-{label}-")).resolve()


def image_world(label: str, components) -> tuple[World, str]:
    """An S3 world for a real Compose run: the invoking user's IDs, a free loopback board
    port, a `t9b-test-*` project and the resolved image."""
    docker = required_docker()
    ref = required_image()
    run_id = uuid.uuid4().hex[:8]
    world = World.create(fresh_directory(f"{label}-{run_id}"), components=tuple(components),
                         uid=os.getuid() or 10001, gid=os.getgid() or 10001, board_port=free_port(),
                         project=f"{PREFIX}-{label}-{run_id}")
    return replace(world, image=resolve_image(docker, ref, docker_env(world.home))), docker


def sharing_world(world: World, label: str) -> World:
    """A second image world rendered for `world`'s Compose project (Amendment 7, J1: two roots, one project
    name): its own never-reused root, HOME and board port; the same project, components, IDs and image."""
    other = World.create(fresh_directory(f"{label}-{uuid.uuid4().hex[:8]}"), components=tuple(world.components),
                         uid=world.uid, gid=world.gid, board_port=free_port(), project=world.project)
    return replace(other, image=world.image)


def install_argv(world: World) -> list[str]:
    argv = world.args()
    if "capture" in world.components:
        for root in (world.home / ".claude" / "projects", world.home / ".codex" / "sessions"):
            argv += ["--transcript-root", str(root)]
    return argv


def plan_sha(world: World, argv: list[str]) -> str:
    proc, value = world.plan(argv)
    assert proc.returncode == 0 and isinstance(value, dict) and isinstance(value.get("plan_sha256"), str), (
        "`kp-agent-install plan` refused valid inputs\n" + explain(proc))
    return value["plan_sha256"]


def docker_variables() -> dict[str, str]:
    """The test's own Docker client settings (DOCKER_HOST, DOCKER_CONFIG, ...), so that the installer
    reaches the same daemon as the test (Amendment 3, D1: measured with Docker reachable)."""
    return {key: value for key, value in os.environ.items() if key.startswith("DOCKER_")}


def run_installer(world: World, action: str, argv: list[str], *, env: dict | None = None,
                  path: str | None = None, docker: bool = True) -> subprocess.CompletedProcess:
    """The installer as World.run runs it (hermetic HOME, cwd and TMPDIR), with the test's Docker
    settings (`docker`), extra environment (for example a dead DOCKER_HOST) or another PATH."""
    environment = hermetic_env(world.home, world.itmp)
    if docker:
        environment.update(docker_variables())
    environment.update(env or {})
    if path is not None:
        environment["PATH"] = path
    return subprocess.run([str(INSTALLER), action, *argv], cwd=world.cwd, env=environment, capture_output=True,
                          text=True, timeout=600, umask=0o022, stdin=subprocess.DEVNULL)


def apply_plan(world: World, argv: list[str], **options) -> tuple[subprocess.CompletedProcess, str]:
    """plan, then apply that plan (Amendment 4: apply never calls Docker)."""
    planned = run_installer(world, "plan", argv, **options)
    value = result_json(planned)
    assert planned.returncode == 0 and isinstance(value, dict) and isinstance(value.get("plan_sha256"), str), (
        "`kp-agent-install plan` refused valid inputs\n" + explain(planned))
    digest = value["plan_sha256"]
    return run_installer(world, "apply", [*argv, "--expected-plan-sha256", digest], **options), digest


def apply_only(world: World) -> list[str]:
    """plan and apply on the root, nothing else: a root whose volume is not prepared yet."""
    argv = install_argv(world)
    applied, _ = apply_plan(world, argv)
    assert applied.returncode == 0, "`kp-agent-install apply` refused its reviewed plan\n" + explain(applied)
    return argv


_PREPARED_ROOTS: dict[str, World] = {}


def _down_prepared_roots() -> None:
    """Meet (Coordinator), P8: a `<project>_memory` volume never survives a test, even when a setup step
    after `prepare` fails before the test's own teardown is in place. At interpreter exit, take down every
    root this process prepared, with its volumes; a root already taken down is a no-op."""
    docker = shutil.which("docker")
    for world in _PREPARED_ROOTS.values():
        if docker and (world.root / "compose.yaml").exists():
            subprocess.run([docker, "compose", "--project-directory", str(world.root), "down", "--volumes",
                            "--remove-orphans", "--timeout", "10"], cwd=world.root, env=docker_env(world.home),
                           capture_output=True, text=True, timeout=300)


def prepare(world: World, **options) -> subprocess.CompletedProcess:
    """`kp-agent-install prepare --runtime-root <root>` (Amendment 4), with Docker reachable unless `options`
    say otherwise."""
    if not _PREPARED_ROOTS:
        atexit.register(_down_prepared_roots)
    _PREPARED_ROOTS[str(world.root)] = world
    return run_installer(world, "prepare", ["--runtime-root", str(world.root)], **options)


def prepared(proc: subprocess.CompletedProcess) -> str | None:
    """None when `prepare` reported `prepared`; otherwise why not."""
    if proc.returncode != 0:
        return f"prepare exited {proc.returncode}"
    if "prepared" not in proc.stdout.replace("unprepared", ""):
        return "prepare did not report `prepared`"
    return None


def prepare_refused(proc: subprocess.CompletedProcess, reason: str) -> str | None:
    """None when `prepare` refused with the named `reason`; otherwise why not."""
    output = proc.stdout + proc.stderr
    if proc.returncode == 0:
        return "prepare exited 0"
    if TRACEBACK in proc.stderr:
        return "prepare crashed (a Python traceback), it did not refuse"
    if "refused" not in output or reason not in output:
        return f"prepare did not report `refused` with the reason `{reason}`"
    return None


def install(world: World) -> list[str]:
    """plan, apply and prepare (docs/DOCKER.md's order), with Docker reachable and the image present.

    An installer that has no `prepare` verb at all (argparse exit 2, "invalid choice") is noted and the
    setup goes on without it, so that a test using this setup still measures its own property; every
    test of `prepare` itself calls it directly and fails there."""
    argv = apply_only(world)
    done = prepare(world)
    if done.returncode == 2 and "invalid choice" in done.stderr:
        print(f"NOTE: this kp-agent-install has no `prepare` verb; {world.root} was not prepared")
        return argv
    why = prepared(done)
    assert why is None, f"`kp-agent-install prepare` did not prepare the root: {why}\n" + explain(done)
    return argv


def verify(world: World, **options) -> subprocess.CompletedProcess:
    return run_installer(world, "verify", ["--runtime-root", str(world.root)], **options)


def result_json(proc: subprocess.CompletedProcess):
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def refused(proc: subprocess.CompletedProcess) -> str | None:
    """None when `proc` is a refusal; otherwise why it is not one."""
    value = result_json(proc)
    if proc.returncode == 0:
        return "apply exited 0"
    if isinstance(value, dict) and value.get("status") in ("applied", "unchanged"):
        return f"apply reported {value.get('status')}"
    if TRACEBACK in proc.stderr:
        return "apply crashed (a Python traceback), it did not refuse"
    return None


# ---------------------------------------------------------------- projects


class Project(Runtime):
    """A rendered root driven with `docker compose --project-directory "$root"`; taken down
    with its volumes, and every leftover `t9b-test` resource of the project removed."""

    def __init__(self, docker: str, world: World):
        super().__init__(docker, world)
        assert self.project.startswith(PREFIX + "-"), self.project

    def config(self) -> dict:
        proc = self.compose("config", "--format", "json", timeout=120)
        assert proc.returncode == 0, "docker compose config rejected the rendered root\n" + explain(proc)
        return json.loads(proc.stdout)

    def store_volumes(self) -> dict[str, tuple[str, str]]:
        """{service: (volume key, resolved name)} for every service with a volume at /state/memory."""
        config = self.config()
        declared = config.get("volumes") or {}
        found = {}
        for service, body in (config.get("services") or {}).items():
            for mount in (body or {}).get("volumes") or []:
                if mount.get("target") == STORE_TARGET and mount.get("type") == "volume":
                    key = mount.get("source")
                    found[service] = (key, (declared.get(key) or {}).get("name") or key)
        return found

    def store_volume(self) -> str | None:
        names = {name for _, name in self.store_volumes().values()}
        assert len(names) <= 1, f"more than one volume is mounted at {STORE_TARGET}: {sorted(names)}"
        return names.pop() if names else None

    def volume_exists(self, name: str) -> bool:
        return self.docker_run("volume", "inspect", name, timeout=60).returncode == 0

    def running_services(self) -> list[str]:
        proc = self.compose("ps", "--status", "running", "--format", "{{.Service}}", timeout=120)
        return sorted(set(proc.stdout.split())) if proc.returncode == 0 else []

    def wait_running(self, services, timeout: float = 180.0) -> dict[str, str]:
        deadline = time.monotonic() + timeout
        while True:
            cids = {service: self.container(service) for service in services}
            states = {s: (self.state(c) if c else {}) for s, c in cids.items()}
            if all(state.get("Running") for state in states.values()):
                return cids
            if time.monotonic() > deadline:
                logs = {s: self.logs(c)[-1500:] for s, c in cids.items() if c}
                pytest.fail(f"not running within {timeout}s: {json.dumps(states)}\nlogs={json.dumps(logs)}",
                            pytrace=False)
            time.sleep(2)

    def down(self) -> list[str]:
        self.started = True
        remaining = super().down()
        label = f"label=com.docker.compose.project={self.project}"
        volumes = set(self.docker_run("volume", "ls", "-q", "--filter", label).stdout.split())
        volumes |= {name for name in self.docker_run("volume", "ls", "-q", "--filter",
                                                     f"name={self.project}").stdout.split()
                    if name.startswith(self.project)}
        for name in sorted(volumes):
            self.docker_run("volume", "rm", "-f", name)
        return remaining


def stale_compose(world: World, role: str, *, bind: Path | None = None) -> Path:
    """A copy of the rendered compose.yaml in which `role` has no store volume: its mount at
    /state/memory is removed, or replaced by a bind of the host directory `bind`."""
    data = yaml.safe_load((world.root / "compose.yaml").read_text())
    service = data["services"][role]
    kept = []
    for entry in service.get("volumes") or []:
        target = entry.get("target") if isinstance(entry, dict) else (str(entry).split(":") + [""])[1]
        if target != STORE_TARGET:
            kept.append(entry)
    if bind is not None:
        kept.append({"type": "bind", "source": str(bind), "target": STORE_TARGET,
                     "bind": {"create_host_path": False}})
    service["volumes"] = kept
    path = world.base / f"stale-{role}-{uuid.uuid4().hex[:6]}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False, width=4096))
    return path


def compose_with(project: Project, manifest: Path, *args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """`docker compose` on the rendered root with `manifest` in place of its compose.yaml."""
    files = [manifest, *[p for p in compose_files(project.root) if p.name != "compose.yaml"]]
    argv = [project.docker, "compose", "--project-directory", str(project.root), "-p", project.project]
    for path in files:
        argv += ["-f", str(path)]
    return subprocess.run([*argv, *args], cwd=project.root, env=project.env, capture_output=True, text=True,
                          timeout=timeout)


# ------------------------------------------------------------------- store

# The canonical row digest: the host computes the expected value with it, and the
# inspection container computes the observed one with the same source.
DIGEST_SOURCE = r'''
import hashlib, json
def rows_digest(rows):
    def cell(value):
        if isinstance(value, (bytes, bytearray, memoryview)):
            return {"blob": bytes(value).hex()}
        return value
    data = json.dumps([[cell(value) for value in row] for row in rows], separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()
def table_rows(connection, name):
    """Every row of an ordinary table, in primary-key order (rowid order without one)."""
    quoted = '"%s"' % name.replace('"', '""')
    keys = sorted((row[5], row[1]) for row in connection.execute("pragma table_info(%s)" % quoted) if row[5])
    order = ", ".join('"%s"' % key.replace('"', '""') for _, key in keys) or "rowid"
    return connection.execute("select * from %s order by %s" % (quoted, order)).fetchall()
def tables(connection):
    return [row[0] for row in connection.execute(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
'''
_namespace: dict = {}
exec(DIGEST_SOURCE, _namespace)
rows_digest = _namespace["rows_digest"]
table_rows = _namespace["table_rows"]


def episode_rows(start: int, count: int) -> list[tuple]:
    return [(i, f"session-{i % 7}", f"episode {i} " + "lorem ipsum " * (i % 13),
             hashlib.sha256(str(i).encode()).digest() * 3, i / 7.0) for i in range(start, start + count)]


def cursor_rows(start: int, count: int) -> list[tuple]:
    return [(i, f"cursor-{i}", i * 1000) for i in range(start, start + count)]


def term_rows(count: int) -> list[tuple]:
    return [(i, f"term-{i % 41}-{i}", i % 97) for i in range(1, count + 1)]


def posting_rows(count: int) -> list[tuple]:
    """Rows of a WITHOUT ROWID table keyed (term, episode_id), in primary-key order."""
    return sorted((f"term-{i % 23}", i, i % 5) for i in range(1, count + 1))


_EPISODES_SCHEMA = """
create table episodes(id integer primary key, session text not null, body text, payload blob, score real);
create index episodes_session on episodes(session);
create table cursors(id integer primary key, name text unique, position integer);
"""

# Run in a child process so that a crashed writer can be reproduced exactly: `abandon`
# exits without closing (a WAL keeps its committed frames; with `commit` false a rollback
# journal stays behind, and its transaction is undone on the next open).
_WRITER = r'''
import json, os, sqlite3, sys
spec = json.loads(sys.stdin.read())
def as_rows(rows):
    return [tuple(bytes.fromhex(v["blob"]) if isinstance(v, dict) else v for v in row) for row in rows]
connection = sqlite3.connect(spec["path"], isolation_level=None)
connection.execute("pragma journal_mode=%s" % spec["journal_mode"])
connection.execute("pragma wal_autocheckpoint=0")
connection.execute("pragma cache_size=1")
if spec.get("schema"):
    connection.executescript(spec["schema"])
connection.execute("begin")
for table, rows in spec["inserts"].items():
    rows = as_rows(rows)
    if rows:
        marks = ",".join("?" * len(rows[0]))
        connection.executemany("insert into %s values (%s)" % (table, marks), rows)
if spec.get("commit", True):
    connection.execute("commit")
if spec.get("abandon"):
    os._exit(0)
connection.close()
'''


def _encode(rows):
    return [[{"blob": v.hex()} if isinstance(v, bytes) else v for v in row] for row in rows]


def _write_db(path: Path, *, journal_mode: str, schema: str | None, inserts: dict, abandon: bool = False,
              commit: bool = True) -> None:
    spec = {"path": str(path), "journal_mode": journal_mode, "schema": schema, "commit": commit,
            "inserts": {table: _encode(rows) for table, rows in inserts.items()}, "abandon": abandon}
    # Meet (Coordinator): the spec goes on stdin; a crashed-journal spec exceeds Linux's 128 KiB
    # single-argument limit (MAX_ARG_STRLEN), which macOS does not have.
    proc = subprocess.run([sys.executable, "-c", _WRITER], input=json.dumps(spec), capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, "could not write the fixture database\n" + explain(proc)


def _private_file(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, mode)


def _deterministic(size: int, seed: bytes) -> bytes:
    out, counter = bytearray(), 0
    while len(out) < size:
        out += hashlib.sha256(seed + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:size])


@dataclass
class Store:
    """The pre-T9b stores a test wrote on the host bind, and what the volume must hold after a
    migration (keys are paths relative to /state/memory)."""

    root: Path
    legacy: tuple = ()
    files: dict = field(default_factory=dict)
    sidecars: dict = field(default_factory=dict)  # volume-relative sidecar path -> sha256 of the source's

    def host(self, name: str) -> Path:
        return self.root / "state" / name

    def snapshot(self) -> dict:
        return {name: host_tree(self.host(name)) for name in self.legacy}


def _volume_relative(name: str, relative: str) -> str:
    return f"{LEGACY[name]}/{relative}" if LEGACY[name] else relative


def _sqlite(store: Store, name: str, relative: str, *, journal_mode: str, schema: str, tables: dict,
            crashed: dict | None = None, mode: int = 0o600) -> None:
    """A database at state/<name>/<relative>: `tables` committed (each list in primary-key order);
    `crashed` = {"wal": rows} (committed, left in the WAL by a killed writer) or {"journal": rows}
    (uncommitted, left in a hot rollback journal)."""
    path = store.host(name) / relative
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_db(path, journal_mode=journal_mode, schema=schema, inserts=tables)
    os.chmod(path, mode)
    expected = {table: list(rows) for table, rows in tables.items()}
    for kind, rows in (crashed or {}).items():
        if kind == "wal":
            _write_db(path, journal_mode="wal", schema=None, inserts=rows, abandon=True)
            for table, extra in rows.items():
                expected[table] = expected[table] + list(extra)
        else:
            _write_db(path, journal_mode="delete", schema=None, inserts=rows, abandon=True, commit=False)
        sidecar = Path(f"{path}-{'wal' if kind == 'wal' else 'journal'}")
        assert sidecar.is_file() and sidecar.stat().st_size > 0, f"precondition: {sidecar.name} was left behind"
    target = _volume_relative(name, relative)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.is_file():
            store.sidecars[target + suffix] = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    store.files[target] = {"kind": "sqlite", "mode": mode,
                           "tables": {table: {"rows": len(rows), "digest": rows_digest(rows)}
                                      for table, rows in expected.items()}}


def _plain(store: Store, name: str, relative: str, data: bytes, mode: int) -> None:
    _private_file(store.host(name) / relative, data, mode)
    store.files[_volume_relative(name, relative)] = {"kind": "file", "mode": mode,
                                                     "sha256": hashlib.sha256(data).hexdigest()}


def build_store(root: Path, *, crashed: bool, corrupt: str | None = None, legacy=("memory",)) -> Store:
    """Populate pre-T9b stores on the host bind, as a root installed before T9b holds them.

    - state/memory: desk-history/episodes.sqlite3 (WAL; with `crashed`, its last committed batch
      lives only in its WAL), desk-history/episode-search.sqlite3 (rollback journal, with a
      WITHOUT ROWID table; with `crashed`, an uncommitted transaction left in a hot journal),
      and plain files with distinct modes (0640, 0600, 0400, 0644), an empty file, a name with
      a space, every byte value and 1 MiB;
    - state/registry: a WAL registry database (with a WITHOUT ROWID table), roles.json and a
      launch receipt;
    - state/assistant: the assistant store (store/) and a launch (launches/);
    - state/desk-memory: a WAL database and a note;
    - `corrupt`: one more database in state/memory that fails: `malformed-page` (a valid header,
      a b-tree page whose type byte is zeroed) or `not-a-database`.
    """
    store = Store(root, tuple(legacy))
    for name in legacy:
        store.host(name).mkdir(mode=0o700, exist_ok=True)
        os.chmod(store.host(name), 0o700)
    if "memory" in legacy:
        _sqlite(store, "memory", "desk-history/episodes.sqlite3", journal_mode="wal", schema=_EPISODES_SCHEMA,
                tables={"episodes": episode_rows(1, 400), "cursors": cursor_rows(1, 5)},
                crashed={"wal": {"episodes": episode_rows(401, 250), "cursors": cursor_rows(6, 3)}} if crashed else None)
        _sqlite(store, "memory", "desk-history/episode-search.sqlite3", journal_mode="delete",
                schema="create table terms(id integer primary key, term text, episode_id integer);"
                       "create table postings(term text, episode_id integer, weight integer,"
                       " primary key(term, episode_id)) without rowid;",
                tables={"terms": term_rows(300), "postings": posting_rows(200)},
                crashed={"journal": {"terms": [(i, "x" * 3000, 0) for i in range(1001, 1061)]}} if crashed else None)
        for relative, data, mode in (
                ("desk-history/README.txt", b"T9b store fixture: desk history\n", 0o640),
                ("sessions/s-1/config.json", json.dumps({"session": "s-1", "desk": "t9b"}).encode() + b"\n", 0o600),
                ("sessions/s-1/notes with space.txt", b"a name with a space\n", 0o400),
                ("blobs/empty.bin", b"", 0o600),
                ("blobs/all-bytes.bin", bytes(range(256)) * 16, 0o644),
                ("blobs/large.bin", _deterministic(1 << 20, b"t9b-large"), 0o600)):
            _plain(store, "memory", relative, data, mode)
    if "registry" in legacy:
        _sqlite(store, "registry", "registry.sqlite3", journal_mode="wal",
                schema="create table desks(id integer primary key, name text);"
                       "create table bindings(native_session_id text primary key, desk_id integer) without rowid;",
                tables={"desks": [(i, f"desk {i}") for i in range(1, 6)],
                        "bindings": sorted((f"session-{i:03d}", i % 5 + 1) for i in range(1, 21))},
                crashed={"wal": {"desks": [(6, "desk 6")]}} if crashed else None)
        _plain(store, "registry", "roles.json", json.dumps({"roles": [{"role_id": "general"}]}).encode(), 0o600)
        _plain(store, "registry", "launches/l-1/receipt.json", b'{"launch": "l-1"}\n', 0o600)
    if "assistant" in legacy:
        _sqlite(store, "assistant", "store/assistant.sqlite3", journal_mode="delete",
                schema="create table turns(id integer primary key, body text);",
                tables={"turns": [(i, f"turn {i}") for i in range(1, 31)]})
        _plain(store, "assistant", "launches/a-1/launch.json", b'{"launch": "a-1"}\n', 0o600)
    if "desk-memory" in legacy:
        _sqlite(store, "desk-memory", "desk.sqlite3", journal_mode="wal",
                schema="create table events(id integer primary key, kind text);",
                tables={"events": [(i, f"event-{i % 3}") for i in range(1, 41)]})
        _plain(store, "desk-memory", "notes.txt", b"desk memory notes\n", 0o640)

    if corrupt == "malformed-page":
        path = store.host("memory") / "desk-history" / "zz-malformed.sqlite3"
        _write_db(path, journal_mode="delete", schema="create table t(id integer primary key, v text);",
                  inserts={"t": [(i, "x" * 200) for i in range(1, 2001)]})
        data = bytearray(path.read_bytes())
        page = int.from_bytes(data[16:18], "big")
        assert len(data) >= 4 * page, "precondition: the malformed database spans several pages"
        data[2 * page] = 0x00  # page 3's b-tree page type
        path.write_bytes(bytes(data))
        os.chmod(path, 0o600)
    elif corrupt == "not-a-database":
        _private_file(store.host("memory") / "desk-history" / "zz-not-a-database.sqlite3",
                      _deterministic(8192, b"t9b-not-a-database"), 0o600)
    elif corrupt is not None:
        raise ValueError(corrupt)
    return store


def host_tree(path: Path) -> dict:
    """{relative: entry} of a host directory, never following links; see the module readings
    for the sidecars it leaves out."""
    out: dict = {}
    if not path.exists():
        return out
    for current, dirs, files in os.walk(path, followlinks=False):
        for name in sorted(dirs + files):
            full = Path(current) / name
            relative = str(full.relative_to(path))
            info = os.lstat(full)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                out[relative] = ("link", os.readlink(full))
            elif stat.S_ISDIR(info.st_mode):
                out[relative] = ("dir", mode)
            elif stat.S_ISREG(info.st_mode):
                if name.endswith(".sqlite3-shm") or (name.endswith(".sqlite3-wal") and info.st_size == 0):
                    continue
                out[relative] = ("file", mode, hashlib.sha256(full.read_bytes()).hexdigest())
            else:
                out[relative] = ("other", mode)
    return out


def tree_diff(before: dict, after: dict) -> list[str]:
    return [f"{key}: {before.get(key, ('absent',))} -> {after.get(key, ('absent',))}"
            for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]


def migrated_directories(root: Path) -> list[Path]:
    state = root / "state"
    return sorted(p for p in state.iterdir() if MIGRATED in p.name) if state.is_dir() else []


def regular_files(path: Path) -> list[str]:
    if not path.is_dir():
        return []
    return sorted(str(p.relative_to(path)) for p in path.rglob("*") if p.is_file() or p.is_symlink())


def host_copy_problems(directory: Path, store: Store, name: str) -> list[str]:
    """Every file of the pre-T9b store `name` is still in `directory` (its renamed host copy): plain
    files byte- and mode-identical, databases with the same rows (read from a private copy)."""
    problems = []
    prefix = LEGACY[name]
    scratch = Path(tempfile.mkdtemp(prefix=f"{PREFIX}-readback-"))
    try:
        for key, expected in sorted(store.files.items()):
            if prefix and not key.startswith(prefix + "/"):
                continue
            if not prefix and any(key.startswith(p + "/") for p in LEGACY.values() if p):
                continue
            relative = key[len(prefix) + 1:] if prefix else key
            path = directory / relative
            if not path.is_file():
                problems.append(f"{relative}: missing from {directory}")
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode != expected["mode"]:
                problems.append(f"{relative}: mode {oct(mode)}, was {oct(expected['mode'])}")
            if expected["kind"] == "file":
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
                    problems.append(f"{relative}: bytes changed")
                continue
            copy = scratch / hashlib.sha256(relative.encode()).hexdigest()[:12]
            copy.mkdir()
            for suffix in ("", "-wal", "-journal"):
                if Path(f"{path}{suffix}").is_file():
                    shutil.copyfile(f"{path}{suffix}", copy / f"db.sqlite3{suffix}")
            connection = sqlite3.connect(copy / "db.sqlite3")
            try:
                for table, want in expected["tables"].items():
                    rows = table_rows(connection, table)
                    if (len(rows), rows_digest(rows)) != (want["rows"], want["digest"]):
                        problems.append(f"{relative}: table {table} changed ({len(rows)} rows, was {want['rows']})")
            except sqlite3.Error as error:
                problems.append(f"{relative}: unreadable: {error}")
            finally:
                connection.close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return problems


# ----------------------------------------------------------------- volume

INVENTORY = DIGEST_SOURCE + r'''
import os, shutil, sqlite3, stat, sys
root, scratch = sys.argv[1], sys.argv[2]
def info(path):
    st = os.lstat(path)
    kind = ("link" if stat.S_ISLNK(st.st_mode) else "dir" if stat.S_ISDIR(st.st_mode)
            else "file" if stat.S_ISREG(st.st_mode) else "other")
    return {"type": kind, "uid": st.st_uid, "gid": st.st_gid, "mode": stat.S_IMODE(st.st_mode),
            "size": st.st_size}
def database(path):
    work = os.path.join(scratch, hashlib.sha256(path.encode()).hexdigest()[:16])
    os.makedirs(work)
    for suffix in ("", "-wal", "-journal"):
        if os.path.isfile(path + suffix):
            shutil.copyfile(path + suffix, os.path.join(work, "db.sqlite3" + suffix))
    out = {"tables": {}}
    try:
        connection = sqlite3.connect(os.path.join(work, "db.sqlite3"))
        try:
            try:
                out["quick_check"] = [row[0] for row in connection.execute("pragma quick_check")]
            except sqlite3.Error as error:
                out["quick_check"] = ["error: %s" % error]
            for name in tables(connection):
                rows = table_rows(connection, name)
                out["tables"][name] = {"rows": len(rows), "digest": rows_digest(rows)}
        finally:
            connection.close()
    except sqlite3.Error as error:
        out["error"] = str(error)
    shutil.rmtree(work, ignore_errors=True)
    return out
result = {"root": info(root), "entries": {}, "errors": []}
def walk(directory):
    try:
        names = sorted(os.listdir(directory))
    except OSError as error:
        result["errors"].append("%s: %s" % (os.path.relpath(directory, root), error))
        return
    for name in names:
        path = os.path.join(directory, name)
        entry = info(path)
        if entry["type"] == "file":
            try:
                with open(path, "rb") as handle:
                    entry["sha256"] = hashlib.sha256(handle.read()).hexdigest()
                if name.endswith(".sqlite3"):
                    entry["sqlite"] = database(path)
            except OSError as error:
                entry["error"] = str(error)
        result["entries"][os.path.relpath(path, root)] = entry
        if entry["type"] == "dir":
            walk(path)
walk(root)
print(json.dumps(result))
'''


def volume_state(project: Project, name: str | None, *, image: str, uid: int, gid: int) -> dict | None:
    """The inventory of volume `name`, or None when it does not exist (never creates it)."""
    if not name or not project.volume_exists(name):
        return None
    proc = project.docker_run(
        "run", "--rm", "--name", f"{PREFIX}-inspect-{uuid.uuid4().hex[:10]}", "--network", "none",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", f"{uid}:{gid}",
        "--tmpfs", "/scratch:rw,size=256m,mode=1777",
        "--mount", f"type=volume,source={name},target=/m,readonly,volume-nocopy",
        "--entrypoint", "python3", image, "-c", INVENTORY, "/m", "/scratch", timeout=300)
    assert proc.returncode == 0, f"could not inspect volume {name}\n" + explain(proc)
    return json.loads(proc.stdout)


def volume_problems(inventory: dict | None, store: Store, uid: int, gid: int) -> list[str]:
    """What the volume misses of the store: P1's owner and mode, P3's files, rows and sidecars."""
    if inventory is None:
        return ["the project's store volume does not exist"]
    problems = [f"inspection error: {error}" for error in inventory["errors"]]
    top = inventory["root"]
    if (top["type"], top["uid"], top["gid"], top["mode"]) != ("dir", uid, gid, 0o700):
        problems.append(f"volume root is {top['type']} {top['uid']}:{top['gid']} {oct(top['mode'])}, "
                        f"not dir {uid}:{gid} 0o700")
    entries = inventory["entries"]
    for relative, expected in sorted(store.files.items()):
        entry = entries.get(relative)
        if entry is None or entry["type"] != "file":
            problems.append(f"{relative}: not in the volume ({entry})")
            continue
        if (entry["uid"], entry["gid"]) != (uid, gid):
            problems.append(f"{relative}: owner {entry['uid']}:{entry['gid']}, not {uid}:{gid}")
        if entry["mode"] != expected["mode"]:
            problems.append(f"{relative}: mode {oct(entry['mode'])}, source {oct(expected['mode'])}")
        if "error" in entry:
            problems.append(f"{relative}: unreadable in the volume: {entry['error']}")
            continue
        if expected["kind"] == "file":
            if entry.get("sha256") != expected["sha256"]:
                problems.append(f"{relative}: bytes differ from the source")
            continue
        database = entry.get("sqlite") or {}
        if database.get("quick_check") != ["ok"]:
            problems.append(f"{relative}: quick_check {database.get('quick_check')} {database.get('error', '')}")
        for table, want in expected["tables"].items():
            got = (database.get("tables") or {}).get(table)
            if got != want:
                problems.append(f"{relative}: table {table} is {got}, source {want}")
    for relative, digest in sorted(store.sidecars.items()):
        entry = entries.get(relative)
        if entry is not None and entry.get("sha256") == digest:
            problems.append(f"{relative}: a sidecar was copied into the volume")
    return problems


def store_entries_in(inventory: dict | None, store: Store) -> list[str]:
    if inventory is None:
        return []
    return sorted(name for name in inventory["entries"] if name in store.files)


# -------------------------------------------------------------- in roles


def exec_python(project: Project, cid: str, source: str, *args: str, timeout: int = 120):
    return project.docker_run("exec", "-i", cid, "python3", "-c", source, *args, timeout=timeout)


def mounts(project: Project, cid: str) -> list[dict]:
    proc = project.docker_run("inspect", "--format", "{{json .Mounts}}", cid)
    assert proc.returncode == 0, explain(proc)
    return json.loads(proc.stdout) or []


def wait_exit(project: Project, cid: str, timeout: float = 20.0) -> dict:
    """The container's state once it is no longer running, or its state at `timeout`."""
    deadline = time.monotonic() + timeout
    while True:
        state = project.state(cid)
        if not state.get("Running") or time.monotonic() > deadline:
            return state
        time.sleep(1)


# Locks (Amendment 1, P2): lockf and flock on a file; SQLite BEGIN IMMEDIATE on a database,
# in WAL and in rollback-journal mode.
MECHANISMS = ("lockf", "flock", "sqlite-wal", "sqlite-rollback")

HOLD = r'''
import fcntl, os, sqlite3, sys, time
mechanism, path, error_file = sys.argv[1:4]
try:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if mechanism.startswith("sqlite"):
        handle = sqlite3.connect(path, isolation_level=None, timeout=0)
        handle.execute("pragma journal_mode=%s" % ("wal" if mechanism == "sqlite-wal" else "delete"))
        handle.execute("create table if not exists t(x)")
        handle.execute("begin immediate")
        handle.execute("insert into t values (1)")
    else:
        handle = open(path, "a+")
        (fcntl.lockf if mechanism == "lockf" else fcntl.flock)(handle, fcntl.LOCK_EX)
    with open(path + ".ready", "w") as ready:
        ready.write(str(os.getpid()))
except Exception as error:
    with open(error_file, "w") as out:
        out.write("%s: %s" % (type(error).__name__, error))
    raise
deadline = time.time() + 180
while not os.path.exists(path + ".release") and time.time() < deadline:
    time.sleep(0.2)
os.unlink(path + ".ready")
if mechanism.startswith("sqlite"):
    handle.execute("rollback")
handle.close()
'''

PROBE = r'''
import errno, fcntl, json, sqlite3, sys
mechanism, path = sys.argv[1:3]
if mechanism.startswith("sqlite"):
    try:
        connection = sqlite3.connect(path, isolation_level=None, timeout=0)
        connection.execute("begin immediate")
    except sqlite3.OperationalError as error:
        locked = "database is locked" in str(error)
        print(json.dumps({"result": "refused" if locked else "error", "detail": str(error)}))
    except sqlite3.Error as error:
        print(json.dumps({"result": "error", "detail": "%s: %s" % (type(error).__name__, error)}))
    else:
        connection.execute("rollback")
        print(json.dumps({"result": "granted"}))
    sys.exit(0)
try:
    handle = open(path, "a+")
except OSError as error:
    print(json.dumps({"result": "open-failed", "errno": error.errno, "detail": str(error)}))
    sys.exit(0)
lock = fcntl.lockf if mechanism == "lockf" else fcntl.flock
try:
    lock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError as error:
    refused = error.errno in (errno.EACCES, errno.EAGAIN)
    print(json.dumps({"result": "refused" if refused else "error", "errno": error.errno, "detail": str(error)}))
else:
    lock(handle, fcntl.LOCK_UN)
    print(json.dumps({"result": "granted"}))
'''

EXISTS = "import os, sys; print(os.path.exists(sys.argv[1]))"


def probe_lock(project: Project, cid: str, mechanism: str, path: str) -> dict:
    proc = exec_python(project, cid, PROBE, mechanism, path)
    assert proc.returncode == 0, "the lock probe did not run\n" + explain(proc)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def wait_exists(project: Project, cid: str, path: str, *, present: bool = True, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        proc = exec_python(project, cid, EXISTS, path)
        if proc.returncode == 0 and proc.stdout.strip() == str(present):
            return True
        time.sleep(0.5)
    return False


# ------------------------------------------------------- role refusals (C2)
#
# Coordinator rule (Verification C2), binding for T12a and T11b: every assertion that a ROLE's store
# preflight refused goes through `assert_role_refused(project, service, reason)`. Its body here is
# T12a's reading (docs/work/orders/T12a-capture-survives.md, A2: "Store-preflight refusals no longer exit
# 3 ... the role stays up and logs the refusal as JSON; reports health `refused`; re-checks on an
# interval; never execs the role command until the refusal clears"). Reconciled at T12a's meet (C2): T11b's
# exit-3 body is retired here, and T11b's caller (test_t11b_q4_template_image) reads this body unchanged.
# `docker exec`'d open-time refusals are not role refusals and do not use it.
#
# Readings (repeated in the T12a TEST arm report under AMBIGUITY):
# - "health `refused`": `tooling-container health`, run in the container with `docker exec` (the command
#   every role but board also uses as its Compose healthcheck), prints a JSON object whose `status` is
#   `refused`. Its exit code is not pinned. For board, whose Compose healthcheck is HTTP, the same exec is
#   the only health that can say `refused`.
# - "logs the refusal as JSON": a line of the container's log is a JSON object whose `reason` is the named
#   reason.
# - "never execs the role command": no process of the container runs a program of the role command. The
#   programs are read from the container's own entrypoint and command (`docker inspect`): the first word
#   after `tooling-container`, the first word after a `--` (capture's supervised command), and the server
#   `kanban` execs (`node /app/dist/cli.js`). A process runs program P when its argv[0], or for an
#   interpreter (python, node, sh) its script argv[1], is named P. Tooling's command `wait` is served inside
#   tooling-container itself, so for tooling this probe is vacuous and health is the observation.
# - "not restarting": Running, not Restarting, RestartCount 0 (a refusal causes no restart).

REFUSAL_TIMEOUT = 30.0
INTERPRETERS = ("python", "python3", "node", "nodejs", "sh", "bash", "dash")
# Programs a role command execs into, beyond its own words.
REEXEC = {"kanban": ("cli.js",)}

PROCESSES = r'''
import json, os
me = os.getpid()
rows = []
for name in os.listdir("/proc"):
    if not name.isdigit() or int(name) == me:
        continue
    try:
        with open("/proc/%s/cmdline" % name, "rb") as handle:
            raw = handle.read()
        with open("/proc/%s/stat" % name) as handle:
            ppid = int(handle.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        continue
    rows.append({"pid": int(name), "ppid": ppid, "argv": [p.decode(errors="replace") for p in raw.split(b"\0") if p]})
print(json.dumps(rows))
'''


def _program(argv: list[str]) -> str:
    if not argv:
        return ""
    head = Path(argv[0]).name
    interpreter = head in INTERPRETERS or (head.startswith("python") and head[6:].replace(".", "").isdigit())
    if interpreter and len(argv) > 1 and not argv[1].startswith("-"):
        return Path(argv[1]).name
    return head


def role_programs(project: Project, cid: str) -> set[str]:
    """The programs the role command of container `cid` execs (see the readings above)."""
    proc = project.docker_run("inspect", "--format", "{{json .Config.Entrypoint}} {{json .Config.Cmd}}", cid)
    assert proc.returncode == 0, explain(proc)
    entry, _, cmd = proc.stdout.strip().partition(" ")
    argv = [*(json.loads(entry) or []), *(json.loads(cmd) or [])]
    names = [Path(word).name for word in argv]
    role = argv[names.index("tooling-container") + 1:] if "tooling-container" in names else argv
    programs = set()
    if role and role[0] not in ("wait", "health"):
        programs.add(Path(role[0]).name)
    if "--" in role[1:] and role.index("--") + 1 < len(role):
        programs.add(Path(role[role.index("--") + 1]).name)
    for name in list(programs):
        programs.update(REEXEC.get(name, ()))
    return programs


def role_processes(project: Project, cid: str, programs: set[str] | None = None) -> list[dict] | None:
    """The processes of `cid` that run a program of its role command; None when the probe could not run."""
    programs = role_programs(project, cid) if programs is None else programs
    proc = exec_python(project, cid, PROCESSES, timeout=60)
    if proc.returncode != 0:
        return None
    rows = json.loads(proc.stdout.strip().splitlines()[-1])
    return [row for row in rows if _program(row["argv"]) in programs]


def health_report(project: Project, cid: str) -> dict | None:
    """The last JSON object `tooling-container health` prints in `cid` (stdout first, then stderr)."""
    proc = project.docker_run("exec", cid, "tooling-container", "health", timeout=60)
    for stream in (proc.stdout, proc.stderr):
        for line in reversed((stream or "").strip().splitlines()):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


def logged_reasons(logs: str) -> list[str]:
    """The `reason` of every JSON object line of a container log."""
    found = []
    for line in logs.splitlines():
        try:
            value = json.loads(line.strip())
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("reason"), str):
            found.append(value["reason"])
    return found


def assert_role_refused(project: Project, service: str, reason: str) -> dict:
    """The role `service`'s store preflight refused it with `reason`, T12a's reading: its container is
    running and not restarting, `tooling-container health` says `refused`, a JSON log line names `reason`,
    and no process runs its role command. Waits up to REFUSAL_TIMEOUT for the refusal to be reported, and
    fails at once if the container stops running meanwhile. Returns what it observed."""
    cid = project.container(service)
    assert cid, f"no {service} container exists; compose ps: {project.services()}"
    deadline = time.monotonic() + REFUSAL_TIMEOUT
    while True:
        state = project.state(cid)
        if not state.get("Running") or state.get("Restarting") or state.get("RestartCount"):
            pytest.fail(f"{service} did not stay up on its refusal `{reason}` (it exited or restarted): "
                        f"state={json.dumps(state)}\nlog:\n{project.logs(cid)[-2500:]}", pytrace=False)
        health = health_report(project, cid)
        logs = project.logs(cid)
        if (health or {}).get("status") == "refused" and reason in logged_reasons(logs):
            break
        if time.monotonic() > deadline:
            pytest.fail(f"{service} did not report the refusal `{reason}` within {REFUSAL_TIMEOUT}s: health="
                        f"{json.dumps(health)} logged reasons={logged_reasons(logs)}\nlog:\n{logs[-2500:]}",
                        pytrace=False)
        time.sleep(1)
    programs = role_programs(project, cid)
    running = role_processes(project, cid, programs)
    assert running is not None, f"the process probe could not run in {service}"
    assert not running, (f"{service} runs its role command ({sorted(programs)}) while refused `{reason}`: "
                         f"{json.dumps(running)}")
    state = project.state(cid)
    assert state.get("Running") and not state.get("Restarting") and not state.get("RestartCount"), (
        f"{service} restarted while refused `{reason}`: {json.dumps(state)}")
    return {"cid": cid, "state": state, "health": health, "programs": programs, "logs": logs}
