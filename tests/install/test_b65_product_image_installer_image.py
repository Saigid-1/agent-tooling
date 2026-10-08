"""B65: DOCKER.md's product-image installer path, run as written (P4), and its statements (P2, P3).

Order: docs/work/orders/B64-66-docker-installer-and-step-9.md, B65.
The region is docs/DOCKER.md from the paragraph that starts
`**From the product image, with no Python on the host.**` up to, not including, `### Plan and apply`.

What the image-marked test does (P4):
- it reads the region's one fenced block and runs it in one bash process, as written, with the
  variables the region tells the reader to set (`root`, `image`, `product`, `home`, `project`,
  `port`) assigned in front of it: a never-reused directory under TMPDIR, the image named by
  AGENT_TOOLING_TEST_IMAGE (a digest as given, any other reference resolved to its local image ID),
  a committed fixture repository, a `b65-<random>` Compose project and a free loopback port;
- right after the documented `plan`, it runs the host's `kp-agent-install plan` with the same
  arguments (the documented plan's text after `plan`), the host-side plan of P3;
- it asserts: every documented step exits 0; `plan` and `apply` ran in the image (`docker run`,
  `--entrypoint kp-agent-install`, `--network none`, and a manifest read from another installation
  than the host's); the container's `plan_sha256` equals the host's; `apply` reports `applied` for
  that plan; `prepare` reports `prepared`; the host's `verify` reports `verified` with no drift;
- it removes every container and volume it created (`b65-<random>` names and labels; the store volume
  `prepare` creates is `<project>_memory`, so it carries the prefix too), and fails if one is left.
It FAILS (never skips) when AGENT_TOOLING_TEST_IMAGE is unset, like the other image-marked tests.

The static tests run in the default suite, so a pull request that touches only docs/** still runs
them (the image test then first runs in images.yml or the release rehearsals):
- the region carries no stale "not yet verifiable" claim and states what was measured (P2; the
  image test checks this too, so `-m image` alone catches its falsifier);
- the documented container mounts every path the installer is given at its identical path (P1, P3);
- every variable the block uses without assigning it is named in the region's prose.

Readings (repeated in the arm report under AMBIGUITY):
- "a logical command" is one line of the block, or several joined by a trailing backslash, or a
  multi-line array assignment `name=(...)`; comment and blank lines are not commands;
- the documented `plan` is the one command with a ` plan ` word that is not ` apply `; its output
  file is its trailing `> file` redirect;
- "the same arguments" for the host-side plan are the documented plan's own text after ` plan `,
  expanded by the same shell, so `$(id -u)` and the arrays evaluate identically.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from s3_harness import docker_env, explain, hermetic_env, make_repo
from t7a_harness import free_port, resolve_image

DOC = Path(__file__).resolve().parents[2] / "docs" / "DOCKER.md"
REGION_START = "**From the product image, with no Python on the host.**"
REGION_END = "### Plan and apply"
IMAGE_VARIABLE = "AGENT_TOOLING_TEST_IMAGE"
PREFIX = "b65"
# The variables the reader sets before the block (the region's prose names them).
VARIABLES = ("root", "image", "product", "home", "project", "port")
HOST_PLAN = "b65-host-plan.json"
BLOCK_TIMEOUT = 900

ARRAY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=\((.*)\)\s*$", re.S)
ASSIGNED = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
REFERENCED = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
STALE = (re.compile(r"\bnot\s+(?:yet\s+)?verifiable\b", re.I),
         re.compile(r"\bT6a\b"),
         re.compile(r"\bplanned in AT-0004\b", re.I))
MARK = re.compile(r"\n?@@B65 (\d+) (\d+)@@\n")
PATH_OPTIONS = ("--runtime-root", "--transcript-root", "--repository", "--home")


# ------------------------------------------------------------------ the document


def region(text: str | None = None) -> str:
    """The B65 region of docs/DOCKER.md."""
    lines = (DOC.read_text() if text is None else text).splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(REGION_START)), None)
    assert start is not None, f"docs/DOCKER.md has no paragraph starting {REGION_START!r}"
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() == REGION_END), None)
    assert end is not None, f"docs/DOCKER.md has no {REGION_END!r} after the product-image paragraph"
    return "\n".join(lines[start:end])


def split_region(text: str) -> tuple[list[list[str]], list[str]]:
    """(fenced blocks as lines, prose paragraphs) of the region."""
    blocks, prose, inside, code, paragraph = [], [], False, [], []
    for line in text.splitlines():
        if line.strip().startswith("```"):
            if inside:
                blocks.append(code)
                code = []
            elif paragraph:
                prose.append(" ".join(paragraph))
                paragraph = []
            inside = not inside
            continue
        if inside:
            code.append(line)
        elif line.strip():
            paragraph.append(line.strip())
        elif paragraph:
            prose.append(" ".join(paragraph))
            paragraph = []
    assert not inside, "the region has an unterminated code fence"
    if paragraph:
        prose.append(" ".join(paragraph))
    return blocks, prose


def documented_block(region_text: str | None = None) -> list[str]:
    """The region's one fenced block (`region_text` is the region itself; default: read it)."""
    blocks, _ = split_region(region() if region_text is None else region_text)
    assert len(blocks) == 1, f"the product-image paragraph must hold exactly one code block, found {len(blocks)}"
    return blocks[0]


def logical_commands(block: list[str]) -> list[str]:
    commands, index = [], 0
    while index < len(block):
        line = block[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        text = line.rstrip()
        while text.endswith("\\") and index < len(block):
            text = text[:-1].rstrip() + " " + block[index].strip()
            index += 1
        if ASSIGNED.match(text) and "=(" in text:
            while text.count("(") > text.count(")") and index < len(block):
                text += " " + block[index].strip()
                index += 1
        commands.append(text)
    return commands


def arrays(commands: list[str]) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in (ARRAY.match(c) for c in commands) if m}


def expand(command: str, bodies: dict[str, str]) -> str:
    """The command with each `"${name[@]}"` replaced by the array's text (textual, no evaluation)."""
    for name, body in bodies.items():
        command = re.sub(r'"?\$\{' + re.escape(name) + r'\[@\]\}"?', lambda _m, b=body: b, command)
    return command


def kind(command: str) -> str:
    if re.search(r"\sapply(\s|$)", command):
        return "apply"
    if re.search(r"\splan(\s|$)", command):
        return "plan"
    for verb in ("prepare", "verify"):
        if re.search(r"(^|\s)kp-agent-install\s+" + verb + r"(\s|$)", command):
            return verb
    return "other"


def steps(commands: list[str]) -> dict[str, int]:
    """{kind: index} of the four documented steps; each must be documented exactly once."""
    found: dict[str, list[int]] = {}
    for index, command in enumerate(commands):
        found.setdefault(kind(command), []).append(index)
    wrong = {k: len(found.get(k, [])) for k in ("plan", "apply", "prepare", "verify") if len(found.get(k, [])) != 1}
    assert not wrong, f"the block must document plan, apply, prepare and verify once each; counts: {wrong}"
    return {k: found[k][0] for k in ("plan", "apply", "prepare", "verify")}


def free_variables(commands: list[str]) -> set[str]:
    assigned = {m.group(1) for m in (ASSIGNED.match(c) for c in commands) if m}
    return {name for c in commands for name in REFERENCED.findall(c)} - assigned


def stale_statements(text: str) -> list[str]:
    _, prose = split_region(text)
    return [p for p in prose if any(rx.search(p) for rx in STALE)]


def measurement_statements(text: str) -> list[str]:
    _, prose = split_region(text)
    return [p for p in prose if re.search(r"\b(?:Verified|Measured)\b", p) and "plan_sha256" in p]


def _option_values(tokens: list[str], option: str) -> list[str]:
    values = []
    for index, token in enumerate(tokens):
        if token == option and index + 1 < len(tokens):
            values.append(tokens[index + 1])
        elif token.startswith(option + "="):
            values.append(token[len(option) + 1:])
    return values


def binds(tokens: list[str]) -> list[tuple[str, str]]:
    """(source, target) of every bind mount of a `docker run` argv (`--mount type=bind,...` or `-v`)."""
    found = []
    for spec in _option_values(tokens, "--mount"):
        fields = dict(part.split("=", 1) if "=" in part else (part, "") for part in spec.split(","))
        if fields.get("type") == "bind":
            found.append((fields.get("source") or fields.get("src") or "",
                          fields.get("target") or fields.get("destination") or fields.get("dst") or ""))
    for option in ("-v", "--volume"):
        for spec in _option_values(tokens, option):
            parts = spec.split(":")
            if len(parts) >= 2 and parts[0].startswith(("/", "$", "~", ".")):
                found.append((parts[0], parts[1]))
    return found


def _within(path: str, outer: str) -> bool:
    outer = outer.rstrip("/")
    return path == outer or path.startswith(outer + "/")


def in_image_problems(command: str) -> list[str]:
    """Why the expanded `plan`/`apply` command does not run the installer in the image, offline."""
    tokens = shlex.split(command)
    problems = []
    if not re.search(r"\bdocker\s+run\b", command) or "kp-agent-install" not in _option_values(tokens, "--entrypoint"):
        problems.append("it is not `docker run ... --entrypoint kp-agent-install` (the installer in the image)")
    if "none" not in _option_values(tokens, "--network"):
        problems.append("its container is not run with `--network none`")
    return problems


def mount_problems(command: str) -> list[str]:
    """Why the expanded `plan`/`apply` command does not run the installer in the image, offline, with every
    installer path at its identical path (empty when it does)."""
    tokens = shlex.split(command)
    problems = in_image_problems(command)
    mounted = binds(tokens)
    problems += [f"bind {source} is mounted at another path, {target}" for source, target in mounted if source != target]
    targets = [target for source, target in mounted if source == target]
    paths = [(o, v.split("=", 1)[1] if o == "--repository" else v) for o in PATH_OPTIONS for v in _option_values(tokens, o)]
    for option, path in paths:
        if option == "--home":
            seen = any(_within(target, path) or _within(path, target) for target in targets)
        else:
            seen = any(_within(path, target) for target in targets)
        if not seen:
            problems.append(f"{option} {path} is not bind-mounted at its own path")
    for option in ("--runtime-root", "--uid", "--gid", "--home"):
        if not _option_values(tokens, option):
            problems.append(f"the installer is not given {option}")
    return problems


# --------------------------------------------------------- static (default suite)


def test_region_states_the_measured_path_and_no_stale_claim():
    """GREEN-IF the region carries no "not yet verifiable" / T6a claim, and one paragraph states a
    measurement (Verified/Measured) of `plan_sha256` (P2, P3)."""
    text = region()
    stale = stale_statements(text)
    assert not stale, "the product-image paragraph restates the stale claim:\n" + "\n".join(stale)
    assert measurement_statements(text), "the product-image paragraph states no measured plan_sha256 identity"


def test_documented_container_mounts_every_installer_path_at_its_own_path():
    """GREEN-IF the documented `plan` and `apply` run the installer in the image with `--network none`,
    every bind mount's source equals its target, and each `--runtime-root`, `--repository` and
    `--transcript-root` path is at or under such a mount (`--home` contains or is within one)."""
    commands = logical_commands(documented_block())
    index = steps(commands)
    bodies = arrays(commands)
    problems = {k: mount_problems(expand(commands[index[k]], bodies)) for k in ("plan", "apply")}
    problems = {k: v for k, v in problems.items() if v}
    assert not problems, f"the documented container commands do not mount the installer's paths: {problems}"


def test_block_variables_are_named_in_the_prose():
    """GREEN-IF every variable the block uses without assigning it is one of VARIABLES and is named, in
    backticks, in the region's prose (the reader is told to set it)."""
    text = region()
    commands = logical_commands(documented_block(text))
    free = free_variables(commands)
    _, prose = split_region(text)
    named = set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", " ".join(prose)))
    assert free <= set(VARIABLES), f"the block uses variables this test does not supply: {sorted(free - set(VARIABLES))}"
    assert free <= named, f"the block uses variables its prose does not name: {sorted(free - named)}"


# ------------------------------------------------------------------- image run


def _required_image() -> str:
    ref = os.environ.get(IMAGE_VARIABLE, "").strip()
    if not ref:
        pytest.fail(f"{IMAGE_VARIABLE} is unset. This B65 test is image-marked and needs a built product image; "
                    "this is a failure, not a skip.", pytrace=False)
    return ref


def _required(tool: str) -> str:
    found = shutil.which(tool)
    if not found:
        pytest.fail(f"`{tool}` is not on PATH; the documented block needs it", pytrace=False)
    return found


def _host_plan(command: str) -> tuple[str, str | None]:
    """The host-side plan with the documented plan's arguments, and the documented plan's output file."""
    match = re.match(r"^(?P<runner>.+?)\s+plan\s+(?P<args>.+?)(?:\s+>\s*(?P<out>\S+))?\s*$", command)
    assert match, f"cannot read the documented plan's arguments: {command}"
    return f"kp-agent-install plan {match.group('args')} > {HOST_PLAN}", match.group("out")


def _script(variables: dict[str, str], commands: list[str]) -> str:
    lines = ["set -u"] + [f"{name}={shlex.quote(value)}" for name, value in variables.items()]
    for index, command in enumerate(commands):
        lines.append(command)
        lines.append(f"__b65=$?; printf '\\n@@B65 {index} %s@@\\n' \"$__b65\"; "
                     f"printf '\\n@@B65 {index} %s@@\\n' \"$__b65\" >&2; [ \"$__b65\" -eq 0 ] || exit \"$__b65\"")
    return "\n".join(lines) + "\n"


def _segments(output: str) -> dict[int, tuple[str, int]]:
    parts = MARK.split(output)
    return {int(parts[i + 1]): (parts[i], int(parts[i + 2])) for i in range(0, len(parts) - 2, 3)}


def _json(text: str, what: str):
    try:
        return json.loads(text)
    except ValueError:
        pytest.fail(f"{what} printed no JSON:\n{text[-3000:]}", pytrace=False)


def _cleanup(docker: str, env: dict, project: str, root: Path) -> list[str]:
    """Remove every container and volume of this run; return what is still there."""
    def run(*args):
        return subprocess.run([docker, *args], env=env, capture_output=True, text=True, timeout=120)

    def containers():
        found = set()
        for selector in (f"label=agent-tooling.install.project={project}", f"name={project}", f"volume={root}"):
            found |= set(run("ps", "-aq", "--filter", selector).stdout.split())
        return found

    def volumes():
        found = {name for name in run("volume", "ls", "-q", "--filter", f"name={project}").stdout.split()
                 if name.startswith(project)}
        return found | set(run("volume", "ls", "-q", "--filter",
                               f"label=com.docker.compose.project={project}").stdout.split())

    for cid in sorted(containers()):
        run("rm", "-f", cid)
    for name in sorted(volumes() | {f"{project}_memory"}):
        run("volume", "rm", "-f", name)
    return [f"container {c}" for c in sorted(containers())] + [f"volume {v}" for v in sorted(volumes())]


@pytest.mark.image
def test_documented_product_image_path_plans_applies_prepares_and_verifies(installer):
    """GREEN-IF the region's block, run as written, plans and applies in the image (exit 0), the container's
    plan_sha256 equals the host's for the same arguments, `prepare` reports prepared, the host's `verify`
    reports verified, nothing of the run is left in Docker, and the region states no stale claim."""
    text = region()
    stale = stale_statements(text)
    assert not stale, "the product-image paragraph restates the stale claim:\n" + "\n".join(stale)
    commands = logical_commands(documented_block(text))
    index = steps(commands)
    bodies = arrays(commands)
    # Statically only that plan and apply are container runs; what they mount is measured by running them.
    offline = {verb: in_image_problems(expand(commands[index[verb]], bodies)) for verb in ("plan", "apply")}
    assert not any(offline.values()), f"the documented plan/apply do not run the installer in the image: {offline}"
    free = free_variables(commands)
    assert free <= set(VARIABLES), f"the block uses variables this test does not supply: {sorted(free - set(VARIABLES))}"

    docker, bash = _required("docker"), _required("bash")
    _required("jq")
    ref = _required_image()
    base = Path(tempfile.mkdtemp(prefix=f"{PREFIX}-test-")).resolve()
    project = f"{PREFIX}-{uuid.uuid4().hex[:8]}"
    cwd, itmp, repos = base / "cwd", base / "itmp", base / "repos"
    for directory in (cwd, itmp, repos):
        directory.mkdir()
    product = repos / "product"
    make_repo(product)
    home, root = base / "home", base / "root"
    env = docker_env(home)
    env.update(hermetic_env(home, itmp))
    env["PATH"] = os.pathsep.join([str(installer.parent), os.environ.get("PATH", "/usr/bin:/bin")])
    image = resolve_image(docker, ref, env)
    variables = {"root": str(root), "image": image, "product": str(product), "home": str(home),
                 "project": project, "port": str(free_port())}

    # The host-side plan runs right after the documented plan, on the still-empty root.
    host_plan, plan_file = _host_plan(commands[index["plan"]])
    run = list(commands)
    run.insert(index["plan"] + 1, host_plan)
    at = {verb: (position + 1 if position > index["plan"] else position) for verb, position in index.items()}
    at["host-side plan"] = index["plan"] + 1
    names = {position: verb for verb, position in at.items()}

    started = time.monotonic()
    try:
        proc = subprocess.run([bash, "-c", _script(variables, run)], cwd=cwd, env=env, capture_output=True,
                              text=True, timeout=BLOCK_TIMEOUT, stdin=subprocess.DEVNULL)
    finally:
        leftovers = _cleanup(docker, env, project, root)
    elapsed = time.monotonic() - started
    out, err = _segments(proc.stdout), _segments(proc.stderr)
    log = "\n".join(f"[{names.get(i, 'step')} {i}] exit={out[i][1]} : {run[i]}" for i in sorted(out))
    print(f"B65 block run in {elapsed:.1f}s (Docker cleanup included), project {project}:\n{log}")
    failed = next((i for i in range(len(run)) if i not in out or out[i][1] != 0), None)
    assert failed is None, (f"step {failed} ({names.get(failed, 'step')}) failed or did not run: {run[failed]}\n"
                            f"its stderr: {err.get(failed, ('(none)', 0))[0][-3000:]}\n" + explain(proc)
                            + "\n\nsession:\n" + log)
    assert not leftovers, f"the run left Docker resources behind: {leftovers}"

    planned = _json((cwd / plan_file).read_text() if plan_file else out[at["plan"]][0], "the container's plan")
    hosted = _json((cwd / HOST_PLAN).read_text(), "the host-side plan")
    container_source = (planned.get("preview") or {}).get("manifest_source")
    host_source = (hosted.get("preview") or {}).get("manifest_source")
    assert container_source and host_source and container_source != host_source, (
        f"the documented plan did not read another installation's manifest than the host's: "
        f"{container_source!r} vs {host_source!r}")
    assert planned.get("plan_sha256") == hosted.get("plan_sha256"), (
        "P3: the container's plan_sha256 differs from the host-side plan's for the same arguments; "
        "differing keys: " + json.dumps({k: [planned.get(k), hosted.get(k)] for k in sorted(set(planned) | set(hosted))
                                         if k != "preview" and planned.get(k) != hosted.get(k)})[:3000])
    applied = _json(out[at["apply"]][0], "the container's apply")
    assert applied.get("status") == "applied" and applied.get("plan_sha256") == planned["plan_sha256"], (
        f"apply did not report `applied` for the reviewed plan: {json.dumps(applied)[:2000]}")
    prepared = _json(out[at["prepare"]][0], "the host's prepare")
    assert prepared.get("status") == "prepared", f"prepare did not report prepared: {json.dumps(prepared)[:2000]}"
    verified = _json(out[at["verify"]][0], "the host's verify")
    assert verified.get("status") == "verified" and not verified.get("drift"), (
        f"the host's verify did not report verified: {json.dumps(verified)[:2000]}")
    shutil.rmtree(base, ignore_errors=True)  # kept for inspection when an assertion failed
