"""D0b R4 (labels, the public home) and R10 (DOCKER.md's first run), read from the tree.

Order: docs/work/orders/D0b-release-workflow.md. The public home is `ghcr.io/saigid-1/agent-tooling` and
https://github.com/Saigid-1/agent-tooling. The tree checks are positive: they name only the public owner
and report a finding by file, line and class, never by the value found, so no test and no failure
message ever names another owner.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import d0b_seams as seams

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "deploy" / "Dockerfile"
DOCKER_MD = ROOT / "docs" / "DOCKER.md"
SOURCE_LABEL = "org.opencontainers.image.source"
LICENCES_LABEL = "org.opencontainers.image.licenses"


# ======================================================================= Dockerfile


@dataclass
class Stage:
    name: str
    args: dict = field(default_factory=dict)          # ARG name -> default (None when declared without one)
    labels: list = field(default_factory=list)        # (line, key, value) in order
    arg_lines: dict = field(default_factory=dict)     # ARG name -> first line declaring it


@dataclass
class Dockerfile:
    global_args: dict
    stages: dict


_INSTRUCTION = re.compile(r"^\s*([A-Za-z]+)\s+(.*)$", re.S)
_HEREDOC = re.compile(r"<<-?\s*([\"']?)([A-Za-z_][A-Za-z0-9_]*)\1")


def parse_dockerfile(text: str) -> Dockerfile:
    """Stages, ARGs and LABELs: comments and heredoc bodies skipped, `\\` continuations joined."""
    instructions, buffer, start, heredocs = [], "", 0, []
    for number, line in enumerate(text.splitlines(), 1):
        if heredocs:
            if line.strip() == heredocs[0]:
                heredocs.pop(0)
            continue
        if not buffer and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if not buffer:
            start = number
        if line.rstrip().endswith("\\"):
            buffer += line.rstrip()[:-1] + " "
            continue
        buffer += line
        instructions.append((start, buffer))
        heredocs = [m.group(2) for m in _HEREDOC.finditer(buffer)]
        buffer = ""
    global_args, stages, current = {}, {}, None
    for number, instruction in instructions:
        match = _INSTRUCTION.match(instruction)
        if not match:
            continue
        keyword, rest = match.group(1).upper(), match.group(2).strip()
        if keyword == "FROM":
            name = re.search(r"\s+AS\s+([A-Za-z0-9_.-]+)\s*$", rest, re.I)
            current = Stage(name.group(1).lower() if name else f"stage-{len(stages)}")
            stages[current.name] = current
        elif keyword == "ARG":
            for item in _words(rest):
                key, eq, value = item.partition("=")
                target = global_args if current is None else current.args
                target[key] = value if eq else None
                if current is not None:
                    current.arg_lines.setdefault(key, number)
        elif keyword == "LABEL" and current is not None:
            words = _words(rest)
            if words and "=" not in words[0]:
                current.labels.append((number, words[0], " ".join(words[1:])))
            else:
                for item in words:
                    key, _, value = item.partition("=")
                    current.labels.append((number, key, value))
    return Dockerfile(global_args, stages)


def _words(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


_ARG_REFERENCE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)(?::?[-+][^}]*)?\}|([A-Za-z_][A-Za-z0-9_]*))")


def _argument(value: str) -> str | None:
    """The build argument a label value is, when it is exactly one argument reference."""
    match = _ARG_REFERENCE.fullmatch(value.strip())
    return (match.group(1) or match.group(2)) if match else None


def _resolve(value: str, stage: Stage, dockerfile: Dockerfile) -> str:
    def default(match):
        name = match.group(1) or match.group(2)
        if name in stage.args and stage.args[name] is not None:
            return stage.args[name]
        if name in stage.args and dockerfile.global_args.get(name) is not None:
            return dockerfile.global_args[name]
        return ""
    return _ARG_REFERENCE.sub(default, value)


def _labelled_stages(dockerfile: Dockerfile) -> list[Stage]:
    return [s for s in dockerfile.stages.values() if s.labels]


def test_r4_every_label_source_reads_a_build_argument_never_a_literal_url():
    """GREEN-IF deploy/Dockerfile has at least one LABEL that sets org.opencontainers.image.source, and every LABEL
    that sets it, in every stage, sets it to a build-argument reference (`$NAME` or `${NAME}`), never to a literal
    value. This catches a target added later with a hard-coded repository."""
    dockerfile = parse_dockerfile(DOCKERFILE.read_text())
    sites = [(s.name, line, value) for s in dockerfile.stages.values() for line, key, value in s.labels if key == SOURCE_LABEL]
    assert sites, f"no LABEL in deploy/Dockerfile sets {SOURCE_LABEL}"
    literal = [f"deploy/Dockerfile:{line} (stage {name})" for name, line, value in sites if _argument(value) is None]
    assert not literal, f"{SOURCE_LABEL} is a literal value, not the build argument, at: {literal}"


def test_r4_one_source_argument_defaults_to_the_public_url_in_every_labelled_stage():
    """GREEN-IF every stage of deploy/Dockerfile that has a LABEL sets org.opencontainers.image.source from the
    one build argument SOURCE_URL (the seam's name), declares that ARG in the stage before the LABEL, and the
    argument's default there (the stage's own, else the one declared before the first FROM) is
    https://github.com/Saigid-1/agent-tooling."""
    dockerfile = parse_dockerfile(DOCKERFILE.read_text())
    stages = _labelled_stages(dockerfile)
    assert stages, "deploy/Dockerfile has no labelled stage"
    problems = []
    for stage in stages:
        sources = [(line, value) for line, key, value in stage.labels if key == SOURCE_LABEL]
        if not sources:
            problems.append(f"stage {stage.name}: no {SOURCE_LABEL} label")
            continue
        for line, value in sources:
            name = _argument(value)
            if name != seams.SOURCE_URL_ARG:
                problems.append(f"stage {stage.name} (line {line}): reads {name or 'a literal'}, not ${{{seams.SOURCE_URL_ARG}}}")
                continue
            declared = stage.arg_lines.get(name)
            if declared is None or declared > line:
                problems.append(f"stage {stage.name} (line {line}): ARG {name} is not declared in the stage before the LABEL")
                continue
            if _resolve(value, stage, dockerfile) != seams.PUBLIC_SOURCE_URL:
                problems.append(f"stage {stage.name} (line {line}): ${{{name}}} defaults to something other than "
                                "the public URL")
    assert not problems, "\n".join(problems)


def test_r4_published_targets_carry_the_licences_label():
    """GREEN-IF the final stages runtime, product and ops each set org.opencontainers.image.licenses to
    `AGPL-3.0-only`, and opencode to `AGPL-3.0-only AND MIT` (its bundled CLI's upstream licence)."""
    dockerfile = parse_dockerfile(DOCKERFILE.read_text())
    problems = []
    for target, expected in seams.LICENCES.items():
        stage = dockerfile.stages.get(target)
        if stage is None:
            problems.append(f"no stage {target}")
            continue
        values = [_resolve(v, stage, dockerfile) for _, k, v in stage.labels if k == LICENCES_LABEL]
        if values[-1:] != [expected]:
            problems.append(f"{target}: {LICENCES_LABEL} is {values or 'absent'}, expected {expected!r}")
    assert not problems, "\n".join(problems)


# ======================================================================= the public home in the tree

_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".pytest_cache", ".venv"})
_HOSTED = re.compile(r"(?i)\b(ghcr\.io|github\.com)[/:]([A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9_])?)/agent-tooling(?![A-Za-z0-9_])")
_SLUG = re.compile(r"(?<![\w./@:%+~-])([A-Za-z0-9][A-Za-z0-9_.-]*)/agent-tooling(?![\w-]|\.\w)")


def tracked_files(root: Path) -> list[str]:
    """`git ls-files` at root; every file under root when root is not a Git checkout."""
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        if top.returncode == 0 and Path(top.stdout.strip()).resolve() == root.resolve():
            listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True)
            return [name.decode() for name in listed.stdout.split(b"\0") if name]
    except FileNotFoundError:
        pass
    found = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        found.extend(Path(directory, name).relative_to(root).as_posix() for name in files)
    return sorted(found)


def _lines(root: Path):
    for name in tracked_files(root):
        path = root / name
        try:
            data = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            continue
        if b"\0" in data:
            continue
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            yield name, number, line


def _not_public(pattern: re.Pattern, group: int) -> tuple[list[str], int]:
    findings, public = [], 0
    for name, number, line in _lines(ROOT):
        for match in pattern.finditer(line):
            owner = match.group(group)
            if owner.lower() == seams.PUBLIC_OWNER:
                public += 1
            else:
                findings.append(f"{name}:{number} (an owner of {len(owner)} characters that is not the public owner)")
    return findings, public


def test_r4_every_ghcr_and_github_reference_to_agent_tooling_names_the_public_owner():
    """GREEN-IF in every tracked file (`git ls-files`; every file outside a Git checkout) each
    `ghcr.io/<owner>/agent-tooling` and `github.com/<owner>/agent-tooling` (or `github.com:<owner>/...`) has the
    owner saigid-1, compared case-insensitively, and at least one such reference exists. The test names only the
    public owner; a finding is reported by file and line, never by its value."""
    findings, public = _not_public(_HOSTED, 2)
    assert public, "positive control: the tree names the public home nowhere"
    assert not findings, "references to agent-tooling under another owner:\n" + "\n".join(findings)


def test_r4_every_owner_slug_of_agent_tooling_names_the_public_owner():
    """GREEN-IF every bare `<owner>/agent-tooling` repository slug in a tracked file names saigid-1,
    case-insensitively. A slug starts a word: a path such as `x/y/agent-tooling` (preceded by `/`), a URL (covered
    by the test above) and a file name such as `agent-tooling.local.json` are not slugs. Reported by file and line,
    never by value."""
    findings, _public = _not_public(_SLUG, 1)
    assert not findings, "agent-tooling repository slugs under another owner:\n" + "\n".join(findings)


# ======================================================================= R10: DOCKER.md


_PUBLIC_DIGEST = re.compile(re.escape(seams.REGISTRY_REPOSITORY) + r"@sha256:(?:<[^>\s]+>|[0-9a-f]{64})")


def _first_run_section(text: str) -> list[str]:
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if re.match(r"^##\s+First run from an empty root\s*$", l)), None)
    assert start is not None, "docs/DOCKER.md has no '## First run from an empty root' section"
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^##\s", lines[i])), len(lines))
    return lines[start:end]


def _code(section: list[str]) -> list[tuple[int, str]]:
    out, inside = [], False
    for index, line in enumerate(section):
        if line.strip().startswith("```"):
            inside = not inside
            continue
        if inside:
            out.append((index, line.split(" #", 1)[0].rstrip()))
    return out


def test_r10_docker_md_first_run_pulls_the_published_digest_before_plan():
    """GREEN-IF the "First run from an empty root" section of docs/DOCKER.md runs `docker pull` of
    `ghcr.io/saigid-1/agent-tooling@sha256:<digest>` (literally, or through a shell variable assigned that value)
    before its first `kp-agent-install plan`; the `--image` the plan takes resolves to that same reference (one of
    the variable's values before it); and the section presents the opencode image as optional before the plan."""
    section = _first_run_section(DOCKER_MD.read_text())
    code = _code(section)
    plan = next((i for i, line in code if "kp-agent-install plan" in line), None)
    assert plan is not None, "the first run has no `kp-agent-install plan`"
    values: dict[str, list[str]] = {}
    pulled, image_refs = [], []
    for index, line in code:
        if index > plan:
            break
        for name, value in re.findall(r"(?:^|\s|;)([A-Za-z_][A-Za-z0-9_]*)=('[^']*'|\"[^\"]*\"|\S+)", line):
            values.setdefault(name, []).append(value.strip("'\""))
        def resolve(word: str) -> list[str]:
            word = word.strip("'\"")
            variable = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", word)
            return values.get(variable.group(1), []) if variable else [word]
        pull = re.search(r"\bdocker\s+(?:image\s+)?pull\s+(\S+)", line)
        if pull:
            pulled.extend(resolve(pull.group(1)))
        for word in re.findall(r"--image[= ](\S+)", line):
            image_refs.extend(resolve(word))
    public = [ref for ref in pulled if _PUBLIC_DIGEST.fullmatch(ref)]
    assert public, (f"the first run pulls no {seams.REGISTRY_REPOSITORY}@sha256:<digest> before `plan` "
                    f"(pulls seen: {len(pulled)})")
    assert any(ref in public for ref in image_refs), \
        f"`--image` before `plan` does not take the pulled reference (it can be: {image_refs})"
    before = " ".join(section[: plan + 1]).lower()
    assert re.search(r"opencode[^.]*optional|optional[^.]*opencode", before), \
        "the first run does not present the opencode image as an optional pull before `plan`"
