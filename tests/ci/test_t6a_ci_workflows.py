"""T6a CI structure (docs/work/orders/T6a-images-and-ci.md): P4 and the P3 jobs.

P4 — "CI is safe. No workflow references `secrets.`, pushes an image, publishes
anything, or lacks a timeout." Interface: "Workflows use `permissions: contents: read`,
read no secrets, push nothing and publish nothing, and every job has a timeout."

P3 jobs (interface "CI"): `.github/workflows/*.yml` runs
- core `tests` on Python 3.11 and 3.12, with the core only installed;
- `extensions/ops/tests` with core plus extension installed;
- Kanban `npm ci`, the typecheck, the full vitest suite and `npm run build`;
- the web-ui tests;
- an image job that builds `product`, `runtime`, `agents` and `ops`, then runs
  `tests/image -m image`, `tests/install -m image` and an `ops` check.
"The image job runs on pull requests that touch `deploy/`, `packages/`, `extensions/`,
`apps/kanban/`, `tests/image/` or `tests/install/`, and on pushes to `main`."

The one exception to P4 is the release workflow (order D0b, R1; its exception is R1's guard).
A workflow is a release workflow iff its `push` trigger is restricted to tags matching `v*` and
names no branches: a structural predicate, never a file name. In it, non-read permissions, any
secret or token reference and any publishing step occur ONLY in jobs whose job-level `if` carries
all three parts of R1's guard (event push, a `refs/tags/v` ref, the public repository's numeric
id); every unguarded job keeps P4, with one carve-out: a push whose registry host is `localhost`
or `127.0.0.1` (the job's own `registry:2` service) is a rehearsal, not a publish. The image
trigger test does not bind the release workflow (images.yml stays bound to it). Every other
workflow keeps all four P4 properties unchanged.

These are structural: they read the workflow files and never run them. Whether each job
actually goes red on its falsifier is proven by the Coordinator on GitHub at the meet.
What each check recognises is listed in tests/ci/t6a_workflows.py.
"""
from __future__ import annotations

import re

from t6a_workflows import (
    DOCKERFILE, EXTENSION_PROJECT, IMAGE_TARGETS, KANBAN, WEB_UI, image_builds, installs_extension, job_runs,
    local_actions, npm_runs, pull_request_runs_for, push_to_main_runs_for, pytest_runs, pytest_runs_with_installs,
    require_workflows, runs_full_vitest, runs_typecheck, triggers, uses, visible_env_names, walk,
)

READ_ONLY = {"contents": "read"}
IMAGE_SUITE_VARIABLES = ("AGENT_TOOLING_TEST_IMAGE", "AGENT_TOOLING_TEST_IMAGE_OPS")
IMAGE_PATH_PREFIXES = ("deploy", "packages", "extensions", "apps/kanban", "tests/image", "tests/install")
UNRELATED_PATH = "docs/t6a-probe.md"

# D0b R1: the public repository's numeric id (the order's own value) and the guard's three parts.
PUBLIC_REPOSITORY_ID = "1404822873"
_GUARD_PARTS = (
    re.compile(r"github\.event_name\s*==\s*'push'"),
    re.compile(r"startsWith\(\s*github\.ref\s*,\s*'refs/tags/v'\s*\)"),
    re.compile(r"github\.repository_id\s*==\s*'?" + PUBLIC_REPOSITORY_ID + r"'?(?![0-9])"),
)


def is_release_workflow(workflow) -> bool:
    """D0b R1, structural: the push trigger runs on `v*` tags only and names no branches."""
    push = triggers(workflow).get("push")
    if push is None:
        return False
    tags = push.get("tags")
    tags = [tags] if isinstance(tags, str) else tags
    return (isinstance(tags, list) and bool(tags) and all(isinstance(t, str) and t.startswith("v") for t in tags)
            and "branches" not in push and "branches-ignore" not in push)


def guarded(job) -> bool:
    """The job-level `if` carries all three parts of D0b R1's guard, and no `||` can bypass them."""
    condition = job.get("if") if isinstance(job, dict) else None
    return (isinstance(condition, str) and "||" not in condition
            and all(part.search(condition) for part in _GUARD_PARTS))


_ENV_REF = re.compile(r"\$\{\{\s*env\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}|\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
_LOCAL_HOST = re.compile(r"(?:^|[\s=,\"'@])(?:localhost|127\.0\.0\.1)(?::[0-9]+)?/")
_REMOTE_HOST = re.compile(r"(?:^|[\s=,\"'@])(?!localhost|127\.0\.0\.1)[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::[0-9]+)?/", re.I)


def _resolve(text: str, *environments) -> str:
    merged = {}
    for environment in environments:
        if isinstance(environment, dict):
            merged.update({str(k): str(v) for k, v in environment.items()})
    return _ENV_REF.sub(lambda m: merged.get(m.group(1) or m.group(2), m.group(0)), text)


def _local_only(text: str) -> bool:
    """A push whose every registry host is the runner's own registry:2 (localhost or 127.0.0.1)."""
    return bool(_LOCAL_HOST.search(text)) and not _REMOTE_HOST.search(text)


# ======================================================================= P4


def _job_permissions_ok(value) -> bool:
    if value is None:
        return True
    return isinstance(value, dict) and set(value) <= {"contents"} and all(v in ("read", "none") for v in value.values())


def test_every_workflow_grants_only_contents_read():
    problems = []
    for workflow in require_workflows():
        top = (workflow.data or {}).get("permissions")
        jobs = workflow.jobs
        if top != READ_ONLY and not (jobs and all(isinstance(j, dict) and j.get("permissions") == READ_ONLY
                                                  for j in jobs.values())):
            problems.append(f"{workflow.name}: top-level permissions {top!r}, expected {READ_ONLY}")
        release = is_release_workflow(workflow)
        for job_id, job in jobs.items():
            if release and guarded(job):
                continue  # D0b R1: a guarded release job may hold what R9 names
            if isinstance(job, dict) and not _job_permissions_ok(job.get("permissions")):
                problems.append(f"{workflow.name}:{job_id}: job permissions {job.get('permissions')!r} "
                                "grant more than contents: read")
    assert not problems, "workflows not restricted to permissions: contents: read:\n" + "\n".join(problems)


_SECRET_REFERENCE = re.compile(r"\bsecrets\s*(?:\.|\[)|\bgithub\.token\b")


def test_no_workflow_references_secrets():
    problems = []
    for document in [*require_workflows(), *local_actions()]:
        if document.error:
            problems.append(f"{document.name}: does not parse ({document.error})")
            continue
        release = is_release_workflow(document)
        for path, key, text in walk(document.data):
            parts = path.split(".")
            if release and len(parts) > 2 and parts[0] == "jobs" and guarded(document.jobs.get(parts[1])):
                continue  # D0b R1: GITHUB_TOKEN inside a guarded release job
            if key == "secrets":
                problems.append(f"{document.name}: {path} declares or passes secrets")
            elif text is not None and _SECRET_REFERENCE.search(text):
                problems.append(f"{document.name}: {path} reads a secret: {text.strip()[:200]!r}")
    assert not problems, "workflows that reference secrets:\n" + "\n".join(problems)


_PUSH_OR_PUBLISH_COMMANDS = [
    (r"\bdocker\s+(?:image\s+|manifest\s+)?push\b", "docker push"),
    (r"\bdocker\s+(?:buildx\s+)?(?:build|bake)\b[^\n]*\s--push\b", "docker build --push"),
    (r"\btype=registry\b", "a registry build output"),
    (r"\bpush=true\b", "a push=true build output"),
    (r"\bdocker\s+login\b", "docker login"),
    (r"\b(?:npm|pnpm|yarn)\s+(?:[^\n;&|]*\s)?publish\b", "a package publish"),
    (r"\btwine\s+upload\b", "twine upload"),
    (r"\b(?:poetry|flit|uv|hatch|pdm|cargo)\s+publish\b", "a package publish"),
    (r"\bgh\s+release\s+(?:create|upload|edit)\b", "a GitHub release"),
    (r"\bgit\s+push\b", "git push"),
    (r"\b(?:crane|gcrane)\s+(?:push|copy|cp|append|mutate|tag)\b", "a registry write"),
    (r"\bskopeo\s+copy\b", "a registry copy"),
    (r"\boras\s+push\b", "oras push"),
    (r"\bdocker\s+buildx\s+imagetools\s+create\b", "a manifest list"),
]
_PUSH_OR_PUBLISH_ACTIONS = (
    "docker/login-action", "pypa/gh-action-pypi-publish", "softprops/action-gh-release", "actions/create-release",
    "actions/upload-release-asset", "ncipollo/release-action", "js-devtools/npm-publish", "peaceiris/actions-gh-pages",
    "actions/deploy-pages", "actions/upload-pages-artifact", "jamesives/github-pages-deploy-action",
    "ad-m/github-push-action", "stefanzweifel/git-auto-commit-action", "endbug/add-and-commit", "changesets/action",
    "svenstaro/upload-release-action", "aws-actions/amazon-ecr-login", "azure/docker-login",
    "redhat-actions/push-to-registry", "google-github-actions/upload-cloud-storage",
    "actions/attest-build-provenance", "actions/attest",
)


def _steps_of(document) -> list:
    data = document.data or {}
    steps = []
    for job in (data.get("jobs") or {}).values() if isinstance(data.get("jobs"), dict) else []:
        if isinstance(job, dict) and isinstance(job.get("steps"), list):
            steps += [s for s in job["steps"] if isinstance(s, dict)]
    runs = data.get("runs")
    if isinstance(runs, dict) and isinstance(runs.get("steps"), list):
        steps += [s for s in runs["steps"] if isinstance(s, dict)]
    return steps


def _publishing(document, step, environments=()) -> list:
    """What a step publishes; in a release workflow a push to the runner's own registry is a rehearsal."""
    found = []
    action = uses(step)
    if any(action.startswith(name + "@") or action == name for name in _PUSH_OR_PUBLISH_ACTIONS):
        found.append(f"step uses {step.get('uses')}")
    if action.startswith("docker/build-push-action@"):
        config = step.get("with") or {}
        push = str(config.get("push", "false")).strip().lower()
        outputs = str(config.get("outputs", ""))
        if push not in ("false", "") or re.search(r"type=registry|push=true", outputs):
            target = _resolve(f"{outputs} {config.get('tags', '')}", *environments)
            if not (environments and _local_only(target)):
                found.append(f"docker/build-push-action with push={config.get('push')!r} outputs={outputs!r}")
    script = step.get("run")
    if isinstance(script, str):
        code = "\n".join(line for line in re.sub(r"\\\r?\n", " ", script).splitlines()
                         if not line.strip().startswith("#"))
        for line in code.splitlines():
            for pattern, what in _PUSH_OR_PUBLISH_COMMANDS:
                match = re.search(pattern, line)
                if match and not (environments and what != "docker login"
                                  and _local_only(_resolve(line, *environments))):
                    found.append(f"run step does {what}: {match.group(0)!r}")
    return found


def _release_problems(document) -> list:
    problems = []
    workflow_env = (document.data or {}).get("env")
    for job_id, job in document.jobs.items():
        if not isinstance(job, dict) or guarded(job):
            continue  # D0b R1: publishing is allowed only inside a guarded job
        for step in job.get("steps") or []:
            if isinstance(step, dict):
                problems += [f"{document.name}:{job_id}: {what}"
                             for what in _publishing(document, step, (workflow_env, job.get("env"), step.get("env")))]
    return problems


def test_no_workflow_pushes_or_publishes():
    problems = []
    for document in [*require_workflows(), *local_actions()]:
        if is_release_workflow(document):
            problems += _release_problems(document)
            continue
        for step in _steps_of(document):
            action = uses(step)
            if any(action.startswith(name + "@") or action == name for name in _PUSH_OR_PUBLISH_ACTIONS):
                problems.append(f"{document.name}: step uses {step.get('uses')}")
            if action.startswith("docker/build-push-action@"):
                config = step.get("with") or {}
                push = str(config.get("push", "false")).strip().lower()
                outputs = str(config.get("outputs", ""))
                if push not in ("false", "") or re.search(r"type=registry|push=true", outputs):
                    problems.append(f"{document.name}: docker/build-push-action with push={config.get('push')!r} "
                                    f"outputs={outputs!r}")
            script = step.get("run")
            if isinstance(script, str):
                code = "\n".join(line for line in re.sub(r"\\\r?\n", " ", script).splitlines()
                                 if not line.strip().startswith("#"))
                for pattern, what in _PUSH_OR_PUBLISH_COMMANDS:
                    match = re.search(pattern, code)
                    if match:
                        problems.append(f"{document.name}: run step does {what}: {match.group(0)!r}")
    assert not problems, "workflows that push or publish:\n" + "\n".join(problems)


def _has_timeout(job: dict) -> bool:
    value = job.get("timeout-minutes")
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return value > 0
    return isinstance(value, str) and "${{" in value


def test_every_job_has_a_timeout():
    problems, seen = [], 0
    for workflow in require_workflows():
        for job_id, job in workflow.jobs.items():
            seen += 1
            if not isinstance(job, dict):
                problems.append(f"{workflow.name}:{job_id}: not a mapping")
            elif str(job.get("uses", "")).startswith("./.github/workflows/"):
                continue  # a local reusable workflow: its own jobs are checked where it is defined
            elif not _has_timeout(job):
                problems.append(f"{workflow.name}:{job_id}: timeout-minutes is {job.get('timeout-minutes')!r}")
    assert seen, "the workflows define no jobs"
    assert not problems, "jobs without a positive timeout-minutes:\n" + "\n".join(problems)


# ======================================================================= P3 jobs


def _describe_pytest(runs) -> str:
    rows = [f"  {p.command.run.label} step {p.command.step_index}: python={p.version} targets={p.targets} "
            f"-m={p.markexpr!r}" for run in runs for p in pytest_runs(run)]
    return "\n".join(rows) or "  (no pytest invocation found)"


def test_core_tests_run_on_python_3_11_and_3_12_with_the_core_only_installed():
    runs = job_runs(require_workflows())
    covered = {}
    for run in runs:
        for invocation, installed in pytest_runs_with_installs(run):
            if any(installs_extension(t) for t in installed):
                continue  # the extension is installed by the time this run starts: not core only
            if "tests" in invocation.targets and not invocation.selects_image() and invocation.version:
                covered.setdefault(invocation.version, []).append(run.label)
    missing = [v for v in ("3.11", "3.12") if v not in covered]
    assert not missing, (f"no job runs the core suite (pytest tests, not -m image) with only the core installed on "
                         f"Python {missing}; covered: {covered}\npytest invocations seen:\n{_describe_pytest(runs)}")


def test_extension_tests_run_with_core_and_extension_installed():
    runs = job_runs(require_workflows())
    found = []
    for run in runs:
        for invocation, installed in pytest_runs_with_installs(run):
            if not any(installs_extension(t) for t in installed):
                continue
            if (any(t in (EXTENSION_PROJECT, EXTENSION_PROJECT + "/tests") for t in invocation.targets)
                    and not invocation.selects_image()):
                found.append(run.label)
    assert found, ("no job installs the extension (extensions/ops) and runs extensions/ops/tests; "
                   f"pytest invocations seen:\n{_describe_pytest(runs)}")


def _describe_npm(runs) -> str:
    rows = [f"  {n.command.run.label} step {n.command.step_index}: dir={n.directory} {n.verb} {n.script or ''} "
            f"{n.extra}" for run in runs for n in npm_runs(run)]
    return "\n".join(rows) or "  (no npm/npx invocation found)"


def test_kanban_runs_npm_ci_typecheck_full_vitest_and_build():
    runs = job_runs(require_workflows())
    satisfied = {"typecheck": [], "full vitest suite": [], "npm run build": []}
    for run in runs:
        npms = [n for n in npm_runs(run) if n.directory == KANBAN]
        if not any(n.verb == "ci" for n in npms):
            continue
        for n in npms:
            if runs_typecheck(n):
                satisfied["typecheck"].append(run.label)
            if runs_full_vitest(n):
                satisfied["full vitest suite"].append(run.label)
            if n.verb == "run" and n.script == "build":
                satisfied["npm run build"].append(run.label)
    missing = [name for name, where in satisfied.items() if not where]
    assert not missing, (f"no job runs `npm ci` in {KANBAN} together with: {missing}\n"
                         f"npm invocations seen:\n{_describe_npm(runs)}")


def test_web_ui_tests_run():
    runs = job_runs(require_workflows())
    found = []
    for run in runs:
        for n in npm_runs(run):
            if n.directory == KANBAN and n.verb == "run" and n.script == "web:test":
                found.append(run.label)
            elif n.directory == WEB_UI and runs_full_vitest(n):
                found.append(run.label)
    assert found, (f"no job runs the web-ui tests (`npm run web:test` in {KANBAN}, or `npm test` / "
                   f"`npx vitest run` in {WEB_UI}); npm invocations seen:\n{_describe_npm(runs)}")


def test_image_job_builds_product_runtime_agents_and_ops():
    runs = job_runs(require_workflows())
    best = {}
    for run in runs:
        targets = {b.target for b in image_builds(run)}
        if set(IMAGE_TARGETS) <= targets:
            return
        best[run.label] = sorted(t for t in targets if t)
    seen = {label: targets for label, targets in best.items() if targets}
    assert False, (f"no single job builds every target {list(IMAGE_TARGETS)} from {DOCKERFILE}; "
                   f"targets built per job: {seen or 'none'}")


def _image_suite_runs(runs):
    """Job runs that build from deploy/Dockerfile and then run both image suites with the
    product and ops image variables visible to the tests/image run."""
    found, notes = [], []
    for run in runs:
        builds = image_builds(run)
        if not builds:
            continue
        first_build = min(b.step_index for b in builds)
        invocations = [p for p in pytest_runs(run) if p.selects_image() and p.command.step_index > first_build]
        image_suite = [p for p in invocations if "tests/image" in p.targets]
        install_suite = [p for p in invocations if "tests/install" in p.targets]
        if not (image_suite and install_suite):
            notes.append(f"{run.label}: tests/image -m image after a build: {bool(image_suite)}; "
                         f"tests/install -m image after a build: {bool(install_suite)}")
            continue
        wired = [p for p in image_suite
                 if all(v in visible_env_names(run, p.command.step_index, p.command) for v in IMAGE_SUITE_VARIABLES)]
        if not wired:
            notes.append(f"{run.label}: tests/image runs without {list(IMAGE_SUITE_VARIABLES)} visible to it")
            continue
        found.append(run)
    return found, notes


def test_image_job_runs_the_image_and_install_suites_and_the_ops_check():
    runs = job_runs(require_workflows())
    found, notes = _image_suite_runs(runs)
    assert found, ("no job builds from deploy/Dockerfile and then runs `pytest tests/image -m image` (with "
                   f"{' and '.join(IMAGE_SUITE_VARIABLES)} set for it, the ops check) and `pytest tests/install "
                   "-m image`:\n" + ("\n".join(notes) or "  (no job builds from deploy/Dockerfile)")
                   + f"\npytest invocations seen:\n{_describe_pytest(runs)}")


def test_image_job_runs_on_pull_requests_touching_its_paths_and_on_pushes_to_main():
    runs = job_runs(require_workflows())
    image_jobs = {run.workflow.name: run.workflow for run in runs
                  if set(IMAGE_TARGETS) <= {b.target for b in image_builds(run)}}
    image_jobs.update({run.workflow.name: run.workflow for run in _image_suite_runs(runs)[0]})
    # D0b R1: the release workflow's triggers are R1's own; images.yml stays bound to these.
    image_jobs = {name: workflow for name, workflow in image_jobs.items() if not is_release_workflow(workflow)}
    assert image_jobs, "no image job (one that builds the deploy/Dockerfile targets or runs the image suites)"
    problems = []
    for name, workflow in image_jobs.items():
        for prefix in IMAGE_PATH_PREFIXES:
            for probe in (f"{prefix}/t6a-probe.txt", f"{prefix}/t6a/deeper/probe.txt"):
                if not pull_request_runs_for(workflow, probe):
                    problems.append(f"{name}: a pull request touching only {probe} does not run it")
        if pull_request_runs_for(workflow, UNRELATED_PATH):
            problems.append(f"{name}: a pull request touching only {UNRELATED_PATH} runs it (no path filter); "
                            f"pull_request trigger: {triggers(workflow).get('pull_request')!r}")
        if not push_to_main_runs_for(workflow, UNRELATED_PATH):
            problems.append(f"{name}: a push to main does not always run it; push trigger: "
                            f"{triggers(workflow).get('push')!r}")
    assert not problems, "image job triggers:\n" + "\n".join(problems)
