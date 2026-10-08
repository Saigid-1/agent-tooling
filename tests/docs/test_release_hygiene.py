"""Order D0a-1 (docs/work/orders/D0a-1-release-hygiene.md): release hygiene, H2 to H6.

H2  The root LICENSE is the verbatim GNU AGPL-3.0 text (sha256 pinned below).
H3  NOTICE names the Kanban fork point by its upstream commit, and both agents-image
    CLIs at the versions deploy/Dockerfile pins, with their npm licence fields.
H4  CONTRIBUTING.md embeds the Developer Certificate of Origin 1.1 byte for byte
    (sha256 pinned below), states the `git commit -s` rule, and no CLA exists.
H5  Both Python packages declare `license = "AGPL-3.0-only"` (PEP 639); the Kanban
    fork keeps its Apache-2.0 package.json licence.
H6  The tree falsifier. No tracked file (`git ls-files`) carries a private value of
    these classes. The test matches CLASSES, never literal names, and this file
    contains none of the names it forbids:
      DRIVE    a drive mount, `/Volumes/<name>`;
      HOME     a home directory, `/Users/<name>` or `/home/<name>`;
      EMAIL    a personal email address, `<local-part>@<domain>`;
      SESSION  a session id of the `local_<8 hex>-<4 hex>-...` shape.
    `<name>` starts with a letter, digit or underscore, so a bare prefix (`/Volumes/`)
    and a placeholder (`/Users/<name>`, `/Volumes/...`) are not findings.
    EMAIL does not count, as a class:
      - a domain reserved for examples (RFC 2606 and RFC 6761): example.com,
        example.net, example.org, and any domain under .example, .invalid, .test
        or .localhost;
      - a Git remote's service account, `git@<host>:` or `git@<host>/`.
    The project's own public addresses would be listed in PROJECT_ADDRESSES after
    review; there are none today.

Every finding the tree carries today is allowlisted below, with a reason:
  - GUARD_SITES: the seven sites that refuse `/Volumes/` paths by design (the path scrub
    kept them), pinned by file and line. Each pinned line must still carry the marker.
  - PENDING_SITES: session ids that the public-readiness audit
    (docs/work/PUBLIC-READINESS-AUDIT.md) leaves to the Principal, pinned by file and
    line. D0a-3 applied the decisions and deleted every entry; the mechanism stays for
    a later pending decision.
  - ALLOWED_LINES: per file and class, the exact number of lines with a finding, for
    third-party content (the upstream Kanban files, npm registry metadata) and for
    placeholder paths in fixtures. One more line is a new finding; one fewer is a
    stale entry. Both fail.
Outside a Git checkout (for example a `git archive` export) the test reads every
file under the root instead of `git ls-files`.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

LICENSE_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"
LICENSE_BYTES = 34523
DCO_SHA256 = "f7ac75b443f4ca16b503241344b41aeff9503b0c30bedc2b119551d83cb0fa90"
DCO_BYTES = 1366
DCO_FIRST_LINE = b"Developer Certificate of Origin\nVersion 1.1\n"
KANBAN_FORK_POINT = "abd4912c27ce6b7f18b5a8106c145fd838e90cc4"
# The local branch head the fork was developed on; not an upstream commit. H3 keeps it as
# its guard value (a named D0a-3 exception: tests/d0a3_ruled_exceptions.py).
KANBAN_LOCAL_HEAD = "b8ea5a7a7cce947298d79f44651dff8bedd6e309"
PYPROJECTS = ("packages/tooling/pyproject.toml", "extensions/ops/pyproject.toml")

DRIVE, HOME, EMAIL, SESSION = "drive mount", "home directory", "personal email", "session id"

_DRIVE = re.compile(r"/Volumes/(?=\w)")
_HOME = re.compile(r"/(?:Users|home)/(?=\w)")
_EMAIL = re.compile(r"(?<![\w.%+-])([\w.%+-]+)@((?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,})"
                    r"(?![\w-])(.?)")
_SESSION = re.compile(r"local_[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-")
RESERVED_EMAIL_DOMAINS = frozenset({"example.com", "example.net", "example.org"})
RESERVED_EMAIL_TLDS = frozenset({"example", "invalid", "test", "localhost"})
PROJECT_ADDRESSES: frozenset[str] = frozenset()  # reviewed public project addresses; none yet

GUARD_MARKER = "/Volumes/"
GUARD_SITES = {
    ("extensions/ops/scripts/prepare_tooling_docker.py", 65):
        "refuses to create a state root under an unmounted macOS volume",
    ("tests/docs/test_doc_paths.py", 33):
        "T6b P1's forbidden-prefix list for the maintained docs",
    ("tests/docs/test_doc_paths.py", 235):
        "T6b's self-test feeds the guard one placeholder of each forbidden prefix",
    ("tests/host/test_t4_p5_portability.py", 22):
        "T4 P5's docstring names the build-host markers the image must not carry",
    ("tests/host/test_t4_p5_portability.py", 85):
        "T4 P5's build-host marker list",
    ("tests/install/test_p4_rendered_isolation.py", 266):
        "S3 P4 refuses a rendered manifest value that carries a host path",
    ("tests/install/test_t9b_p1_p2_store_volume_image.py", 84):
        "T9b's docstring on Docker Desktop's VM-side prefix, stripped before comparing bind paths",
}
GUARD_CLASSES = frozenset({DRIVE, HOME})

PENDING_SITES: dict[tuple[str, int], str] = {}
PENDING_CLASSES = frozenset({SESSION})

UPSTREAM = ("upstream Cline Kanban file, byte-identical to cline/kanban at the fork point: example "
            "home paths and upstream contributors' home paths, already public upstream")
PLACEHOLDER = "placeholder home path (`example`, `alice`, `operator`, `user`) in a test or an example"
ALLOWED_LINES = {
    (HOME, "apps/kanban/.plan/02-kanban-to-kanban-rename/inventory.md"): (2, UPSTREAM),
    (HOME, "apps/kanban/.plan/02-kanban-to-kanban-rename/plan.md"): (2, UPSTREAM),
    (HOME, "apps/kanban/.plan/desktop-5-way-split-handoff.md"): (3, UPSTREAM),
    (HOME, "apps/kanban/.plan/docs/cline-sdk-native-integration-plan.md"): (1, UPSTREAM),
    (HOME, "apps/kanban/.plan/docs/hooks-update/codex-hooks-research.md"): (3, UPSTREAM),
    (HOME, "apps/kanban/.plan/docs/hooks-update/hooks-update-research.md"): (3, UPSTREAM),
    (HOME, "apps/kanban/.plan/docs/kanban-terminal-gap-analysis.md"): (15, UPSTREAM),
    (HOME, "apps/kanban/.plan/docs/opencode-terminal-investigation-report.md"): (20, UPSTREAM),
    (HOME, "apps/kanban/packages/desktop/test/runtime-child-env.test.ts"): (2, UPSTREAM),
    (HOME, "apps/kanban/packages/desktop/test/window-registry.test.ts"): (2, UPSTREAM),
    (HOME, "apps/kanban/packages/desktop/test/window-state.test.ts"): (6, UPSTREAM),
    (HOME, "apps/kanban/test/runtime/append-system-prompt.test.ts"): (14, UPSTREAM),
    (HOME, "apps/kanban/test/runtime/cline-sdk/cline-task-session-service.test.ts"): (2, UPSTREAM),
    (HOME, "apps/kanban/test/runtime/graceful-shutdown.test.ts"): (1, UPSTREAM),
    (HOME, "apps/kanban/test/runtime/hooks-source-inference.test.ts"): (3, UPSTREAM),
    (HOME, "apps/kanban/test/runtime/terminal/codex-workspace-trust.test.ts"): (1, UPSTREAM),
    (HOME, "apps/kanban/test/runtime/terminal/opencode-paths.test.ts"): (6, UPSTREAM),
    (HOME, "apps/kanban/test/runtime/update/auto-update.test.ts"): (42, UPSTREAM),
    (HOME, "apps/kanban/test/runtime/workspace/git-clone.test.ts"): (7, UPSTREAM),
    (HOME, "apps/kanban/test/runtime/workspace/path-sandbox.test.ts"): (9, UPSTREAM),
    (HOME, "apps/kanban/web-ui/src/utils/file-url.test.ts"): (4, UPSTREAM),
    (HOME, "apps/kanban/web-ui/src/utils/server-path.test.ts"): (1, UPSTREAM),
    (HOME, "apps/kanban/test/runtime/launch/t3-launch-binding.test.ts"): (2, PLACEHOLDER),
    (HOME, "apps/kanban/test/runtime/terminal/agent-session-adapters.test.ts"): (3, PLACEHOLDER),
    (HOME, "apps/kanban/web-ui/src/components/board-card.test.tsx"): (1, PLACEHOLDER),
    (HOME, "docs/memory/ASSISTANT-UI-CAPTURE.md"): (1, PLACEHOLDER),
    (HOME, "tests/fixtures/t11a/generate_identity_corpus.py"): (1, PLACEHOLDER),
    (HOME, "tests/fixtures/t11a/identity_corpus.json"): (56, PLACEHOLDER + "; generated golden"),
    (EMAIL, "apps/kanban/test/utilities/git-env.ts"):
        (2, "upstream Cline Kanban test identity at a placeholder address; byte-identical upstream"),
    (EMAIL, "apps/kanban/packages/desktop/package-lock.json"):
        (1, "npm registry metadata: a third-party package's deprecation message names its maintainer"),
    (EMAIL, "deploy/image/toolchain/package-lock.json"):
        (1, "npm registry metadata: a third-party package's deprecation message names its maintainer"),
}

_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".pytest_cache", ".venv"})


def tracked_files(root: Path) -> list[str]:
    """`git ls-files` at root; every file under root when root is not a Git checkout."""
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True)
        if top.returncode == 0 and Path(top.stdout.strip()).resolve() == root.resolve():
            listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True)
            return [name.decode() for name in listed.stdout.split(b"\0") if name]
    except FileNotFoundError:  # no git executable
        pass
    found = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        found.extend(Path(directory, name).relative_to(root).as_posix() for name in files)
    return sorted(found)


def _personal_email(match: re.Match[str]) -> bool:
    local, domain, after = match.group(1), match.group(2).lower(), match.group(3)
    labels = domain.split(".")
    if labels[-1] in RESERVED_EMAIL_TLDS or ".".join(labels[-2:]) in RESERVED_EMAIL_DOMAINS:
        return False
    if local == "git" and after in (":", "/"):
        return False
    return f"{local}@{domain}" not in PROJECT_ADDRESSES


def line_classes(line: str) -> set[str]:
    found = set()
    if _DRIVE.search(line):
        found.add(DRIVE)
    if _HOME.search(line):
        found.add(HOME)
    if any(_personal_email(match) for match in _EMAIL.finditer(line)):
        found.add(EMAIL)
    if _SESSION.search(line):
        found.add(SESSION)
    return found


def _text(root: Path, name: str) -> str | None:
    path = root / name
    if path.is_symlink() and not path.exists():
        return None
    try:
        data = path.read_bytes()
    except (FileNotFoundError, IsADirectoryError):
        return None
    if b"\0" in data:
        return None  # binary
    return data.decode("utf-8", "replace")


def scan(root: Path) -> list[tuple[str, int, str]]:
    """Every (file, line, class) finding in the tracked files under root."""
    findings = []
    for name in tracked_files(root):
        text = _text(root, name)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            findings.extend((name, number, cls) for cls in sorted(line_classes(line)))
    return findings


def judge(findings: list[tuple[str, int, str]], root: Path) -> tuple[list[str], list[str]]:
    """(violations, stale allowlist entries) for the findings of the tree at root."""
    violations, stale = [], []
    counted: dict[tuple[str, str], list[int]] = collections.defaultdict(list)
    for name, number, cls in findings:
        if (name, number) in GUARD_SITES and cls in GUARD_CLASSES:
            continue
        if (name, number) in PENDING_SITES and cls in PENDING_CLASSES:
            continue
        counted[(cls, name)].append(number)
    for (cls, name), numbers in sorted(counted.items()):
        allowed = ALLOWED_LINES.get((cls, name), (0, ""))[0]
        if len(numbers) > allowed:
            violations.append(f"{name}: {cls} on {len(numbers)} line(s) {numbers}; {allowed} allowlisted")
    for (cls, name), (allowed, _reason) in sorted(ALLOWED_LINES.items()):
        if len(counted.get((cls, name), [])) < allowed:
            stale.append(f"{name}: {cls} allowlisted on {allowed} line(s), found on "
                         f"{len(counted.get((cls, name), []))}; lower the count")
    for (name, number), _reason in sorted(GUARD_SITES.items()):
        lines = (_text(root, name) or "").splitlines()
        if number > len(lines) or GUARD_MARKER not in lines[number - 1]:
            stale.append(f"{name}:{number}: guard site no longer carries {GUARD_MARKER!r}; re-pin it")
    found_at = {(name, number) for name, number, cls in findings if cls in PENDING_CLASSES}
    for site in sorted(PENDING_SITES):
        if site not in found_at:
            stale.append(f"{site[0]}:{site[1]}: pending session-id site has no finding; remove the entry")
    return violations, stale


# --------------------------------------------------------------------------------- H2


def test_license_is_the_pinned_agpl_text():
    data = (ROOT / "LICENSE").read_bytes()
    assert (len(data), hashlib.sha256(data).hexdigest()) == (LICENSE_BYTES, LICENSE_SHA256)


# --------------------------------------------------------------------------------- H3


def test_notice_names_the_upstream_fork_point_and_the_pinned_agent_clis():
    notice = (ROOT / "NOTICE").read_text()
    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text()
    pins = dict(re.findall(r"(?m)^ARG (CLAUDE_CODE_VERSION|CODEX_VERSION)=(\S+)$", dockerfile))
    assert set(pins) == {"CLAUDE_CODE_VERSION", "CODEX_VERSION"}, pins
    assert KANBAN_FORK_POINT in notice
    assert KANBAN_LOCAL_HEAD not in notice and KANBAN_LOCAL_HEAD[:8] not in notice
    assert f"@openai/codex {pins['CODEX_VERSION']}" in notice
    assert f"@anthropic-ai/claude-code {pins['CLAUDE_CODE_VERSION']}" in notice
    assert '"Apache-2.0"' in notice and '"SEE LICENSE IN README.md"' in notice
    for component in ("Cline Kanban", "SCIP", "Serena", "OpenTelemetry", "Grafana Tempo"):
        assert component in notice, component


# --------------------------------------------------------------------------------- H4


def test_contributing_embeds_the_pinned_dco_and_the_sign_off_rule():
    data = (ROOT / "CONTRIBUTING.md").read_bytes()
    start = data.find(DCO_FIRST_LINE)
    assert start >= 0, "CONTRIBUTING.md does not embed the DCO text"
    embedded = data[start:start + DCO_BYTES]
    assert hashlib.sha256(embedded).hexdigest() == DCO_SHA256
    text = data.decode()
    assert "git commit -s" in text and "Signed-off-by:" in text
    assert "There is no Contributor License Agreement" in text
    clas = [name for name in tracked_files(ROOT) if re.fullmatch(r"(?i)cla(\.\w+)?", Path(name).name)]
    assert not clas, clas


# --------------------------------------------------------------------------------- H5


def test_python_packages_declare_agpl_only_and_kanban_keeps_apache():
    for name in PYPROJECTS:
        project = tomllib.loads((ROOT / name).read_text())["project"]
        assert project.get("license") == "AGPL-3.0-only", name
    kanban = json.loads((ROOT / "apps" / "kanban" / "package.json").read_text())
    assert kanban.get("license") == "Apache-2.0"


# --------------------------------------------------------------------------------- H6


def test_no_tracked_file_carries_a_private_path_email_or_session_id():
    violations, _stale = judge(scan(ROOT), ROOT)
    assert not violations, "\n".join(violations)


def test_allowlist_has_no_stale_entry():
    _violations, stale = judge(scan(ROOT), ROOT)
    assert not stale, "\n".join(stale)


def test_each_class_matches_its_shape_and_not_its_placeholders():
    # The probes are assembled at run time so that this file contains none of them.
    sep, at = "/", "@"
    hits = {
        sep.join(["", "Volumes", "Probe Drive", "x"]): DRIVE,
        sep.join(["", "Users", "probe", "x"]): HOME,
        sep.join(["", "home", "probe"]): HOME,
        "probe.person" + at + "mail-provider.com": EMAIL,
        "local_" + "0123abcd" + "-" + "4567" + "-89ab-cdef": SESSION,
    }
    for probe, cls in hits.items():
        assert line_classes(f"a {probe} b") == {cls}, probe
    misses = [
        sep.join(["", "Volumes", ""]), sep.join(["", "Users", "<name>"]), sep.join(["", "Volumes", "..."]),
        "jane" + at + "example.com", "ci" + at + "build.invalid", "git" + at + "github.com:org/repo.git",
        "codex" + at + "0.159.2", "local_" + "0123abcd",
    ]
    for probe in misses:
        assert line_classes(f"a {probe} b") == set(), probe
