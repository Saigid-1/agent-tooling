"""D0b amendment 1 (the Principal, 2026-10-07): the release stance in the release notes from day 0 (R8).

Order: docs/work/orders/D0b-release-workflow.md, R8 as amendment 1 extends it. The release page's body carries
the project's release stance from ONE committed file, docs/RELEASE-STANCE.md, which the release job READS
(never a copy pasted into the workflow), plus a fourth line, in the release notes only, verbatim.

The claims' wording may be tightened at the meet, so the file test asserts each claim's substance, not its
sentences. The not-inlined test reads its sentences from the file itself, so it follows any rewording. The
release job is the job that creates the release on a push of v0.4.0-rc.1, as tests/ci/d0b_workflow.py reads
the release workflow (its file name is a seam in tests/d0b_seams.py).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_CI = Path(__file__).resolve().parents[1] / "ci"
if str(_CI) not in sys.path:
    sys.path.insert(0, str(_CI))

import d0b_seams as seams  # noqa: E402
import d0b_workflow as w  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
STANCE = ROOT / seams.RELEASE_STANCE
MIN_SENTENCE = 40  # shorter sentences (headings) are too generic to prove a copy

# Each claim's substance (amendment 1): what must survive any tightening of the wording.
CLAIMS = {
    "1. what blocks a release": {
        "the leak class": r"\bleak",
        "the redistribution class": r"\bredistribution\b",
        "the data-loss class": r"\bdata loss\b",
        "the silent-failure class": r"\bsilent failure",
        "everything else carried in the open": r"\bcarried\b,?\s+in the open\b",
    },
    "2. every carried item is an issue": {
        "every carried item is an issue": r"\bevery carried item is an issue\b",
        "where it is in the code": r"\bwhere\b[^.]*\bin the code\b",
        "what done looks like": r"\bdone\b",
    },
    "3. every property has a falsifier": {
        "every property has a falsifier": r"\bevery (?:shipped )?property has a falsifier\b",
    },
}


def _normal(text: str) -> str:
    """Lower case, Markdown emphasis and code marks removed, whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"[*`]", "", text)).strip().lower()


def _stance() -> str:
    assert STANCE.is_file(), f"{seams.RELEASE_STANCE} does not exist (amendment 1: the release stance's one source)"
    return STANCE.read_text()


def _release_views():
    workflow = w.require_release()
    views = [v for v in w.views(workflow, w.TAG_RC) if v.job_if is not False and v.of("release")]
    assert views, "no job creates the release on a push of v0.4.0-rc.1"
    return views


_PATH = re.compile(r"(?<![\w/.-])(?:\./)?((?:docs|scripts|deploy|tests|\.github)/[\w./-]+)")


def _named_files(text: str) -> list[Path]:
    return [ROOT / p for p in dict.fromkeys(_PATH.findall(text)) if (ROOT / p).is_file()]


def test_release_stance_file_holds_the_three_claims():
    """GREEN-IF docs/RELEASE-STANCE.md exists and holds the three claims of amendment 1, each by its substance:
    (1) a release is blocked only by a leak, a redistribution, data loss or a silent failure, and everything else
    is carried in the open; (2) every carried item is an issue that names where it is in the code and what done
    looks like; (3) every property has a falsifier."""
    text = _normal(_stance())
    missing = [f"claim {claim}: {what}" for claim, parts in CLAIMS.items() for what, pattern in parts.items()
               if not re.search(pattern, text)]
    assert not missing, f"{seams.RELEASE_STANCE} lacks:\n" + "\n".join(missing)


def test_release_job_reads_the_stance_file_and_never_inlines_it():
    """GREEN-IF docs/RELEASE-STANCE.md exists; the job that creates the release names docs/RELEASE-STANCE.md in a
    step that can run, after an actions/checkout in that job (so the committed file is what it reads); and no
    sentence of the file (of 40 or more characters, compared without case, emphasis or line breaks) appears in
    any file under .github/ (the text is read, never pasted into the workflow)."""
    stance = _stance()
    problems = []
    for view in _release_views():
        reads = sorted({step for step, _kind, text in view.texts if seams.RELEASE_STANCE in text})
        checkouts = [a.step for a in view.of("checkout")]
        if not reads:
            problems.append(f"{view.label}: no step of the release job reads {seams.RELEASE_STANCE}")
        elif not any(c < reads[0] for c in checkouts):
            problems.append(f"{view.label}: reads {seams.RELEASE_STANCE} without checking out the commit first")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", _normal(re.sub(r"^#+.*$", "", stance, flags=re.M)))
                 if len(s) >= MIN_SENTENCE]
    assert sentences, f"{seams.RELEASE_STANCE} has no sentence of {MIN_SENTENCE}+ characters to compare"
    for path in sorted((ROOT / ".github").rglob("*")):
        if path.is_file():
            body = _normal(path.read_text(errors="replace"))
            pasted = [s for s in sentences if s in body]
            if pasted:
                problems.append(f"{path.relative_to(ROOT).as_posix()} inlines the stance: {pasted[0][:80]!r}")
    assert not problems, w.redact("\n".join(problems))


def test_release_notes_carry_the_cutover_scan_line_verbatim():
    """GREEN-IF the job that creates the release, with the in-tree files its steps name, carries this line verbatim
    (exact characters): "The private-content scan in the cutover record read zero on every gate line before this
    tag was pushed. That scan is the cutover gate; it does not run in CI." """
    texts = []
    for view in _release_views():
        for _step, _kind, text in view.texts:
            texts.append(text)
            texts.extend(path.read_text(errors="replace") for path in _named_files(text))
    assert any(seams.CUTOVER_SCAN_LINE in text for text in texts), \
        f"the release notes' sources do not carry, verbatim: {seams.CUTOVER_SCAN_LINE!r}"
