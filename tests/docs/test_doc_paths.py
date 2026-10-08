"""Order T6b (docs/work/orders/T6b-docs-sweep.md): P1 and P3 of the documentation sweep.

P1, no stale paths in maintained docs. Maintained docs are `README.md`, every `*.md`
under `docs/` except `docs/adr/` and `docs/work/`, and
`packages/tooling/src/kp_agent_tooling/assets/*.md`. They name no host-specific
absolute path, no `portable_tooling/`, no `deploy/tooling/`, no
`deploy/kanban/Dockerfile`, no root `scripts/<file>` for a file that moved to
`extensions/ops/scripts/`, and every repository path they name exists.

P3, the ledger is accurate. Every repository path in `tests/EXTRACTED-TEST-LEDGER.md`
exists. The ledger also records paths in the OPS repository it was ported from; such a
path is written directly after the word `OPS` or `OPS-only` (for example
"OPS-only scripts/desk_memory_cli.py") and is not a path in this tree.

What counts as a named repository path:
- a Markdown link target without a URL scheme, resolved against the document's
  directory (a leading `/` means the repository root);
- any other token that starts with a top-level directory of this repository
  (`apps/`, `deploy/`, `tests/`, ...) and is not itself part of a longer path, URL,
  variable or qualified name. `*` in such a token is a glob and must match.
Existence is checked on the checkout, so docs added later are covered automatically.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "tests" / "EXTRACTED-TEST-LEDGER.md"
ASSET_DOCS = ROOT / "packages" / "tooling" / "src" / "kp_agent_tooling" / "assets"
EXTENSION_SCRIPTS = ROOT / "extensions" / "ops" / "scripts"

FORBIDDEN = ("/Volumes/", "/Users/", "portable_tooling/", "deploy/tooling/", "deploy/kanban/Dockerfile")

# Known top-level directories are listed as well as discovered, so that removing a whole
# directory cannot make references into it stop counting as repository paths.
KNOWN_TOP_LEVEL = {"apps", "config", "deploy", "docs", "examples", "extensions", "packages",
                   "records", "scripts", "tests", ".github"}


def _top_level() -> set[str]:
    found = {p.name for p in ROOT.iterdir() if p.is_dir() and p.name not in {".git"}}
    return (found | KNOWN_TOP_LEVEL) - {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}


_LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_IMAGE = re.compile(r"!\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_REFDEF = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*<?(\S+?)>?(?:\s+.*)?$", re.M)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
# A token may not continue a longer path, URL, variable, qualified name or host path.
_BEFORE = r"(?<![A-Za-z0-9_.~/$}@:\\-])"
_OPS_QUALIFIER = re.compile(r"\bOPS(?:-only)?\s+[`'\"]?$")


def _path_token(top: set[str]) -> re.Pattern[str]:
    names = "|".join(sorted((re.escape(t) for t in top), key=len, reverse=True))
    return re.compile(_BEFORE + r"(?:\./)?((?:" + names + r")/[A-Za-z0-9_.*/-]*)")


# A document-relative path written in text rather than as a link, e.g. `../records/x.md`.
_RELATIVE_TOKEN = re.compile(_BEFORE + r"((?:\.\./)+[A-Za-z0-9_.*-][A-Za-z0-9_.*/-]*)")


def maintained_docs() -> list[Path]:
    docs = [ROOT / "README.md"]
    for path in sorted((ROOT / "docs").rglob("*.md")):
        rel = path.relative_to(ROOT / "docs")
        if rel.parts[0] in {"adr", "work"}:
            continue
        docs.append(path)
    docs.extend(sorted(ASSET_DOCS.glob("*.md")))
    return docs


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _clean(token: str) -> str:
    token = token.rstrip(".,;:")
    while token.endswith("..."):
        token = token[:-3]
    return token


def _exists(rel: str) -> bool:
    rel = rel.rstrip("/") or "."
    if "*" in rel:
        return any(ROOT.glob(rel))
    target = (ROOT / rel)
    try:
        target.resolve().relative_to(ROOT)
    except ValueError:
        return False
    return target.exists()


def _from_doc(doc: Path, target: str) -> str:
    """Repository-relative form of a document-relative target ("../x" when it escapes)."""
    if target.startswith("/"):
        return target.lstrip("/")
    resolved = (doc.parent / target).resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return "../" + target


def named_paths(doc: Path, text: str | None = None, *, allow_ops: bool = False):
    """Yield (line number, as written, repository-relative path) for each named path."""
    text = doc.read_text(encoding="utf-8") if text is None else text
    token = _path_token(_top_level())
    for number, line in enumerate(text.splitlines(), start=1):
        scan = line
        for pattern in (_LINK, _IMAGE, _REFDEF):
            for match in pattern.finditer(line):
                # The target is checked here, relative to the document, and not again below.
                scan = scan[: match.start(1)] + " " * (match.end(1) - match.start(1)) + scan[match.end(1):]
                target = match.group(1).split("#", 1)[0]
                if target and not _SCHEME.match(target):
                    yield number, match.group(1), _from_doc(doc, target)
        for match in token.finditer(scan):
            written = _clean(match.group(1))
            if not written or written.count("/") == 0:
                continue
            if allow_ops and _OPS_QUALIFIER.search(scan[: match.start()]):
                continue
            yield number, written, written
        for match in _RELATIVE_TOKEN.finditer(scan):
            written = _clean(match.group(1))
            yield number, written, _from_doc(doc, written)


def missing_paths(doc: Path, text: str | None = None, *, allow_ops: bool = False) -> list[str]:
    return [f"{_rel(doc)}:{n}: {written}" for n, written, rel in named_paths(doc, text, allow_ops=allow_ops)
            if not _exists(rel)]


def forbidden_hits(doc: Path, text: str | None = None) -> list[str]:
    text = doc.read_text(encoding="utf-8") if text is None else text
    return [f"{_rel(doc)}:{number}: {needle}"
            for number, line in enumerate(text.splitlines(), start=1)
            for needle in FORBIDDEN if needle in line]


def moved_script_hits(doc: Path, text: str | None = None) -> list[str]:
    """Root `scripts/<file>` references to a file that now lives in extensions/ops/scripts/."""
    text = doc.read_text(encoding="utf-8") if text is None else text
    moved = {p.name for p in EXTENSION_SCRIPTS.iterdir() if p.is_file()}
    token = re.compile(_BEFORE + r"(?:\./)?scripts/([A-Za-z0-9_.-]+)")
    return [f"{_rel(doc)}:{number}: scripts/{name} (now extensions/ops/scripts/{name})"
            for number, line in enumerate(text.splitlines(), start=1)
            for name in (_clean(m.group(1)) for m in token.finditer(line))
            if name in moved and not (ROOT / "scripts" / name).exists()]


def unmoved_ledger_hits(doc: Path, text: str | None = None) -> list[str]:
    """`tests/<file>` rows for a test file that now lives in extensions/ops/tests/."""
    moved = {p.name for p in (ROOT / "extensions" / "ops" / "tests").iterdir() if p.is_file()}
    found = []
    for number, written, _ in named_paths(doc, text, allow_ops=True):
        head, _, name = written.partition("tests/")
        if head == "" and "/" not in name and name in moved and not (ROOT / "tests" / name).exists():
            found.append(f"{_rel(doc)}:{number}: {written} (now extensions/ops/tests/{name})")
    return found


# ---------------------------------------------------------------------------- P1


def test_p1_scope_is_the_maintained_docs():
    docs = {_rel(d) for d in maintained_docs()}
    assert "README.md" in docs
    assert any(d.startswith("docs/") for d in docs)
    assert any(d.startswith("packages/tooling/src/kp_agent_tooling/assets/") for d in docs)
    assert not any(d.startswith(("docs/adr/", "docs/work/", "records/")) for d in docs)


def test_p1_no_host_paths_or_retired_locations():
    found = [hit for doc in maintained_docs() for hit in forbidden_hits(doc)]
    assert found == [], "\n".join(found)


def test_p1_no_root_scripts_path_for_a_moved_script():
    found = [hit for doc in maintained_docs() for hit in moved_script_hits(doc)]
    assert found == [], "\n".join(found)


def test_p1_every_named_repository_path_exists():
    found = [hit for doc in maintained_docs() for hit in missing_paths(doc)]
    assert found == [], "\n".join(found)


# ---------------------------------------------------------------------------- P3


def test_p3_every_ledger_path_exists():
    found = missing_paths(LEDGER, allow_ops=True)
    assert found == [], "\n".join(found)


def test_p3_ledger_names_moved_tests_at_their_new_location():
    found = unmoved_ledger_hits(LEDGER)
    assert found == [], "\n".join(found)


# ------------------------------------------------------------ checker self-tests
# Each check must be able to fail: a probe document (never written to disk) plants one
# instance of every stale-path kind and the checks must report exactly those.

PROBE = ROOT / "docs" / "_t6b_probe.md"


def _written(rows: list[str]) -> list[str]:
    return sorted(row.split(": ", 1)[1] for row in rows)


def test_checker_reports_missing_named_paths():
    text = "\n".join([
        "Run `python scripts/does_not_exist.py` and see [x](../no/such/file.md).",
        "Relative [ok](CUTOVER.md) and missing [gone](GONE.md) and [anchor](#here).",
        "Glob tests/test_*.py matches; glob tests/nothing_*.zz does not.",
        "Not repository paths: /workspace/tests/x, $STATE/records/y, https://x.test/docs/z,",
        "<root>/config/navigation.json, kp_agent_tooling.tests/x and extensions/ops/scripts/.",
        "OPS-only scripts/desk_memory_cli.py is an OPS path.",
        "Text-relative ../README.md exists; ../../outside/x.md and ../gone/y.md do not.",
    ])
    expected = ["scripts/does_not_exist.py", "../no/such/file.md", "GONE.md", "tests/nothing_*.zz",
                "../../outside/x.md", "../gone/y.md"]
    assert _written(missing_paths(PROBE, text)) == sorted(expected + ["scripts/desk_memory_cli.py"])
    assert _written(missing_paths(PROBE, text, allow_ops=True)) == sorted(expected)


def test_checker_reports_forbidden_locations():
    text = "\n".join(["a /Volumes/X b", "c /Users/y d", "portable_tooling/setup.py", "deploy/tooling/image",
                      "deploy/kanban/Dockerfile", "deploy/kanban/ alone and deploy/compose.yaml are fine"])
    assert _written(forbidden_hits(PROBE, text)) == sorted(FORBIDDEN)


def test_checker_reports_moved_scripts_and_unmoved_ledger_rows():
    moved_script = sorted(p.name for p in EXTENSION_SCRIPTS.glob("*.py")
                          if not (ROOT / "scripts" / p.name).exists())[0]
    moved_test = sorted(p.name for p in (ROOT / "extensions" / "ops" / "tests").glob("test_*.py"))[0]
    kept_test = sorted(p.name for p in (ROOT / "tests").glob("test_*.py"))[0]
    text = "\n".join([f"python scripts/{moved_script}", f"python extensions/ops/scripts/{moved_script}",
                      f"ported: tests/{moved_test}", f"ported: extensions/ops/tests/{moved_test}",
                      f"ported: tests/{kept_test}"])
    assert _written(moved_script_hits(PROBE, text)) == [
        f"scripts/{moved_script} (now extensions/ops/scripts/{moved_script})"]
    assert _written(unmoved_ledger_hits(PROBE, text)) == [
        f"tests/{moved_test} (now extensions/ops/tests/{moved_test})"]
