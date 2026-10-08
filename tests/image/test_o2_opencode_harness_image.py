"""O2 on the real pinned binary: F1 (the stranger's run, with A1) and F2 (per-launch isolation).

Order: docs/work/orders/O2-opencode-third-harness.md. The in-container runner is
apps/kanban/test/runtime/terminal/o2-opencode-image-runner.ts (bundled here with apps/kanban's esbuild, run
with the image's node and node-pty); the store probe is tests/image/o2_store_probe.py.

Image, by variable (unset FAILS; never skips): AGENT_TOOLING_TEST_IMAGE_OPENCODE, O3's `opencode` target
built from the tree under test, which carries neither claude nor codex nor an agent SDK (O3's and D0f's image
tests hold that). Precondition: `npm ci` in apps/kanban.

Each run is one container with `--network none`. Inside it, /etc/resolv.conf names a DNS recorder that
answers every lookup with 127.0.0.1, where recorders on ports 80 and 443 log the host or TLS server name and
close: so the npm registry and models.opencode.ai stay unreachable, and any attempt to reach them is
recorded. The model is a stub OpenAI-compatible provider on 127.0.0.1 (@ai-sdk/openai-compatible with
baseURL, given through the managed config directory). No key is read; no paid call is possible.

- F1, GREEN-IF a board task launched with harness `opencode` and bound to a desk runs one turn in the TUI;
  the plugin's Stop binds the session to that desk and captures the turn; exactly one queue job is
  enqueued for it; the session's own desk memory server finds the turn's text within 2x the indexer's
  interval of the seal (T12b's bound, the runtime in Compose's mode with the indexer role draining); the
  launched process carries the disable flags and no provider key; nothing was looked up or connected to
  but the stub; nothing was installed; the terminal's exit (SessionEnd) adds nothing.
  The A1 control on the same launch environment: a writable config directory without node_modules as the
  carrier (the order's A1 mutant) IS seen reaching for the npm registry, so the instrument measures.
- F2, GREEN-IF two board tasks bound to two desks, running side by side, each resolve exactly one
  `kp_desk_memory` server, its own launch's, and a call of it answers with that launch's desk as `own`. The
  second task starts once the first is bound, and both are still running when each is probed: measured at
  the pin, two OpenCode TUIs that start within a second of each other on one data directory can stall one of
  them after its `init` log line, bound or not, so F2 does not measure a simultaneous cold start.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from image_harness import REPO_ROOT, docker, remove, required
from opencode_pin import OPENCODE_IMAGE_VARIABLE

pytestmark = pytest.mark.image

RUNNER = 'apps/kanban/test/runtime/terminal/o2-opencode-image-runner.ts'
PROBE = Path(__file__).with_name('o2_store_probe.py')
ESBUILD = 'apps/kanban/node_modules/.bin/esbuild'
LABEL = 'agent-tooling-image-test=o2'
STUB_PORT = 18450
INDEXER_INTERVAL = 2
STUB_PROVIDER_CONFIG = {
    'provider': {'o2stub': {'npm': '@ai-sdk/openai-compatible', 'name': 'o2 stub',
                            'options': {'baseURL': f'http://127.0.0.1:{STUB_PORT}/v1', 'apiKey': 'o2-stub-placeholder'},
                            'models': {'stub-model': {'name': 'o2 stub model', 'tool_call': True}}}},
    'model': 'o2stub/stub-model', 'small_model': 'o2stub/stub-model',
}
# The board host's npm configuration (the container's, never the launch's): with nothing answering, an install
# attempt fails at once instead of retrying, so the A1 control finishes in bounded time.
BOARD_HOST_ENV = {'npm_config_fetch_retries': '0', 'AGENT_MEMORY_VOLUME': 'o2-image-test'}
DISABLE_FLAGS = ('OPENCODE_DISABLE_AUTOUPDATE', 'OPENCODE_DISABLE_SHARE', 'OPENCODE_DISABLE_MODELS_FETCH',
                 'OPENCODE_DISABLE_CLAUDE_CODE')


def _bundle(work: Path) -> Path:
    esbuild = REPO_ROOT / ESBUILD
    if not esbuild.exists():
        pytest.fail(f'{ESBUILD} is missing: run `npm ci` in apps/kanban first', pytrace=False)
    out = work / 'bundle' / 'runner.mjs'
    out.parent.mkdir(parents=True, exist_ok=True)
    banner = ('import { createRequire as __o2_createRequire } from "node:module";'
              'const require = __o2_createRequire(import.meta.url);')
    done = subprocess.run([str(esbuild), str(REPO_ROOT / RUNNER), '--bundle', '--platform=node', '--format=esm',
                           '--target=node20', '--external:node-pty', f'--outfile={out}', f'--banner:js={banner}',
                           '--log-level=warning'], capture_output=True, text=True, timeout=300,
                          cwd=REPO_ROOT / 'apps/kanban')
    if done.returncode:
        pytest.fail(f'bundling the O2 runner failed ({done.returncode}): {done.stderr[-2000:]}', pytrace=False)
    return out


def _run(tmp_path_factory, mode: str, timeout: float) -> dict:
    image = required(OPENCODE_IMAGE_VARIABLE)
    work = tmp_path_factory.mktemp(f'o2-{mode}')
    _bundle(work)
    inputs = work / 'in'
    inputs.mkdir()
    (inputs / 'o2_store_probe.py').write_text(PROBE.read_text())
    (inputs / 'resolv.conf').write_text('nameserver 127.0.0.1\n')
    managed = work / 'managed'
    managed.mkdir()
    (managed / 'opencode.json').write_text(json.dumps(STUB_PROVIDER_CONFIG, indent=1) + '\n')
    for path in (inputs / 'o2_store_probe.py', inputs / 'resolv.conf', managed / 'opencode.json'):
        path.chmod(0o644)
    for path in (inputs, managed, work / 'bundle'):
        path.chmod(0o755)
    out = work / 'out'
    out.mkdir()
    out.chmod(0o777)
    (out / 'spec.json').write_text(json.dumps({'out': '/o2/out/result.json', 'interval': INDEXER_INTERVAL,
                                               'timeoutMs': 240_000}))
    name = f'o2-{mode}-{uuid.uuid4().hex[:10]}'
    script = ('mkdir -p /state/o2b && cp /o2/bundle/runner.mjs /state/o2b/runner.mjs '
              '&& ln -sfn /app/node_modules /state/o2b/node_modules '
              f'&& exec node /state/o2b/runner.mjs {mode} /o2/out/spec.json')
    args = ['run', '--rm', '--pull', 'never', '--name', name, '--label', LABEL, '--network', 'none',
            *[arg for key, value in BOARD_HOST_ENV.items() for arg in ('-e', f'{key}={value}')],
            '--entrypoint', '/bin/sh',
            '-v', f'{(work / "bundle").resolve()}:/o2/bundle:ro',
            '-v', f'{inputs.resolve()}:/o2/in:ro',
            '-v', f'{(inputs / "resolv.conf").resolve()}:/etc/resolv.conf:ro',
            '-v', f'{out.resolve()}:/o2/out',
            '-v', f'{managed.resolve()}:/etc/opencode:ro',
            image, '-c', script]
    try:
        completed = docker(*args, timeout=timeout)
    except subprocess.TimeoutExpired:
        completed = None
    finally:
        remove(name)
    result_path = out / 'result.json'
    result = json.loads(result_path.read_text()) if result_path.exists() else {}
    if completed is None:
        result['_runner'] = f'the {mode} container did not finish within {timeout}s'
    elif completed.returncode:
        result['_runner'] = f'runner exit {completed.returncode}: {(completed.stdout + completed.stderr)[-3000:]}'
    for log in ('hooks.jsonl', 'stub.jsonl', 'egress.jsonl', 'indexer.jsonl'):
        path = out / log
        result[f'_{log}'] = path.read_text()[-4000:] if path.exists() else ''
    return result


@pytest.fixture(scope='module')
def f1(tmp_path_factory) -> dict:
    return _run(tmp_path_factory, 'f1', timeout=900)


@pytest.fixture(scope='module')
def f2(tmp_path_factory) -> dict:
    return _run(tmp_path_factory, 'f2', timeout=900)


def _explain(result: dict) -> str:
    shown = {key: value for key, value in result.items() if key not in ('stubWhileRunning',)}
    return json.dumps(shown, indent=1)[:12000]


def _names(entries) -> list:
    return sorted({entry.get('name') or entry.get('detail') or '?' for entry in entries})


def test_f1_the_turn_is_captured_enqueued_once_and_found_within_the_indexer_bound(f1):
    assert f1.get('launched'), f'the board launch was refused or failed:\n{_explain(f1)}'
    observed = f1.get('observe') or {}
    assert (observed.get('bound') or {}).get('session', '').startswith('ses_'), f'never bound:\n{_explain(f1)}'
    counts = observed.get('counts') or {}
    assert counts.get('harness') == 'opencode' and counts.get('desk_id') == f1['desk']['deskId'], _explain(f1)
    [episode] = counts.get('episodes') or [None]
    assert episode, f'nothing was captured:\n{_explain(f1)}'
    roles = {(event['role'], event['text']) for event in episode['events']}
    assert any(role == 'user' and 'o2-turn token=' in body for role, body in roles), roles
    assert any(role == 'assistant' and body.startswith('o2 answer ') for role, body in roles), roles
    assert all(event['event_id'].startswith('prt_') for event in episode['events']), episode['events']
    assert [job['episodes'] for job in counts.get('jobs', [])] == [[episode['episode_id']]], counts.get('jobs')
    assert observed.get('seal_to_search_s') is not None, f'the desk search never found the turn:\n{_explain(f1)}'
    assert observed['seal_to_search_s'] <= 2 * INDEXER_INTERVAL, f'not within T12b bound:\n{_explain(f1)}'


def test_f1_the_launch_reaches_nothing_but_the_stub_and_installs_nothing(f1):
    assert f1.get('launched'), _explain(f1)
    env = f1['launch']['opencodeEnv']
    assert all(env.get(flag) == '1' for flag in DISABLE_FLAGS), env
    assert f1['launch']['providerKeys'] == [], f1['launch']
    content = json.loads(env['OPENCODE_CONFIG_CONTENT'])
    assert list(content) == ['mcp'] and list(content['mcp']) == ['kp_desk_memory'], content
    offered = {tool for entry in f1.get('stubWhileRunning', []) for tool in entry.get('tools', [])}
    assert any('kp_desk_memory' in tool for tool in offered), f'the per-launch server never reached OpenCode: {sorted(offered)}'
    assert f1['egressDuringLaunch'] == [], f'the launch looked something up or connected: {_names(f1["egressDuringLaunch"])}'
    assert f1['egressAfterSessionEnd'] == [], _names(f1['egressAfterSessionEnd'])
    dirs = f1['configDirs']
    assert dirs['boundConfigHome'] == [] and dirs['boundConfigHomeMode'] == '555', dirs
    assert dirs['homeOpencode'] is None, dirs


def test_f1_session_end_at_the_terminal_exit_adds_nothing(f1):
    """The hook ran twice, as /proc shows its detached processes: at the turn's end (the plugin's Stop) and at
    the terminal's exit (the board's SessionEnd), each with its `--pure` export child; the second added nothing."""
    kinds = lambda name: sorted({entry['kind'] for entry in f1.get(name) or []})  # noqa: E731
    assert kinds('processesDuringTurn') == ['export-child', 'launch-hook'], _explain(f1)
    assert kinds('processesAfterExit') == ['export-child', 'launch-hook'], _explain(f1)
    before, after = (f1.get('observe') or {}).get('counts') or {}, f1.get('afterSessionEnd') or {}
    assert after.get('episodes') == before.get('episodes') and after.get('jobs') == before.get('jobs'), _explain(f1)
    assert after.get('ledger', {}).get('pending') in (None, [None]), after.get('ledger')


def test_f1_a1_control_a_writable_carrier_directory_is_seen_reaching_for_the_registry(f1):
    """Not a falsifier: the order's A1 mutant on the same launch environment. The instrument must see it."""
    control = f1.get('controlWritableCarrier') or {}
    assert 'registry.npmjs.org' in _names(control.get('egress', [])), f'the instrument did not see the install:\n{json.dumps(control)[:3000]}'
    quiet = f1.get('controlBoundEnvironmentRun') or {}
    assert quiet.get('status') == 0 and quiet.get('egress') == [], json.dumps(quiet)[:3000]


def test_f2_each_concurrent_launch_sees_only_its_own_desk_memory_server(f2):
    launches = f2.get('launches') or []
    assert len(launches) == 2, _explain(f2)
    assert all(state in ('running', 'awaiting_review') for state in f2.get('bothRunning') or [None]), _explain(f2)
    memory_configs = []
    for row in launches:
        assert (row.get('bound') or {}).get('session'), f'{row["task"]} never bound:\n{_explain(f2)}'
        mcp = row.get('mcp') or {}
        assert list(mcp) == ['kp_desk_memory'], mcp
        command = mcp['kp_desk_memory']['command']
        assert str(Path(row['receipt']).parent / 'memory.json') in command, command
        memory_configs.append(command[command.index('--config') + 1])
        answered = [entry for entry in row.get('stub', []) if entry.get('toolResults')]
        assert any(entry.get('call') for entry in row.get('stub', [])), f'no memory call was issued:\n{json.dumps(row)[:3000]}'
        assert answered, f'no tool result came back:\n{json.dumps(row)[:3000]}'
        result = json.loads(answered[-1]['toolResults'][0])
        own = [binding['binding_key'] for binding in result['bindings'] if binding['own']]
        assert own == [row['bindingKey']], (own, row['bindingKey'])
    assert len(set(memory_configs)) == 2, memory_configs
