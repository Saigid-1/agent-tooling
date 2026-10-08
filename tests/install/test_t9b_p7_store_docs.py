"""T9b P7 and the documented parts of P1 and P4 (docs/DOCKER.md, deploy/examples; no Docker).

Order: docs/work/orders/T9b-memory-store-volume.md, Amendment 5 (the consolidated order), P7:
- the operator sequence: stop writers; `plan`; `apply`; `prepare`; operator file edits; `up`;
  `verify`;
- first-run store steps are in-runtime commands (`docker compose run --rm -T <role> sh -c '...'`);
- the Docker runtime's store files are reached only from inside the runtime; status, backup and
  integrity checks are documented as one-off containers;
- `docker compose down -v` deletes the volume;
- `memory.migrated-<...>` may be removed after `verify` passes and one in-runtime backup completes;
- sessions launched before a migration must be relaunched;
- known limits, out of scope: `.refresh.lock`, the `/state/knowledge` locks, the assistant binding
  lock on `/config`, and the open-time refusal (T11).
Falsifier: a documented step that writes a store path from the host; any listed item missing.
Also P1 (documented and example store paths under /state/memory; operator-written files under
/config) and P4 step 5 (the documented recovery a `source_damaged` refusal names).
Not tested: "the new Compose or Engine floor is stated, if one is raised" (conditional on the
feature's choice).

Instrument: docs/DOCKER.md as text, and deploy/examples/*.json. A "documented step" is a
command in a fenced code block: continuation lines are joined, a heredoc body belongs to its
command, and a Bash array that holds a command (`name=(docker compose ... run ...)`) is
expanded where `"${name[@]}"` uses it. A command's context is its own text, its trailing `#`
comment and the prose paragraph just before its code block (in its section).

Readings (repeated in the arm report under AMBIGUITY):
- each statement is one paragraph, list item, table row or code-comment line that names its
  parts (listed in each test's GREEN-IF);
- a status, backup or integrity check is a one-off container command (`docker compose ... run`
  or `docker run`) whose own text names the store or memory, with `status`, `backup`/`back up`,
  or `quick_check`/`integrity` in its context;
- a host step on a store: a fenced command that names a store directory by a host path
  (`$root/state/memory`, `"$root"/state/registry`, `${root}/state/assistant`, a relative
  `state/desk-memory`, ...), not by its container path. A renamed `<name>.migrated-*` is
  another path;
- the operator sequence is read from the Upgrade section: the first position of each step
  (`stop`, `kp-agent-install plan`, `... apply`, `... prepare`, a `state_root`/`roster_path`
  edit, `up`, `kp-agent-install verify`) must increase in that order;
- a store path of a JSON value is `state_root` or `roster_path`; an operator-written file is
  `config_template`, `catalog_path` or `assistant_policy_path`; the values are read from
  docs/DOCKER.md (fenced code) and from every deploy/examples/*.json.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import pytest

DOC = Path(__file__).resolve().parents[2] / "docs" / "DOCKER.md"

FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
ARRAY = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=\((.*)\)\s*(?:#.*)?$", re.S)
COMPOSE_RUN = re.compile(r"\bdocker\s+compose\b(?:(?!\s(?:\||&&|;)\s).)*?\srun\b", re.S)
CONTAINER = re.compile(r"\bdocker\s+compose\b(?:(?!\s(?:\||&&|;)\s).)*?\srun\b|\bdocker\s+(?:run|exec)\b", re.S)
STORES = r"(?:memory|registry|assistant|desk-memory)"
HOST_STORE = re.compile(
    r"(?:\$\{?(?:root|AGENT_ROOT)\}?\"?/state/" + STORES + r"|(?<![\w/.$}-])state/" + STORES + r")(?![\w.-])")
EXAMPLES = DOC.parents[1] / "deploy" / "examples"
KINDS = {
    "status": re.compile(r"\bstatus\b", re.I),
    "backup": re.compile(r"\bback[\s-]?up", re.I),
    "integrity": re.compile(r"quick_check|integrity", re.I),
}


@dataclass
class Command:
    text: str
    comment: str
    paragraph: str
    section: str


def _lines() -> list[str]:
    assert DOC.is_file(), f"{DOC} is missing"
    return DOC.read_text().splitlines()


def _blocks(lines):
    """(code lines, the prose paragraph just before the block in its section, section title)
    for every fenced block."""
    out, inside, code, section, prose, last = [], False, [], "", [], []
    for line in lines:
        if FENCE.match(line):
            if inside:
                out.append((code, " ".join(prose or last), section))
                code, prose, last = [], [], []
            inside = not inside
            continue
        if inside:
            code.append(line)
            continue
        heading = HEADING.match(line)
        if heading:
            section, prose, last = heading.group(2), [], []
        elif not line.strip():
            if prose:
                last, prose = prose, []
        else:
            prose.append(line.strip())
    return out


def _arrays(blocks) -> dict[str, str]:
    arrays = {}
    for code, _, _ in blocks:
        for command in _split(code):
            match = ARRAY.match(command[0])
            if match:
                arrays[match.group(1)] = match.group(2)
    return arrays


def _split(code) -> list[tuple[str, str]]:
    """Logical commands: (text including any heredoc body, trailing comment of its first line)."""
    commands, index = [], 0
    while index < len(code):
        line = code[index]
        index += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        text = line
        while text.rstrip().endswith("\\") and index < len(code):
            text = text.rstrip()[:-1] + " " + code[index].strip()
            index += 1
        # Meet (Coordinator): a single-quoted argument that spans lines (`python3 -c '...'`)
        # is one logical command, like a backslash continuation.
        while text.count("'") % 2 == 1 and index < len(code):
            text += "\n" + code[index]
            index += 1
        # A multi-line array assignment `name=(... \n ...)`.
        if re.match(r"^\s*[A-Za-z_][A-Za-z0-9_]*=\(", text) and text.count("(") > text.count(")"):
            while index < len(code) and text.count("(") > text.count(")"):
                text += " " + code[index].strip()
                index += 1
        comment = ""
        match = re.search(r"\s#\s?(.*)$", text)
        if match and text.count("'", 0, match.start()) % 2 == 0 and text.count('"', 0, match.start()) % 2 == 0:
            comment = match.group(1)
        heredoc = HEREDOC.search(text)
        if heredoc:
            terminator, body = heredoc.group(2), []
            while index < len(code) and code[index].strip() != terminator:
                body.append(code[index])
                index += 1
            index += 1
            text = text + "\n" + "\n".join(body)
        commands.append((text, comment))
    return commands


def commands() -> list[Command]:
    blocks = _blocks(_lines())
    arrays = _arrays(blocks)
    out = []
    for code, paragraph, section in blocks:
        for text, comment in _split(code):
            for name, value in arrays.items():
                text = re.sub(r'"?\$\{' + re.escape(name) + r'\[@\]\}"?', lambda _m, v=value: v, text)
            out.append(Command(text, comment, paragraph, section))
    return out


def units(lines=None) -> list[str]:
    """Prose paragraphs, list items and table rows outside code; code comments too."""
    lines = _lines() if lines is None else lines
    out, current, inside = [], [], False
    for line in lines:
        if FENCE.match(line):
            inside = not inside
            if current:
                out.append(" ".join(current))
                current = []
            continue
        if inside:
            if "#" in line:
                out.append(line.strip())
            continue
        stripped = line.strip()
        starts_item = bool(re.match(r"^([-*+]|\d+[.)])\s", stripped)) or stripped.startswith("|")
        if not stripped or HEADING.match(line) or starts_item:
            if current:
                out.append(" ".join(current))
            current = [stripped] if stripped and not HEADING.match(line) else []
            continue
        current.append(stripped)
    if current:
        out.append(" ".join(current))
    return out


def _slug(title: str) -> str:
    slug = re.sub(r"[^\w\- ]", "", title.strip().lower().replace("`", ""))
    return slug.replace(" ", "-")


def _sections(lines):
    """{slug: (title, lines)} for every heading."""
    out, title, body = {}, None, []
    for line in lines:
        heading = HEADING.match(line)
        if heading:
            if title is not None:
                out.setdefault(_slug(title), (title, body))
            title, body = heading.group(2), []
        elif title is not None:
            body.append(line)
    if title is not None:
        out.setdefault(_slug(title), (title, body))
    return out


ONE_OFF = re.compile(r"\bdocker\s+compose\b(?:(?!\s(?:\||&&|;)\s).)*?\srun\b|\bdocker\s+run\b", re.S)


def _store_command(command: Command, kind: str) -> bool:
    if not ONE_OFF.search(command.text) or not re.search(r"memory", command.text, re.I):
        return False
    pattern = KINDS[kind]
    return any(pattern.search(part) for part in (command.text, command.comment, command.paragraph))


# ------------------------------------------------------------------- tests


def test_docker_md_states_store_files_are_reached_only_from_inside_the_runtime():
    """GREEN-IF one unit names the store (`state/memory` or the memory store) and says its files are reached
    only from inside the runtime, or that the host must not open them."""
    found = [unit for unit in units()
             if re.search(r"state/memory|memory store|store files", unit, re.I)
             and ((re.search(r"\bonly\b", unit, re.I) and re.search(r"inside|within|in the runtime|container", unit, re.I))
                  or (re.search(r"\bhost\b", unit, re.I) and re.search(r"\b(never|not|no)\b|n't", unit, re.I)))]
    assert found, ("docs/DOCKER.md does not state that the Docker runtime's store files are reached only from "
                   "inside the runtime")


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_docker_md_documents_store_checks_as_one_off_compose_run(kind):
    """GREEN-IF a fenced `docker compose ... run` command on the store is documented for `kind`."""
    found = [c.text for c in commands() if _store_command(c, kind)]
    assert found, (f"docs/DOCKER.md documents no one-off `docker compose ... run` command for the store's "
                   f"{kind} (a fenced command that names the store, with {KINDS[kind].pattern!r} in its context)")


def test_no_documented_step_reaches_a_store_from_the_host():
    """GREEN-IF no fenced command names a store directory by a host path."""
    host_steps = [c.text for c in commands() if HOST_STORE.search(c.text)]
    assert not host_steps, "documented steps reach a store from the host:\n" + "\n---\n".join(host_steps)


def test_docker_md_warns_that_down_v_deletes_the_store_volume():
    """GREEN-IF one paragraph, item, row or code comment names `down -v`, a deletion and the volume."""
    found = [unit for unit in units()
             if re.search(r"\bdown\s+(?:-v\b|--volumes\b)", unit)
             and re.search(r"delet|remov|destroy|eras|wipe|\blos[et]", unit, re.I)
             and re.search(r"volume|store|memory", unit, re.I)]
    assert found, "docs/DOCKER.md does not warn that `docker compose down -v` deletes the store volume"


def _units_with(*patterns) -> list[str]:
    return [unit for unit in units() if all(re.search(p, unit, re.I) for p in patterns)]


def test_docker_md_states_when_memory_migrated_may_be_removed():
    """GREEN-IF one unit names the renamed `.migrated-` directory, `verify`, a backup and its removal."""
    found = _units_with(r"\.migrated-|memory\.migrated", r"\bverify\b", r"back[\s-]?up", r"\b(remov|delet|rm\b)")
    assert found, ("docs/DOCKER.md does not say when the renamed memory.migrated-<...> directory may be removed "
                   "(after verify passes and an in-runtime backup completed)")


def test_docker_md_lists_the_locks_that_stay_on_binds_as_known_limits():
    """GREEN-IF `.refresh.lock`, `/state/knowledge` and the assistant binding lock are each named in a unit
    that mentions locks, and the open-time refusal (T11) in a unit that mentions refusal."""
    missing = [name for name, pattern in ((".refresh.lock", r"\.refresh\.lock"),
                                          ("/state/knowledge", r"/state/knowledge"),
                                          ("assistant binding lock", r"assistant[\s_-]*binding"))
               if not _units_with(pattern, r"\block")]
    if not _units_with(r"\bT11\b|open[- ]time", r"refus"):
        missing.append("the open-time refusal (T11)")
    assert not missing, f"docs/DOCKER.md does not list these known limits as out of scope: {missing}"


def test_docker_md_documents_the_recovery_of_a_damaged_store_database():
    """GREEN-IF one unit names a damaged store database (quick_check, damaged, corrupt or malformed) and
    its recovery (recover, restore or repair)."""
    found = _units_with(r"quick_check|damaged|corrupt|malformed", r"recover|restore|repair",
                        r"database|sqlite|store")
    assert found, "docs/DOCKER.md documents no recovery for a store database that fails quick_check"


def _json_values(text: str, keys) -> list[tuple[str, str]]:
    pattern = r'"(' + "|".join(keys) + r')"\s*:\s*"([^"]*)"'
    return re.findall(pattern, text)


def _example_values(keys) -> list[tuple[str, str, str]]:
    found = []
    for path in sorted(EXAMPLES.glob("*.json")):
        found += [(path.name, key, value) for key, value in _json_values(path.read_text(), keys)]
    return found


def _under(value: str, root: str) -> bool:
    if not value.startswith("/"):
        return True  # a shell variable or a placeholder, not a path
    normal = PurePosixPath(posixpath.normpath(value.split("<")[0] or "/"))
    return normal.is_relative_to(root)


def test_documented_and_example_store_paths_are_under_state_memory():
    """GREEN-IF every documented and example `state_root`/`roster_path` resolves under /state/memory."""
    code = "\n".join(c.text for c in commands())
    stores = [("DOCKER.md", k, v) for k, v in _json_values(code, ("state_root", "roster_path"))]
    stores += _example_values(("state_root", "roster_path"))
    assert stores, "precondition: docs/DOCKER.md and deploy/examples document store paths"
    outside = [row for row in stores if not _under(row[2], "/state/memory")]
    assert not outside, f"documented or example store paths outside /state/memory: {outside}"


def test_documented_operator_written_files_are_under_config():
    """GREEN-IF every documented and example `config_template`, `catalog_path` and `assistant_policy_path`
    is under /config (Amendment 2: "Operator-written files live under /config, never on the volume")."""
    code = "\n".join(c.text for c in commands())
    operator = [("DOCKER.md", k, v) for k, v in _json_values(code, ("config_template", "catalog_path",
                                                                    "assistant_policy_path"))]
    operator += _example_values(("config_template", "catalog_path", "assistant_policy_path"))
    assert operator, "precondition: docs/DOCKER.md and deploy/examples document operator-written files"
    misplaced = [row for row in operator if not _under(row[2], "/config")]
    assert not misplaced, f"documented or example operator-written files outside /config: {misplaced}"


WRITES = re.compile(r"\bmkdir\b|>\s*\S|\btee\b|\bcp\b|\bmv\b|\btouch\b|\binstall\b|initialize")


def test_docker_md_first_run_store_steps_run_inside_the_runtime():
    """GREEN-IF, in the first-run section, every command whose own line (or heredoc body) writes a path
    under /state/memory is a `docker compose ... run --rm -T <role> sh -c '...'` command, or runs a console
    script in a role container (`docker exec`); and no first-run command names a store by a host path."""
    first = [c for c in commands() if re.search(r"first run", c.section, re.I)]
    assert first, "docs/DOCKER.md has no first-run section"
    bad = []
    for command in first:
        head, _, body = command.text.partition("\n")
        in_runtime = (COMPOSE_RUN.search(head) and "--rm" in head and re.search(r"\s-T\b", head)
                      and re.search(r"\bsh\s+-c\b", head)) or re.search(r"\bdocker\s+exec\b", head)
        writes_store = any(re.search(r"/state/memory", part) and WRITES.search(part) for part in (head, body))
        if HOST_STORE.search(command.text) or (writes_store and not in_runtime):
            bad.append(command.text)
    assert not bad, "first-run steps that write a store outside the runtime:\n" + "\n---\n".join(bad)


SEQUENCE = (("stop writers", r"docker compose\b[^\n;`]*?\bstop\b|\bstop the writers\b"),
            ("plan", r"kp-agent-install plan\b|`plan`"),
            ("apply", r"kp-agent-install apply\b|`apply`"),
            ("prepare", r"kp-agent-install prepare\b|`prepare`"),
            ("operator file edits", r"state_root|roster_path"),
            ("up", r"docker compose\b[^\n;`]*?\bup\b|`up -d`|`up`"),
            ("verify", r"kp-agent-install verify\b|`verify`"))


def test_docker_md_upgrade_follows_the_operator_sequence():
    """GREEN-IF, in the Upgrade section, the steps appear in the order: stop writers; plan; apply; prepare;
    operator file edits; up; verify."""
    sections = _sections(_lines())
    upgrade = [body for slug, (title, body) in sections.items() if re.match(r"upgrade\b", slug)]
    assert upgrade, "docs/DOCKER.md has no Upgrade section"
    text = "\n".join(upgrade[0])
    position, previous = -1, "the start"
    for step, pattern in SEQUENCE:
        match = re.compile(pattern, re.I).search(text, position + 1)
        assert match, (f"the Upgrade section does not state `{step}` after `{previous}` "
                       f"(sequence: {[name for name, _ in SEQUENCE]})")
        position, previous = match.start(), step


def test_docker_md_first_run_prepares_before_up():
    """GREEN-IF the first run runs `kp-agent-install prepare` after its apply and before its first `up`."""
    first = [c.text for c in commands() if re.search(r"first run", c.section, re.I)]
    text = "\n".join(first)
    apply, prepare = text.find("kp-agent-install apply"), text.find("kp-agent-install prepare")
    up = [m.start() for m in re.finditer(r"docker compose\b[^\n]*\bup\b", text)]
    assert apply >= 0 and prepare > apply and up and up[0] > prepare, (
        "the first run does not run `kp-agent-install prepare` between apply and up")


def test_docker_md_says_earlier_sessions_must_be_relaunched():
    """GREEN-IF one unit says that sessions launched before a migration must be relaunched."""
    found = _units_with(r"relaunch|launch\w*\s+(them\s+)?again", r"session", r"migrat|upgrade|before")
    assert found, "docs/DOCKER.md does not say that sessions launched before the migration must be relaunched"
