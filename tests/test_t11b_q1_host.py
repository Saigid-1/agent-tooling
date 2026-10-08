"""T11b Q1 outside Docker: the variable's direction (docs/work/orders/T11b-leaf-behaviour.md, Q1).

Order, Q1: "The leaf treats a process as being in the Docker runtime when AGENT_MEMORY_VOLUME is
set." ... "Direction of the variable. A host process that sets the variable only refuses more. A
host install without it is exempt by design, as T9b P5 scoped." Positive control (f): "a host
install with no AGENT_MEMORY_VOLUME keeps its own state_root working" (in-process, not
image-marked). The image-marked falsifiers (a)-(e) are tests/install/test_t11b_q1_store_paths_image.py.

Two tests here:
- (f), in-process: with AGENT_MEMORY_VOLUME unset, a host state_root outside /state/memory works for
  the desk-memory lifecycle (``kp-agent-desk`` initialize, admit, context, through ``desk_cli.main``)
  and a registry launch (``launch_binding.prepare``), and their stores land under that state_root;
- the other direction, in a fresh host interpreter (so a value read at import is seen): with
  AGENT_MEMORY_VOLUME set, ``kp-agent-desk`` initialize (an empty state_root) and context (a populated
  one) and ``kp-agent-launch prepare`` are refused with ``store_outside_volume`` naming the path, and
  under the audit hook of tests/t11b_audit.py no file below the state_root is opened (``open``,
  ``sqlite3.connect``), no directory at or below it is made (``os.mkdir``), and its tree is unchanged.

Readings (repeated under AMBIGUITY): "refused" is a non-zero exit with no Python traceback whose
output contains ``store_outside_volume`` and the state_root (as given or resolved); "a store file"
is any path below the state_root (its derivations: the stores, launches/, receipts, per-launch
configs).
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'launch'))

from t11b_audit import TRACEBACK, describe, run_host, touches, without_record  # noqa: E402

VARIABLE = 'AGENT_MEMORY_VOLUME'
REFUSAL = 'store_outside_volume'


def tree(root: Path) -> dict:
    out = {}
    for path in sorted(root.rglob('*')):
        info = path.lstat()
        kind = 'link' if stat.S_ISLNK(info.st_mode) else 'dir' if stat.S_ISDIR(info.st_mode) else 'file'
        out[str(path.relative_to(root))] = (kind, oct(stat.S_IMODE(info.st_mode)),
                                            hashlib.sha256(path.read_bytes()).hexdigest() if kind == 'file' else None)
    return out


def desk_config(tmp_path: Path, name: str):
    from test_portable_desk_memory import config
    base = tmp_path / name
    base.mkdir()
    return config(base, 'session-q1', 'q1-host'), base / 'state'


def in_runtime_env(home: Path | None = None) -> dict:
    env = dict(os.environ)
    env[VARIABLE] = 't11b-host_memory'
    if home is not None:
        env['HOME'] = str(home)
        for name in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME'):
            env.pop(name, None)
    return env


def assert_refused(proc, record, root: Path, what: str, before: dict):
    shown = proc.stdout + without_record(proc.stderr)
    problems = []
    if record is None:
        problems.append('the audit wrapper left no record')
    elif record['crashed'] or TRACEBACK in shown:
        problems.append('the CLI crashed (a Python traceback); a crash is not a refusal')
    if proc.returncode == 0:
        problems.append('it exited 0')
    if REFUSAL not in shown:
        problems.append(f'its output does not name `{REFUSAL}`')
    if str(root) not in shown and str(root.resolve()) not in shown:
        problems.append(f'its output does not name the path {root}')
    opened = touches(record, str(root)) + (touches(record, str(root.resolve())) if root.resolve() != root else []) \
        if record else []
    if opened:
        problems.append('store files opened or created below the state_root:\n  ' + '\n  '.join(opened))
    after = tree(root)
    if after != before:
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        problems.append(f'the state_root changed: {changed}')
    assert not problems, f'{what}: not refused as Q1 states:\n- ' + '\n- '.join(problems) + '\n' + describe(proc, record)


# ------------------------------------------------------------------------------------------- (f)

def test_q1f_host_install_without_the_variable_keeps_its_own_state_root(tmp_path, monkeypatch, capsys):
    """GREEN-IF, with AGENT_MEMORY_VOLUME unset, ``kp-agent-desk`` initialize, admit and context (desk_cli.main,
    in-process) exit 0 on a host state_root outside /state/memory and its stores are created there; and a
    registry launch prepared in-process (codex, hook strategy) writes its receipt under <state_root>/launches."""
    monkeypatch.delenv(VARIABLE, raising=False)
    from kp_agent_tooling import desk_cli
    config, state = desk_config(tmp_path, 'desk')
    for argv in (['initialize'], ['admit', '--desk-id', 'implementation-desk', '--provider-id', 'test',
                                  '--model-id', 'model-a'], ['context']):
        code = desk_cli.main(['--config', str(config), *argv])
        out = capsys.readouterr()
        assert code == 0, f'kp-agent-desk {argv[0]} on a host state_root failed ({code}): {out.out[-800:]} {out.err[-800:]}'
    for name in ('sessions.sqlite3', 'episodes.sqlite3'):
        assert (state / name).is_file(), f'{name} was not created under the host state_root {state}'

    from t3_harness import World
    from kp_agent_tooling._impl.service import launch_binding
    world = World.create(tmp_path / 'launch')
    desk = world.save_desk()
    monkeypatch.setenv('HOME', str(world.root / 'home'))
    for name in ('CLAUDE_CONFIG_DIR', 'CODEX_HOME'):
        monkeypatch.delenv(name, raising=False)
    prepared = launch_binding.prepare(world.operator, world.request(harness='codex', desk_id=desk))
    receipt = Path(prepared['receipt_path'])
    assert receipt.is_file() and receipt.parent.parent == world.state / 'launches', (
        f'the launch receipt is not under <state_root>/launches: {receipt}')


# ------------------------------------------------------------------------------ the variable set

def test_q1_host_process_that_sets_the_variable_refuses_desk_stores_outside_the_volume(tmp_path, monkeypatch):
    """GREEN-IF, in a fresh interpreter with AGENT_MEMORY_VOLUME set, ``kp-agent-desk initialize`` on an empty host
    state_root and ``kp-agent-desk context`` on a populated one each exit non-zero naming ``store_outside_volume``
    and the state_root, open no file below it and make no directory at or below it, and leave it unchanged."""
    monkeypatch.delenv(VARIABLE, raising=False)
    from kp_agent_tooling._impl.service.desk_memory_runtime import admit, initialize
    empty_config, empty = desk_config(tmp_path, 'empty')
    populated_config, populated = desk_config(tmp_path, 'populated')
    initialize(populated_config)
    admit(populated_config, desk_id='implementation-desk', provider_id='test', model_id='model-a')
    for config, root, action in ((empty_config, empty, 'initialize'), (populated_config, populated, 'context')):
        before = tree(root)
        proc, record = run_host('kp_agent_tooling.desk_cli:main', ['--config', config, action],
                                argv0='kp-agent-desk', watch=[str(tmp_path), str(tmp_path.resolve())],
                                env=in_runtime_env())
        assert_refused(proc, record, root, f'kp-agent-desk {action} with {VARIABLE} set', before)


def test_q1_host_process_that_sets_the_variable_refuses_a_launch_outside_the_volume(tmp_path, monkeypatch):
    """GREEN-IF, in a fresh interpreter with AGENT_MEMORY_VOLUME set, ``kp-agent-launch --config <operator> prepare``
    (a valid codex request on a host registry) exits non-zero naming ``store_outside_volume`` and the state_root,
    opens no file below the state_root, creates no directory (``launches/`` included) and leaves it unchanged."""
    monkeypatch.delenv(VARIABLE, raising=False)
    from t3_harness import World
    world = World.create(tmp_path / 'launch')
    desk = world.save_desk()
    before = tree(world.state)
    proc, record = run_host('kp_agent_tooling.launch_cli:main', ['--config', world.operator, 'prepare'],
                            argv0='kp-agent-launch', watch=[str(tmp_path), str(tmp_path.resolve())],
                            env=in_runtime_env(world.root / 'home'),
                            stdin=json.dumps(world.request(harness='codex', desk_id=desk)))
    assert_refused(proc, record, world.state, f'kp-agent-launch prepare with {VARIABLE} set', before)
