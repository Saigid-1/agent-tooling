"""B66 P2: step 9 of DOCKER.md's first run names the hooks that bring the turn to the desk.

Order: docs/work/orders/B64-66-docker-installer-and-step-9.md, B66.

Step 9 is docs/DOCKER.md from the line that starts `**9. Search.**` up to, but not including, the
heading `### Board tasks with in-container agents`. The hook events are read from the harness
profile document the installed package ships (`assets/harness-profiles.json` of `kp_agent_tooling`,
through `importlib.resources`), never from a copied list:
- the Claude profile's events are the `hooks.events` of the profile whose `harness` is `claude`;
- the universe of hook events is the union of `hooks.events` over every profile in the document, so
  another harness's event (Codex's `UserPromptSubmit`) is detectable as outside Claude's.

An event is named where its exact name stands as a whole word (case-sensitive) anywhere in step 9.

Seams for the falsifiers (each run against a scratch copy, never the tracked files):
- `DOCKER`: the document step 9 is read from (Fa: a copy that names `UserPromptSubmit`; Fb: a copy
  without the `Stop` sentence);
- `PROFILES_SOURCE`: the harness profile document the tests read; None is the installed package's
  asset (Fc: a copy whose Claude profile has no `SessionEnd`).
A scratch test module can set either attribute on this module and re-export its tests.
"""
from __future__ import annotations

import json
import re
from importlib import resources
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKER = ROOT / "docs" / "DOCKER.md"

PACKAGE = "kp_agent_tooling"
ASSET = "assets/harness-profiles.json"
PROFILES_SOURCE = None

HARNESS = "claude"
STEP_START = re.compile(r"^\*\*9\. Search\.\*\*")
STEP_END = re.compile(r"^#{1,6}\s+Board tasks with in-container agents\b")
# The order's P1: the turn is captured under the desk when `Stop` fires at the end of the turn, and at
# exit by `SessionEnd`.
REQUIRED = ("Stop", "SessionEnd")


def read_profiles(source=None) -> dict:
    """The harness profile document: the installed package's asset, or `source` (a path to a copy)."""
    if source is None:
        source = resources.files(PACKAGE).joinpath(ASSET)
    return json.loads(source.read_text())


def hook_events(document: dict) -> tuple[list[str], set[str]]:
    """(the Claude profile's hook events, the union of every profile's hook events) of `document`."""
    profiles = document["profiles"]
    claude = [p for p in profiles if p.get("harness") == HARNESS]
    assert len(claude) == 1, f"the harness profile document has {len(claude)} {HARNESS!r} profile(s), not one"
    universe = {event for profile in profiles for event in profile["hooks"]["events"]}
    return list(claude[0]["hooks"]["events"]), universe


def step_9(text: str) -> str:
    """Step 9's text: from `**9. Search.**` up to the board-tasks heading (outside code blocks)."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if STEP_START.match(line)), None)
    assert start is not None, f"{DOCKER.name} has no line starting `**9. Search.**`"
    end, fenced = None, False
    for index in range(start + 1, len(lines)):
        if lines[index].strip().startswith("```"):
            fenced = not fenced
        elif not fenced and STEP_END.match(lines[index]):
            end = index
            break
    assert end is not None, f"{DOCKER.name} has no 'Board tasks with in-container agents' heading after step 9"
    return "\n".join(lines[start:end])


def named(event: str, text: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(event)}(?![A-Za-z0-9_])", text) is not None


def _events():
    return hook_events(read_profiles(PROFILES_SOURCE))


def _step_9():
    return step_9(DOCKER.read_text())


def test_the_claude_profile_has_the_events_step_9_must_name():
    """GREEN-IF the Claude profile, as read, configures every event step 9 must name (`Stop`, `SessionEnd`):
    step 9's statement about them is then a statement about this profile's hooks."""
    claude, _ = _events()
    missing = [event for event in REQUIRED if event not in claude]
    assert not missing, f"the {HARNESS!r} harness profile has no hook for {missing} (its events: {claude})"


def test_step_9_names_stop_and_session_end():
    """GREEN-IF step 9 names `Stop` and `SessionEnd`. RED at base: step 9 names no hook event."""
    text = _step_9()
    missing = [event for event in REQUIRED if not named(event, text)]
    assert not missing, f"step 9 of {DOCKER.name} does not name the hook event(s) {missing}"


def test_step_9_names_no_hook_event_outside_the_claude_profile():
    """GREEN-IF every hook event of any packaged profile that step 9 names is an event of the Claude
    profile."""
    claude, universe = _events()
    text = _step_9()
    outside = sorted(event for event in universe - set(claude) if named(event, text))
    assert not outside, (f"step 9 of {DOCKER.name} names hook event(s) {outside}, which the {HARNESS!r} "
                         f"profile does not configure (its events: {claude})")
