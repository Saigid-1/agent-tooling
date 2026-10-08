"""T12a A1: the workspace-capture watch loop survives a failing pass (in-process).

Order: docs/work/orders/T12a-capture-survives.md, A1, frozen at its merge commit.

- A pass that raises anything reports one JSON line: `status: error`, a `category` (the exception
  class family), the message capped at 512 characters, and the attempt number. The loop then sleeps
  with bounded exponential backoff, from the interval up to 10x the interval; a clean pass resets it.
- Only a signal, reaching `--max-passes`, or operator-file absence ends or parks the loop.
- Test seam (S7): `watch --max-passes N`. Sleeps are recorded through an injectable clock, never by
  patching `time.sleep`.
Falsifier: `main(['--config', ..., '--policy', ..., 'watch', '--max-passes', '6', '--interval-seconds',
'5'])` with failures injected on passes 2-4 (`CaptureError`, `sqlite3.OperationalError('database is
locked')`, `RuntimeError`) does not complete pass 6, does not print three error lines each with its
category, or does not back off.

The run is real: a portable admission (tests/test_portable_desk_memory.py), a private capture policy on
empty native roots, and the `kp-agent-workspace-capture` entry point `main`. Passes 1, 5 and 6 run the
real `WorkspaceCapture.once`; passes 2-4 raise instead of running it.

THE SEAM THIS ARM ASSUMES (named in the arm report; the meet reconciles it with FEATURE's):
- `kp_agent_tooling.workspace_capture_cli.main(argv, clock=<clock>)`: `main` takes a keyword argument
  `clock`; the watch loop waits only through `clock.sleep(seconds)`. The fake clock below also offers
  `monotonic()` and `time()`, which advance by exactly what was slept, so a loop that measures its
  deadline with them sees consistent time.
- A pass is one call of `WorkspaceCapture.once` (kp_agent_tooling._impl.service.workspace_capture). Failures
  are injected there, at the class, so the injection does not depend on how the CLI imports it.

Readings (repeated in the arm report under AMBIGUITY):
- "category (the exception class family)": a non-empty string; the three injected classes, from three
  different families, give three different categories. Its spelling is not pinned.
- "the attempt number": an integer under a key containing `attempt`; it increases across the three
  consecutive failures (1, 2, 3 and 2, 3, 4 both read as attempt numbers).
- The wait after a pass is the total the clock slept between that pass and the next (a loop may sleep
  in slices). "From the interval up to 10x": the first wait after a failure is at least the interval
  (less 20%), every wait is at most 10x the interval, and the wait grows across consecutive failures.
  "Resets": the wait after a clean pass that follows failures is the interval again (within 20%, which
  leaves room for jitter the order neither asks for nor forbids).
- "`--max-passes` ends the loop": `main` returns (a `SystemExit` is accepted as a return) after exactly N
  passes. Its exit code is not pinned; it is recorded in the failure message.

Added at the meet (Coordinator mutation-RED, survivors U1 and U2):
- U1, "a clean pass resets it" also resets the NEXT failure: a failure after a clean pass backs off as the
  first failure did (its wait equals the first backoff, within 20%) and carries the first failure's attempt
  number. This narrows "the attempt number" above: it counts consecutive failures since the last clean
  pass (a pass number would not repeat).
- U2, "only a deliberate stop (a signal) ... ends the loop": a pass that raises KeyboardInterrupt (what
  SIGINT raises in Python), or SystemExit, ends the loop: no later pass starts and no `status: error` line
  is printed for it. Reading: the loop "ends" when `main` returns, or when the injected exception (or a
  SystemExit, a CLI's own way of ending with a code) propagates out of `main`; any other exception is not
  an end but a crash, and fails the test.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from test_portable_desk_memory import config, episode_store
from test_workspace_capture_adversarial import _repo, _worker
from kp_agent_tooling._impl.service import workspace_capture as capture_module
from kp_agent_tooling._impl.service.workspace_capture import CaptureError
from kp_agent_tooling import workspace_capture_cli

INTERVAL = 5
CAP = 10 * INTERVAL
SLACK = 0.2
LONG_MESSAGE = 'database is locked; ' + 'x' * 2000
# A runaway loop (one that ignores --max-passes or the clock) is stopped by these, never by a hang.
RUNAWAY_PASSES = 40
RUNAWAY_SLEEPS = 400


class Runaway(BaseException):
    """Raised when the loop runs far past what the test asked for. A BaseException, so that a loop
    which (rightly) catches every Exception cannot swallow it."""


class FakeClock:
    """The injectable clock: records every sleep and advances its own time by it; never really sleeps."""

    def __init__(self):
        self.now = 1_000_000.0
        self.slept = 0.0
        self.calls = []

    def sleep(self, seconds):
        seconds = float(seconds)
        self.calls.append(seconds)
        if len(self.calls) > RUNAWAY_SLEEPS:
            raise Runaway(f'the loop slept {len(self.calls)} times')
        assert seconds >= 0, f'negative sleep {seconds}'
        self.slept += seconds
        self.now += seconds

    def monotonic(self):
        return self.now

    def time(self):
        return self.now


class Passes:
    """`WorkspaceCapture.once`, counted: pass k raises `failures[k]` when it names one, else runs the real
    method. Records the clock's sleep total when each pass starts."""

    def __init__(self, real, clock, failures):
        self.real, self.clock, self.failures = real, clock, dict(failures)
        self.started = []    # clock.slept at the start of each pass
        self.completed = []  # numbers of the passes that returned

    def install(self, monkeypatch):
        passes = self

        def once(worker):
            number = len(passes.started) + 1
            if number > RUNAWAY_PASSES:
                raise Runaway(f'pass {number}: the loop did not stop')
            passes.started.append(passes.clock.slept)
            failure = passes.failures.get(number)
            if failure is not None:
                raise failure
            result = passes.real(worker)
            passes.completed.append(number)
            return result

        monkeypatch.setattr(capture_module.WorkspaceCapture, 'once', once)

    def waits(self):
        """The total slept after pass k and before pass k+1, for each k that has a next pass."""
        return [later - earlier for earlier, later in zip(self.started, self.started[1:])]


def _files(tmp_path):
    store = episode_store(tmp_path)
    repo = _repo(tmp_path / 'repo')
    codex = tmp_path / 'codex'
    codex.mkdir()
    worker = _worker(tmp_path, store, repo, codex)
    policy = tmp_path / 'policy-t12a.json'
    policy.write_text(json.dumps(worker.policy))
    policy.chmod(0o600)
    return config(tmp_path, session='session-1', instance='fixture'), policy


def _watch(tmp_path, monkeypatch, capsys, *, max_passes, failures):
    """Run `watch --max-passes N --interval-seconds 5` in-process; return (exit, passes, clock, JSON lines)."""
    configuration, policy = _files(tmp_path)
    clock = FakeClock()
    passes = Passes(capture_module.WorkspaceCapture.once, clock, failures)
    passes.install(monkeypatch)
    argv = ['--config', str(configuration), '--policy', str(policy), 'watch',
            '--max-passes', str(max_passes), '--interval-seconds', str(INTERVAL)]
    capsys.readouterr()
    try:
        code = workspace_capture_cli.main(argv, clock=clock)
    except SystemExit as stopped:
        code = ('SystemExit', stopped.code)
    out, err = capsys.readouterr()
    lines = []
    for raw in out.splitlines():
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict):
            lines.append(value)
    return code, passes, clock, lines, out, err


def _story(code, passes, clock, out, err):
    return (f'\nexit={code!r} passes started={len(passes.started)} completed={passes.completed} '
            f'waits={passes.waits()} sleeps={clock.calls[:30]}\nstdout={out[-3000:]}\nstderr={err[-2000:]}')


def _three_failures():
    return {2: CaptureError('git identity check failed (transient)'),
            3: sqlite3.OperationalError('database is locked'),
            4: RuntimeError(LONG_MESSAGE)}


def _run_until_stopped(tmp_path, monkeypatch, capsys, *, max_passes, failures):
    """As `_watch`, but every BaseException out of `main` (other than the test's own Runaway) is returned
    as the outcome instead of raised: ('returned', code) or ('raised', exception)."""
    configuration, policy = _files(tmp_path)
    clock = FakeClock()
    passes = Passes(capture_module.WorkspaceCapture.once, clock, failures)
    passes.install(monkeypatch)
    argv = ['--config', str(configuration), '--policy', str(policy), 'watch',
            '--max-passes', str(max_passes), '--interval-seconds', str(INTERVAL)]
    capsys.readouterr()
    try:
        outcome = ('returned', workspace_capture_cli.main(argv, clock=clock))
    except Runaway:
        raise
    except BaseException as stopped:  # noqa: B036 - the outcome under test is exactly what escapes
        outcome = ('raised', stopped)
    out, err = capsys.readouterr()
    lines = []
    for raw in out.splitlines():
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict):
            lines.append(value)
    return outcome, passes, clock, lines, out, err


def _errors(lines):
    return [line for line in lines if line.get('status') == 'error']


def _attempt(line):
    found = [value for key, value in line.items() if 'attempt' in key and isinstance(value, int)
             and not isinstance(value, bool)]
    return found[0] if len(found) == 1 else None


def test_a1_watch_completes_pass_six_through_three_failing_passes(tmp_path, monkeypatch, capsys):
    """GREEN-IF `watch --max-passes 6`, with passes 2-4 raising CaptureError, sqlite3.OperationalError
    ('database is locked') and RuntimeError, runs exactly six passes, pass 6 returns and its report is
    printed as a non-error JSON line after the three error lines, and `main` returns."""
    code, passes, clock, lines, out, err = _watch(tmp_path, monkeypatch, capsys, max_passes=6,
                                                  failures=_three_failures())
    story = _story(code, passes, clock, out, err)
    assert len(passes.started) == 6, f'the loop ran {len(passes.started)} passes, not 6' + story
    assert passes.completed == [1, 5, 6], f'passes 1, 5 and 6 did not all complete' + story
    reports = [line for line in lines if line.get('status') != 'error']
    assert len(reports) >= 3, f'the three clean passes did not each print their report' + story
    assert lines and lines[-1].get('status') != 'error', 'the last line is not pass 6\'s report' + story


def test_a1_each_failing_pass_prints_one_error_line_with_category_message_and_attempt(
        tmp_path, monkeypatch, capsys):
    """GREEN-IF the three failing passes print exactly three JSON lines with `status: error`, in order, each
    with a non-empty `category` (three different categories for the three families), a `message` of at
    most 512 characters (the 2000-character one capped, not dropped), and an integer attempt number that
    increases across the three."""
    code, passes, clock, lines, out, err = _watch(tmp_path, monkeypatch, capsys, max_passes=6,
                                                  failures=_three_failures())
    story = _story(code, passes, clock, out, err)
    errors = _errors(lines)
    assert len(errors) == 3, f'{len(errors)} error lines, not one per failing pass (3)' + story
    categories = [line.get('category') for line in errors]
    assert all(isinstance(c, str) and c.strip() for c in categories), f'an error line has no category' + story
    assert len(set(categories)) == 3, f'three exception families share categories {categories}' + story
    messages = [line.get('message') for line in errors]
    assert all(isinstance(m, str) and m for m in messages), f'an error line has no message' + story
    assert all(len(m) <= 512 for m in messages), f'a message exceeds 512 characters' + story
    assert 'database is locked' in messages[1], f'the OperationalError message is not reported' + story
    assert 'database is locked; xxx' in messages[2] and len(messages[2]) >= 256, (
        'the 2000-character message is not reported capped (dropped or summarised instead)' + story)
    attempts = [_attempt(line) for line in errors]
    assert all(a is not None for a in attempts), f'an error line has no integer attempt number' + story
    assert attempts[0] < attempts[1] < attempts[2], f'attempt numbers {attempts} do not increase' + story


def test_a1_backoff_grows_from_the_interval_and_resets_after_a_clean_pass(tmp_path, monkeypatch, capsys):
    """GREEN-IF, through the injected clock: the wait after clean pass 1 is the interval; the waits after
    failing passes 2, 3 and 4 start at least at the interval, never exceed 10x it, and grow; the wait after
    clean pass 5 is the interval again."""
    code, passes, clock, lines, out, err = _watch(tmp_path, monkeypatch, capsys, max_passes=6,
                                                  failures=_three_failures())
    story = _story(code, passes, clock, out, err)
    waits = passes.waits()
    assert len(waits) == 5, f'six passes with a wait between each were not observed' + story
    assert clock.slept > 0, 'nothing slept through the injected clock' + story
    after_clean, backoff, after_reset = waits[0], waits[1:4], waits[4]
    assert after_clean == pytest.approx(INTERVAL, rel=SLACK), f'the wait after a clean pass is not the interval' + story
    assert backoff[0] >= INTERVAL * (1 - SLACK), f'the first backoff {backoff[0]} is below the interval' + story
    assert all(wait <= CAP + 1e-6 for wait in backoff), f'a backoff exceeds 10x the interval ({CAP})' + story
    assert backoff[0] <= backoff[1] <= backoff[2] and backoff[2] > backoff[0], (
        f'the backoff {backoff} does not grow across consecutive failures' + story)
    assert after_reset == pytest.approx(INTERVAL, rel=SLACK), (
        f'the wait after a clean pass {after_reset} did not reset to the interval' + story)


def test_a1_backoff_never_exceeds_ten_intervals(tmp_path, monkeypatch, capsys):
    """GREEN-IF, with passes 1-9 all raising and `--max-passes 10`, every wait is at most 10x the interval,
    the waits never shrink while the failures continue, and pass 10 still runs."""
    failures = {n: RuntimeError(f'failure {n}') for n in range(1, 10)}
    code, passes, clock, lines, out, err = _watch(tmp_path, monkeypatch, capsys, max_passes=10, failures=failures)
    story = _story(code, passes, clock, out, err)
    assert len(passes.started) == 10 and passes.completed == [10], f'pass 10 did not run and complete' + story
    waits = passes.waits()
    assert len(waits) == 9, story
    assert all(wait <= CAP + 1e-6 for wait in waits), f'a wait exceeds 10x the interval ({CAP})' + story
    assert all(a <= b for a, b in zip(waits, waits[1:])), f'the backoff shrank during failures' + story
    assert waits[-1] > waits[0], f'the backoff never grew' + story


def test_a1_max_passes_ends_a_clean_loop(tmp_path, monkeypatch, capsys):
    """GREEN-IF `watch --max-passes 2` with no failure runs exactly two passes, prints two reports and no
    error line, and `main` returns."""
    code, passes, clock, lines, out, err = _watch(tmp_path, monkeypatch, capsys, max_passes=2, failures={})
    story = _story(code, passes, clock, out, err)
    assert len(passes.started) == 2 and passes.completed == [1, 2], f'not exactly two passes' + story
    assert not _errors(lines), f'a clean run printed an error line' + story
    assert len([line for line in lines if line.get('status') != 'error']) >= 2, story


def test_a1_a_failure_after_a_clean_pass_backs_off_from_the_start_again(tmp_path, monkeypatch, capsys):
    """U1. GREEN-IF `watch --max-passes 7`, with passes 2, 3, 4 and 6 raising and pass 5 clean, runs seven
    passes and prints four error lines, and the failure after the clean pass starts over: the wait after
    pass 6 equals the wait after pass 2 (the first backoff, within 20%), and pass 6's error line carries the
    same attempt number as pass 2's."""
    failures = {**_three_failures(), 6: RuntimeError('a failure after a clean pass')}
    code, passes, clock, lines, out, err = _watch(tmp_path, monkeypatch, capsys, max_passes=7, failures=failures)
    story = _story(code, passes, clock, out, err)
    assert len(passes.started) == 7 and passes.completed == [1, 5, 7], f'not seven passes as injected' + story
    errors = _errors(lines)
    assert len(errors) == 4, f'{len(errors)} error lines, not one per failing pass (4)' + story
    waits = passes.waits()
    first, again = waits[1], waits[5]
    assert first > 0 and again == pytest.approx(first, rel=SLACK), (
        f'the wait after the failure that follows a clean pass ({again}) is not the first backoff ({first})'
        + story)
    attempts = [_attempt(line) for line in errors]
    assert attempts[0] is not None and attempts[3] == attempts[0], (
        f'the failure after a clean pass carries attempt {attempts[3]}, not the first failure\'s '
        f'{attempts[0]} (attempts {attempts})' + story)


@pytest.mark.parametrize('stop', [KeyboardInterrupt(), SystemExit(0)], ids=['KeyboardInterrupt', 'SystemExit'])
def test_a1_a_deliberate_stop_ends_the_loop(tmp_path, monkeypatch, capsys, stop):
    """U2. GREEN-IF, with `--max-passes 5` and pass 2 raising KeyboardInterrupt (or SystemExit), no pass after
    pass 2 starts, no `status: error` line is printed, and `main` either returns or lets the injected
    exception (or a SystemExit) out; any other exception out of `main` fails."""
    outcome, passes, clock, lines, out, err = _run_until_stopped(tmp_path, monkeypatch, capsys, max_passes=5,
                                                                 failures={2: stop})
    story = (f'\noutcome={outcome!r} passes started={len(passes.started)} completed={passes.completed} '
             f'sleeps={clock.calls[:10]}\nstdout={out[-2000:]}\nstderr={err[-1500:]}')
    assert len(passes.started) == 2, (f'{len(passes.started)} passes started, not 2: pass 2 raised '
                                      f'{type(stop).__name__} and must be the last' + story)
    assert not _errors(lines), f'a {type(stop).__name__} was reported as a failing pass' + story
    kind, value = outcome
    assert kind == 'returned' or isinstance(value, (type(stop), SystemExit)), (
        f'{type(stop).__name__} did not end `main` (it returned, or the stop propagated); instead '
        f'{type(value).__name__} escaped' + story)
