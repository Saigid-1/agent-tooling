"""D0a-2 F1: no README or HARNESSES sentence claims what the tree does not have Built.

Order: docs/work/orders/D0a2-readme-and-harnesses.md, F1 and its CLOSED list of claim patterns. Each
case below is one pattern of that list. The test derives, from the symbol the order names, whether the
tree makes the claim true; only when the tree makes it false is an affirmative sentence of that
pattern a finding. Nothing here hard-codes "false": if the tree's fact changes, the claim is allowed.

Readings (repeated in the arm report under AMBIGUITY):
- The documents are README.md and docs/HARNESSES.md. Fenced code blocks are not sentences.
- A claim is affirmative when one clause carries both the pattern's subject and its predicate and no
  negation or hedge word. A clause is a part of a sentence (d0a2_markdown.sentences) split at `;`,
  `:`, a dash, a parenthesis, a table cell or ", and|but|while|whereas|so|yet ". A sentence that
  says the opposite ("not a desk-task harness", "a host launch is refused") is therefore not a
  finding, and an affirmative clause that shares its clause with an unrelated negation is missed
  (cleared, not measured).
- Before reading the documents, each case checks its own instrument: the pattern finds the
  canonical affirmative sentence (the order's mutant) and passes over the canonical negated one.
- A fact that cannot be derived (the named symbol is gone) fails the case: the claim cannot be
  judged, which is not the same as the claim being allowed.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Callable

import pytest
import yaml

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
_TESTS = _HERE.parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import d0a2_markdown as md  # noqa: E402
import d0a2_seams as seams  # noqa: E402
import d0b_seams  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DOCUMENTS = (seams.README, seams.HARNESSES)

NEGATION = r"\b(?:not|no|never|neither|nor|none|nothing|without|cannot|refus\w*|planned|future|later)\b|n't\b"
_CLAUSE = re.compile(r"\s*(?:;|:\s|\s[—–]\s|\s--\s|\(|\)|,\s+(?:and|but|while|whereas|so|yet)\s+)\s*",
                     re.I)


# ----------------------------------------------------------------------------- the tree's facts
# Each returns True when the tree makes the claim TRUE (so the claim is allowed), False when it makes
# it false. Each raises AssertionError when the named symbol cannot be read.

def _local_model_runs_desk_tasks() -> bool:
    """The gateway's provider kinds (model_gateway.py, model_gateway_providers.py's provider table) and
    the desk-task harness profiles (harness_profiles.default_profiles): true only if a profile names a
    provider kind."""
    from kp_agent_tooling._impl.service import harness_profiles, model_gateway, model_gateway_providers
    kinds = set(model_gateway_providers.PROVIDER_PATHS) | set(model_gateway.PROVIDER_KINDS)
    assert kinds, "the model gateway names no provider kind"
    profiles = harness_profiles.default_profiles()
    assert profiles, "no desk-task harness profile is packaged"
    naming = sorted(harness for harness, profile in profiles.items()
                    if any(kind in json.dumps(profile) for kind in kinds))
    return bool(naming)


def _opencode_runs_on_the_host() -> bool:
    """O2's host refusal, by its named test: true only if that test runs and fails (the refusal no
    longer holds). A test that cannot be found or collected cannot state the fact."""
    with tempfile.TemporaryDirectory(prefix="d0a2-f1-") as scratch:
        done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                               "--basetemp", str(Path(scratch) / "basetemp"), seams.O2_HOST_REFUSAL_TEST],
                              cwd=ROOT, capture_output=True, text=True, timeout=600)
    tail = (done.stdout + done.stderr).strip().splitlines()[-3:]
    assert done.returncode in (0, 1), f"the named host-refusal test could not run (exit {done.returncode}): {tail}"
    if done.returncode == 0:
        assert any(re.search(r"\b1 passed\b", line) for line in tail), f"the named test did not run: {tail}"
    return done.returncode == 1


class _Unbound(Exception):
    pass


class _RefusingStore:
    def _binding(self, session):  # noqa: D401 - the store's admission check
        raise _Unbound(session)


def _opencode_captures_host_sessions() -> bool:
    """The capture code (not the launch test): the `opencode` profile captures in mode `export` with an
    `EXPORT_PARSERS` parser; opencode_export_capture reads only a BOUND session (the store's binding is
    checked before any read) that is a ROOT session (`check_identity` refuses a child); and the host
    sweep, workspace_capture.py, names no export mode and no OpenCode. True if any of these fails."""
    from kp_agent_tooling._impl.service import harness_profiles, opencode_export_capture, workspace_capture
    profile = harness_profiles.default_profiles().get(seams.OPENCODE_HARNESS)
    assert profile is not None, f"no packaged {seams.OPENCODE_HARNESS!r} harness profile"
    export_only = (profile["capture"].get("mode") == "export"
                   and profile["capture"].get("parser") in harness_profiles.EXPORT_PARSERS)

    with tempfile.TemporaryDirectory(prefix="d0a2-f1-") as workspace:
        root = {"info": {"id": "ses_root", "directory": workspace}, "messages": []}
        child = {"info": {"id": "ses_child", "directory": workspace, "parentID": "ses_root"}, "messages": []}
        opencode_export_capture.check_identity(root, "ses_root", workspace)
        try:
            opencode_export_capture.check_identity(child, "ses_child", workspace)
            root_only = False
        except opencode_export_capture.OpenCodeExportRefused:
            root_only = True
        capture = opencode_export_capture.OpenCodeExportCapture(Path(workspace) / "absent.db",
                                                                executable=str(Path(workspace) / "absent"))
        try:
            capture.capture("ses_root", store=_RefusingStore(), queue=None, workspace=workspace)
            bound_only = False
        except _Unbound:
            bound_only = True
        except Exception:  # read something before asking for the binding
            bound_only = False

    sweep = Path(workspace_capture.__file__).read_text()
    sweep_names_none = not re.search(r"(?i)opencode|\bexport\b", sweep)
    return not (export_only and root_only and bound_only and sweep_names_none)


def _summarizer_runs_by_default() -> bool:
    """The packaged compose.yaml asset's summarizer service: true only if it carries no Compose profile."""
    compose = yaml.safe_load(files("kp_agent_tooling").joinpath(seams.COMPOSE_ASSET).read_text())
    service = (compose.get("services") or {}).get(seams.SUMMARIZER_SERVICE)
    assert service is not None, f"the packaged manifest has no {seams.SUMMARIZER_SERVICE!r} service"
    return not service.get("profiles")


def _published_image_carries_an_agent() -> bool:
    """D0b's R7 step and D0f's install action: true unless (a) a step of the release workflow runs D0f's
    check (`scan_image` in its file) and lists Claude Code's and Codex's packages, for every published
    target; (b) the board defines the install mutation; and (c) the board's package.json declares no
    Agent SDK dependency of any kind."""
    workflow = yaml.safe_load((ROOT / d0b_seams.RELEASE_WORKFLOW).read_text())
    check = ROOT / d0b_seams.SDK_CHECK
    assert check.is_file(), f"{d0b_seams.SDK_CHECK} is missing"
    scan_image = any(isinstance(node, ast.FunctionDef) and node.name == "scan_image"
                     for node in ast.parse(check.read_text()).body)
    r7_steps = []
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            run = step.get("run") or ""
            if (d0b_seams.SDK_CHECK in run and seams.CLAUDE_CODE_PACKAGE in run and seams.CODEX_PACKAGE in run
                    and all(re.search(rf"(?m)^\s*{re.escape(t)}\s", run) for t in d0b_seams.PUBLISHED_TARGETS)):
                r7_steps.append(step.get("name"))
    router = (ROOT / seams.D0F_INSTALL_ROUTER).read_text()
    install_action = bool(re.search(rf"\b{seams.D0F_INSTALL_PROCEDURE}\s*:\s*t\.procedure[\s\S]{{0,400}}?\.mutation\(",
                                    router))
    package = json.loads((ROOT / seams.KANBAN_PACKAGE).read_text())
    declared = [section for section in ("dependencies", "devDependencies", "optionalDependencies",
                                        "peerDependencies", "bundleDependencies", "bundledDependencies")
                if seams.AGENT_SDK_PACKAGE in (package.get(section) or {})]
    return not (scan_image and r7_steps and install_action and not declared)


def _in_board_agent_launches_tasks() -> bool:
    """K1's gate: the production runtime server creates its Cline task service with `disableLaunch: true`,
    and the Cline runtime's `startTaskSession` throws under it. True if either is gone."""
    server = (ROOT / seams.K1_SERVER).read_text()
    runtime = (ROOT / seams.K1_RUNTIME).read_text()
    gated_server = bool(re.search(r"createInMemoryClineTaskSessionService\(\s*\{[^}]*\bdisableLaunch:\s*true\b", server))
    start = re.search(r"\basync startTaskSession\([^)]*\)[^{]*\{\s*if \(this\.disableLaunch\)\s*\{\s*throw\b", runtime)
    return not (gated_server and start)


# ----------------------------------------------------------------------------- the closed list

@dataclass(frozen=True)
class Claim:
    name: str
    subject: str
    predicate: str
    fact: Callable[[], bool]
    affirmative: str  # the order's mutant, inserted as an affirmative sentence
    negated: str  # what the order asks the docs to say instead
    hedges: tuple[str, ...] = field(default_factory=tuple)

    def finds(self, clause: str) -> bool:
        if not (re.search(self.subject, clause, re.I) and re.search(self.predicate, clause, re.I)):
            return False
        return not re.search(NEGATION, clause, re.I) and not any(re.search(h, clause, re.I) for h in self.hedges)


_PUBLISHED = "|".join(re.escape(t) for t in d0b_seams.PUBLISHED_TARGETS)
_AGENT = r"(?:claude code|codex|claude agent sdk|claude-agent-sdk|claude-code|@openai/codex)"

CLAIMS = (
    Claim(
        "a local model runs desk tasks",
        subject=r"\blocal (?:open[- ]weight )?(?:llm|models?)\b|\bopen[- ]weight models?\b|\bollama\b|\bllama\.cpp\b"
                r"|\blm studio\b|\bvllm\b|\bopenai[- ]compatible\b",
        predicate=r"\bdesk[- ]tasks?\b|\btask harness\b|\bharness for (?:desk |board )?tasks\b"
                  r"|\b(?:runs?|launch(?:es)?|starts?|executes?|drives?|powers?) (?:\w+ ){0,2}tasks?\b",
        fact=_local_model_runs_desk_tasks,
        affirmative="A local model runs desk tasks.",
        negated="A local model is a gateway route, not a desk-task harness, in 0.4.0.",
    ),
    Claim(
        "OpenCode runs on the host",
        subject=r"\bopencode\b",
        predicate=r"\b(?:runs?|launch(?:es|ed)?|starts?|started|works?|available|supported|usable)"
                  r" (?:\w+ ){0,3}?(?:on|from) (?:the|your|their|a|its) (?:own )?host\b"
                  r"|\bhost[- ]launch(?:es|ed)?\b(?: of opencode)? (?:is |are )?(?:supported|available|allowed|possible|works?)\b"
                  r"|\bas a host harness\b|\bkp-agent-host (?:\w+ ){0,2}opencode\b",
        fact=_opencode_runs_on_the_host,
        affirmative="OpenCode runs on the host.",
        negated="OpenCode is board-launched only; a host launch of OpenCode is refused.",
    ),
    Claim(
        "OpenCode captures host sessions",
        subject=r"\bopencode\b",
        predicate=r"\bcaptur\w*\b(?:\W+\w+){0,5}?\W+host\b|\bhost\b(?:\W+\w+){0,5}?\W+captur\w*"
                  r"|\bworkspace capture\b(?:\W+\w+){0,6}?\W+opencode\b|\bopencode\b(?:\W+\w+){0,6}?\W+workspace capture\b",
        fact=_opencode_captures_host_sessions,
        affirmative="OpenCode captures host sessions.",
        negated="Host sessions of OpenCode are not captured.",
    ),
    Claim(
        "the summarizer runs by default",
        subject=r"\bsummari[sz]er\b",
        predicate=r"\bby default\b|\bdefault (?:role|component)\b|\balways (?:runs?|on|starts?)\b"
                  r"|\b(?:starts?|runs?) automatically\b|\bout of the box\b",
        fact=_summarizer_runs_by_default,
        affirmative="The summarizer runs by default.",
        negated="The summarizer is opt-in and off by default.",
        hedges=(r"\boff\b", r"\bopt-in\b", r"\boptional\b", r"\bonly (?:when|if)\b", r"\bunless\b"),
    ),
    Claim(
        "a published image contains Claude Code, Codex or the Claude Agent SDK",
        subject=rf"\bpublished (?:\w+ ){{0,2}}images?\b|\b(?:{_PUBLISHED}) (?:target |image|images)\b",
        predicate=rf"\b(?:contains?|includes?|ships?|shipped|bundles?|bundled|carr(?:y|ies|ied)|comes? with|has"
                  rf"|pre-?installed)\b(?:\W+\w+){{0,6}}?\W+{_AGENT}"
                  rf"|(?<![\w/-]){_AGENT}(?:\W+\w+){{0,6}}?\W+(?:is|are|comes?|ships?)\s+(?:\w+\s+){{0,2}}"
                  rf"(?:in|inside|with|included in|bundled in|preinstalled in)\b",
        fact=_published_image_carries_an_agent,
        affirmative="The product image contains Claude Code.",
        negated="No published image contains Claude Code, Codex or the Claude Agent SDK.",
    ),
    Claim(
        "the in-board agent launches tasks in 0.4.0",
        subject=r"\bin-board (?:cline )?agent\b|\bcline agent\b|\bnative cline\b"
                r"|\bboard's (?:own |built-in )?(?:cline )?agent\b|\bbuilt-in (?:cline )?agent\b",
        predicate=r"\b(?:launch(?:es|ed)?|runs?|starts?|executes?|works? on|picks? up|can launch|can run)"
                  r" (?:\w+ ){0,2}tasks?\b",
        fact=_in_board_agent_launches_tasks,
        affirmative="The in-board Cline agent launches tasks in 0.4.0.",
        negated="The in-board Cline agent's launch is gated in 0.4.0.",
        hedges=(r"\bgated\b", r"\bdisabled\b", r"\bblocked\b", r"\bturned off\b", r"\bonce\b", r"\buntil\b"),
    ),
)


def clauses(text: str) -> list[str]:
    return [part for sentence in md.sentences(text) for part in _CLAUSE.split(sentence) if part and part.strip()]


def findings(claim: Claim, text: str) -> list[str]:
    lines = text.splitlines()
    return [clause for _line, unit in md.prose_units(lines) for clause in clauses(unit) if claim.finds(clause)]


@pytest.mark.parametrize("claim", CLAIMS, ids=[c.name for c in CLAIMS])
def test_f1_no_sentence_claims_what_the_tree_does_not_have_built(claim: Claim):
    """GREEN-IF, for this pattern of F1's closed list: the pattern's instrument is live (it finds the
    canonical affirmative sentence and passes over the canonical negated one), the tree fact the order
    names can be derived from its symbol, and, when that fact makes the claim false, no clause of
    README.md or docs/HARNESSES.md states the claim affirmatively (subject and predicate in one clause,
    no negation or hedge word). When the fact makes the claim true, the claim is allowed and the case is
    green. The six patterns and their facts:
    - a local model runs desk tasks: false unless a packaged desk-task harness profile names one of the
      gateway's provider kinds (PROVIDER_KINDS / the provider table);
    - OpenCode runs on the host: false while O2's named test (a host launch of opencode is refused before
      anything is written) passes;
    - OpenCode captures host sessions: false while the opencode profile captures by `export` with an
      EXPORT_PARSERS parser, opencode_export_capture reads only a bound root session, and
      workspace_capture.py names no export mode and no OpenCode;
    - the summarizer runs by default: false while the packaged compose.yaml's summarizer service carries
      a Compose profile;
    - a published image contains Claude Code, Codex or the Claude Agent SDK: false while D0b's R7 step
      runs D0f's check and the CLI listing on every published target, the board defines D0f's install
      mutation, and apps/kanban/package.json declares no Agent SDK dependency;
    - the in-board agent launches tasks in 0.4.0: false while K1's gate holds (the runtime server passes
      `disableLaunch: true` and `startTaskSession` throws under it)."""
    assert findings(claim, claim.affirmative), f"instrument: the pattern misses {claim.affirmative!r}"
    assert not findings(claim, claim.negated), f"instrument: the pattern takes {claim.negated!r} for a claim"
    if claim.fact():
        return  # the tree has it Built: the claim is allowed
    found = []
    for name in DOCUMENTS:
        path = ROOT / name
        if path.is_file():
            found.extend(f"{name}: {clause!r}" for clause in findings(claim, path.read_text()))
    assert not found, (f"the tree does not have it Built ({claim.name}), but the docs claim it:\n"
                       + "\n".join(found))
