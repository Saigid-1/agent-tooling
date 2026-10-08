"""S1a: the MEMORY.md property change is stated, in both copies.

Order: docs/work/orders/S1a-scheduled-summarizer.md, P1 "The MEMORY.md property change, stated": FEATURE
writes, in substance, in BOTH docs/MEMORY.md and the packaged assets/MEMORY.md (which differ today, census
§13), (1) the standing-approval exception (with the optional summarizer role selected and a desk in its
standing approval, that desk's sealed episodes are sent under the approved model and the gateway budget,
without a per-episode digest approval) and (2) for the role-less case, the sentence that install, binding,
capture and reads never trigger an outbound call. The test asserts that the change is stated, not its wording.

Reading, stated once: a "block" is a paragraph (blank-line separated) or one list item of it, whitespace
collapsed, compared case-insensitively.
- the exception is stated when one block names the summarizer (`summarizer`/`summariser`), the `approval`
  (the noun), the `budget` and a `desk` (at the order's base no block of either file does);
- the role-less sentence is kept when one block names `install`, `binding`, `capture` and `read`, a negation
  (`never`/`not`) and `trigger` or `outbound` (at base, one block of each file does).

GREEN-IF `test_memory_md_states_the_change[docs|assets]`: that file has a block stating the exception AND a
block keeping the role-less sentence.

Mutants that must be RED: the exception written in one copy only; the role-less sentence deleted while the
exception is added.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COPIES = {'docs': ROOT / 'docs' / 'MEMORY.md',
          'assets': ROOT / 'packages' / 'tooling' / 'src' / 'kp_agent_tooling' / 'assets' / 'MEMORY.md'}
EXCEPTION = (r'summari[sz]er', r'\bapproval\b', r'\bbudget', r'\bdesks?\b')
ROLE_LESS = (r'\binstall', r'\bbinding', r'\bcapture', r'\breads?\b', r'\b(never|not)\b', r'trigger|outbound')


def blocks(text):
    found = []
    for paragraph in re.split(r'\n\s*\n', text):
        for item in re.split(r'\n(?=\s*(?:[-*+]|\d+\.)\s)', paragraph):
            found.append(' '.join(item.split()).lower())
    return found


def _stating(text, patterns):
    return [b for b in blocks(text) if all(re.search(p, b) for p in patterns)]


@pytest.mark.parametrize('copy', sorted(COPIES))
def test_memory_md_states_the_change(copy):
    text = COPIES[copy].read_text()
    assert _stating(text, EXCEPTION), (
        f'{COPIES[copy].relative_to(ROOT)} does not state the standing-approval exception (no block names the '
        'summarizer, the approval, the budget and a desk)')
    assert _stating(text, ROLE_LESS), (
        f'{COPIES[copy].relative_to(ROOT)} no longer keeps the role-less sentence (install, binding, capture '
        'and reads never trigger an outbound call)')
