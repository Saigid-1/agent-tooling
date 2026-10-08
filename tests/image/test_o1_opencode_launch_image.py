"""O1 on the real pinned binary: the EFFECTIVE policy (A1) and where an ask is noticed (amendment-1).

Order: docs/work/orders/O1-opencode-board-launch.md. Seams: tests/image/o1_seams.py.

Image, by variable (unset FAILS; never skips): AGENT_TOOLING_TEST_IMAGE_OPENCODE, O3's `opencode` target built
from the tree under test. Precondition: `npm ci` in apps/kanban (the runner is bundled with its esbuild).

One container runs the effective-policy matrix (`opencode run`, no network, a stub provider on loopback) and
one runs the board's own PTY launch of the TUI. The runner prepares every launch through the board
(TerminalSessionManager.startTaskSession and the real adapter) in a fresh HOME and git worktree, with the
scenario's loosening files written first.

- Instrument controls (not falsifiers; they hold the instrument): plain `opencode` with every key "allow"
  is seen to allow all three probes; plain `opencode` with the P1 policy is seen to ask, ask, deny.
- A1: the board launch with no loosening file asks for bash and webfetch and denies an external write.
- A1 + amendment-1 (a), and the Coordinator's placement clarification: each loosening shape in the project
  file, in each HOME config path opencode-paths.ts lists, and in the project and every HOME file at once.
  GREEN-IF the launch still asks, asks and denies; or the launch is refused while that file is present AND
  the same launch with no loosening file proceeds and holds the policy (the order's third outcome).
- Amendment-1 (c): the TUI launch's bash call asks, and the board learns of it through the plugin while the
  ask is pending (a `to_review` hook command before any tool result reached the model).
"""
from __future__ import annotations

import json
import re
import subprocess
import uuid
from pathlib import Path

import pytest

from image_harness import REPO_ROOT, docker, remove, required
from o1_seams import (
    ASK_ERROR, BOARD_HOST_ENV, DENY_ERROR, ESBUILD, HOME_FILES, PLACEMENTS, POLICY, REQUIRED, RUNNER, SHAPES,
    IMAGE_VARIABLE,
    STUB_PROVIDER_CONFIG, STUB_PROVIDER_DIR,
)

pytestmark = pytest.mark.image

LABEL = "agent-tooling-image-test=o1"
PROBES = ("bash", "webfetch", "write")


def _slug(text: str) -> str:
    return re.sub(r"[^\w.-]", "-", text)


def _fixture(base: str, path: str, permission: object) -> dict:
    return {"base": base, "path": path, "text": json.dumps({"permission": permission}) + "\n"}


BASELINE = "board-no-loosening"
CONTROL_ALLOW = "control-plain-every-key-allow"
CONTROL_POLICY = "control-plain-p1-policy"
LOOSENING = {
    _slug(f"{shape}__{placement}"): (shape, placement)
    for shape in SHAPES
    for placement in PLACEMENTS
}


def _scenarios() -> list[dict]:
    scenarios = [
        {"id": CONTROL_ALLOW, "launch": "raw",
         "fixtures": [_fixture("project", "opencode.json", SHAPES["every-key-allow"])]},
        {"id": CONTROL_POLICY, "launch": "raw", "fixtures": [_fixture("project", "opencode.json", POLICY)]},
        {"id": BASELINE, "launch": "board", "fixtures": []},
    ]
    for scenario_id, (shape, placement) in LOOSENING.items():
        scenarios.append({
            "id": scenario_id, "launch": "board",
            "fixtures": [_fixture(base, path, SHAPES[shape]) for base, path in PLACEMENTS[placement]],
        })
    return scenarios


# --------------------------------------------------------------------------- running the container


def _bundle(work: Path) -> Path:
    esbuild = REPO_ROOT / ESBUILD
    if not esbuild.exists():
        pytest.fail(f"{ESBUILD} is missing: run `npm ci` in apps/kanban first (precondition of the O1 image tests)",
                    pytrace=False)
    out = work / "bundle" / "runner.mjs"
    out.parent.mkdir(parents=True, exist_ok=True)
    banner = ('import { createRequire as __o1_createRequire } from "node:module";'
              "const require = __o1_createRequire(import.meta.url);")
    result = subprocess.run(
        [str(esbuild), str(REPO_ROOT / RUNNER), "--bundle", "--platform=node", "--format=esm", "--target=node20",
         "--external:node-pty", f"--outfile={out}", f"--banner:js={banner}", "--log-level=warning"],
        capture_output=True, text=True, timeout=300, cwd=REPO_ROOT / "apps/kanban",
    )
    if result.returncode:
        pytest.fail(f"bundling the O1 runner failed ({result.returncode}): {result.stderr[-2000:]}", pytrace=False)
    return out


def _run_container(tmp_path_factory, mode: str, scenarios: list[dict], timeout: float) -> dict:
    image = required(IMAGE_VARIABLE)
    work = tmp_path_factory.mktemp(f"o1-{mode}")
    _bundle(work)
    out = work / "out"
    out.mkdir()
    out.chmod(0o777)
    managed = work / "managed"
    managed.mkdir()
    (managed / "opencode.json").write_text(json.dumps(STUB_PROVIDER_CONFIG, indent=1) + "\n")
    (managed / "opencode.json").chmod(0o644)
    managed.chmod(0o755)
    (out / "spec.json").write_text(json.dumps({"out": "/o1/out/result.json", "scenarios": scenarios}))
    name = f"o1-{mode}-{uuid.uuid4().hex[:10]}"
    script = ("mkdir -p /state/o1 && cp /o1/bundle/runner.mjs /state/o1/runner.mjs "
              "&& ln -sfn /app/node_modules /state/o1/node_modules "
              f"&& exec node /state/o1/runner.mjs {mode} /o1/out/spec.json")
    args = ["run", "--rm", "--pull", "never", "--name", name, "--label", LABEL, "--network", "none",
            *[arg for key, value in BOARD_HOST_ENV.items() for arg in ("-e", f"{key}={value}")],
            "--entrypoint", "/bin/sh",
            "-v", f"{(work / 'bundle').resolve()}:/o1/bundle:ro",
            "-v", f"{out.resolve()}:/o1/out",
            "-v", f"{managed.resolve()}:{STUB_PROVIDER_DIR}:ro",
            image, "-c", script]
    try:
        completed = docker(*args, timeout=timeout)
    except subprocess.TimeoutExpired:
        completed = None
    finally:
        remove(name)
    result_path = out / "result.json"
    result = json.loads(result_path.read_text()) if result_path.exists() else {}
    if completed is None:
        result["_runner"] = f"the {mode} container did not finish within {timeout}s"
    elif completed.returncode:
        result["_runner"] = f"runner exit {completed.returncode}: {(completed.stdout + completed.stderr)[-3000:]}"
    return result


@pytest.fixture(scope="module")
def matrix(tmp_path_factory) -> dict:
    return _run_container(tmp_path_factory, "matrix", _scenarios(), timeout=3600)


@pytest.fixture(scope="module")
def tui(tmp_path_factory) -> dict:
    return _run_container(tmp_path_factory, "tui", [{"id": "tui-ask", "launch": "board", "fixtures": []}], timeout=600)


# --------------------------------------------------------------------------- reading results


def _result(matrix: dict, scenario_id: str) -> dict:
    result = matrix.get(scenario_id)
    if result is None:
        pytest.fail(f"no result for {scenario_id}; runner: {matrix.get('_runner', 'finished')}", pytrace=False)
    return result


def _outcomes(result: dict) -> dict[str, str]:
    return {probe["tool"]: probe["outcome"] for probe in result.get("probes", [])}


def _explain(scenario_id: str, result: dict) -> str:
    lines = [f"{scenario_id}: launched={result.get('launched')} refusal={result.get('refusal')!r}"]
    if result.get("launched"):
        lines.append(f"  argv={result.get('argv')} opencode env={result.get('opencodeEnv')}")
        for probe in result.get("probes", []):
            lines.append(f"  {probe['tool']}: {probe['outcome']} status={probe['status']} side_effect={probe['sideEffect']} "
                         f"offered={probe.get('offered')} provider_reached={probe['providerReached']} exit={probe['code']} "
                         f"timed_out={probe['timedOut']} error={probe['error']!r} "
                         f"stderr={probe['stderrTail'][-300:]!r}")
        lines.append(f"  merged ruleset (opencode debug agent): {json.dumps(result.get('ruleset'))[:3000]}")
    return "\n".join(lines)


def _assert_holds(scenario_id: str, result: dict) -> None:
    assert result.get("launched"), f"the board launch was refused:\n{_explain(scenario_id, result)}"
    outcomes = _outcomes(result)
    unmeasured = [tool for tool in PROBES if outcomes.get(tool, "unknown") == "unknown"]
    assert not unmeasured, f"instrument: no classification for {unmeasured} (no measurement):\n{_explain(scenario_id, result)}"
    assert outcomes == REQUIRED, f"effective policy is not ask/ask/deny:\n{_explain(scenario_id, result)}"


# --------------------------------------------------------------------------- tests


def test_instrument_controls_see_allow_ask_and_deny(matrix):
    """Not a falsifier: the instrument sees all three outcomes on plain `opencode` (the image, the stub provider
    and the classification all work), so a GREEN below is a measurement, not a silent instrument."""
    allow = _result(matrix, CONTROL_ALLOW)
    assert _outcomes(allow) == {"bash": "allow", "webfetch": "allow", "write": "allow"}, _explain(CONTROL_ALLOW, allow)
    assert all(probe["sideEffect"] for probe in allow["probes"]), _explain(CONTROL_ALLOW, allow)
    policy = _result(matrix, CONTROL_POLICY)
    assert _outcomes(policy) == REQUIRED, _explain(CONTROL_POLICY, policy)
    errors = {probe["tool"]: probe["error"] or "" for probe in policy["probes"]}
    assert errors["bash"].startswith(ASK_ERROR) and errors["write"].startswith(DENY_ERROR), errors


def test_board_launch_holds_the_policy_with_no_loosening_file(matrix):
    """A1, GREEN-IF the board's OpenCode launch (no loosening file anywhere) asks for bash and webfetch and denies
    an external write, on the pinned binary."""
    _assert_holds(BASELINE, _result(matrix, BASELINE))


@pytest.mark.parametrize("scenario_id", sorted(LOOSENING))
def test_loosening_config_is_not_honoured(matrix, scenario_id):
    """A1 and amendment-1 (a). GREEN-IF, with the shape in the placement, the launched OpenCode still asks for bash
    and webfetch and denies an external write; or the launch is refused while the file is present and the same
    launch with no loosening file proceeds and holds the policy. RED when the launch proceeds loosened."""
    result = _result(matrix, scenario_id)
    if not result.get("launched"):
        baseline = _result(matrix, BASELINE)
        assert baseline.get("launched") and _outcomes(baseline) == REQUIRED, (
            f"refused with the loosening file present, but the same launch without it does not hold the policy, so "
            f"the refusal is not caused by the file:\n{_explain(scenario_id, result)}\n{_explain(BASELINE, baseline)}")
        return
    _assert_holds(scenario_id, result)


def test_home_placements_cover_opencode_paths():
    """Not a falsifier: the matrix's HOME placements are every HOME config path opencode-paths.ts lists."""
    source = (REPO_ROOT / "apps/kanban/src/terminal/opencode-paths.ts").read_text()
    body = source.split("export function getOpenCodeConfigPathCandidates", 1)[1].split("\nexport function", 1)[0]
    listed = {"/".join(re.findall(r'"([^"]+)"', args)) for args in re.findall(r"join\(homePath,([^)]*)\)", body)}
    assert listed, "no HOME config path found in opencode-paths.ts: the parse no longer matches the file"
    assert listed == {path for _base, path in HOME_FILES}, listed


def test_an_ask_moves_the_card_to_review_through_the_plugin(tui):
    """Amendment-1 (c). GREEN-IF the board's TUI launch (its PTY, argv and env unchanged) reaches the stub, whose
    one bash call ASKS, and the Kanban plugin runs the board's `hooks ingest --event to_review` command while that
    ask is pending: no tool result has reached the model and the bash marker does not exist."""
    detail = json.dumps({key: value for key, value in tui.items() if key != "screenTail"}, indent=1)[:4000]
    screen = str(tui.get("screenTail", ""))[-1500:]
    assert tui.get("launched"), f"the board launch was refused or failed: {tui.get('refusal')!r} {tui.get('_runner', '')}"
    assert tui.get("bashOffered"), \
        f"bash was never offered to the model: the prompt did not reach the stub, or bash is denied outright (OpenCode " \
        f"withholds such a tool), so it cannot ask:\n{detail}\nscreen: {screen}"
    assert tui.get("toolCallsIssued", 0) >= 1, f"instrument: the stub issued no bash call:\n{detail}\nscreen: {screen}"
    assert tui.get("reviewAtMs") is not None, f"no to_review hook ran while the bash ask was pending:\n{detail}\nscreen: {screen}"
    assert tui.get("toolResultsAtReview") == 0 and tui.get("toolResultsAtEnd") == 0, \
        f"a tool result reached the model, so the bash call did not wait on an ask:\n{detail}"
    assert not tui.get("markerExists"), f"the bash command ran: bash was allowed, not asked:\n{detail}"
