"""T7a P5: the capture boundary is documented.

Order: docs/work/orders/T7a-role-readiness.md, P5.
DOCKER.md's capture section and MEMORY.md state:
- workspace capture records unresolved evidence, which topic-scope search reaches;
- desk-attributed capture comes from launch hooks (T3) and host spool ingestion (T4).
Falsifier: a statement the code contradicts.

What the code does (cited, not re-tested here; these existing tests stay green):
- workspace capture registers its sources with `attribution_status: unresolved`
  (tests/test_workspace_capture_claude.py, test_claude_parent_control_child_and_replay);
- desk-scope `memory.search` excludes unresolved sources and reports
  `excluded_unresolved_episodes`, topic scope (`scope: "topic"`) includes them
  (tests/test_session_sources.py; packages/tooling/src/kp_agent_tooling/_impl/service/session_sources.py);
- the launch hook binds and captures under the desk (tests/launch/test_t3_p4_capture_follows_the_desk.py)
  and host spool ingestion applies the same hooks (tests/host/test_t4_p1_host_launch.py).

Readings (repeated in the arm report under AMBIGUITY):
- "DOCKER.md's capture section" is every part of docs/DOCKER.md whose heading, or
  whose bold run-in title (`**capture.**`), names capture; MEMORY.md is read whole;
- a statement is one paragraph (a blank-line-separated block, so a list counts as one)
  that carries all of its terms, so terms scattered over unrelated paragraphs do
  not count:
  - unresolved workspace capture: "workspace capture" (or `kp-agent-workspace-capture`),
    "unresolved", and "topic";
  - desk attribution: "desk", "launch", "hook" and "spool";
- the statement must not contradict the code: a paragraph that says workspace capture
  is desk-attributed (or bound to a desk) without saying it is unresolved fails.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "docs" / "DOCKER.md"
MEMORY = ROOT / "docs" / "MEMORY.md"

WORKSPACE_CAPTURE = re.compile(r"workspace[ -]capture|kp-agent-workspace-capture", re.I)


def paragraphs(text: str) -> list[str]:
    blocks, current, fenced = [], [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
        if not line.strip() and not fenced:
            if current:
                blocks.append("\n".join(current))
            current = []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_RUN_IN = re.compile(r"^\*\*([^*]+?)\.?\*\*")


def capture_section(text: str) -> str:
    """Text under any heading that names capture, plus any run-in titled paragraph group (`**capture.**`)."""
    out, keep, level, run_in = [], False, 7, False
    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            depth = len(heading.group(1))
            if keep and depth <= level:
                keep = False
            if not keep and re.search(r"capture", heading.group(2), re.I):
                keep, level = True, depth
            run_in = False
        else:
            title = _RUN_IN.match(line.strip())
            if title:
                run_in = re.search(r"capture", title.group(1), re.I) is not None
        if keep or run_in:
            out.append(line)
    return "\n".join(out)


def _unresolved_statements(text: str) -> list[str]:
    return [p for p in paragraphs(text)
            if WORKSPACE_CAPTURE.search(p) and re.search(r"unresolved", p, re.I) and re.search(r"\btopic\b", p, re.I)]


def _attribution_statements(text: str) -> list[str]:
    return [p for p in paragraphs(text)
            if all(re.search(term, p, re.I) for term in (r"\bdesk", r"\blaunch", r"\bhook", r"\bspool"))]


def _contradictions(text: str) -> list[str]:
    claims = re.compile(r"workspace[ -]capture[^.]*\b(desk[- ]attributed|attributed to (the|its|a) desk|"
                        r"bound to (the|its|a) desk|under (the|its) desk)", re.I)
    return [p for p in paragraphs(text) if claims.search(p) and not re.search(r"unresolved", p, re.I)]


DOCUMENTS = {
    "DOCKER.md capture section": lambda: capture_section(DOCKER.read_text()),
    "MEMORY.md": lambda: MEMORY.read_text(),
}


def test_docker_md_has_a_capture_section():
    """Precondition: the capture section exists, so the statements below are looked for in the right place."""
    section = capture_section(DOCKER.read_text())
    assert WORKSPACE_CAPTURE.search(section) or "capture" in section.lower(), (
        "docs/DOCKER.md has no section about the capture role")


@pytest.mark.parametrize("document", sorted(DOCUMENTS))
def test_workspace_capture_is_documented_as_unresolved_and_topic_reachable(document):
    """GREEN-IF one paragraph says workspace capture records unresolved evidence that topic-scope search reaches."""
    text = DOCUMENTS[document]()
    assert _unresolved_statements(text), (
        f"{document} has no paragraph stating that workspace capture records unresolved evidence "
        f"reached by topic-scope search (terms: workspace capture, unresolved, topic)")


@pytest.mark.parametrize("document", sorted(DOCUMENTS))
def test_desk_attributed_capture_is_documented_as_launch_hooks_and_spool_ingestion(document):
    """GREEN-IF one paragraph says desk-attributed capture comes from launch hooks and host spool ingestion."""
    text = DOCUMENTS[document]()
    assert _attribution_statements(text), (
        f"{document} has no paragraph stating that desk-attributed capture comes from launch hooks (T3) "
        f"and host spool ingestion (T4) (terms: desk, launch, hook, spool)")


@pytest.mark.parametrize("document", sorted(DOCUMENTS))
def test_no_statement_attributes_workspace_capture_to_a_desk(document):
    """GREEN-IF no paragraph claims workspace capture is desk-attributed without saying it is unresolved."""
    found = _contradictions(DOCUMENTS[document]())
    assert not found, f"{document} contradicts the code (workspace capture is unresolved): {found}"
