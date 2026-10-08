"""T7b P5 (and the documentation half of P3 and P4): the board wiring is documented as the code behaves.

Order: docs/work/orders/T7b-board-wiring.md.
- P5: "DOCKER.md's board section, LAUNCH-BINDING.md (container example) and HOST-ADAPTER.md
  (one registry for host and board launches) describe P1–P4 as the code behaves."
  Falsifier: a statement the code contradicts.
- P3: `--board-agents` is "documented for use with the `agents` image".
- P4: "DOCKER.md states the container form of the assistant memory command, its binding file
  under `$root/config/board/` and the workspace variable."
- P1: `verify` readiness names `config/launch/registry.json` for `board` (DOCKER.md's
  operator-files table is where readiness is documented).

What the code fixes, and these tests hold the documents to (cited, not re-tested here):
- P1 and P2 fix the board's commands: the desk registry `kp-agent-desk-registry --config
  /config/launch/registry.json` and the launch binding JSON argv
  `["/usr/local/bin/kp-agent-launch", "--config", "/config/launch/registry.json"]`;
- the board sees `$root/config` at `/config` (deploy/compose.yaml, `x-config`);
- Kanban runs KANBAN_ASSISTANT_MEMORY_COMMAND as a JSON argv whose first element is
  absolute (apps/kanban/src/terminal/assistant-memory-launch.ts) and the in-image assistant
  host accepts `--binding` (packages/tooling/src/kp_agent_tooling/assistant_host_cli.py;
  outside T7b's FEATURE write scope, so its flags are fixed).

Readings (repeated in the arm report under AMBIGUITY):
- a statement is one table row, one list item, one fenced code block or one other
  paragraph, so terms scattered over unrelated rows do not count;
- "DOCKER.md's board section" is every part of DOCKER.md under a heading that names the
  board, every run-in titled paragraph group whose title names it (`**board.**`), and the
  `board` row of the table under "## Roles"; a statement about launches must say so in
  words or name the launch command, not only through the path segment `config/launch/`;
- a documented JSON argv is the first JSON array on a line (in code or prose) that names the
  variable, or on the next non-empty line;
- the container form of the assistant memory command is a documented argv for
  KANBAN_ASSISTANT_MEMORY_COMMAND with an absolute first element and `--binding` followed by
  an absolute path under `/config/board/`; if it runs a module with `-m`, that module is
  `kp_agent_tooling.assistant_host_cli`; its binding file is then named as
  `$root/config/board/<file>` (or root-relative `config/board/<file>`) for the same file.
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
REGISTRY = "/config/launch/registry.json"
LAUNCH_ARGV = ["/usr/local/bin/kp-agent-launch", "--config", REGISTRY]

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_RUN_IN = re.compile(r"^\*\*([^*]+?)\.?\*\*")
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+")
BOARD = re.compile(r"\bboard\b", re.I)
# Path segments (`config/board/`, `/board`) and option names (`--board-agents`, `--board-port`) are not the board.
_NOT_THE_BOARD = re.compile(r"[\w$.{}~*-]*/board\b[\w./*-]*|--board-[\w-]+|\bboard-[\w-]+", re.I)


def mentions_board(text: str) -> bool:
    return BOARD.search(_NOT_THE_BOARD.sub(" ", text)) is not None


def read(name: str) -> str:
    return (DOCS / name).read_text()


def statements(text: str) -> list[str]:
    """Table rows, list items, fenced code blocks and other paragraphs, each one statement."""
    out, current, fenced = [], [], False

    def flush():
        if current:
            out.append("\n".join(current))
            current.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            if not fenced:
                flush()
                current.append(line)
                fenced = True
            else:
                current.append(line)
                flush()
                fenced = False
            continue
        if fenced:
            current.append(line)
        elif not stripped:
            flush()
        elif stripped.startswith("|"):
            flush()
            out.append(line)
        elif _ITEM.match(line) and not line.startswith("    "):
            flush()
            current.append(line)
        else:
            current.append(line)
    flush()
    return out


def board_section(text: str) -> str:
    out, keep, level, run_in, roles = [], False, 7, False, False
    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            depth = len(heading.group(1))
            if keep and depth <= level:
                keep = False
            if not keep and BOARD.search(heading.group(2)):
                keep, level = True, depth
            run_in = False
            roles = heading.group(2).strip().lower() == "roles"
        else:
            title = _RUN_IN.match(line.strip())
            if title:
                run_in = BOARD.search(title.group(1)) is not None
        row = roles and line.strip().startswith("| `board`")
        if keep or run_in or row:
            out.append(line)
    return "\n".join(out)


def first_json_argv(candidate: str) -> list | None:
    """The first balanced `[...]` in `candidate` that parses as a non-empty JSON list of strings."""
    for start in [i for i, char in enumerate(candidate) if char == "["]:
        depth = 0
        for position in range(start, len(candidate)):
            depth += {"[": 1, "]": -1}.get(candidate[position], 0)
            if depth == 0:
                try:
                    value = json.loads(candidate[start:position + 1])
                except ValueError:
                    break
                if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
                    return value
                break
    return None


def documented_argvs(text: str, variable: str) -> list[list]:
    """Every JSON argv documented for `variable`: the first on the rest of its line, else, when the line
    ends at the variable (`NAME`, `NAME=`, `NAME:`), on the next non-empty line."""
    lines = text.splitlines()
    found = []
    for index, line in enumerate(lines):
        if variable not in line:
            continue
        rest = line[line.index(variable) + len(variable):]
        argv = first_json_argv(rest)
        if argv is None and not rest.strip(" \t`'\"=:\\"):
            following = next((l for l in lines[index + 1:index + 4] if l.strip()), "")
            argv = first_json_argv(following)
        if argv:
            found.append(argv)
    return found


def is_registry_argv(argv: list) -> bool:
    return (len(argv) == 3 and argv[0].startswith("/") and PurePosixPath(argv[0]).name == "kp-agent-desk-registry"
            and argv[1:] == ["--config", REGISTRY])


# --------------------------------------------------------------- DOCKER.md


def test_docker_md_board_section_describes_the_desk_registry_and_desk_task_launches():
    """GREEN-IF DOCKER.md's board section names /config/launch/registry.json for the Desks menu (desk registry)
    and for desk task launches (launch binding), and any argv it gives for either is the contract's."""
    section = board_section(read("DOCKER.md"))
    assert section.strip(), "docs/DOCKER.md has no board section"
    blocks = [s for s in statements(section) if "config/launch/registry.json" in s]
    desks = [s for s in blocks if re.search(r"desks? menu|desk registry|KANBAN_DESK_REGISTRY_COMMAND|"
                                            r"kp-agent-desk-registry", s, re.I)]
    launches = [s for s in blocks if re.search(r"launch|KANBAN_LAUNCH_BINDING_COMMAND|kp-agent-launch",
                                               s.replace("config/launch/", ""), re.I)]
    assert desks, ("DOCKER.md's board section has no statement naming config/launch/registry.json for the Desks "
                   "menu / desk registry")
    assert launches, ("DOCKER.md's board section has no statement naming config/launch/registry.json for desk "
                      "task launches (launch binding)")
    text = read("DOCKER.md")
    wrong = [a for a in documented_argvs(text, "KANBAN_DESK_REGISTRY_COMMAND") if not is_registry_argv(a)]
    wrong += [a for a in documented_argvs(text, "KANBAN_LAUNCH_BINDING_COMMAND") if a != LAUNCH_ARGV]
    assert not wrong, f"DOCKER.md documents board commands the code contradicts: {wrong}"


def test_docker_md_readiness_table_names_the_board_registry_config():
    """GREEN-IF one row or statement of DOCKER.md names `board` and its operator file config/launch/registry.json."""
    rows = [s for s in statements(read("DOCKER.md"))
            if mentions_board(s) and re.search(r"(?<![A-Za-z0-9_.-])config/launch/registry\.json", s)
            and (s.lstrip().startswith("|") or re.search(r"readiness|verify|operator file", s, re.I))]
    assert rows, ("DOCKER.md has no readiness/operator-files statement naming config/launch/registry.json for "
                  "the board")


def test_docker_md_documents_board_agents_for_the_agents_image():
    """GREEN-IF one statement of DOCKER.md names --board-agents with the `agents` image, the board, and
    read-write repositories."""
    found = [s for s in statements(read("DOCKER.md"))
             if "--board-agents" in s
             and re.search(r"`agents`|\bagents\b (image|target)", s.replace("--board-agents", ""), re.I)
             and mentions_board(s)
             and re.search(r"read-write|writable|\brw\b", s, re.I)]
    assert found, ("DOCKER.md has no statement documenting --board-agents for the `agents` image (the board, "
                   "read-write repositories)")


def test_docker_md_states_the_assistant_memory_container_form():
    """GREEN-IF DOCKER.md documents KANBAN_ASSISTANT_MEMORY_COMMAND as a container argv (absolute executable,
    `--binding /config/board/<file>`), names that binding file under $root/config/board/, and states
    KANBAN_ASSISTANT_MEMORY_WORKSPACE."""
    text = read("DOCKER.md")
    argvs = documented_argvs(text, "KANBAN_ASSISTANT_MEMORY_COMMAND")
    assert argvs, "DOCKER.md documents no JSON argv for KANBAN_ASSISTANT_MEMORY_COMMAND"
    forms = []
    for argv in argvs:
        if not argv[0].startswith("/") or "--binding" not in argv[:-1]:
            continue
        binding = argv[argv.index("--binding") + 1]
        if not binding.startswith("/config/board/") or PurePosixPath(binding).name in ("", "board"):
            continue
        if "-m" in argv[:-1] and argv[argv.index("-m") + 1] != "kp_agent_tooling.assistant_host_cli":
            continue
        forms.append((argv, PurePosixPath(binding).relative_to("/config/board")))
    assert forms, (f"no documented KANBAN_ASSISTANT_MEMORY_COMMAND is a container form (absolute executable, "
                   f"--binding /config/board/<file>, the assistant host module): {argvs}")
    named = [str(rel) for _, rel in forms
             if re.search(r"(?:\$\{?root\}?/|(?<![A-Za-z0-9_./$~-]))config/board/" + re.escape(str(rel)) +
                          r"(?![A-Za-z0-9_/-])", text)]
    assert named, (f"DOCKER.md does not name the binding file under $root/config/board/ "
                   f"({[str(r) for _, r in forms]})")
    assert "KANBAN_ASSISTANT_MEMORY_WORKSPACE" in text, "DOCKER.md does not state KANBAN_ASSISTANT_MEMORY_WORKSPACE"


# ---------------------------------------------------- LAUNCH-BINDING.md, HOST-ADAPTER.md


def test_launch_binding_md_gives_the_container_example():
    """GREEN-IF LAUNCH-BINDING.md documents KANBAN_LAUNCH_BINDING_COMMAND as the board container's argv."""
    argvs = documented_argvs(read("LAUNCH-BINDING.md"), "KANBAN_LAUNCH_BINDING_COMMAND")
    assert LAUNCH_ARGV in argvs, (f"LAUNCH-BINDING.md has no container example "
                                  f"KANBAN_LAUNCH_BINDING_COMMAND={json.dumps(LAUNCH_ARGV)}; documented: {argvs}")


def test_host_adapter_md_states_one_registry_for_host_and_board_launches():
    """GREEN-IF one statement of HOST-ADAPTER.md names host and board launches with config/launch/registry.json,
    and any launch binding argv it gives is the contract's."""
    text = read("HOST-ADAPTER.md")
    found = [s for s in statements(text)
             if mentions_board(s) and re.search(r"\bhost\b", s, re.I) and "config/launch/registry.json" in s]
    assert found, ("HOST-ADAPTER.md has no statement that host and board launches use the one registry "
                   "config/launch/registry.json")
    wrong = [a for a in documented_argvs(text, "KANBAN_LAUNCH_BINDING_COMMAND") if a != LAUNCH_ARGV]
    assert not wrong, f"HOST-ADAPTER.md documents a launch binding argv the code contradicts: {wrong}"
