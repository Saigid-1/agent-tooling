"""T5b P4: documented.

Order: docs/work/orders/T5b-retention-references.md. "The DOCKER.md refresh options
table and the retention paragraph state the field and when to use it: a maintained
knowledge catalog pinned separately from refresh." Falsifier: documentation the code
contradicts.

What the code is held to (tested in tests/refresh/test_t5b_p*.py, cited here):
``retention_references`` is a top-level refresh request field of at most 32 absolute
paths of files under ``/config`` or ``/state``; any generation a listed file mentions
is kept.

Readings (repeated in the arm report under AMBIGUITY):
- the refresh options table is the Markdown table in DOCKER.md's run-in ``**refresh.**``
  section (up to the next run-in title or heading); its rows are keyed by the field
  in backticks in the first cell;
- the retention paragraph is any paragraph of that section outside the table that
  names the field; "when to use it" is satisfied by the word "catalog" in that
  paragraph or in the field's row;
- the ``retain_generations`` row enumerates what retention keeps ("keep the published
  generations, generations referenced by ..., and at most this many minus one other
  complete generations"); left as it is, it contradicts a code that also keeps the
  generations a listed file names, so it must name them.
- The T7 runbook is not in this repository at the base; it is not tested here.
"""
from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "docs" / "DOCKER.md"
FIELD = "retention_references"
_RUN_IN = re.compile(r"^\*\*[^*]+?\.\*\*")


def refresh_section() -> list[str]:
    lines = DOCKER.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("**refresh.**"))
    end = next((i for i in range(start + 1, len(lines))
                if _RUN_IN.match(lines[i]) or lines[i].startswith("#")), len(lines))
    return lines[start:end]


def cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def table_rows(lines: list[str]) -> dict[str, str]:
    rows = {}
    for line in lines:
        if line.lstrip().startswith("|"):
            first = cells(line)[0]
            match = re.fullmatch(r"`([A-Za-z0-9_]+)`", first)
            if match:
                rows.setdefault(match.group(1), line)
    return rows


def paragraphs(lines: list[str]) -> list[str]:
    found, current = [], []
    for line in lines + [""]:
        if not line.strip() or line.lstrip().startswith("|"):
            if current:
                found.append(" ".join(current))
                current = []
            continue
        current.append(line.strip())
    return found


def test_p4_options_table_has_a_retention_references_row_stating_where_and_domain():
    rows = table_rows(refresh_section())
    assert FIELD in rows, f"DOCKER.md's refresh options table has no `{FIELD}` row: {sorted(rows)}"
    row = rows[FIELD]
    where = cells(row)[1].lower() if len(cells(row)) > 1 else ""
    assert "top level" in where, f"the `{FIELD}` row does not say it is a top-level field: {row}"
    assert re.search(r"\b32\b", row), f"the `{FIELD}` row does not state the 32-entry bound: {row}"
    assert "absolute" in row.lower(), f"the `{FIELD}` row does not say the paths are absolute: {row}"
    assert "/config" in row and "/state" in row, (
        f"the `{FIELD}` row does not name /config and /state as the allowed roots: {row}")


def test_p4_retention_paragraph_states_the_field_and_when_to_use_it():
    section = refresh_section()
    named = [text for text in paragraphs(section) if f"`{FIELD}`" in text or FIELD in text]
    assert named, f"no paragraph of DOCKER.md's refresh section (outside the table) names `{FIELD}`"
    row = table_rows(section).get(FIELD, "")
    assert any("catalog" in text.lower() for text in named) or "catalog" in row.lower(), (
        f"DOCKER.md does not say when to use `{FIELD}` (a maintained knowledge catalog pinned "
        f"separately from refresh): {named}")


def test_p4_retain_generations_row_counts_listed_files_among_what_is_kept():
    row = table_rows(refresh_section()).get("retain_generations", "")
    assert row, "DOCKER.md's refresh options table has no `retain_generations` row"
    assert FIELD in row or "reference file" in row.lower(), (
        "the `retain_generations` row lists what retention keeps but not the generations a "
        f"`{FIELD}` file names, which the code keeps: {row}")
