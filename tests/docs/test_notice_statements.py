"""Order D0a-3 (docs/work/orders/D0a-3-apply-public-readiness.md), falsifiers of A4, A5 and A6.

- A6: NOTICE names Serena's corresponding source, https://github.com/oraios/serena at the
  commit deploy/Dockerfile pins (read here, never hard-coded), with the licence as measured
  (GPL-3.0-or-later for serena-agent; SolidLSP MIT); it points the Debian packages at Debian's
  source archive and says how to read an image's versions (`dpkg-query -W`); and it says that
  claude-agent-sdk is not redistributed in any published image.
- A5: NOTICE, CONTRIBUTING.md and apps/kanban/NOTICE carry the copyright line.
- A4: apps/kanban/NOTICE states Apache-2.0 for the fork's own changes and carries the
  Apache-2.0 section 4(b) change notice, naming the upstream fork point and pointing to the
  delta manifest.
Text is compared with runs of whitespace collapsed, so re-wrapping a paragraph is not a change.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COPYRIGHT = "Copyright 2026 Andrew Wedding and the agent-tooling contributors"
FORK_POINT = "abd4912c27ce6b7f18b5a8106c145fd838e90cc4"


def _flat(path: Path) -> str:
    return re.sub(r"\s+", " ", path.read_text())


def _section(text: str, start: str, end: str) -> str:
    assert start in text, f"NOTICE has no {start!r} section"
    after = text[text.index(start):]
    return after[:after.index(end)] if end in after else after


def _serena_pin() -> str:
    dockerfile = (ROOT / "deploy" / "Dockerfile").read_text()
    pins = re.findall(r"serena-agent @ git\+https://github\.com/oraios/serena\.git@([0-9a-f]{40})", dockerfile)
    assert len(set(pins)) == 1, pins
    return pins[0]


def test_notice_names_serenas_source_at_the_dockerfile_pin_and_its_licence():
    pin = _serena_pin()
    serena = _section(_flat(ROOT / "NOTICE"), "Serena (", "SCIP indexers")
    assert re.search(r"corresponding source for the serena-agent package installed in the images is "
                     r"https://github\.com/oraios/serena at commit " + pin, serena), serena
    assert "GPL-3.0-or-later for the serena-agent package" in serena
    assert re.search(r"SolidLSP component[^.]* is MIT", serena)


def test_notice_points_the_debian_packages_at_debians_source_archive():
    debian = _section(_flat(ROOT / "NOTICE"), "Source of the Debian packages", "kp-agent-tooling runtime dependencies")
    assert re.search(r"https://(?:sources|snapshot)\.debian\.org/", debian), debian
    assert "`dpkg-query -W`" in debian


def test_notice_says_claude_agent_sdk_is_not_redistributed():
    assert ("@anthropic-ai/claude-agent-sdk is not redistributed in any published image"
            in _flat(ROOT / "NOTICE"))


def test_notice_contributing_and_kanban_notice_carry_the_copyright_line():
    for name in ("NOTICE", "CONTRIBUTING.md", "apps/kanban/NOTICE"):
        assert COPYRIGHT in _flat(ROOT / name), name


def test_kanban_notice_states_apache_for_the_forks_changes_and_the_4b_change_notice():
    kanban = _flat(ROOT / "apps" / "kanban" / "NOTICE")
    assert "(SPDX: Apache-2.0)" in kanban
    assert re.search(r"This repository's own changes to the upstream work, .{0,120}are also licensed under the "
                     r"Apache License, Version 2\.0", kanban), kanban
    notice = _section(kanban, "Change notice (Apache-2.0, section 4(b))", "\0")
    assert "docs/ops-local-delta-manifest.md" in notice
    assert (ROOT / "apps" / "kanban" / "docs" / "ops-local-delta-manifest.md").is_file()
    assert FORK_POINT in kanban
