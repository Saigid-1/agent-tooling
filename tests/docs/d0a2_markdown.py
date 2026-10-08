"""Markdown reading for D0a-2's tests: headings, sections, fences, prose units, links and code.

No names live here (they are in d0a2_seams.py); this is the instrument. The readings it fixes:
- a heading is an ATX heading (`#` to `######`) outside a fenced code block;
- a section runs from its heading to the next heading of the same or a higher level, so it holds
  its subsections;
- a fence opens with three or more backticks or tildes (indented by up to three spaces, or inside
  a list item) and closes with the same character, at least as long;
- a prose unit is one paragraph, one list item, one heading or one table row outside the fences;
  a sentence ends at `.`, `!` or `?` followed by white space;
- normalising removes Markdown emphasis (`*`, `_` at a word's edge), code marks, link syntax (the
  link text stays) and collapses white space; `normal_stance` (F2) removes emphasis and line
  breaks only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_LINK = re.compile(r"(?<!!)\[([^\]\n]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_IMAGE = re.compile(r"!\[([^\]\n]*)\]\([^)]*\)")
_INLINE_CODE = re.compile(r"(`+)(.+?)\1", re.S)
_EMPHASIS = re.compile(r"\*+|(?<![A-Za-z0-9])_+|_+(?![A-Za-z0-9])")


@dataclass(frozen=True)
class Heading:
    line: int  # 0-based line index
    level: int
    title: str  # normalised text


@dataclass(frozen=True)
class Fence:
    start: int  # the opening fence line
    end: int  # the closing fence line (or the last line when unclosed)
    info: str
    body: str


def fences(lines: list[str]) -> list[Fence]:
    found, opening, start, info, body = [], None, 0, "", []
    for index, line in enumerate(lines):
        match = _FENCE.match(line)
        if opening is None:
            if match:
                opening, start, info, body = match.group(1), index, match.group(2).strip(), []
            continue
        stripped = line.strip()
        if match and stripped and set(stripped) == {opening[0]} and len(stripped) >= len(opening):
            found.append(Fence(start, index, info, "\n".join(body)))
            opening = None
        else:
            body.append(line)
    if opening is not None:
        found.append(Fence(start, len(lines) - 1, info, "\n".join(body)))
    return found


def fenced_lines(lines: list[str]) -> set[int]:
    inside = set()
    for fence in fences(lines):
        inside.update(range(fence.start, fence.end + 1))
    return inside


def headings(lines: list[str]) -> list[Heading]:
    inside = fenced_lines(lines)
    found = []
    for index, line in enumerate(lines):
        if index in inside:
            continue
        match = _HEADING.match(line)
        if match:
            found.append(Heading(index, len(match.group(1)), normal(match.group(2))))
    return found


def section_end(lines: list[str], heading: Heading) -> int:
    """The line index after the section that `heading` opens (its subsections included)."""
    for other in headings(lines):
        if other.line > heading.line and other.level <= heading.level:
            return other.line
    return len(lines)


def section_lines(lines: list[str], heading: Heading) -> list[str]:
    """The section's lines after its heading line."""
    return lines[heading.line + 1:section_end(lines, heading)]


def normal(text: str) -> str:
    """Link syntax to its text, code marks and emphasis removed, white space collapsed."""
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _INLINE_CODE.sub(lambda m: m.group(2), text)
    text = text.replace("`", "")
    text = _EMPHASIS.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def normal_stance(text: str) -> str:
    """F2's normalisation: Markdown emphasis and line breaks only (white space collapsed)."""
    return re.sub(r"\s+", " ", _EMPHASIS.sub("", text)).strip()


def prose_units(lines: list[str]) -> list[tuple[int, str]]:
    """(first line index, normalised text) of each paragraph, list item, heading and table row."""
    inside = fenced_lines(lines)
    units, current, first = [], [], 0

    def flush():
        nonlocal current
        if current:
            units.append((first, normal(" ".join(current))))
        current = []

    for index, line in enumerate(lines):
        if index in inside or not line.strip():
            flush()
            continue
        heading = _HEADING.match(line)
        if heading:
            flush()
            units.append((index, normal(heading.group(2))))
            continue
        if line.lstrip().startswith("|"):
            flush()
            cells = [cell for cell in line.strip().strip("|").split("|")]
            if not all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells):
                units.append((index, normal(" ; ".join(cells))))
            continue
        if _LIST_ITEM.match(line):
            flush()
            first, current = index, [_LIST_ITEM.sub("", line, count=1)]
            continue
        if not current:
            first = index
        current.append(line)
    flush()
    return units


def sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]


def links(text: str) -> list[str]:
    """Every Markdown link target in `text`."""
    return [match.group(2) for match in _LINK.finditer(text)]


def inline_code(text: str) -> list[str]:
    """The content of every inline code span in `text` (fenced blocks excluded by the caller)."""
    return [match.group(2) for match in _INLINE_CODE.finditer(text)]
