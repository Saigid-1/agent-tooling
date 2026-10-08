"""B64: DOCKER.md's "Getting the installer" offers only what the release publishes.

Order: docs/work/orders/B64-66-docker-installer-and-step-9.md, B64 (P1 and P3).

The section is docs/DOCKER.md from the `### Getting the installer` heading up to, but not including,
the paragraph that starts `**From the product image, with no Python on the host.**` (B65's region; it
is read only to find where the section ends).

Whether a wheel is published is read from the release workflow (.github/workflows/release.yml), never
assumed. A step of any job uploads a wheel when it:
- uses `pypa/gh-action-pypi-publish`, or runs `twine upload` or `<uv|poetry|flit|hatch|pdm|maturin>
  publish`;
- runs `gh release create` or `gh release upload` and names a `.whl` file or a `dist/` path; or
- passes a `.whl` file or a `dist/` path to an action other than the workflow-artifact, cache, checkout
  and setup actions (a release action's `files:`, for example).
Comment lines of a step's script do not count. The reader's self-test feeds it one synthetic workflow
per rule, so it can say yes.

What counts as an offer of a published package or wheel (the order's definition):
- an install line that names `kp-agent-tooling` or `kp_agent_tooling` outside a local path. An install
  line is a fenced code line (continuations joined, its comment included) or an inline code span that
  runs a Python package installer or fetcher: `pip`/`pip3` `install|download|wheel` (also as
  `python -m pip`), `pipx install|run`, `uv pip install`, `uv tool install|run`, `uv add`, `uvx`,
  `poetry add`, `pdm add`. A name is inside a local path when its shell word starts with `./`, `../`,
  `/`, `~/`, a shell variable or `packages/`;
- in one prose sentence, or one code line with its comment: "PyPI" or "release page" beside "wheel" or
  "package"; or "released" with "package". No negation is exempt: "not on PyPI" beside "package" is an
  offer under this reading, so the section does not use those words.
A local-path install (`pip install -e packages/tooling`, or `pip install ./dist/kp_agent_tooling-...whl`
after the documented build) is not an offer.

What counts as a named source path:
- every relative path token of the section (at least one `/`; not part of a longer path, a URL, a
  variable or a host name; not continued by a `<placeholder>`), checked in the tree (a `*` is a glob);
  a token whose first part is `kp_agent_tooling` is package data, checked in the installed package;
- the source argument of every build or editable install in the section (`pip wheel <dir>`,
  `pip install -e <dir>`, `python -m build <dir>`), even without a `/`.
An output directory the section's own build writes (`--wheel-dir`, `-w`, `--outdir`, `-o`) is not a
source path.
"""
from __future__ import annotations

import glob
import importlib.util
import re
import shlex
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import d0a2_markdown as md  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "docs" / "DOCKER.md"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
PROJECT = "kp-agent-tooling"
START = re.compile(r"^###\s+Getting the installer\s*$")
END = "**From the product image, with no Python on the host.**"

# ------------------------------------------------------------------ the release workflow


_NAMES_WHEEL = re.compile(r"\.whl\b|(?<![\w.$/-])dist/")
_PUBLISH_RUN = re.compile(r"\btwine\s+upload\b|\b(?:uv|poetry|flit|hatch|pdm|maturin)\s+publish\b")
_GH_RELEASE = re.compile(r"\bgh\s+release\s+(?:create|upload)\b")
_NOT_PUBLISHING = {"actions/upload-artifact", "actions/download-artifact", "actions/cache",
                   "actions/checkout", "actions/setup-python"}


def _script(run: str) -> str:
    """A step's script without its comment lines and trailing comments."""
    kept = []
    for line in run.splitlines():
        if line.lstrip().startswith("#"):
            continue
        kept.append(re.sub(r"\s#\s.*$", "", line))
    return "\n".join(kept)


def wheel_uploads(text: str) -> list[str]:
    """Each step of the workflow `text` that uploads a wheel, by job and step (empty: none)."""
    data = yaml.safe_load(text) or {}
    found = []
    for job_id, job in (data.get("jobs") or {}).items():
        for index, step in enumerate((job or {}).get("steps") or []):
            uses = str(step.get("uses") or "")
            run = _script(str(step.get("run") or ""))
            given = " ".join(str(value) for value in (step.get("with") or {}).values())
            where = f"jobs.{job_id}.steps[{index}] ({step.get('name') or uses or 'run'})"
            action = uses.split("@", 1)[0]
            if action == "pypa/gh-action-pypi-publish":
                found.append(f"{where}: uses {uses}")
            if _PUBLISH_RUN.search(run):
                found.append(f"{where}: runs {_PUBLISH_RUN.search(run).group(0)}")
            if _GH_RELEASE.search(run) and _NAMES_WHEEL.search(run):
                found.append(f"{where}: attaches a wheel to the release")
            if action and action not in _NOT_PUBLISHING and _NAMES_WHEEL.search(given):
                found.append(f"{where}: passes a wheel to {action}")
    return found


def release_wheel_uploads() -> list[str]:
    assert RELEASE.is_file(), f"{RELEASE.relative_to(ROOT)} is missing: whether a wheel is published is read there"
    return wheel_uploads(RELEASE.read_text())


# ------------------------------------------------------------------ the section


def section() -> list[str]:
    lines = DOCKER.read_text().splitlines()
    fenced = md.fenced_lines(lines)
    start = next((i for i, line in enumerate(lines) if i not in fenced and START.match(line)), None)
    assert start is not None, "docs/DOCKER.md has no `### Getting the installer` heading"
    end = next((i for i, line in enumerate(lines)
                if i > start and i not in fenced and line.startswith(END)), None)
    assert end is not None, f"docs/DOCKER.md has no paragraph starting {END!r} after `### Getting the installer`"
    return lines[start:end]


def code_lines(lines: list[str]) -> list[str]:
    """Logical lines of the fenced blocks: continuations joined, comments kept."""
    out = []
    for fence in md.fences(lines):
        joined = ""
        for line in fence.body.splitlines():
            joined = f"{joined} {line.strip()}" if joined else line.strip()
            if joined.endswith("\\"):
                joined = joined[:-1].rstrip()
                continue
            if joined:
                out.append(joined)
            joined = ""
        if joined:
            out.append(joined)
    return out


def paragraphs(lines: list[str]) -> list[str]:
    """Raw prose paragraphs (and list items) outside the fences, line breaks joined."""
    fenced = md.fenced_lines(lines)
    out, current = [], []
    for index, line in enumerate(lines):
        if index in fenced or not line.strip() or re.match(r"^\s*(?:[-*+]|\d+[.)])\s", line):
            if current:
                out.append(" ".join(current))
            current = [line.strip()] if index not in fenced and line.strip() else []
            continue
        current.append(line.strip())
    if current:
        out.append(" ".join(current))
    return out


def commands(lines: list[str]) -> list[str]:
    """Every command the section shows: fenced code lines and inline code spans of the prose."""
    return code_lines(lines) + [span for para in paragraphs(lines) for span in md.inline_code(para)]


def prose_sentences(lines: list[str]) -> list[str]:
    return [sentence for para in paragraphs(lines) for sentence in md.sentences(md.normal(para))]


# ------------------------------------------------------------------ offers

_INSTALLER = re.compile(
    r"\bpip3?[\"']?\s+(?:install|download|wheel)\b|\bpipx\s+(?:install|run)\b|\buv\s+pip\s+install\b"
    r"|\buv\s+tool\s+(?:install|run)\b|\buv\s+add\b|\buvx\b|\bpoetry\s+add\b|\bpdm\s+add\b", re.I)
_PROJECT_NAME = re.compile(r"kp[-_]agent[-_]tooling", re.I)
_LOCAL = ("./", "../", "/", "~/", "$", "packages/")


def _word_at(text: str, start: int, end: int) -> str:
    """The shell word around text[start:end], quotes and code marks stripped."""
    stops = " \t\"'`()="
    left = start
    while left > 0 and text[left - 1] not in stops:
        left -= 1
    right = end
    while right < len(text) and text[right] not in stops:
        right += 1
    return text[left:right]


def install_offers(command: str) -> list[str]:
    """`command` is an install line naming the project outside a local path: the offending words."""
    if not _INSTALLER.search(command):
        return []
    return [word for match in _PROJECT_NAME.finditer(command)
            for word in [_word_at(command, match.start(), match.end())] if not word.startswith(_LOCAL)]


def words_offer(text: str) -> bool:
    """"PyPI" or "release page" beside "wheel"/"package", or "released" with "package", in `text`."""
    packaged = re.search(r"\b(?:wheels?|packages?)\b", text, re.I)
    if packaged and re.search(r"pypi|\brelease\s+pages?\b", text, re.I):
        return True
    return bool(re.search(r"\breleased\b", text, re.I) and re.search(r"\bpackages?\b", text, re.I))


def offers(lines: list[str]) -> list[str]:
    found = []
    for command in commands(lines):
        found += [f"install line {command!r} names {word!r} outside a local path"
                  for word in install_offers(command)]
    found += [f"code line {line!r} offers a released or index package" for line in code_lines(lines)
              if words_offer(line)]
    found += [f"sentence {sentence!r} offers a released or index package" for sentence in prose_sentences(lines)
              if words_offer(sentence)]
    return found


# ------------------------------------------------------------------ source paths

_PATH_TOKEN = re.compile(r"(?<![\w$/.~{}@:\\-])((?:\.\.?/)?[A-Za-z0-9_*][A-Za-z0-9_.*-]*(?:/[A-Za-z0-9_.*-]*)+)")
_HOST_NAME = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
_OUTPUT_FLAGS = {"--wheel-dir", "-w", "--outdir", "-o"}
_VALUE_FLAGS = _OUTPUT_FLAGS | {"-r", "--requirement", "-c", "--constraint", "-i", "--index-url",
                                "--extra-index-url", "-f", "--find-links", "--src", "-C", "--config-settings",
                                "--build-option", "--global-option", "--progress-bar", "--python"}


def _split(command: str) -> list[str]:
    try:
        return shlex.split(command, comments=True)
    except ValueError:
        return command.split()


def builds(command: str) -> tuple[list[str], list[str]]:
    """(source arguments, output directories) of a wheel build or editable install in `command`."""
    words = _split(command)
    sources, outputs = [], []
    for at, word in enumerate(words):
        if word == "pip" and at + 1 < len(words) and words[at + 1] == "wheel":
            rest, positional = words[at + 2:], True
        elif word == "build" and at >= 1 and words[at - 1] == "-m":
            rest, positional = words[at + 1:], True
        elif word == "pip" and at + 1 < len(words) and words[at + 1] == "install":
            rest, positional = words[at + 2:], False
        else:
            continue
        skip = False
        for index, arg in enumerate(rest):
            if skip:
                skip = False
                continue
            if arg in ("-e", "--editable") and index + 1 < len(rest):
                sources.append(rest[index + 1])
                skip = True
            elif arg in _VALUE_FLAGS:
                if arg in _OUTPUT_FLAGS and index + 1 < len(rest):
                    outputs.append(rest[index + 1])
                skip = True
            elif "=" in arg and arg.split("=", 1)[0] in _VALUE_FLAGS:
                if arg.split("=", 1)[0] in _OUTPUT_FLAGS:
                    outputs.append(arg.split("=", 1)[1])
            elif positional and not arg.startswith("-"):
                sources.append(arg)
        break
    return sources, outputs


def _first_part(path: str) -> str:
    return re.sub(r"^(?:\.\./|\./)+", "", path).split("/", 1)[0]


def _package_root() -> Path:
    spec = importlib.util.find_spec("kp_agent_tooling")
    assert spec and spec.submodule_search_locations, "kp_agent_tooling is not installed"
    return Path(list(spec.submodule_search_locations)[0]).parent


def _exists(path: str) -> bool:
    if _first_part(path) == "kp_agent_tooling":
        base, rel = _package_root(), re.sub(r"^\./", "", path)
    else:
        base, rel = ROOT, path
    rel = rel.rstrip("/") or "."
    if "*" in rel:
        return bool(glob.glob(str(base / rel)))
    target = (base / rel).resolve()
    return target.is_relative_to(base.resolve()) and target.exists()


def named_sources(lines: list[str]) -> list[str]:
    """Every source path the section names (see the module docstring), as written."""
    text = "\n".join(lines)
    outputs, found = set(), []
    for command in commands(lines):
        sources, made = builds(command)
        outputs.update(_first_part(out) for out in made)
        found += [s for s in sources if not re.search(r"[<$]", s) and "://" not in s]
    for match in _PATH_TOKEN.finditer(text):
        token = match.group(1).rstrip(".,;:")
        following = text[match.end():match.end() + 1]
        if following in ("<", "$", "{") or _HOST_NAME.match(_first_part(token)):
            continue
        found.append(token)
    return [path for path in dict.fromkeys(found) if _first_part(path) not in outputs]


# ------------------------------------------------------------------ the project directory


def _declares_project(directory: str) -> bool:
    pyproject = ROOT / re.sub(r"^\./", "", directory).rstrip("/") / "pyproject.toml"
    if not pyproject.is_file():
        return False
    project = tomllib.loads(pyproject.read_text()).get("project", {})
    return project.get("name") == PROJECT and "kp-agent-install" in (project.get("scripts") or {})


# ------------------------------------------------------------------ P3


def test_p3_the_section_offers_no_package_or_wheel_the_release_does_not_upload():
    """GREEN-IF the release workflow uploads a wheel, or the section makes no offer of a published package or
    wheel (the module docstring's definition)."""
    uploads = release_wheel_uploads()
    if uploads:
        return  # a published wheel may be offered: the release uploads one (see `uploads`)
    found = offers(section())
    assert not found, ("docs/DOCKER.md \"Getting the installer\" offers a package or wheel, but "
                       ".github/workflows/release.yml uploads no wheel:\n" + "\n".join(found))


def test_p3_every_source_path_the_section_names_exists():
    """GREEN-IF every source path the section names (module docstring) exists in the tree, or in the
    installed package for a `kp_agent_tooling/` path."""
    names = named_sources(section())
    assert names, "precondition: the section names no source path (it must name packages/tooling)"
    missing = [path for path in names if not _exists(path)]
    assert not missing, f"docs/DOCKER.md \"Getting the installer\" names paths that do not exist: {missing}"


# ------------------------------------------------------------------ P1


_IMAGES_ONLY = (re.compile(r"\bcontainer images?\s+only\b|\bonly\s+container images?\b", re.I),
                re.compile(r"\bno wheels?\b", re.I),
                re.compile(r"\bno\b[^.;]*\bpackage index\b", re.I))


def test_p1_the_section_states_whether_the_release_publishes_a_wheel():
    """GREEN-IF, when the release workflow uploads no wheel, one sentence of the section says the release
    publishes container images only, no wheel and nothing on a package index; when it uploads one, no
    sentence says so."""
    stated = [s for s in prose_sentences(section()) if all(rx.search(s) for rx in _IMAGES_ONLY)]
    uploads = release_wheel_uploads()
    if uploads:
        assert not stated, f"the release uploads a wheel ({uploads}), but the section says it does not: {stated}"
    else:
        assert stated, ("docs/DOCKER.md \"Getting the installer\" does not say that the release publishes "
                        "container images only, with no wheel and nothing on a package index")


def test_p1_the_section_names_the_three_ways_to_get_the_installer():
    """GREEN-IF the section shows a wheel build (`pip wheel`) and an editable install (`pip install -e`) of the
    directory whose pyproject.toml declares kp-agent-tooling with the `kp-agent-install` script, an install of
    the built wheel from a local path, and points to the product image."""
    lines = section()
    built = [s for c in commands(lines) if re.search(r"\bpip\s+wheel\b", c) for s in builds(c)[0]]
    editable = [s for c in commands(lines) if re.search(r"\s(?:-e|--editable)\s", c) for s in builds(c)[0]]
    wheel_installs = [c for c in commands(lines) if _INSTALLER.search(c) and re.search(r"\.whl\b", c)]
    assert built and all(_declares_project(s) for s in built), (
        f"no wheel build of the {PROJECT} project directory in the section (built: {built})")
    assert editable and all(_declares_project(s) for s in editable), (
        f"no editable install of the {PROJECT} project directory in the section (installed: {editable})")
    assert wheel_installs and not any(install_offers(c) for c in wheel_installs), (
        f"no install of the built wheel from a local path in the section: {wheel_installs}")
    assert any(re.search(r"\bproduct\s+image\b", s, re.I) for s in prose_sentences(lines)), (
        "the section does not point to the product image")


# ------------------------------------------------------------------ the readers can say yes

_STEP = "jobs:\n  release:\n    runs-on: ubuntu-latest\n    steps:\n{steps}"


@pytest.mark.parametrize("steps", [
    "      - uses: pypa/gh-action-pypi-publish@release/v1\n",
    "      - run: python -m twine upload dist/*\n",
    "      - run: uv publish\n",
    "      - run: gh release create \"$TAG\" dist/*.whl --notes x\n",
    "      - run: |\n          gh release upload \"$TAG\" ./dist/kp_agent_tooling-0.4.0-py3-none-any.whl\n",
    "      - uses: softprops/action-gh-release@v2\n        with:\n          files: dist/*.whl\n",
])
def test_reader_finds_a_wheel_upload(steps):
    assert wheel_uploads(_STEP.format(steps=steps))


@pytest.mark.parametrize("steps", [
    "      - run: gh release create \"$TAG\" --notes-file notes.md\n",
    "      - uses: actions/upload-artifact@v4\n        with:\n          path: dist/*.whl\n",
    "      - run: |\n          # no .whl is attached; gh release create below\n          gh release create \"$TAG\"\n",
    "      - run: python -m pip wheel --no-deps --wheel-dir dist ./packages/tooling\n",
])
def test_reader_finds_no_wheel_upload(steps):
    assert not wheel_uploads(_STEP.format(steps=steps))


@pytest.mark.parametrize("text", [
    "pipx install ./kp_agent_tooling-<version>-py3-none-any.whl   # or the released kp-agent-tooling package",
    "pip install kp-agent-tooling",
    "python3 -m pip install 'kp-agent-tooling==0.4.0'",
    "uvx --from kp-agent-tooling kp-agent-install plan --help",
    "pip install https://example.com/kp_agent_tooling-0.4.0-py3-none-any.whl",
])
def test_reader_finds_an_install_offer(text):
    assert install_offers(text) or words_offer(text)


@pytest.mark.parametrize("text", [
    "pip install -e packages/tooling",
    "pipx install ./kp_agent_tooling-<version>-py3-none-any.whl",
    "\"$venv/bin/pip\" install ./dist/kp_agent_tooling-<version>-py3-none-any.whl",
    "python3 -m pip wheel --no-deps --wheel-dir dist ./packages/tooling",
])
def test_reader_accepts_a_local_install(text):
    assert not install_offers(text) and not words_offer(text)


@pytest.mark.parametrize("sentence", [
    "Install the wheel from PyPI.",
    "Download the wheel from the release page.",
    "Or install the released kp-agent-tooling package.",
])
def test_reader_finds_an_offer_in_prose(sentence):
    assert words_offer(sentence)


def test_reader_names_sources_and_skips_outputs_and_placeholders():
    lines = ["### Getting the installer", "", "Built from `packages/tooling` and `pkgs/tooling`.", "",
             "```sh", "python3 -m pip wheel --no-deps --wheel-dir dist ./packages/tooling",
             "pip install -e tooling", "\"$venv/bin/pip\" install ./dist/kp_agent_tooling-<version>-py3-none-any.whl",
             "docker pull ghcr.io/owner/name@sha256:<digest>", "```"]
    names = named_sources(lines)
    assert set(names) == {"packages/tooling", "pkgs/tooling", "./packages/tooling", "tooling"}, names
    assert sorted(path for path in names if not _exists(path)) == ["pkgs/tooling", "tooling"]
