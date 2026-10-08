"""T5 P4: rebuilds are debounced by min_rebuild_interval_seconds.

A cycle that sees new revisions inside the window reports status
"pending_rebuild" with the next eligible time, and does no clone and no index.

Falsifier: two rebuilds inside N, or a clone or index during a pending cycle.
"""
import json
import subprocess
import time

from t5_rig import console_script, core_and_ops, instants

WINDOW = 3600
SLACK = 2


def test_p4_new_revision_inside_window_is_pending_without_clone_or_index(rig):
    core_and_ops(rig, dependent=False)
    rig.request['min_rebuild_interval_seconds'] = WINDOW
    assert rig.refresh()['status'] == 'published'
    started, finished = rig.started, rig.finished
    clones, runs = list(rig.clones), list(rig.index_runs)

    rig.bump('ops')
    result = rig.refresh()
    assert result['status'] == 'pending_rebuild', result
    assert rig.clones == clones, f'clone during a pending cycle: {rig.clones[len(clones):]}'
    assert rig.index_runs == runs, f'index during a pending cycle: {rig.index_runs[len(runs):]}'
    eligible = [t for t in instants(result) if started + WINDOW - SLACK <= t <= finished + WINDOW + SLACK]
    assert eligible, f'pending_rebuild names no next eligible time (last rebuild start + {WINDOW}s): {result}'


def test_p4_console_script_reports_pending_without_clone_or_index(rig):
    core_and_ops(rig, dependent=False)
    rig.request['min_rebuild_interval_seconds'] = WINDOW
    assert rig.refresh()['status'] == 'published'
    clones = list(rig.clones)
    rig.bump('ops')
    completed = subprocess.run([str(console_script()), '--request', str(rig.write_request())],
                               capture_output=True, text=True, timeout=300)
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    report = json.loads(lines[-1]) if lines else {}
    assert (completed.returncode, report.get('status')) == (0, 'pending_rebuild'), (
        completed.returncode, completed.stdout[-2000:], completed.stderr[-2000:])
    assert rig.clones == clones and rig.bypass_runs == []


def test_p4_unchanged_sources_inside_window_stay_current(rig):
    core_and_ops(rig, dependent=False)
    rig.request['min_rebuild_interval_seconds'] = WINDOW
    assert rig.refresh()['status'] == 'published'
    assert rig.refresh()['status'] == 'current'


def test_p4_rebuild_proceeds_once_the_window_has_elapsed(rig):
    core_and_ops(rig, dependent=False)
    rig.request['min_rebuild_interval_seconds'] = 2
    assert rig.refresh()['status'] == 'published'
    time.sleep(max(0.0, rig.finished + 2 + 0.5 - time.time()))
    rig.bump('ops')
    assert rig.refresh()['status'] == 'published'
    assert rig.clones.count('ops') == 2
