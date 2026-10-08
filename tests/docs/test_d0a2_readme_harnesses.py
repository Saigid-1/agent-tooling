"""D0a-2: the README, the harnesses page and the first run's search (F2, F3, F5, F6, F7, F8).

Order: docs/work/orders/D0a2-readme-and-harnesses.md. F1 is test_d0a2_f1_claims.py; F4 is D0b's
public-home test (tests/test_d0b_public_home_and_labels.py) and has no second copy here. Every name
these tests assume about FEATURE's words is in d0a2_seams.py; d0a2_markdown.py states how the
Markdown is read.

Guards, GREEN at base by design:
- test_f7_guard_d0b_pull_by_digest_stays_before_plan (D0b's step 0 text, which P4 leaves untouched);
- test_f8_every_version_number_equals_the_tree_pin (no three-part version number in either document
  at base; its mutant needs a number to bump).
"""
from __future__ import annotations

import difflib
import json
import re
import sys
import tomllib
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import d0a2_markdown as md  # noqa: E402
import d0a2_seams as seams  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def _lines(name: str) -> list[str]:
    path = ROOT / name
    assert path.is_file(), f"{name} does not exist"
    return path.read_text().splitlines()


def _titled(lines: list[str], pattern: str) -> list[md.Heading]:
    return [h for h in md.headings(lines) if re.search(pattern, h.title)]


# ------------------------------------------------------------------------------------------- F2


def _stance_body(lines: list[str]) -> str:
    """The stance file after its first heading (the whole file when it has none)."""
    found = md.headings(lines)
    start = found[0].line + 1 if found and not any(l.strip() for l in lines[:found[0].line]) else 0
    return "\n".join(lines[start:])


def test_f2_readme_release_stance_equals_the_stance_file():
    """GREEN-IF README.md has exactly one section titled `README_STANCE_TITLE` ("Release stance", any
    heading level, compared without case or emphasis), and that section's text after its heading, up to
    the next heading of the same or a higher level, equals docs/RELEASE-STANCE.md's text after its first
    heading, once both are normalised by removing Markdown emphasis (`*`, `_` at a word's edge) and
    collapsing line breaks and runs of white space. Nothing else is normalised: a reworded, dropped,
    added or reordered claim, or an extra sentence in either copy, is a difference."""
    readme = _lines(seams.README)
    titled = [h for h in md.headings(readme) if h.title.lower() == seams.README_STANCE_TITLE.lower()]
    assert len(titled) == 1, (f"README.md has {len(titled)} section(s) titled {seams.README_STANCE_TITLE!r}; "
                              "P2 wants exactly one")
    section = md.normal_stance("\n".join(md.section_lines(readme, titled[0])))
    stance = md.normal_stance(_stance_body(_lines(seams.RELEASE_STANCE)))
    assert stance, f"{seams.RELEASE_STANCE} has no text after its heading"
    if section != stance:
        split = lambda text: re.split(r"(?<=[.!?])\s+", text)  # noqa: E731
        diff = "\n".join(difflib.unified_diff(split(stance), split(section), seams.RELEASE_STANCE,
                                              "README.md#" + seams.README_STANCE_TITLE, lineterm=""))
        raise AssertionError("the README's stance differs from the stance file (normalised):\n" + diff)


# ------------------------------------------------------------------------------------------- F3

_TOOL = "|".join(re.escape(t) for t in seams.RESTATED_TOOLS)


def _restated_commands(section: list[str]) -> list[str]:
    """Every fenced line that names a tool, and every inline code span that runs one with an argument."""
    found = []
    fenced = md.fenced_lines(section)
    for fence in md.fences(section):
        found.extend(line.strip() for line in fence.body.splitlines() if re.search(rf"(?<![\w-])(?:{_TOOL})\b", line))
    prose = "\n".join(line for index, line in enumerate(section) if index not in fenced)
    found.extend(span for span in md.inline_code(prose)
                 if re.search(rf"(?<![\w-])(?:{_TOOL})\s+\S", span.strip()))
    return found


def test_f3_readme_first_run_links_docker_md_and_restates_no_command():
    """GREEN-IF README.md has at least one section whose heading matches `README_FIRST_RUN_TITLE` ("first
    run"); at least one such section holds a Markdown link to docs/DOCKER.md (an anchor is allowed); and
    no such section (its subsections included) restates a `kp-agent-install` or `docker compose`
    (`docker-compose`) command: no line of a fenced code block names either tool, and no inline code span
    runs either with an argument. Naming the bare tool in prose or in an inline code span is not a
    command."""
    readme = _lines(seams.README)
    titled = _titled(readme, seams.README_FIRST_RUN_TITLE)
    assert titled, f"README.md has no section whose heading matches {seams.README_FIRST_RUN_TITLE!r} (P1)"
    targets = []
    restated = []
    for heading in titled:
        section = md.section_lines(readme, heading)
        targets.extend(t.split("#", 1)[0].removeprefix("./") for t in md.links("\n".join(section)))
        restated.extend(f"{heading.title}: {command}" for command in _restated_commands(section))
    assert seams.DOCKER in targets, f"no first-run section of README.md links {seams.DOCKER} (links: {targets})"
    assert not restated, "README.md's first run restates commands instead of linking DOCKER.md:\n" + "\n".join(restated)


# ------------------------------------------------------------------------------------------- F5 and F6


def _harness_entries() -> tuple[list[str], dict[str, md.Heading | None]]:
    lines = _lines(seams.HARNESSES)
    taken: set[int] = set()
    entries: dict[str, md.Heading | None] = {}
    for name, pattern in seams.HARNESS_ENTRIES.items():
        match = next((h for h in md.headings(lines) if h.level >= 2 and h.line not in taken
                      and re.search(pattern, h.title)), None)
        entries[name] = match
        if match is not None:
            taken.add(match.line)
    return lines, entries


def test_f5_every_harnesses_entry_has_an_install_pointer_and_a_status():
    """GREEN-IF docs/HARNESSES.md has a heading (level 2 or deeper) for each of P3's five entries, as
    `HARNESS_ENTRIES` names them (Claude Code; Codex; OpenCode; the model gateway; the in-board Cline agent
    and the Claude Agent SDK), one heading per entry; and each entry's section (its subsections included,
    emphasis and code marks removed) has an install pointer (`INSTALL_POINTER`: install, ships, shipped or
    pull) and a status (`STATUS`: the word Status, then within three words Built, so "Built" or "not
    Built"; a bare "supported" is no status)."""
    lines, entries = _harness_entries()
    problems = []
    for name, heading in entries.items():
        if heading is None:
            problems.append(f"{name}: no entry (no heading matches {seams.HARNESS_ENTRIES[name]!r})")
            continue
        body = md.normal("\n".join(md.section_lines(lines, heading)))
        if not re.search(seams.INSTALL_POINTER, body):
            problems.append(f"{name} ({heading.title!r}): no install pointer")
        if not re.search(seams.STATUS, body):
            problems.append(f"{name} ({heading.title!r}): no status (Built or not Built)")
    assert not problems, "docs/HARNESSES.md:\n" + "\n".join(problems)


_EGRESS = r"(?:reach|contact|call|access|touch|download|fetch|connect|egress)\w*"
_EGRESS_NEGATED = re.compile(rf"\b(?:not|never|no longer|cannot)\s+(?:\w+\s+){{0,2}}?{_EGRESS}"
                             rf"|n't\s+(?:\w+\s+){{0,2}}?{_EGRESS}|\bno\s+unbound\b", re.I)
_PARTS = re.compile(r"\s*(?:;|,\s+(?:but|while|whereas)\s+)\s*", re.I)


def _names_the_egress(part: str) -> bool:
    """`part` names all of UNBOUND_EGRESS, and the span they cover (with 40 characters before it) does not
    negate the egress."""
    matches = [re.search(pattern, part) for pattern in seams.UNBOUND_EGRESS]
    if not all(matches):
        return False
    start = max(0, min(m.start() for m in matches) - 40)
    end = max(m.end() for m in matches)
    return not _EGRESS_NEGATED.search(part[start:end])


def test_f6_harnesses_names_the_unbound_opencode_launch_registry_egress():
    """GREEN-IF a part of a sentence in docs/HARNESSES.md (a sentence split at `;` and at ", but|while|
    whereas ") names an unbound launch, npm and a registry (`UNBOUND_EGRESS`: `unbound`, `npm`, `registry`)
    and is about OpenCode (the part, or a heading above it, names OpenCode); and, from 40 characters before
    the first of those words to the end of the last, the part does not negate the egress ("not", "never",
    "no longer", "cannot" or "n't" within two words before reach, contact, call, access, touch, download,
    fetch, connect or egress; or "no unbound"). What the part says about bound launches after those words
    does not count."""
    lines = _lines(seams.HARNESSES)
    heads = md.headings(lines)
    found = []
    for first, unit in md.prose_units(lines):
        above = [h for h in heads if h.line <= first]
        trail, level = [], 7
        for heading in reversed(above):
            if heading.level < level:
                trail.append(heading.title)
                level = heading.level
        about_opencode = any(re.search(r"(?i)\bopencode\b", title) for title in trail)
        for sentence in md.sentences(unit):
            for part in _PARTS.split(sentence):
                if (about_opencode or re.search(r"(?i)\bopencode\b", part)) and _names_the_egress(part):
                    found.append(part)
    assert found, ("docs/HARNESSES.md does not name the unbound OpenCode launch's npm registry egress "
                   "(backlog 50, Verification's ruling)")


# ------------------------------------------------------------------------------------------- F7


def _first_run_range(lines: list[str]) -> tuple[int, int]:
    heads = md.headings(lines)
    start = next((h for h in heads if h.title == seams.DOCKER_FIRST_RUN_HEADING), None)
    assert start is not None, f"{seams.DOCKER} has no heading {seams.DOCKER_FIRST_RUN_HEADING!r}"
    end = next((h for h in heads if h.line > start.line and h.title.startswith(seams.DOCKER_BOARD_TASKS_HEADING)),
               None)
    assert end is not None, f"{seams.DOCKER} has no {seams.DOCKER_BOARD_TASKS_HEADING!r} heading after the first run"
    return start.line, end.line


def test_f7_docker_first_run_ends_at_a_search_that_answers():
    """GREEN-IF, in docs/DOCKER.md, between the heading "First run from an empty root" and the next heading
    that starts "Board tasks with in-container agents" (the first run's own steps), a fenced code block
    (a command block the stranger runs) contains `memory.search`, and the prose after that block and
    before the board-tasks heading (outside code blocks; emphasis, code marks and case ignored) says
    `DOCKER_SEARCH_ANSWERS` ("a search that answers"). A `memory.search` in prose only, or a search step
    inside the board-tasks subsection, does not count. RED at base by design."""
    lines = _lines(seams.DOCKER)
    start, end = _first_run_range(lines)
    region = lines[start:end]
    fenced = md.fenced_lines(region)
    calls = [f for f in md.fences(region) if seams.SEARCH_CALL in f.body]
    assert calls, (f"{seams.DOCKER}'s first run (before {seams.DOCKER_BOARD_TASKS_HEADING!r}) has no command "
                   f"block that calls {seams.SEARCH_CALL}")
    phrase = seams.DOCKER_SEARCH_ANSWERS.lower()
    answered = [f for f in calls
                if phrase in md.normal(" ".join(line for i, line in enumerate(region)
                                                if i > f.end and i not in fenced)).lower()]
    assert answered, (f"no text after the {seams.SEARCH_CALL} command block, within the first run, calls its "
                      f"result {seams.DOCKER_SEARCH_ANSWERS!r}")


def _pulls_by_digest(text: str) -> bool:
    """`docker pull <ref>@sha256:...`, or `docker pull "$var"` after `var='<ref>@sha256:...'` in `text`."""
    if re.search(r"docker pull\s+['\"]?[^\s'\"`$]*@sha256:", text):
        return True
    for pull in re.finditer(r"docker pull\s+\"?\$\{?(\w+)\}?\"?", text):
        if re.search(rf"(?m)\b{pull.group(1)}=['\"]?[^\s'\"]*@sha256:", text[:pull.start()]):
            return True
    return False


def test_f7_guard_d0b_pull_by_digest_stays_before_plan():
    """GREEN-IF (GREEN at base: D0b's text, which P4 leaves untouched) docs/DOCKER.md's first run, before
    its first `kp-agent-install plan`, still pulls by digest (D0b R10): `docker pull` of a reference with
    `@sha256:`, or `docker pull "$var"` after `var='<reference>@sha256:...'`, appears there."""
    lines = _lines(seams.DOCKER)
    start, end = _first_run_range(lines)
    text = "\n".join(lines[start:end])
    plan = re.search(r"kp-agent-install\s+plan\b", text)
    assert plan, f"{seams.DOCKER}'s first run has no `kp-agent-install plan`"
    assert _pulls_by_digest(text[:plan.start()]), \
        f"{seams.DOCKER}'s first run no longer pulls by digest before `plan` (D0b's step 0)"


# ------------------------------------------------------------------------------------------- F8

_VERSION = re.compile(r"(?<![\w.+/])v?(\d+\.\d+\.\d+)(-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?(?![\w+]|\.\d)")


def _lock_version(name: str, package: str) -> str:
    lock = json.loads((ROOT / name).read_text())
    version = (lock.get("packages") or {}).get(f"node_modules/{package}", {}).get("version")
    assert version, f"{name} pins no {package}"
    return version


def _pins() -> dict[str, tuple[str, list[str]]]:
    """name -> (the tree's pin, the names a document may attribute a version to)."""
    opencode = _lock_version(seams.OPENCODE_LOCK, seams.OPENCODE_PACKAGE)
    arg = re.search(rf"(?m)^ARG {seams.OPENCODE_ARG}=(\S+)$", (ROOT / seams.DOCKERFILE).read_text())
    assert arg, f"{seams.DOCKERFILE} has no ARG {seams.OPENCODE_ARG}"
    assert arg.group(1) == opencode, (f"the tree's two OpenCode pins disagree: {seams.OPENCODE_LOCK} {opencode}, "
                                      f"{seams.DOCKERFILE} {arg.group(1)}")
    projects = {tomllib.loads((ROOT / p).read_text())["project"]["version"] for p in seams.PROJECT_PYPROJECTS}
    assert len(projects) == 1, f"the project's packages disagree on its version: {sorted(projects)}"
    return {
        "OpenCode": (opencode, [seams.OPENCODE_PACKAGE, "opencode"]),
        "Claude Code": (_lock_version(seams.AGENTS_LOCK, seams.CLAUDE_CODE_PACKAGE),
                        [seams.CLAUDE_CODE_PACKAGE, "claude-code", "claude code"]),
        "Codex": (_lock_version(seams.AGENTS_LOCK, seams.CODEX_PACKAGE), [seams.CODEX_PACKAGE, "codex"]),
        "Claude Agent SDK": (_lock_version(seams.KANBAN_LOCK, seams.AGENT_SDK_PACKAGE),
                             [seams.AGENT_SDK_PACKAGE, "claude-agent-sdk", "claude agent sdk"]),
        "the SDK's provider adapter": (_lock_version(seams.KANBAN_LOCK, seams.AGENT_SDK_ADAPTER_PACKAGE),
                                       [seams.AGENT_SDK_ADAPTER_PACKAGE]),
        "the project": (projects.pop(), ["kp-agent-tooling-ops", "kp-agent-tooling", "agent-tooling"]),
    }


def _attributed(text: str, at: int, names: list[str]) -> bool:
    """True when a name stands right before the version: `name@`, `name `, `name version `, `name pinned
    (at) `, `name at `, `name v`, with code marks or emphasis and an opening parenthesis allowed."""
    before = text[max(0, at - 80):at]
    alias = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return bool(re.search(rf"(?i)(?<![\w/-])(?:{alias})[`*_]*(?:@|\s*\(?\s*(?:(?:version|pinned(?: at)?|at)\s+)?)"
                          rf"[`*_]*v?$", before))


def test_f8_every_version_number_equals_the_tree_pin():
    """GREEN-IF the tree's pins can be read and agree with themselves (OpenCode: deploy/image/opencode's lock
    equals the Dockerfile's OPENCODE_VERSION ARG; Claude Code and Codex: deploy/image/agents' lock; the
    project's version: both pyproject.toml files), and every three-part version number (`X.Y.Z`, with an
    optional `v` and pre-release suffix) in README.md and docs/HARNESSES.md, code blocks included, equals a
    pin of the tree: when a pinned name stands right before it (`OpenCode 1.2.3`, `opencode-ai@1.2.3`,
    `Codex version 1.2.3`, `Claude Code (pinned 1.2.3)`), that name's pin exactly; otherwise any of the
    pins above or the board's own pins of the Claude Agent SDK and its provider adapter
    (apps/kanban/package-lock.json). The project's version may carry a pre-release suffix (`0.4.0-rc.1`).
    GREEN at base: neither document has a three-part version number."""
    pins = _pins()
    problems = []
    for name in (seams.README, seams.HARNESSES):
        path = ROOT / name
        if not path.is_file():
            continue
        text = path.read_text()
        for match in _VERSION.finditer(text):
            version, suffix = match.group(1), match.group(2) or ""
            owners = [owner for owner, (_pin, names) in pins.items() if _attributed(text, match.start(), names)]
            line = text.count("\n", 0, match.start()) + 1
            if owners:
                owner = owners[0]
                pin = pins[owner][0]
                ok = (version == pin and not suffix) or (owner == "the project" and version == pin)
                if not ok:
                    problems.append(f"{name}:{line}: {owner} {match.group(0)} is not the tree's pin {pin}")
                continue
            ok = any(version == pin and (not suffix or owner == "the project") for owner, (pin, _) in pins.items())
            if not ok:
                problems.append(f"{name}:{line}: {match.group(0)} is no pin of the tree "
                                f"({', '.join(f'{o} {p}' for o, (p, _) in pins.items())})")
    assert not problems, "\n".join(problems)
