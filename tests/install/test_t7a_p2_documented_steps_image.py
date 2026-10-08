"""T7a P2 (image-marked): the documented steps work verbatim.

Order: docs/work/orders/T7a-role-readiness.md, P2.
From an empty root, an operator following DOCKER.md, DESK-MENU.md, MODEL-GATEWAY.md
and HOST-ADAPTER.md exactly reaches: a healthy role set; an initialized portable
registry with one desk; one bound session; capture running; a gateway `doctor`
reporting `ready`. No step may be undocumented, such as an unlisted `mkdir`. Either
the behaviour changes (for example, `initialize` creates a missing leaf `state_root`
at 0700 under an existing private parent, and nothing else) or the document does.
Falsifier: any step that needs an action the documents do not state.

How "verbatim" is encoded. The test is one operator session, in order. Every step is
a command or file the four documents state; before running it, the test asserts that
the statement is still in the documents (`Docs.require`, which fails naming the step
and the statement it looked for) and each step cites the line it was written from at
the order's base (22569364). Values the documents leave to the operator (paths,
names, the image digest, a free loopback port, the fixture repository as `product`)
are the operator's choices, not steps. Commands the documents write as shell are run
through `/bin/sh -c` in an operator shell (`root`, `HOME`, `PATH` with the console
scripts and docker).

Two steps exist only if the documents state them, because the order lets either the
behaviour or the document change:
- creating the registry `state_root` before `initialize` (defect 2);
- creating the gateway `artifact_root` before `doctor` (defect 5).
The test performs such a step only when one of the four documents states it (a
`mkdir`, or a sentence telling the operator to create a directory under
`$root/state/`, in the context of the registry/memory state or of the artifact root),
and then the following step must succeed. Likewise, a restart or recreate after
writing operator files is performed only if a document states one.

Readings (repeated in the arm report under AMBIGUITY):
- "a healthy role set": tooling, refresh, capture and board, each `healthy`. Refresh
  is configured from deploy/examples/refresh.example.json as DOCKER.md says; the
  fixture repository has no remote, so its refresh cycles fail and are recorded,
  which health (lifecycle and configuration) does not cover;
- "capture running": the capture container is up and its workspace capture imports
  a new transcript of the repository, which topic-scope `memory.search` then finds
  (the boundary P5 documents);
- registry commands run in the tooling container (`docker exec -i <project>-tooling`,
  DESK-MENU.md "Operator configuration"; DOCKER.md: tooling is the memory exec target);
- the gateway configuration, its key file and artifact root live in the runtime root
  (`$root/config/…`, `/state/…`) and `doctor` runs in the tooling container.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from s3_harness import World, docker_env, explain
from t7a_harness import (Runtime, fresh_directory, free_port, required_docker, required_image, resolve_image,
                         write_private)

pytestmark = pytest.mark.image

ROOT = Path(__file__).resolve().parents[2]
DOC_NAMES = ("DOCKER.md", "DESK-MENU.md", "MODEL-GATEWAY.md", "HOST-ADAPTER.md")
EXAMPLES = ROOT / "deploy" / "examples"

# T9b: stores live in the project volume under /state/memory, created in the runtime.
REGISTRY_STATE = "/state/memory/desk-registry"
ARTIFACT_ROOT = "/state/model-artifacts"
# What a documented directory creation is for, judged from (in order) the created path itself, the
# command's own comment, then the paragraph just before its code block: the first of these that names
# exactly one kind decides; one that names both is ambiguous and decides nothing.
KINDS = {"state": re.compile(r"state_root|state root|registry|memory|desk", re.I),
         "artifact": re.compile(r"artifact", re.I)}
CAPTURE_TIMEOUT = 240.0


# ------------------------------------------------------------------- documents


class Docs:
    """The four operator documents: fenced shell lines and prose paragraphs, with line numbers."""

    def __init__(self):
        self.text = {name: (ROOT / "docs" / name).read_text() for name in DOC_NAMES}
        self.code: list[tuple[str, int, str, str]] = []   # (doc, line, command, paragraph before its block)
        self.prose: list[tuple[str, int, str]] = []        # (doc, first line, paragraph)
        for name, text in self.text.items():
            fenced, block, paragraph, first, before = False, [], [], 0, ""
            for number, line in enumerate(text.splitlines(), 1):
                if line.strip().startswith("```"):
                    if fenced:
                        joined, at = "", 0
                        for index, body in block:
                            if not joined:
                                at = index
                            joined = f"{joined} {body.strip()}" if joined else body.strip()
                            if joined.endswith("\\"):
                                joined = joined[:-1].rstrip()
                                continue
                            if joined:
                                self.code.append((name, at, joined, before))
                            joined = ""
                        block = []
                    else:
                        if paragraph:
                            self.prose.append((name, first, " ".join(paragraph)))
                            paragraph = []
                        before = self.prose[-1][2] if self.prose and self.prose[-1][0] == name else ""
                    fenced = not fenced
                    continue
                if fenced:
                    block.append((number, line))
                elif line.strip():
                    if not paragraph:
                        first = number
                    paragraph.append(line.strip())
                elif paragraph:
                    self.prose.append((name, first, " ".join(paragraph)))
                    paragraph = []
            if paragraph:
                self.prose.append((name, first, " ".join(paragraph)))

    def require(self, step: str, *patterns: str, docs=DOC_NAMES) -> str:
        """Every pattern is stated (in code or prose) by one of `docs`; returns the citations."""
        cites = []
        for pattern in patterns:
            regex = re.compile(pattern, re.I | re.S)
            hit = next((f"{d}:{n}" for d, n, line, _ in self.code if d in docs and regex.search(line)), None)
            hit = hit or next((f"{d}:{n}" for d, n, para in self.prose if d in docs and regex.search(para)), None)
            if hit is None:
                pytest.fail(f"P2 step '{step}' is not documented: no statement matching /{pattern}/ in "
                            f"{', '.join(docs)}", pytrace=False)
            cites.append(hit)
        return ", ".join(cites)

    @staticmethod
    def _decides(kind: str, evidence) -> bool | None:
        for text in evidence:
            named = {k for k, rx in KINDS.items() if rx.search(text or "")}
            if len(named) == 1:
                return kind in named
            if named:
                return None
        return None

    def states_creating(self, kind: str) -> str | None:
        """A documented creation of a directory under the runtime root's state/ for `kind`, or None."""
        for doc, number, line, before in self.code:
            if not re.search(r"\bmkdir\b", line):
                continue
            command, _, comment = line.partition("#")
            for path in re.findall(r"\S*/state/\S*", command):
                if self._decides(kind, (path, comment, before)):
                    return f"{doc}:{number}"
        for doc, number, para in self.prose:
            if not re.search(r"\b(create|mkdir)\b", para, re.I):
                continue
            for path in re.findall(r"\$\{?root\}?/state/[^\s`'\")]+", para):
                if self._decides(kind, (path, para)):
                    return f"{doc}:{number}"
        return None

    def restart_after_configuration(self) -> list[tuple[str, str]]:
        """Documented `docker compose ... restart|--force-recreate` commands, in code blocks or inline code."""
        restart = re.compile(r"\bdocker compose\b.*(\brestart\b|--force-recreate\b)")
        found = [(f"{doc}:{number}", line) for doc, number, line, _ in self.code if restart.search(line)]
        for doc, number, para in self.prose:
            found += [(f"{doc}:{number}", span) for span in re.findall(r"`([^`]+)`", para) if restart.search(span)]
        return found


# ---------------------------------------------------------------- operator shell


class Operator:
    def __init__(self, world: World, image: str, runtime: Runtime):
        self.world, self.runtime = world, runtime
        self.env = docker_env(world.home)
        self.env["TMPDIR"] = str(world.itmp) + "/"
        self.env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "/usr/bin:/bin")])
        self.env.update(root=str(world.root), image=image, project=world.project, repo=str(world.repo),
                        port=str(world.board_port),
                        claude_root=str(world.home / ".claude" / "projects"),
                        codex_root=str(world.home / ".codex" / "sessions"))
        self.log: list[str] = []

    def sh(self, step: str, script: str, *, stdin: str | None = None, ok=True, timeout=600) -> subprocess.CompletedProcess:
        proc = subprocess.run(["/bin/sh", "-c", script], cwd=self.world.cwd, env=self.env, capture_output=True,
                              text=True, timeout=timeout, input=stdin if stdin is not None else "")
        self.log.append(f"[{step}] $ {script}\n  exit={proc.returncode}")
        if ok:
            assert proc.returncode == 0, f"P2 step '{step}' failed\n$ {script}\n" + explain(proc) + \
                "\n\nsession so far:\n" + "\n".join(self.log)
        return proc

    def in_tooling(self, step: str, *argv: str, stdin=None, ok=True):
        container = f"{self.world.project}-tooling"
        quoted = " ".join(shlex.quote(a) for a in argv)
        return self.sh(step, f"docker exec -i {shlex.quote(container)} {quoted}", stdin=stdin, ok=ok)


def _claude_transcript(root: Path, workspace: Path, session: str) -> Path:
    return root / re.sub(r"[^a-zA-Z0-9]", "-", str(workspace.resolve())) / f"{session}.jsonl"


def _claude_rows(session: str, workspace: Path, *texts) -> list:
    rows = []
    for index, text in enumerate(texts):
        role = "user" if index % 2 == 0 else "assistant"
        content = text if role == "user" else [{"type": "text", "text": text}]
        rows.append({"type": role, "sessionId": session, "uuid": str(uuid.uuid4()), "cwd": str(workspace),
                     "timestamp": "2026-10-01T12:00:00.000Z", "message": {"role": role, "content": content}})
    return rows


# --------------------------------------------------------------------- the test


def test_documented_steps_reach_the_ready_state_from_an_empty_root():
    """GREEN-IF an operator doing only what the four documents state reaches a healthy role set, one desk,
    one bound session, capture importing a transcript, and a ready gateway doctor."""
    docs = Docs()
    docker = required_docker()
    ref = required_image()
    base = fresh_directory(f"p2-{uuid.uuid4().hex[:8]}")
    world = World.create(base, components=("tooling", "refresh", "capture", "board"), uid=os.getuid() or 10001,
                         gid=os.getgid() or 10001, board_port=free_port(),
                         project=f"t7a-test-p2-{uuid.uuid4().hex[:8]}")
    image = resolve_image(docker, ref, docker_env(world.home))
    world.root.rmdir()  # DOCKER.md: the operator creates the runtime root
    runtime = Runtime(docker, world)
    op = Operator(world, image, runtime)
    try:
        _run_session(docs, op, runtime, world)
    finally:
        remaining = runtime.down()
    assert not remaining, f"the test left containers behind: {remaining}"
    shutil.rmtree(base, ignore_errors=True)  # kept for inspection when any step failed


def _run_session(docs: Docs, op: Operator, runtime: Runtime, world: World) -> None:
    root = world.root
    # DOCKER.md:86-87 (base) "Plan and apply": create the runtime root.
    docs.require("create the runtime root", r'mkdir -m 700 "\$root"', docs=("DOCKER.md",))
    op.sh("create the runtime root", 'mkdir -m 700 "$root"')

    # DOCKER.md:88-95, 101-102; HOST-ADAPTER.md:210 (base): plan with capture's transcript roots.
    docs.require("plan", r"kp-agent-install plan", r'--runtime-root "\$root"',
                 r"--components tooling,refresh,capture,board", r"--transcript-root")
    plan = ('kp-agent-install plan --runtime-root "$root" --image "$image" --repository product="$repo" '
            '--components tooling,refresh,capture,board --uid "$(id -u)" --gid "$(id -g)" --board-port "$port" '
            '--project-name "$project" --transcript-root "$claude_root" --transcript-root "$codex_root"')
    op.sh("plan", plan + " > plan.json")
    # DOCKER.md:120 (base): apply exactly the reviewed plan.
    docs.require("apply", r'kp-agent-install apply .*--expected-plan-sha256 "\$\(jq -r \.plan_sha256 plan\.json\)"')
    op.sh("apply", plan.replace(" plan ", " apply ", 1) + ' --expected-plan-sha256 "$(jq -r .plan_sha256 plan.json)"')

    # Meet (Coordinator ruling, T9b Amendment 6): DOCKER.md runs `prepare` between apply and up; under
    # P1b a role refuses an unprepared store.
    docs.require("prepare the store volume", r'kp-agent-install prepare', docs=("DOCKER.md",))
    from t9b_harness import prepared as _prepared
    readied = op.sh("prepare the store volume", 'kp-agent-install prepare --runtime-root "$root"')
    assert _prepared(readied) is None, explain(readied)

    # DOCKER.md:178 (base) "Start, stop, status": tooling plus the selected profiles.
    docs.require("start the roles", r'docker compose --project-directory "\$root" up -d\s*(#.*)?$')
    runtime.started = True
    op.sh("start the roles", 'docker compose --project-directory "$root" up -d')

    # DOCKER.md:220-224 (base) "Optional roles", refresh: the request from the example, the token.
    docs.require("configure refresh", r"refresh\.example\.json", r"config/refresh\.json",
                 r"secrets/github-token", r"github-token.*\bempty\b", docs=("DOCKER.md",))
    request = json.loads((EXAMPLES / "refresh.example.json").read_text())
    spec = request["repositories"]["product"]
    spec.update(repository="/workspaces/product", workspace_observation_roots=["/workspaces/product"])
    write_private(root / "config" / "refresh.json", json.dumps(request, indent=2) + "\n")
    # "Serena templates referenced from it live under $root/config/."
    docs.require("refresh serena template", r"Serena templates?\b.*config/", docs=("DOCKER.md",))
    write_private(root / spec["serena_template"].lstrip("/"), "name: product\nlanguage: python\n")
    write_private(root / "secrets" / "github-token", b"")

    # HOST-ADAPTER.md:211-212; DESK-MENU.md:15-23 (base): the registry operator config and descriptor.
    docs.require("registry operator config", r"config/launch/registry\.json", r"state_root.{0,40}/state",
                 r"harness_profiles_path.{0,10}/config/launch/harness-profiles\.json", docs=("HOST-ADAPTER.md",))
    docs.require("registry descriptor", r"agent-tooling\.desk-registry\.v1", r"roster_path", docs=("DESK-MENU.md",))
    tenant, instance = "t7a-p2", "claude"
    write_private(root / "config" / "launch" / "desks.json", json.dumps({
        "schema_version": "agent-tooling.desk-registry.v1", "tenant_id": tenant,
        "roster_path": f"{REGISTRY_STATE}/roles.json",  # T9b: the roster is a store, under /state/memory
        "harness_profiles_path": "/config/launch/harness-profiles.json"}))
    registry = {"schema_version": "ops.desk-memory.local.v1", "state_root": REGISTRY_STATE,
                "catalog_path": "/config/launch/desks.json", "workspace_root": "/config/launch",
                "provider_instance": instance, "provider_session_id": "operator-console"}
    write_private(root / "config" / "launch" / "registry.json", json.dumps(registry))

    # Defect 2: create the registry state only if a document says so.
    stated = docs.states_creating("state")
    op.log.append(f"[registry state] documented creation: {stated or 'none; initialize must create it'}")
    if stated:
        op.sh("create the registry state", 'docker compose --project-directory "$root" run --rm -T tooling sh -c '
              + shlex.quote(f"mkdir -p -m 700 {REGISTRY_STATE} && chmod 700 {REGISTRY_STATE}"))
    # DESK-MENU.md:27-31, 50-56 (base): initialize, through the memory container.
    docs.require("initialize the registry", r"kp-agent-desk-registry --config \S+ initialize", r"\bexec\b",
                 docs=("DESK-MENU.md",))
    config = ("kp-agent-desk-registry", "--config", "/config/launch/registry.json")
    op.in_tooling("initialize the registry", *config, "initialize")

    # DESK-MENU.md:33-35 (base): one desk with a role from the roster.
    docs.require("save a desk", r"`save`", r"\bmemory_write\b", r"\bexpected_version\b", docs=("DESK-MENU.md",))
    roles = json.loads(op.in_tooling("list the roles", *config, "roles").stdout)
    role = (roles["roles"] if isinstance(roles, dict) else roles)[0]["role_id"]
    desk_id = "desk:" + str(uuid.uuid4())
    op.in_tooling("save a desk", *config, "save", stdin=json.dumps({
        "desk_id": desk_id, "name": "Rehearsal desk", "description": "The T7a empty-root rehearsal.", "role": role,
        "repos": ["product"], "capture": True, "memory_write": True, "expected_version": 0}))

    # DESK-MENU.md:36, 40 (base): bind one session; its memory config is the registry config with that session.
    docs.require("bind a session", r"`bind`", r"\bnative_session_id\b", r"\bparent_session_id\b",
                 r"provider_session_id.{0,5} set to the bound native session", docs=("DESK-MENU.md",))
    session = "t7a-p2-" + uuid.uuid4().hex[:12]
    op.in_tooling("bind a session", *config, "bind", stdin=json.dumps({
        "harness": instance, "provider": "anthropic", "model": "rehearsal-model", "native_session_id": session,
        "desk_id": desk_id, "source": "operator", "workspace": str(world.repo), "parent_session_id": None}))

    # DOCKER.md:257-267 (base) "Optional roles", capture: an admitted session config, the policy, its approval.
    docs.require("configure capture", r"config/capture/session\.json", r"admitted",
                 r"config/capture/workspace-policy\.json", r"approval", r"host paths", docs=("DOCKER.md",))
    write_private(root / "config" / "capture" / "session.json", json.dumps({**registry, "provider_session_id": session}))
    approval = json.loads((EXAMPLES / "capture-workspace-approval.example.json").read_text())
    approval.update(tenant_id=tenant, repositories=["product"])
    approval_bytes = (json.dumps(approval, indent=2) + "\n").encode()
    policy = json.loads((EXAMPLES / "capture-workspace-policy.example.json").read_text())
    write_private(root / policy["approval_record"].lstrip("/"), approval_bytes)
    policy.update(approval_sha256=hashlib.sha256(approval_bytes).hexdigest(), tenant_id=tenant,
                  approved_repo_keys=["product"], repos={"product": [str(world.repo)]},
                  native_roots={"claude": op.env["claude_root"], "codex": op.env["codex_root"]})
    write_private(root / "config" / "capture" / "workspace-policy.json", json.dumps(policy, indent=2) + "\n")

    # MODEL-GATEWAY.md:22-49, 53-62, 66-75, 230-242 (base): a configuration, a private key file, doctor.
    docs.require("configure the gateway", r"agent-tooling\.model-gateway\.v1", r"artifact_root",
                 r"api_key_file", r"0600", r"kp-agent-models --config \S+ doctor", docs=("MODEL-GATEWAY.md",))
    write_private(root / "config" / "model-gateway.key", "sk-or-v1-t7a-rehearsal-not-a-key\n")
    write_private(root / "config" / "model-gateway.json", json.dumps({
        "schema_version": "agent-tooling.model-gateway.v1", "artifact_root": ARTIFACT_ROOT,
        "providers": {"openrouter": {"kind": "openrouter", "base_url": "https://openrouter.ai/api/v1",
                                     "api_key_file": "/config/model-gateway.key"}},
        "routes": {"text.complete": {"provider": "openrouter", "model": "openai/gpt-4o-mini"}},
        "budgets": {"text.complete": {"max_calls_per_hour": 5, "max_usd_per_day": 0.1}}}))
    # Defect 5: create the artifact root only if a document says so.
    stated = docs.states_creating("artifact")
    op.log.append(f"[artifact root] documented creation: {stated or 'none'}")
    if stated:
        op.sh("create the artifact root", f'mkdir -m 700 "$root{ARTIFACT_ROOT}"')
    doctor = op.in_tooling("gateway doctor", "kp-agent-models", "--config", "/config/model-gateway.json", "doctor")
    report = json.loads(doctor.stdout)
    assert report.get("status") == "ready" and all(r.get("status") == "ready" for r in report.get("routes") or []), (
        f"the gateway doctor is not ready: {report}")

    # Only if a document says to restart or recreate roles after configuring them.
    for cite, line in docs.restart_after_configuration():
        command = re.sub(r"<[^>]+>", "refresh capture", line)
        op.sh(f"documented restart ({cite})", command)

    # The ready state.
    runtime.wait_healthy(("tooling", "refresh", "capture", "board", "indexer"))  # T12b: indexer comes with capture
    # Refresh runs the operator's request: its first attempt writes the request's check_status.
    status = root / request["check_status"].lstrip("/")
    deadline = time.monotonic() + CAPTURE_TIMEOUT
    while not status.is_file() and time.monotonic() < deadline:
        time.sleep(3)
    assert status.is_file(), (f"refresh never ran the operator's request ({request['check_status']} absent after "
                              f"{CAPTURE_TIMEOUT}s)\nrefresh logs:\n{runtime.logs(runtime.container('refresh'))[-3000:]}"
                              "\n\nsession:\n" + "\n".join(op.log))
    listing = json.loads(op.in_tooling("list the registry", *config, "list").stdout)
    assert len(listing.get("desks") or []) == 1, f"the registry does not hold exactly one desk: {listing.get('desks')}"
    bound = [b for b in listing.get("bindings") or [] if b.get("native_session_id") == session]
    assert len(bound) == 1 and len(listing.get("bindings") or []) == 1, (
        f"the registry does not hold exactly the one bound session: {listing.get('bindings')}")

    marker = "Rehearsal juniper " + uuid.uuid4().hex[:8]
    native = str(uuid.uuid4())
    transcript = _claude_transcript(Path(op.env["claude_root"]), world.repo, native)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("".join(json.dumps(row) + "\n" for row in _claude_rows(
        native, world.repo, f"Remember {marker}.", f"Recorded {marker}.")))
    deadline, found = time.monotonic() + CAPTURE_TIMEOUT, None
    while time.monotonic() < deadline:
        searched = op.in_tooling("search captured evidence", "kp-agent-memory", "--config",
                                 "/config/capture/session.json", "call", "--tool", "memory.search",
                                 "--arguments", json.dumps({"query": marker, "scope": "topic"}), ok=False)
        results = []
        if searched.returncode == 0:
            try:
                results = json.loads(searched.stdout).get("results") or []
            except ValueError:
                pass
        if any(marker in json.dumps(row) for row in results):
            found = results
            break
        time.sleep(5)
    capture_logs = runtime.logs(runtime.container("capture"))
    assert found, (f"capture did not import the new transcript within {CAPTURE_TIMEOUT}s (topic-scope search "
                   f"finds no {marker!r})\ncapture logs:\n{capture_logs[-3000:]}\n\nsession:\n" + "\n".join(op.log))
    runtime.assert_stays_up(("tooling", "refresh", "capture", "board", "indexer"), window=4.0)
    print("P2 operator session:\n" + "\n".join(op.log))
