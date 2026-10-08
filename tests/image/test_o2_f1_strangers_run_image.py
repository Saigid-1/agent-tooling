"""O2 F1, the stranger's run, on O3's image (docs/work/orders/O2-opencode-third-harness.md, F1 with A1; F6's
image half). Seams: tests/o2_seams.py. The container: tests/image/o2_image_run.py.

Image, by variable (unset FAILS; never skips): AGENT_TOOLING_TEST_IMAGE_OPENCODE, O3's `opencode` target built
from the tree under test. Precondition: `npm ci` in apps/kanban.

The run: the image passes `agent_sdk_absence.py` and carries no claude or codex; the board launches an
OpenCode task bound to a desk (harness `opencode`); OpenCode runs one turn against a stub OpenAI-compatible
provider on loopback; the npm registry and models.opencode.ai are unreachable (a recorder sees any attempt).

GREEN-IF (each test names its own):
- the image is a stranger's image (precondition, not a falsifier: GREEN at base);
- the turn is captured: the launch is bound to the desk under `opencode`, and the desk's memory holds the
  prompt (user) and the answer (assistant), each exactly once;
- one queue job is enqueued (exactly one, after the Stop and after the terminal's exit, O2-S7);
- a desk search finds the answer within T12b's bound (O2-S8);
- A1: no connection was attempted to the npm registry or the models host, no installer output appeared,
  and the turn completed (so the run was not silent);
- F6 in the image: the operator's key (a sentinel in the mounted key file) is in no launched environment,
  receipt, log, per-launch config or any file under /state or /tmp; control: the launch was bound and the
  key file was readable in the container.

Mutants: the adapter drops the binding (not bound, not captured); the parser drops `text` parts (the
turn's text is not captured or found); the enqueue is removed (no job); the config carrier is a writable
directory without node_modules (the background install reaches for the npm registry).
RED at base: there is no `opencode` profile, so the board launches unbound and nothing is captured; and the
launch reaches the models host and the npm registry.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import o2_seams as seams
from image_harness import required, sh
from o2_image_run import explain, launch, run_container
from opencode_pin import OPENCODE_IMAGE_VARIABLE

pytestmark = pytest.mark.image

SDK_CHECK = Path(__file__).resolve().parent / 'agent_sdk_absence.py'
PROMPT = 'o2-stranger-turn please remember the Quillwort heliograph'
ANSWER = 'Noted the Quillwort heliograph marshwren'
CLI_SCAN = r"""
command -v kp-agent-tooling >/dev/null 2>&1 && echo "CONTROL kp-agent-tooling"
command -v opencode >/dev/null 2>&1 && echo "CONTROL opencode"
for n in claude codex; do p=$(command -v "$n" 2>/dev/null) && echo "FOUND $n $p"; done
find / -xdev \( -type f -o -type l \) \( -name claude -o -name codex \) -perm /111 2>/dev/null | sed 's/^/FOUND /'
echo SCAN-DONE
"""


@pytest.fixture(scope='module')
def run(tmp_path_factory) -> dict:
    return run_container(tmp_path_factory, 'f1', desks=['Stranger desk'],
                         launches=[{'id': 'stranger', 'desk': 0, 'prompt': PROMPT, 'answer': ANSWER}],
                         instrument=False)


def _final(run) -> dict:
    return launch(run, 'stranger').get('final') or {}


def test_f1_the_image_is_a_strangers_image():
    """Precondition (not a falsifier): `agent_sdk_absence.py --expect-board IMAGE` exits 0, and the image
    carries opencode and kp-agent-tooling (controls) and no claude or codex."""
    image = required(OPENCODE_IMAGE_VARIABLE)
    check = subprocess.run([sys.executable, str(SDK_CHECK), '--expect-board', image], capture_output=True, text=True,
                           timeout=3600)
    assert check.returncode == 0, f'agent_sdk_absence.py exited {check.returncode}:\n{(check.stdout + check.stderr)[-3000:]}'
    scan = sh(image, CLI_SCAN, user='0:0', timeout=900)
    lines = scan.stdout.splitlines()
    assert 'SCAN-DONE' in lines and 'CONTROL kp-agent-tooling' in lines and 'CONTROL opencode' in lines, scan.stdout
    assert not [line for line in lines if line.startswith('FOUND ')], scan.stdout


def test_f1_the_turn_is_captured(run):
    """GREEN-IF the board's launch was bound to the desk under `opencode` (source board) and the desk's memory
    holds the turn: the prompt as `user` and the answer as `assistant`, each exactly once."""
    entry = launch(run, 'stranger')
    assert entry.get('launched'), f'the board refused the launch:\n{explain(run, "stranger")}'
    assert entry.get('answered'), f'the stub never answered the turn (instrument):\n{explain(run, "stranger")}'
    final = _final(run)
    desk = run['_world']['desks'][0]
    bound = [(b.get('harness'), b.get('desk_id'), b.get('source')) for b in final.get('bindings') or []]
    assert bound == [(seams.HARNESS, desk, 'board')], f'the launch was not bound to its desk:\n{explain(run, "stranger")}'
    events = final.get('events') or []
    users = [e for e in events if PROMPT in e['text'] and e['role'] == 'user']
    answers = [e for e in events if ANSWER in e['text'] and e['role'] == 'assistant']
    assert len(users) == 1 and len(answers) == 1, \
        f'the turn was not captured exactly once (user {len(users)}, assistant {len(answers)}):\n{explain(run, "stranger")}'


def test_f1_one_queue_job_is_enqueued(run):
    """GREEN-IF the desk's queue holds exactly one job after the turn's Stop and the terminal's exit."""
    final = _final(run)
    jobs = final.get('jobs') or {}
    assert jobs, f'no receipt names a queue for the launch:\n{explain(run, "stranger")}'
    entries = [job for value in jobs.values() if isinstance(value, list) for job in value]
    assert not [v for v in jobs.values() if not isinstance(v, list)], f'queue status failed: {jobs}'
    assert len(entries) == seams.ONE_JOB, f'expected exactly one queue job, found {len(entries)}: {entries}'


def test_f1_a_desk_search_finds_the_turn_within_t12bs_bound(run):
    """GREEN-IF the desk-scoped `memory.search` finds the answer within 2x the indexer interval (plus one poll)
    of the first poll that lists the episode holding it."""
    poll = launch(run, 'stranger').get('poll') or {}
    assert poll.get('episode_after') is not None, f'the episode never appeared:\n{explain(run, "stranger")}'
    assert poll.get('search_after') is not None, f'the search never found the turn:\n{explain(run, "stranger")}'
    took = poll['search_after'] - poll['episode_after']
    assert took <= seams.SEARCH_BOUND_SECONDS + seams.POLL_SECONDS, \
        f'the search found the turn {took:.2f} s after the episode; bound {seams.SEARCH_BOUND_SECONDS} s: {poll}'
    assert (_final(run).get('search_hits') or 0) >= 1, _final(run)


def test_f1_nothing_is_installed_or_fetched(run):
    """A1. GREEN-IF the turn completed, and during the whole run no connection reached for the npm registry
    or the models host (or any other recorded host), and no installer output appeared."""
    entry = launch(run, 'stranger')
    assert entry.get('launched') and entry.get('answered'), f'the turn never completed:\n{explain(run, "stranger")}'
    assert run.get('_netrec.log') and 'ready' in run['_netrec.log'], f'instrument: the recorder never started: {run}'
    assert not run['_net'], f'the run reached for the network: {run["_net"]}'
    assert not run.get('installArtifacts'), f'installer output appeared: {run.get("installArtifacts")}'


def test_f6_the_providers_key_is_written_nowhere(run):
    """F6 in the image. GREEN-IF the sentinel key is in neither the launched process's environment (as the
    board spawned it and as /proc shows it) nor any file under /state or /tmp nor any log of the run, with
    the controls: the launch was bound (so its receipt and per-launch files exist) and the key file was
    readable in the container."""
    entry = launch(run, 'stranger')
    final = _final(run)
    assert final.get('receipts'), f'control: no receipt for the launch (not bound):\n{explain(run, "stranger")}'
    assert run.get('keyFileReadable'), 'control: the key file was not readable in the container'
    sentinel = run['_sentinel']
    for name in ('env', 'procEnv'):
        environment = entry.get(name) or {}
        leaked = [key for key, value in environment.items() if sentinel in key or sentinel in str(value)]
        assert not leaked, f'the key is in the launched environment ({name}): {leaked}'
        assert not [key for key in seams.KEY_ENV_NAMES if key in environment], f'{name} carries a provider-key variable'
    assert not run.get('sentinelFiles'), f'the key was written into: {run.get("sentinelFiles")}'
