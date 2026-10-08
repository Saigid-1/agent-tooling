"""O3 Q3: the OpenCode CLI's licence is recorded, in NOTICE and in the public-readiness audit, first.

Order: docs/work/orders/O3-opencode-optional-image.md, Q3: "Before the image is built, the CLI's npm
package name and licence go into NOTICE and the public-readiness audit (one row)." The licence must be
MIT or Apache-2.0 (otherwise STOP: the Principal decides and the image is not built). Falsifier: "NOTICE
or the audit lacks the CLI's licence row, or names a licence the npm metadata and the upstream LICENSE
do not both state." At the order's base neither file names the CLI's package (census section 8: the
repository names only the `opencode` binary, its GitHub URL and the board's `@opencode-ai/sdk`).

Extension (the Coordinator, 2026-10-06, after the maintainer's licence decision): the OpenCode binary
packages ship under the upstream MIT LICENSE at the pinned tag, and their npm metadata carries no licence
field. So NOTICE and the audit row must also state that basis for the platform binary packages: the
upstream LICENSE at the tag. The test asserts that the basis is stated, not its wording.

How the row is found, without assuming the package name:
- NOTICE: the first entry of the form `<npm package> <version>` (or `<package>@<version>`) whose package
  name contains `opencode` and is not one of the board's OpenCode packages that are not the CLI
  (`NOT_THE_CLI`); that is NOTICE's convention for every pinned npm component. The entry is its line plus
  the following lines indented deeper than it; its licence is the first licence identifier (`LICENCE`)
  after the version in it. The entry's paragraph is the run of non-blank lines around it (NOTICE's
  subsection for the component).
- The audit: the table rows (lines starting with `|`) of docs/work/PUBLIC-READINESS-AUDIT.md that name
  the same package as a whole token; a row's licence is the first licence identifier after the
  package's first mention in it.
- "The basis is stated" (`states_binary_basis`): the text names the platform binary packages (the word
  platform or binary, or a package name like `<package>-linux-…`), the upstream LICENSE file (the token
  `LICENSE`, outside npm's `SEE LICENSE IN` form) and the tag (the word tag, or a `v<version>` token).

What is not tested here: whether the upstream LICENSE at the tag really is MIT needs the network; the
image test test_o3_opencode_licence_text_image.py checks the text the image ships. The npm half is tested
against the committed lockfile, which records the registry's licence field
(`test_notice_row_matches_the_committed_npm_pin`); where that field is absent, as the decision records for
the binary packages, nothing contradicts NOTICE and the basis above carries the licence.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NOTICE = REPO_ROOT / "NOTICE"
AUDIT = REPO_ROOT / "docs" / "work" / "PUBLIC-READINESS-AUDIT.md"
# The committed install manifest of the new target (the order's write scope names this path).
PACKAGE_JSON = REPO_ROOT / "deploy" / "image" / "opencode" / "package.json"
PACKAGE_LOCK = REPO_ROOT / "deploy" / "image" / "opencode" / "package-lock.json"

ALLOWED = ("MIT", "Apache-2.0")
# OpenCode packages the board already depends on (census section 8); none of them is the CLI.
NOT_THE_CLI = frozenset({"@opencode-ai/sdk", "@opencode-ai/plugin", "ai-sdk-provider-opencode-sdk"})

_NPM_NAME = r"(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*"
_VERSION = r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
ENTRY = re.compile(
    rf"(?<![\w@/.-])(?P<package>{_NPM_NAME})(?:[ \t]+v?|@v?)(?P<version>{_VERSION})(?![\w.-]*\w)")
# SPDX identifiers and the npm "SEE LICENSE IN" form; the first one after the package is its licence.
LICENCE = re.compile(
    r"(?<![\w.-])(?:SEE LICENSE IN \S+|MIT-0|MIT|Apache-2\.0|ISC|0BSD|BSD-[234]-Clause(?:-[A-Za-z]+)?"
    r"|(?:A|L)?GPL-[0-9.]+(?:-only|-or-later|\+)?|MPL-[0-9.]+|Artistic-[0-9.]+|PSF-[0-9.]+|CC0-1\.0"
    r"|BUSL-[0-9.]+|SSPL-[0-9.]+|Elastic-[0-9.]+|Unlicense|UNLICENSED)(?!\w)")
# The basis for the binary packages: the upstream LICENSE file at the tag.
UPSTREAM_LICENSE_FILE = re.compile(r"(?<!SEE )(?<!IN )(?<![\w.-])LICENSE(?![\w-]| IN )")
TAG = re.compile(r"\btag\b|(?<![\w.])v\d+\.\d+\.\d+", re.IGNORECASE)


def _platform_packages(package: str) -> re.Pattern:
    stem = re.escape(package.rsplit("/", 1)[-1].removesuffix("-ai"))
    return re.compile(rf"platform|binar|\b{stem}(?:-ai)?-(?:linux|darwin|windows|win32)", re.IGNORECASE)


def states_binary_basis(text: str, package: str) -> list[str]:
    """What `text` leaves out of the binary packages' licence basis (empty when it states it)."""
    missing = []
    if not _platform_packages(package).search(text):
        missing.append("the platform binary packages")
    if not UPSTREAM_LICENSE_FILE.search(text):
        missing.append("the upstream LICENSE file")
    if not TAG.search(text):
        missing.append("the upstream tag")
    return missing


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def notice_entry() -> tuple[str, str, str, str, str]:
    """NOTICE's OpenCode CLI entry: (package, version, licence or '', the entry's text, its paragraph)."""
    lines = NOTICE.read_text().splitlines()
    for index, line in enumerate(lines):
        for match in ENTRY.finditer(line):
            package = match.group("package")
            if "opencode" not in package.lower() or package in NOT_THE_CLI:
                continue
            block = [line[match.end():]]
            for following in lines[index + 1:]:
                if not following.strip() or _indent(following) <= _indent(line):
                    break
                block.append(following)
            licence = LICENCE.search("\n".join(block))
            start, end = index, index
            while start > 0 and lines[start - 1].strip():
                start -= 1
            while end + 1 < len(lines) and lines[end + 1].strip():
                end += 1
            entry = line + "\n" + "\n".join(block[1:])
            return (package, match.group("version"), licence.group(0) if licence else "", entry,
                    "\n".join(lines[start:end + 1]))
    raise AssertionError(
        "NOTICE names no OpenCode CLI package with a version (an entry `<npm package> <version>` whose "
        f"name contains 'opencode', other than {sorted(NOT_THE_CLI)}): the licence row is missing")


def _mentions(package: str) -> re.Pattern:
    return re.compile(rf"(?<![\w@/.-]){re.escape(package)}(?![\w/-])")


def audit_rows(package: str) -> list[tuple[str, str]]:
    """Each audit table row naming the package: (row, the first licence identifier after it or '')."""
    mention = _mentions(package)
    rows = []
    for line in AUDIT.read_text().splitlines():
        if not line.lstrip().startswith("|"):
            continue
        found = mention.search(line)
        if found:
            licence = LICENCE.search(line, found.end())
            rows.append((line, licence.group(0) if licence else ""))
    return rows


def test_notice_and_audit_agree_on_the_opencode_cli_licence():
    """GREEN-IF NOTICE has an entry naming the OpenCode CLI's npm package with a version and a licence in
    {MIT, Apache-2.0}, and the audit has at least one table row naming the same package, every such row
    stating the same licence."""
    package, version, licence, entry, _ = notice_entry()
    assert version, f"NOTICE's entry for {package} states no version:\n{entry}"
    assert licence, f"NOTICE's entry for {package} {version} states no licence:\n{entry}"
    assert licence in ALLOWED, (
        f"NOTICE records {package} {version} under {licence!r}; Q3 allows only {ALLOWED} (otherwise STOP: "
        f"the Principal decides):\n{entry}")
    rows = audit_rows(package)
    assert rows, (f"docs/work/PUBLIC-READINESS-AUDIT.md has no table row naming {package}; NOTICE has it "
                  f"under {licence}")
    disagreeing = [(row, stated) for row, stated in rows if stated != licence]
    assert not disagreeing, (
        f"NOTICE records {package} {version} under {licence!r}; the audit row(s) state otherwise:\n"
        + "\n".join(f"  {stated or '(no licence)'}: {row[:400]}" for row, stated in disagreeing))


def test_notice_and_audit_state_the_binary_packages_licence_basis():
    """GREEN-IF NOTICE's paragraph for the OpenCode CLI entry, and at least one audit row naming the same
    package, each state the licence basis of the platform binary packages: they name those packages, the
    upstream LICENSE file and the tag (`states_binary_basis`; the wording is free)."""
    package, version, _, _, paragraph = notice_entry()
    missing = states_binary_basis(paragraph, package)
    assert not missing, (f"NOTICE's paragraph for {package} {version} does not state the binary packages' "
                         f"licence basis; it lacks {missing}:\n{paragraph}")
    rows = [row for row, _ in audit_rows(package)]
    assert rows, f"docs/work/PUBLIC-READINESS-AUDIT.md has no table row naming {package}"
    stating = [row for row in rows if not states_binary_basis(row, package)]
    assert stating, ("no audit row naming " + package + " states the binary packages' licence basis:\n"
                     + "\n".join(f"  lacks {states_binary_basis(row, package)}: {row[:400]}" for row in rows))


def test_notice_row_matches_the_committed_npm_pin():
    """GREEN-IF the package and version NOTICE records for the OpenCode CLI are the one dependency that
    deploy/image/opencode/package.json pins (exactly, no range), and the committed lockfile installs that
    package at that version with an npm licence field that is absent or states NOTICE's licence (the npm
    half of Q3: the metadata may be silent, as decided, but must not contradict)."""
    package, version, licence, _, _ = notice_entry()
    assert PACKAGE_JSON.is_file() and PACKAGE_LOCK.is_file(), (
        f"the new target's committed install manifest is missing ({PACKAGE_JSON.relative_to(REPO_ROOT)} "
        f"and {PACKAGE_LOCK.relative_to(REPO_ROOT)}), so NOTICE's {package} {version} pins nothing")
    manifest = json.loads(PACKAGE_JSON.read_text())
    dependencies = manifest.get("dependencies") or {}
    assert dependencies.get(package) == version, (
        f"NOTICE records {package} {version}; {PACKAGE_JSON.relative_to(REPO_ROOT)} pins "
        f"{dependencies!r}")
    lock = json.loads(PACKAGE_LOCK.read_text())
    installed = (lock.get("packages") or {}).get(f"node_modules/{package}") or {}
    assert installed.get("version") == version, (
        f"the lockfile installs {package} at {installed.get('version')!r}; NOTICE records {version}")
    field = installed.get("license")
    if field is not None:
        stated = LICENCE.findall(field) if isinstance(field, str) else []
        assert licence in stated, (
            f"the lockfile's npm licence field for {package} {version} is {field!r}; NOTICE records "
            f"{licence!r}, which the npm metadata contradicts")
