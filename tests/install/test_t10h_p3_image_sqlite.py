"""T10h P3: the image runs what the suites run (image-marked).

Order: docs/work/orders/T10h-projection-hotfix.md (P3), frozen at its merge commit. Inside the product
image named by AGENT_TOOLING_TEST_IMAGE (SQLite 3.40.1 at the order's writing), on a populated
store that has a T10 projection:

- (a) `kp-agent-desk --config ... upgrade-sources` exits 0 and reports `status` complete, with
  coverage established: its report names the present index, and `memory.search` afterwards
  reports every in-scope episode covered (before it: `incomplete_index`, as on the live store);
- (b) `PRAGMA quick_check` on the store gives exactly `ok` (and again after (c));
- (c) two consecutive `kp-agent-workspace-capture --config ... --policy ... once` runs both exit 0
  with `status` ok; the second's exit code is asserted on its own (the live role died there);
- (d) the same, starting from a store whose `episode_desks` was given the base writer's order
  `(desk, tenant, kind, seq, episode_id)`, which (a) must rebuild: afterwards no non-key column
  of `episode_desks` is declared before a key column (PRAGMA table_info).

Why tests/install: P3 is the deploy path of T9b's `deploy` script (step 2 of the order's deploy
runs `upgrade-sources` in the tooling container), and this directory holds the deploy-path image
tests, their harness and the conftest that deselects image-marked tests unless `-m image` names
them and then fails (never skips) when AGENT_TOOLING_TEST_IMAGE is unset. tests/image is the S2
image-content suite, whose fixtures name every container `agent-tooling-image-test-*`.

Harness: public surfaces only. One container of the image per case, its entry bypassed
(`--entrypoint sleep`), user and environment as the image declares them, no network, every
capability dropped, `/state` on a named volume (the store lives on a volume, as on the runtime).
Every step is a `docker exec` of a console script or of `python -` reading a script from stdin:
the store is seeded through the image's own product code (public writes, as tests/t10_world.py
does on the host), so every byte of it is written by the image's SQLite. No implementation module
is imported by this file. Every Docker resource is named `t10h-p3-<case>-<random>` and removed
(`docker rm -f`, `docker volume rm`); the test asserts none is left.

Coverage before (a) is made absent the way the live store lost it, with operator steps only: the
index is built (`index-history`), set aside, built again (the store records the second index's
token), and the first index put back, so the store's coverage names another index.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from s3_harness import docker_env
from t7a_harness import required_docker

pytestmark = pytest.mark.image

IMAGE_VARIABLE = "AGENT_TOOLING_TEST_IMAGE"
PREFIX = "t10h-p3"
STATE = "/state"
WORK = "/state/t10h"
CONFIG = f"{WORK}/session-alpha.json"
POLICY = f"{WORK}/capture-policy.json"
STORE = "/state/memory/episodes.sqlite3"
INDEX = "/state/memory/episode-search.sqlite3"
INDEX_ASIDE = f"{WORK}/index-first.sqlite3"
ROW = ["desk", "tenant", "kind", "seq", "episode_id"]

SEED = r'''
import hashlib, json, subprocess, sys
from pathlib import Path
work, state = Path(sys.argv[1]), Path(sys.argv[2])
work.mkdir(mode=0o700, parents=True, exist_ok=True)
state.mkdir(mode=0o700, parents=True, exist_ok=True)
TENANT, REPO = 't10h-tenant', 'repo-main'
ROLES = ('alpha', 'beta', 'gamma')
def binding(role):
    return 'binding:' + hashlib.sha256('|'.join((TENANT, role, REPO)).encode()).hexdigest()
def private(path, value):
    path.write_text(json.dumps(value, sort_keys=True)); path.chmod(0o600); return path
catalog = private(work / 'catalog.json', {'schema_version': 'ops.imported-desk-catalog.v1',
    'approval_ref': 'fixture:t10h', 'bindings': [{'binding_key': binding(r), 'tenant_id': TENANT, 'role': r,
    'repo_key': REPO, 'desk_label': r, 'source': 'fixture:t10h', 'memory_write_allowed': True} for r in ROLES]})
configs = {r: private(work / f'session-{r}.json', {'schema_version': 'ops.desk-memory.local.v1',
    'state_root': str(state), 'catalog_path': str(catalog), 'workspace_root': str(work),
    'provider_instance': 't10h', 'provider_session_id': f'session-{r}'}) for r in ROLES}
from kp_agent_tooling._impl.service.desk_memory_runtime import admit, components, initialize
initialize(str(configs['alpha']))
for r in ROLES:
    admit(str(configs[r]), desk_id=binding(r), provider_id='fixture', model_id='t10h-model')
store = components(str(configs['alpha']))[3]
from kp_agent_tooling._impl.service.session_sources import SessionSources
sources = SessionSources(store)
def events(*texts):
    return [{'event_id': f'e{i}', 'role': 'user' if i % 2 == 0 else 'assistant', 'text': t} for i, t in enumerate(texts)]
episodes = 0
for r in ROLES:
    for i in range(8):
        store.capture(f'session-{r}', source_ref=f't10h:{r}:{i}', events=events(f'{r} cedar note {i}.', f'{r} reply {i}.'))
        episodes += 1
for s in range(3):
    session = sources.register(tenant_id=TENANT, runtime='claude', native_id=f't10h-imported-{s}')
    for j in range(5):
        sources.import_episode(session_id=session, source_ref=f't10h:import:{s}:{j}',
            events=events(f'Imported cedar row {s}-{j}.'),
            provenance={'source_system': 'local:t10h-transcript', 'row_id': f't10h-{s}-{j}',
                        'row_digest': hashlib.sha256(f't10h-{s}-{j}'.encode()).hexdigest(),
                        'evidence_event_map': [], 'import_actor': 'operator:t10h',
                        'source_coordinates': {'path': f'/t10h/native/{s}.jsonl', 'start': 10 * j, 'end': 10 * j + 9}})
        episodes += 1
    if s < 2:
        sources.claim(session_id=session, predicate='session.owner',
            object={'kind': 'desk', 'id': binding(ROLES[s])}, asserted_by='operator:t10h',
            recorded_at='2026-10-03T00:00:00Z', evidence=['fixture:t10h'])
repo = work / 'repo'
repo.mkdir(exist_ok=True)
subprocess.run(['git', 'init', '-q', str(repo)], check=True)
native = {'claude': work / 'native' / 'claude', 'codex': work / 'native' / 'codex'}
for root in native.values():
    root.mkdir(parents=True, exist_ok=True)
approval = work / 'approval.json'
approval.write_text(json.dumps({'tenant_id': TENANT, 'repositories': [REPO]}))
private(work / 'capture-policy.json', {'schema_version': 'ops.workspace-capture.v1',
    'approval_record': str(approval), 'approval_sha256': hashlib.sha256(approval.read_bytes()).hexdigest(),
    'tenant_id': TENANT, 'approved_repo_keys': [REPO], 'repos': {REPO: [str(repo)]},
    'native_roots': {k: str(v) for k, v in native.items()}, 'excluded_sessions': {'claude': [], 'codex': []},
    'max_candidates': 8, 'max_batch_bytes': 512000, 'max_batch_rows': 50})
print(json.dumps({'seeded_episodes': episodes, 'store': str(store.path)}))
'''

OLD_ORDER = r'''
import json, sqlite3, sys
db = sqlite3.connect(sys.argv[1], isolation_level=None)
db.execute('BEGIN IMMEDIATE')
db.execute('ALTER TABLE episode_desks RENAME TO t10h_prior_desks')
db.execute('CREATE TABLE episode_desks (desk TEXT NOT NULL, tenant TEXT NOT NULL,'
           ' kind INTEGER NOT NULL, seq INTEGER NOT NULL, episode_id TEXT NOT NULL,'
           ' PRIMARY KEY (desk, kind, seq, episode_id)) WITHOUT ROWID')
db.execute('INSERT INTO episode_desks (desk, tenant, kind, seq, episode_id)'
           ' SELECT desk, tenant, kind, seq, episode_id FROM t10h_prior_desks')
db.execute('DROP TABLE t10h_prior_desks')
db.execute('CREATE INDEX episode_desks_episode ON episode_desks(episode_id)')
db.execute('COMMIT')
print(json.dumps([row[1] for row in db.execute("PRAGMA table_info('episode_desks')")]))
'''

PROBE = r'''
import json, sqlite3, sys
from pathlib import Path
store, config = sys.argv[1], sys.argv[2]
out = {'sqlite_version': sqlite3.sqlite_version}
db = sqlite3.connect(Path(store).resolve().as_uri() + '?mode=ro', uri=True)
try:
    out['table_info'] = [[r[0], r[1], r[5]] for r in db.execute("PRAGMA table_info('episode_desks')")]
    out['episode_desks_rows'] = db.execute('SELECT count(*) FROM episode_desks').fetchone()[0]
    out['quick_check'] = [r[0] for r in db.execute('PRAGMA quick_check')]
finally:
    db.close()
try:
    from kp_agent_tooling._impl.service.desk_memory_runtime import from_local_config
    found = from_local_config(config).call('memory.search', {'query': 'cedar', 'limit': 5})
    out['search'] = {k: found.get(k) for k in ('index_status', 'covered_episodes', 'total_episodes')}
except Exception as error:
    out['search'] = {'error': type(error).__name__, 'message': str(error)[:300]}
print(json.dumps(out))
'''

TRANSCRIPT = r'''
import json, sys
from pathlib import Path
work, label = Path(sys.argv[1]), sys.argv[2]
native = 'd10a8c3e-5d2f-4e61-9a7b-0c1d2e3f4a5b'
folder = work / 'native' / 'claude' / 'project-folder'
folder.mkdir(parents=True, exist_ok=True)
with (folder / f'{native}.jsonl').open('ab') as stream:
    for i, role in enumerate(('user', 'assistant')):
        row = {'type': role, 'sessionId': native, 'cwd': str(work / 'repo'),
               'message': {'role': role, 'content': f'{label} capture cedar {i}.'}}
        stream.write((json.dumps(row, separators=(',', ':')) + '\n').encode())
print(json.dumps({'appended': 2}))
'''


def required_image() -> str:
    ref = os.environ.get(IMAGE_VARIABLE, "").strip()
    if not ref:
        pytest.fail(f"{IMAGE_VARIABLE} is unset. This T10h test is image-marked and needs the product image; "
                    "this is a failure, not a skip.", pytrace=False)
    return ref


class Box:
    """One container of the image with `/state` on a named volume, both named `t10h-p3-<case>-<random>`."""

    def __init__(self, image: str, case: str):
        self.docker = required_docker()
        self.image = image
        self.name = f"{PREFIX}-{case}-{uuid.uuid4().hex[:8]}"
        self.volume = f"{self.name}-state"
        self.home = Path(tempfile.mkdtemp(prefix=f"{self.name}-")).resolve()
        self.env = docker_env(self.home)
        self.steps: list[dict] = []

    def _docker(self, *args: str, timeout: float = 300, input: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([self.docker, *args], capture_output=True, text=True, env=self.env,
                              timeout=timeout, input=input, stdin=None if input is not None else subprocess.DEVNULL)

    def start(self) -> None:
        done = self._docker("image", "inspect", "--format", "{{.Id}}", self.image, timeout=60)
        assert done.returncode == 0, f"{IMAGE_VARIABLE}={self.image!r} is not a local image: {done.stderr}"
        done = self._docker("volume", "create", self.volume, timeout=60)
        assert done.returncode == 0, f"docker volume create {self.volume} failed: {done.stderr}"
        done = self._docker("run", "-d", "--pull", "never", "--name", self.name, "--network", "none",
                            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                            "-v", f"{self.volume}:{STATE}", "--entrypoint", "sleep", self.image, "infinity", timeout=120)
        assert done.returncode == 0, f"docker run {self.name} failed: {done.stderr}"

    def remove(self) -> None:
        self._docker("rm", "-f", "-v", self.name, timeout=120)
        self._docker("volume", "rm", "-f", self.volume, timeout=120)
        shutil.rmtree(self.home, ignore_errors=True)

    def leftovers(self) -> list[str]:
        found = []
        for kind, args in (("container", ("ps", "-a", "--filter", f"name={self.name}", "--format", "{{.Names}}")),
                           ("volume", ("volume", "ls", "--filter", f"name={self.name}", "--format", "{{.Name}}"))):
            listed = self._docker(*args, timeout=60)
            found += [f"{kind} {line}" for line in listed.stdout.split() if line.startswith(PREFIX)]
        return found

    def run(self, label: str, argv: list[str], *, script: str | None = None, timeout: float = 600) -> dict:
        args = ["exec", *(["-i"] if script is not None else []), "-w", STATE, self.name, *argv]
        try:
            done = self._docker(*args, timeout=timeout, input=script)
            step = {"label": label, "argv": argv, "exit": done.returncode, "stdout": done.stdout, "stderr": done.stderr}
        except subprocess.TimeoutExpired:
            step = {"label": label, "argv": argv, "exit": None, "stdout": "", "stderr": f"timed out after {timeout}s"}
        step["json"] = last_json(step["stdout"])
        self.steps.append(step)
        return step

    def python(self, label: str, script: str, *args: str) -> dict:
        return self.run(label, ["python", "-", *args], script=script)

    def log(self) -> str:
        lines = []
        for step in self.steps:
            lines.append(f"--- {step['label']}: {' '.join(shlex.quote(a) for a in step['argv'])} -> exit {step['exit']}")
            if step["stdout"].strip():
                lines.append("stdout: " + step["stdout"].strip()[-1500:])
            if step["stderr"].strip():
                lines.append("stderr: " + step["stderr"].strip()[-2500:])
        return "\n".join(lines)


def last_json(text: str):
    for line in reversed(text.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def key_order_problem(table_info) -> str | None:
    """A non-key column declared before a key column of `episode_desks` (rows: cid, name, pk)."""
    keys = {name for _, name, pk in table_info if pk}
    declared = [name for _, name, _ in table_info]
    if not keys or set(declared[:len(keys)]) != keys:
        ordered = [name for _, name, pk in sorted(table_info, key=lambda row: row[2]) if pk]
        return f"episode_desks declares {declared} with primary key {ordered}"
    return None


def reason(step: dict) -> str:
    """The last line a failed step printed (its exception or refusal), not the whole traceback."""
    lines = [line for line in (step["stderr"] or step["stdout"] or "").strip().splitlines() if line.strip()]
    return lines[-1][-400:] if lines else ""


def deploy_path(image: str, *, old_order: bool) -> tuple[list[str], str]:
    """Seed, then (a), (b), (c) in the image; the problems found and the step log."""
    box = Box(image, "old-order" if old_order else "new-store")
    problems: list[str] = []
    box.start()
    try:
        seed = box.python("seed (image product code, public writes)", SEED, WORK, "/state/memory")
        assert seed["exit"] == 0, "fixture: seeding the store in the image failed\n" + box.log()
        for label, argv in (("index-history (first index)", ["kp-agent-desk", "--config", CONFIG, "index-history"]),
                            ("set the first index aside", ["mv", INDEX, INDEX_ASIDE]),
                            ("index-history (second index)", ["kp-agent-desk", "--config", CONFIG, "index-history"]),
                            ("put the first index back", ["mv", "-f", INDEX_ASIDE, INDEX])):
            assert box.run(label, argv)["exit"] == 0, f"fixture: {label} failed\n" + box.log()
        if old_order:
            given = box.python("give episode_desks the base order", OLD_ORDER, STORE)
            assert given["exit"] == 0 and given["json"] == ROW, "fixture: old order not written\n" + box.log()
        before = box.python("probe before (a)", PROBE, STORE, CONFIG)["json"] or {}
        assert before.get("episode_desks_rows", 0) >= 20, "fixture: episode_desks is populated\n" + box.log()
        search = before.get("search") or {}
        assert search.get("index_status") == "incomplete_index" and search.get("covered_episodes") == 0 \
            and (search.get("total_episodes") or 0) > 0, (
            "fixture: before (a) the store's coverage names another index (incomplete_index, 0 covered)\n" + box.log())
        if old_order:
            assert [name for _, name, _ in before.get("table_info", [])] == ROW, "fixture: old order\n" + box.log()

        upgraded = box.run("(a) upgrade-sources", ["kp-agent-desk", "--config", CONFIG, "upgrade-sources"])
        after = box.python("probe after (a)", PROBE, STORE, CONFIG)["json"] or {}
        box.python("append transcript rows", TRANSCRIPT, WORK, "first")
        first = box.run("(c) capture once #1", ["kp-agent-workspace-capture", "--config", CONFIG, "--policy", POLICY,
                                                 "once"])
        box.python("append transcript rows", TRANSCRIPT, WORK, "second")
        second = box.run("(c) capture once #2", ["kp-agent-workspace-capture", "--config", CONFIG, "--policy", POLICY,
                                                  "once"])
        final = box.python("probe after (c)", PROBE, STORE, CONFIG)["json"] or {}
    finally:
        box.remove()
    leftovers = box.leftovers()
    if leftovers:
        problems.append(f"Docker resources left behind: {leftovers}")

    report = upgraded["json"] if isinstance(upgraded["json"], dict) else {}
    if upgraded["exit"] != 0 or report.get("status") != "complete":
        problems.append(f"(a) upgrade-sources exited {upgraded['exit']} with status {report.get('status')!r}: "
                        f"{reason(upgraded)}")
    coverage = report.get("coverage")
    if not (isinstance(coverage, dict) and coverage.get("index") == "present"):
        problems.append(f"(a) upgrade-sources did not establish coverage for the present index: coverage={coverage!r}")
    search = after.get("search") or {}
    if not (search.get("index_status") in ("results", "zero_results")
            and search.get("covered_episodes") == search.get("total_episodes") and (search.get("total_episodes") or 0) > 0):
        problems.append(f"(a) after upgrade-sources memory.search does not report the index covering its scope: {search}")
    problem = key_order_problem(after.get("table_info", []))
    if problem:
        problems.append(f"(a) left episode_desks with a non-key column between its key columns: {problem}")
    if after.get("quick_check") != ["ok"]:
        problems.append(f"(b) PRAGMA quick_check after (a) on SQLite {after.get('sqlite_version')}: "
                        f"{len(after.get('quick_check') or [])} rows, first {(after.get('quick_check') or [])[:3]}")
    for label, step in (("#1", first), ("#2", second)):
        status = step["json"].get("status") if isinstance(step["json"], dict) else None
        if step["exit"] != 0 or status != "ok":
            problems.append(f"(c) capture once {label} exited {step['exit']} with status {status!r}: "
                            f"{reason(step)}")
    imported = sum(f.get("imported", 0) for f in (first["json"] or {}).get("files", []) if isinstance(f, dict)) \
        if isinstance(first["json"], dict) else 0
    if first["exit"] == 0 and not imported:
        problems.append(f"(c) positive control: capture once #1 imported nothing: {first['stdout'][-400:]}")
    if second["exit"] != 0:
        problems.append(f"(c) the second consecutive capture once exited {second['exit']} (the live role died here)")
    if final.get("quick_check") != ["ok"]:
        problems.append(f"(b) PRAGMA quick_check after (c): {(final.get('quick_check') or [])[:3]}")
    return problems, f"image {image} (SQLite {before.get('sqlite_version')}), quick_check before (a): " \
                     f"{len(before.get('quick_check') or [])} rows, first {(before.get('quick_check') or [])[:2]}\n" + box.log()


def test_t10h_p3_upgrade_quick_check_and_two_captures_in_the_image():
    """GREEN-IF, in the product image, on a populated store with a T10 projection: (a) upgrade-sources
    exits 0, status complete, coverage established for the present index (memory.search then covers its
    whole scope); (b) quick_check is exactly ok (after (a) and after (c)); (c) two consecutive capture
    `once` runs exit 0 with status ok, the first importing the new transcript rows; nothing t10h-p3-* is
    left in Docker."""
    problems, log = deploy_path(required_image(), old_order=False)
    assert not problems, "\n".join(problems) + "\n\n" + log


def test_t10h_p3_old_order_store_is_rebuilt_by_upgrade_sources_in_the_image():
    """GREEN-IF (d): the same as (a)-(c), starting from a store whose episode_desks has the base order
    (desk, tenant, kind, seq, episode_id): after (a) no non-key column of episode_desks is declared before
    a key column, quick_check is ok, and both capture runs exit 0; nothing t10h-p3-* is left in Docker."""
    problems, log = deploy_path(required_image(), old_order=True)
    assert not problems, "\n".join(problems) + "\n\n" + log
