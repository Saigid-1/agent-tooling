"""Static reader of ``.github/workflows`` for the T6a CI-structure tests.

Contract: ``docs/work/orders/T6a-images-and-ci.md`` (interface surface "CI", P3 job
presence, P4). Nothing here runs a workflow. It parses each workflow file with PyYAML
(a core dependency), expands ``strategy.matrix`` combinations, and reads the shell in
``run:`` steps: line continuations, ``&&``/``;``/``|`` lists, ``cd``, ``for … in …``
loops, step/job/workflow ``working-directory`` and environment-variable prefixes.

Recognised forms, so a reader knows what a red result can mean:

- Python: ``actions/setup-python`` ``python-version`` (literal, list or matrix), a
  ``python:X.Y`` job container, or an interpreter named ``pythonX.Y``.
- Installs: ``pip``/``pip3``/``python -m pip``/``uv pip`` ``install`` with path,
  ``-e``/``--editable`` path, distribution-name or wheel-file arguments.
- pytest: ``pytest``/``py.test``/``python -m pytest``; positional paths resolve against
  the working directory; with none, the ``testpaths`` of the ``pytest.ini`` there.
- npm: ``npm [--prefix DIR|-C DIR] ci|test|run SCRIPT``, ``npx vitest|tsc``.
- Image builds: ``docker [buildx|image] build`` with ``--target``/``-f``, and
  ``docker/build-push-action`` with ``target``/``file``.
- Path filters: workflow-level ``on.pull_request.paths``/``paths-ignore`` and
  ``on.push.branches``/``branches-ignore``/``paths``/``paths-ignore``.
"""
from __future__ import annotations

import configparser
import itertools
import json
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
KANBAN = "apps/kanban"
WEB_UI = "apps/kanban/web-ui"
EXTENSION_PROJECT = "extensions/ops"
DOCKERFILE = "deploy/Dockerfile"
IMAGE_TARGETS = ("product", "runtime", "agents", "ops")


# ------------------------------------------------------------------ loading


@dataclass
class Workflow:
    path: Path
    data: dict | None = None
    error: str | None = None

    @property
    def name(self) -> str:
        return self.path.relative_to(REPO_ROOT).as_posix()

    @property
    def jobs(self) -> dict:
        jobs = (self.data or {}).get("jobs")
        return jobs if isinstance(jobs, dict) else {}


def _load(path: Path) -> Workflow:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as error:
        return Workflow(path, error=f"{type(error).__name__}: {error}")
    if not isinstance(data, dict):
        return Workflow(path, error=f"top level is {type(data).__name__}, not a mapping")
    return Workflow(path, data=data)


def workflow_files() -> list[Path]:
    if not WORKFLOW_DIR.is_dir():
        return []
    return sorted(p for p in WORKFLOW_DIR.iterdir() if p.is_file() and p.suffix in (".yml", ".yaml"))


def require_workflows() -> list[Workflow]:
    """Every workflow file, parsed; FAILS when there is none or one does not parse."""
    files = workflow_files()
    if not files:
        pytest.fail(f"no workflow files (*.yml, *.yaml) under {WORKFLOW_DIR.relative_to(REPO_ROOT)}; "
                    "zero workflows is not a safe or effective CI", pytrace=False)
    loaded = [_load(path) for path in files]
    broken = [f"{w.name}: {w.error}" for w in loaded if w.error]
    if broken:
        pytest.fail("workflow files that do not parse as a YAML mapping:\n" + "\n".join(broken), pytrace=False)
    return loaded


def local_actions() -> list[Workflow]:
    """Local composite actions (``.github/actions/**/action.y*ml``); scanned for P4 only."""
    if not ACTIONS_DIR.is_dir():
        return []
    files = sorted(p for p in ACTIONS_DIR.rglob("action.y*ml") if p.suffix in (".yml", ".yaml"))
    return [_load(path) for path in files]


def triggers(workflow: Workflow) -> dict:
    data = workflow.data or {}
    on = data.get("on", data.get(True))  # YAML 1.1 reads a bare `on` key as True
    if isinstance(on, str):
        return {on: {}}
    if isinstance(on, list):
        return {str(event): {} for event in on}
    if isinstance(on, dict):
        return {str(event): (config if isinstance(config, dict) else {}) for event, config in on.items()}
    return {}


# ------------------------------------------------------------------ matrix


_MATRIX_EXPRESSION = re.compile(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}")


def matrix_combinations(job: dict) -> list[dict]:
    matrix = (job.get("strategy") or {}).get("matrix") if isinstance(job.get("strategy"), dict) else None
    if not isinstance(matrix, dict):
        return [{}]
    axes = {k: v for k, v in matrix.items() if k not in ("include", "exclude") and isinstance(v, list)}
    combos = [dict(zip(axes, values)) for values in itertools.product(*axes.values())] if axes else [{}]
    for excluded in matrix.get("exclude") or []:
        if isinstance(excluded, dict):
            combos = [c for c in combos if not all(c.get(k) == v for k, v in excluded.items())]
    for included in matrix.get("include") or []:
        if not isinstance(included, dict):
            continue
        matched = False
        for combo in combos:
            if axes and all(combo.get(k) == v for k, v in included.items() if k in axes):
                combo.update({k: v for k, v in included.items() if k not in axes})
                matched = True
        if not matched:
            combos.append(dict(included))
    return combos or [{}]


def _text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def substitute(value, combo: dict):
    if isinstance(value, str):
        return _MATRIX_EXPRESSION.sub(lambda m: _text(combo[m.group(1)]) if m.group(1) in combo else m.group(0), value)
    if isinstance(value, list):
        return [substitute(v, combo) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, combo) for k, v in value.items()}
    return value


@dataclass
class JobRun:
    workflow: Workflow
    job_id: str
    combo: dict
    job: dict

    @property
    def label(self) -> str:
        suffix = f" {json.dumps(self.combo, sort_keys=True)}" if self.combo else ""
        return f"{self.workflow.name}:{self.job_id}{suffix}"

    @property
    def steps(self) -> list:
        steps = self.job.get("steps")
        return [s if isinstance(s, dict) else {} for s in steps] if isinstance(steps, list) else []


def job_runs(workflows: list[Workflow]) -> list[JobRun]:
    runs = []
    for workflow in workflows:
        for job_id, job in workflow.jobs.items():
            if not isinstance(job, dict):
                continue
            for combo in matrix_combinations(job):
                runs.append(JobRun(workflow, str(job_id), combo, substitute(job, combo)))
    return runs


# ------------------------------------------------------------------ paths


def norm(path, base: str = ".") -> str:
    """A repository-relative path ('.' is the root)."""
    text = _text(path).strip()
    text = re.sub(r"^(?:\$\{\{\s*github\.workspace\s*\}\}|\$\{?GITHUB_WORKSPACE\}?)/?", "", text)
    if text.startswith("/"):
        return posixpath.normpath(text)
    return posixpath.normpath(posixpath.join(base or ".", text or "."))


def _default_dir(run: JobRun) -> str:
    for scope in (run.job, run.workflow.data or {}):
        defaults = scope.get("defaults") if isinstance(scope.get("defaults"), dict) else {}
        directory = (defaults.get("run") or {}).get("working-directory") if isinstance(defaults.get("run"), dict) else None
        if directory:
            return norm(directory)
    return "."


def step_dir(run: JobRun, step: dict) -> str:
    return norm(step["working-directory"]) if step.get("working-directory") else _default_dir(run)


def default_testpaths(directory: str) -> list[str]:
    ini = REPO_ROOT / directory / "pytest.ini"
    if ini.is_file():
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(ini.read_text())
            raw = parser.get("pytest", "testpaths", fallback="")
        except configparser.Error:
            raw = ""
        if raw.split():
            return [norm(item, directory) for item in raw.split()]
    return [directory]


# ------------------------------------------------------------------ shell


@dataclass
class Command:
    run: JobRun
    step_index: int
    cwd: str
    words: list
    assignments: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return " ".join(self.words)


_SEPARATORS = {"&&", "||", ";", "|", "&", "|&", ";;", "(", ")"}
_KEYWORDS = {"do", "then", "else", "elif", "{", "}", "!", "time", "if", "while", "until"}
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
_REDIRECTION = re.compile(r"(?<![\w$=-])\d*(?:>>|>&|<&|&>|>|<)\s*(?:&\d+|[^\s;&|()]+)")


def _mask_substitutions(line: str) -> str:
    """Replace $(...), $((...)) and `...` with a placeholder word."""
    out, i = [], 0
    while i < len(line):
        if line.startswith("$(", i):
            depth, j = 0, i + 1
            while j < len(line):
                if line[j] == "(":
                    depth += 1
                elif line[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out.append("__SUBST__")
            i = j + 1
        elif line[i] == "`":
            j = line.find("`", i + 1)
            out.append("__SUBST__")
            i = len(line) if j < 0 else j + 1
        else:
            out.append(line[i])
            i += 1
    return "".join(out)


def _tokens(line: str) -> list[str]:
    line = _REDIRECTION.sub(" ", _mask_substitutions(line))
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        return list(lexer)
    except ValueError:
        return line.split()


def logical_lines(script: str) -> list[str]:
    joined = re.sub(r"\\\r?\n", " ", script)
    return [line for line in joined.splitlines() if line.strip() and not line.strip().startswith("#")]


def _expand(var: str, item: str, words: list) -> list:
    return [re.sub(r"\$\{" + re.escape(var) + r"\}|\$" + re.escape(var) + r"\b", item, w) for w in words]


def shell_commands(run: JobRun, step_index: int, script: str, cwd: str) -> list[Command]:
    emitted: list[Command] = []
    loops: list[tuple[str, list, list]] = []  # (variable, items, body word-lists)

    def emit(words: list, directory: str):
        if loops:
            loops[-1][2].append((words, directory))
            return
        assignments = {}
        while words and _ASSIGNMENT.fullmatch(words[0]):
            key, _, value = words[0].partition("=")
            assignments[key] = value
            words = words[1:]
        if words:
            emitted.append(Command(run, step_index, directory, words, assignments))

    for line in logical_lines(script):
        current: list = []
        simple: list[list] = []
        for token in _tokens(line):
            if token in _SEPARATORS:
                simple.append(current)
                current = []
            else:
                current.append(token)
        simple.append(current)
        for words in simple:
            while words and words[0] in _KEYWORDS:
                words = words[1:]
            if not words:
                continue
            if words[0] == "for" and len(words) >= 3 and words[2] == "in":
                loops.append((words[1], words[3:], []))
                continue
            if words[0] == "done" and loops:
                var, items, body = loops.pop()
                for item in items:
                    for body_words, directory in body:
                        emit(_expand(var, item, body_words), directory)
                continue
            if words[0] == "cd" and not loops:
                cwd = norm(words[1], cwd) if len(words) > 1 else "."
                continue
            emit(words, cwd)
    return emitted


def commands(run: JobRun) -> list[Command]:
    found = []
    for index, step in enumerate(run.steps):
        script = step.get("run")
        if isinstance(script, str):
            found += shell_commands(run, index, script, step_dir(run, step))
    return found


def uses(step: dict) -> str:
    return str(step.get("uses") or "").strip().lower()


# ------------------------------------------------------------------ Python


def _minor(text) -> str | None:
    match = re.match(r"\s*(\d+)\.(\d+)", _text(text))
    return f"{match.group(1)}.{match.group(2)}" if match else None


def python_version_before(run: JobRun, step_index: int) -> str | None:
    """The default Python of the job at a step: the last setup-python before it, else the container."""
    version = None
    container = run.job.get("container")
    image = container.get("image") if isinstance(container, dict) else container
    if isinstance(image, str):
        match = re.search(r"python:(\d+\.\d+)", image)
        version = match.group(1) if match else None
    for step in run.steps[:step_index]:
        if uses(step).startswith("actions/setup-python@"):
            configured = (step.get("with") or {}).get("python-version")
            if configured is None:
                continue
            if isinstance(configured, list):
                items = configured
            else:
                items = [part for part in re.split(r"[\n,]", _text(configured)) if part.strip()]
            if items:
                version = _minor(items[-1]) or version
    return version


@dataclass
class Pytest:
    command: Command
    targets: list
    markexpr: str | None
    version: str | None

    def selects_image(self) -> bool:
        expr = self.markexpr or ""
        return bool(re.search(r"\bimage\b", expr)) and not re.search(r"\bnot\s+image\b", expr)


_PYTEST_VALUE_OPTIONS = {
    "-k", "-p", "-c", "-o", "-W", "-n", "--basetemp", "--rootdir", "--confcutdir", "--junitxml",
    "--junit-xml", "--maxfail", "--tb", "--durations", "--durations-min", "--ignore", "--ignore-glob",
    "--deselect", "--timeout", "--log-level", "--log-cli-level", "--log-file", "--override-ini",
    "--import-mode", "--cov", "--cov-report", "--cov-config", "--dist", "--capture", "--color",
    "--reruns", "--reruns-delay", "--junit-prefix", "--config-file", "--maxprocesses", "--rsyncdir",
    "--tx", "--report-log", "--json-report-file", "--html", "--count", "--random-order-seed",
}


def pytest_invocation(command: Command) -> Pytest | None:
    words = command.words
    start, interpreter = None, None
    for i, word in enumerate(words):
        base = posixpath.basename(word)
        if base in ("pytest", "py.test"):
            start = i
            break
        if (word == "-m" and i + 1 < len(words) and words[i + 1] == "pytest" and i > 0
                and re.fullmatch(r"python[0-9.]*", posixpath.basename(words[i - 1]))):
            start, interpreter = i + 1, words[i - 1]
            break
    if start is None:
        return None
    args, targets, markexpr, j = words[start + 1:], [], None, 0
    while j < len(args):
        arg = args[j]
        if arg == "-m":
            markexpr = args[j + 1] if j + 1 < len(args) else ""
            j += 2
        elif arg.startswith("-m") and not arg.startswith("--") and len(arg) > 2:
            markexpr = arg[2:]
            j += 1
        elif arg.startswith("-"):
            name, eq, _ = arg.partition("=")
            j += 2 if (name in _PYTEST_VALUE_OPTIONS and not eq) else 1
        else:
            targets.append(arg)
            j += 1
    resolved = [norm(t.split("::", 1)[0], command.cwd) for t in targets] or default_testpaths(command.cwd)
    version = _minor(posixpath.basename(interpreter)[len("python"):]) if interpreter else None
    return Pytest(command, resolved, markexpr, version or python_version_before(command.run, command.step_index))


def pytest_runs(run: JobRun) -> list[Pytest]:
    return [p for p in (pytest_invocation(c) for c in commands(run)) if p is not None]


# ------------------------------------------------------------------ installs

_PIP_VALUE_OPTIONS = {
    "-r", "--requirement", "-c", "--constraint", "-i", "--index-url", "--extra-index-url", "-f",
    "--find-links", "-t", "--target", "--prefix", "--root", "--python", "-C", "--config-settings",
    "--platform", "--python-version", "--implementation", "--abi", "--only-binary", "--no-binary",
    "--progress-bar", "--report", "--upgrade-strategy", "--log", "--cache-dir", "--src", "--trusted-host",
    "--global-option", "--install-option", "--timeout", "--retries", "--proxy", "--cert", "--client-cert",
}


def install_targets(command: Command) -> list[str]:
    """Arguments of a pip install: repository-relative paths, or names/wheel files as given."""
    words, start = command.words, None
    for i, word in enumerate(words[:-1]):
        base = posixpath.basename(word)
        if re.fullmatch(r"pip[0-9.]*", base) and words[i + 1] == "install":
            start = i + 2
            break
        if word == "-m" and i + 2 < len(words) and words[i + 1] == "pip" and words[i + 2] == "install":
            start = i + 3
            break
    if start is None:
        return []
    found, args, j = [], words[start:], 0
    while j < len(args):
        arg = args[j]
        if arg in ("-e", "--editable"):
            if j + 1 < len(args):
                found.append(args[j + 1])
            j += 2
        elif arg.startswith("--editable="):
            found.append(arg.split("=", 1)[1])
            j += 1
        elif arg.startswith("-"):
            name, eq, _ = arg.partition("=")
            j += 2 if (name in _PIP_VALUE_OPTIONS and not eq) else 1
        else:
            found.append(arg)
            j += 1
    resolved = []
    for target in found:
        target = re.sub(r"\[[^\]]*\]$", "", target.split(" @ ", 1)[0].strip())
        is_path = target.startswith((".", "/", "$")) or "/" in target
        resolved.append(norm(target, command.cwd) if is_path and not target.endswith(".whl") else target)
    return resolved


def installs_extension(target: str) -> bool:
    lowered = target.lower()
    return (target == EXTENSION_PROJECT or target.startswith(EXTENSION_PROJECT + "/")
            or re.search(r"(?:^|/)kp[-_]agent[-_]tooling[-_]ops\b", lowered) is not None)


def pytest_runs_with_installs(run: JobRun) -> list[tuple[Pytest, list]]:
    """Each pytest invocation with the install targets of every command before it, in order."""
    found, installed = [], []
    for command in commands(run):
        invocation = pytest_invocation(command)
        if invocation is not None:
            found.append((invocation, list(installed)))
        installed += install_targets(command)
    return found


# ------------------------------------------------------------------ npm


@dataclass
class Npm:
    command: Command
    directory: str
    verb: str            # ci | run | npx | other
    script: str | None   # npm script name, or the npx binary
    extra: list          # remaining arguments


def _kanban_scripts(directory: str) -> dict:
    path = REPO_ROOT / directory / "package.json"
    try:
        return json.loads(path.read_text()).get("scripts") or {}
    except (OSError, ValueError):
        return {}


def npm_invocation(command: Command) -> Npm | None:
    words = command.words
    if not words:
        return None
    base = posixpath.basename(words[0])
    if base == "npx":
        rest = [w for w in words[1:] if w not in ("--yes", "-y", "--no-install", "--no")]
        if not rest:
            return None
        return Npm(command, command.cwd, "npx", rest[0], rest[1:])
    if base != "npm":
        return None
    prefix, rest, j = None, [], 1
    while j < len(words):
        word = words[j]
        if word in ("--prefix", "-C") and j + 1 < len(words):
            prefix, j = words[j + 1], j + 2
            continue
        if word.startswith("--prefix="):
            prefix = word.split("=", 1)[1]
        else:
            rest.append(word)
        j += 1
    directory = norm(prefix, command.cwd) if prefix else command.cwd
    positional = [k for k, w in enumerate(rest) if not w.startswith("-")]
    if not positional:
        return None
    verb = rest[positional[0]]
    after = rest[positional[0] + 1:]
    if verb in ("ci", "clean-install", "ic", "install-clean", "isntall-clean"):
        return Npm(command, directory, "ci", None, after)
    if verb in ("test", "t", "tst"):
        return Npm(command, directory, "run", "test", after)
    if verb in ("run", "run-script", "rum", "urn"):
        names = [w for w in after if not w.startswith("-")]
        if not names:
            return None
        index = after.index(names[0])
        return Npm(command, directory, "run", names[0], after[index + 1:])
    return Npm(command, directory, verb, None, after)


def npm_runs(run: JobRun) -> list[Npm]:
    return [n for n in (npm_invocation(c) for c in commands(run)) if n is not None]


_VITEST_VALUE_OPTIONS = {"--reporter", "--pool", "--retry", "--maxWorkers", "--minWorkers", "--config", "-c",
                         "--root", "-r", "--dir", "--outputFile", "--testTimeout", "--hookTimeout",
                         "--environment", "--mode", "--bail", "--maxConcurrency", "--silent"}
_VITEST_FILTERS = ("--exclude", "--shard", "-t", "--testNamePattern", "--project", "--changed", "--related")


def _filters_nothing(arguments: list, value_options: set) -> bool:
    j = 0
    while j < len(arguments):
        arg = arguments[j]
        name, eq, _ = arg.partition("=")
        if arg == "--":
            j += 1
        elif name in _VITEST_FILTERS:
            return False
        elif arg.startswith("-"):
            j += 2 if (name in value_options and not eq) else 1
        else:
            return False
    return True


def runs_full_vitest(npm: Npm) -> bool:
    """`npm test` (the package's declared test script) unfiltered, `npm run check`, or an
    unfiltered `npx vitest run`."""
    if npm.verb == "run" and npm.script == "test":
        return _filters_nothing(npm.extra, _VITEST_VALUE_OPTIONS)
    if npm.verb == "run" and npm.script == "check":
        return True
    if npm.verb == "npx" and npm.script == "vitest":
        args = list(npm.extra)
        if "run" in args[:1]:
            args = args[1:]
        elif "--run" in args:
            args.remove("--run")
        else:
            return False  # watch mode is not a CI run
        return _filters_nothing(args, _VITEST_VALUE_OPTIONS)
    return False


def runs_typecheck(npm: Npm) -> bool:
    if npm.verb == "run" and npm.script in ("typecheck", "check"):
        return True
    if npm.verb == "npx" and npm.script == "tsc":
        return "--noEmit" in npm.extra
    if npm.verb == "run" and npm.script:
        body = _kanban_scripts(npm.directory).get(npm.script, "")
        return bool(re.search(r"(?:^|&&\s*)tsc\b[^&]*--noEmit", body)) or "npm run typecheck" in body
    return False


# ------------------------------------------------------------------ images


@dataclass
class Build:
    step_index: int
    target: str | None
    file: str | None


def _docker_build(command: Command) -> Build | None:
    words = command.words
    if not words or posixpath.basename(words[0]) != "docker":
        return None
    rest = words[1:]
    head = [w for w in rest[:3] if not w.startswith("-")]
    if "build" not in head:
        return None
    args = rest[rest.index("build") + 1:]
    target = dockerfile = None
    positional, j = [], 0
    value_options = {"-t", "--tag", "--build-arg", "--label", "--platform", "--cache-from", "--cache-to",
                     "-o", "--output", "--secret", "--ssh", "--network", "--progress", "--builder",
                     "--iidfile", "--metadata-file", "--build-context", "--add-host", "--shm-size",
                     "--ulimit", "--attest", "--annotation", "--allow", "--call"}
    while j < len(args):
        arg = args[j]
        name, eq, value = arg.partition("=")
        if name == "--target":
            target, j = (value, j + 1) if eq else ((args[j + 1] if j + 1 < len(args) else None), j + 2)
        elif name in ("-f", "--file"):
            dockerfile, j = (value, j + 1) if eq else ((args[j + 1] if j + 1 < len(args) else None), j + 2)
        elif arg.startswith("-"):
            j += 2 if (name in value_options and not eq) else 1
        else:
            positional.append(arg)
            j += 1
    context = positional[-1] if positional else "."
    resolved = norm(dockerfile, command.cwd) if dockerfile else norm(posixpath.join(context, "Dockerfile"), command.cwd)
    return Build(command.step_index, target, resolved)


def image_builds(run: JobRun) -> list[Build]:
    """Builds of ``deploy/Dockerfile`` in a job run, by step."""
    found = [b for b in (_docker_build(c) for c in commands(run)) if b is not None]
    for index, step in enumerate(run.steps):
        if uses(step).startswith("docker/build-push-action@"):
            config = step.get("with") or {}
            context = _text(config.get("context") or ".")
            dockerfile = norm(config["file"]) if config.get("file") else norm(posixpath.join(context, "Dockerfile"))
            found.append(Build(index, _text(config["target"]) if config.get("target") else None, dockerfile))
    return [b for b in found if b.file == DOCKERFILE]


def visible_env_names(run: JobRun, step_index: int, command: Command | None = None) -> set:
    """Environment variable names a step can see: workflow/job/step env, command
    prefixes, `export` earlier in the step, and $GITHUB_ENV writes in earlier steps."""
    names = set()
    for scope in ((run.workflow.data or {}).get("env"), run.job.get("env"), run.steps[step_index].get("env")):
        if isinstance(scope, dict):
            names |= {str(k) for k in scope}
    if command is not None:
        names |= set(command.assignments)
    script = run.steps[step_index].get("run")
    if isinstance(script, str):
        names |= set(re.findall(r"\bexport\s+([A-Za-z_][A-Za-z0-9_]*)=", script))
    for step in run.steps[:step_index]:
        text = step.get("run")
        if isinstance(text, str) and "GITHUB_ENV" in text:
            names |= set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)=", text))
    return names


# ------------------------------------------------------------------ path filters


def _glob(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "[":
            end = pattern.find("]", i + 1)
            if end < 0:
                out, i = out + re.escape(pattern[i]), i + 1
            else:
                out, i = out + pattern[i:end + 1], end + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


def filter_matches(patterns, value: str) -> bool:
    """GitHub filter semantics: later patterns win; a leading ! negates."""
    if isinstance(patterns, str):
        patterns = [patterns]
    matched = False
    for pattern in patterns or []:
        pattern = _text(pattern)
        if pattern.startswith("!"):
            if _glob(pattern[1:]).match(value):
                matched = False
        elif _glob(pattern).match(value):
            matched = True
    return matched


def pull_request_runs_for(workflow: Workflow, path: str) -> bool:
    config = triggers(workflow)
    if "pull_request" not in config:
        return False
    pr = config["pull_request"]
    if pr.get("paths") is not None:
        return filter_matches(pr["paths"], path)
    if pr.get("paths-ignore") is not None:
        return not filter_matches(pr["paths-ignore"], path)
    return True


def push_to_main_runs_for(workflow: Workflow, path: str) -> bool:
    config = triggers(workflow)
    if "push" not in config:
        return False
    push = config["push"]
    if "branches" in push:
        if not filter_matches(push["branches"], "main"):
            return False
    elif "branches-ignore" in push:
        if filter_matches(push["branches-ignore"], "main"):
            return False
    elif "tags" in push or "tags-ignore" in push:
        return False  # a tags-only push filter never runs for branch pushes
    if push.get("paths") is not None:
        return filter_matches(push["paths"], path)
    if push.get("paths-ignore") is not None:
        return not filter_matches(push["paths-ignore"], path)
    return True


# ------------------------------------------------------------------ YAML walking


def walk(value, path: str = ""):
    """Yield (path, key-or-None, string) for every mapping key and string value."""
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            yield here, str(key), None
            yield from walk(item, here)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, None, value
