"""One container run of the O2 image tests (order docs/work/orders/O2-opencode-third-harness.md, F1 and F2).

Not a test module. Seams: tests/o2_seams.py. The image is O3's `opencode` target named by its variable
(`opencode_pin.OPENCODE_IMAGE_VARIABLE`), built from the tree under test; an unset variable FAILS.
Precondition: `npm ci` in apps/kanban (the runner is bundled with its esbuild, as O1's is).

The container (O2-S8..S12):
- `--network none`, with the npm registry and the models host resolved to 127.0.0.1 (`--add-host`) where
  `o2_container.py netrec` records every connection on 443 and 80, and the image's user may bind them;
- AGENT_MEMORY_VOLUME set and the registry's stores under /state/memory, with the compose indexer command
  running beside the board, as in the runtime's board and indexer roles;
- OpenCode's managed config directory carries the stub provider; the operator key file the gateway uses
  is mounted read-only with a sentinel key in it;
- the board runs the O2 runner (apps/kanban/test/runtime/terminal/o2-test-image-runner.ts).

O2_IMAGE_EXTRA_RUN_ARGS (a JSON list in the environment, default none) is appended to `docker run`'s options: a
meet mounts a mutant module over the image's copy with it (`-v <file>:<site-packages path>:ro`).
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

import o2_seams as seams
from image_harness import REPO_ROOT, docker, remove, required
from opencode_pin import OPENCODE_IMAGE_VARIABLE

LABEL = 'agent-tooling-image-test=o2'
TOOLS = Path(__file__).resolve().parent / 'o2_container.py'
KEY_DIR = str(Path(seams.KEY_FILE_IN_IMAGE).parent)
EXTRA_RUN_ARGS = 'O2_IMAGE_EXTRA_RUN_ARGS'


def bundle(work: Path) -> Path:
    esbuild = REPO_ROOT / seams.ESBUILD
    if not esbuild.exists():
        pytest.fail(f'{seams.ESBUILD} is missing: run `npm ci` in apps/kanban first (precondition of the O2 image '
                    'tests)', pytrace=False)
    out = work / 'bundle' / 'runner.mjs'
    out.parent.mkdir(parents=True, exist_ok=True)
    banner = ('import { createRequire as __o2_createRequire } from "node:module";'
              'const require = __o2_createRequire(import.meta.url);')
    result = subprocess.run(
        [str(esbuild), str(REPO_ROOT / seams.RUNNER), '--bundle', '--platform=node', '--format=esm', '--target=node20',
         '--external:node-pty', f'--outfile={out}', f'--banner:js={banner}', '--log-level=warning'],
        capture_output=True, text=True, timeout=300, cwd=REPO_ROOT / 'apps/kanban')
    if result.returncode:
        pytest.fail(f'bundling the O2 runner failed ({result.returncode}): {result.stderr[-2000:]}', pytrace=False)
    return out


def run_container(tmp_path_factory, name: str, *, desks: list[str], launches: list[dict], instrument: bool,
                  timeout: float = 1800) -> dict:
    """Run the launches in one container; return the runner's result plus the recorder's and roles' logs."""
    image = required(OPENCODE_IMAGE_VARIABLE)
    work = tmp_path_factory.mktemp(f'o2-{name}')
    bundle(work)
    tools = work / 'tools'
    tools.mkdir()
    (tools / 'o2_container.py').write_text(TOOLS.read_text())
    out = work / 'out'
    out.mkdir()
    out.chmod(0o777)
    managed = work / 'managed'
    managed.mkdir()
    (managed / 'opencode.json').write_text(json.dumps(seams.STUB_PROVIDER_CONFIG, indent=1) + '\n')
    (managed / 'opencode.json').chmod(0o644)
    managed.chmod(0o755)
    keys = work / 'keys'
    keys.mkdir()
    sentinel = 'sk-or-v1-o2sentinel' + uuid.uuid4().hex
    key_file = keys / Path(seams.KEY_FILE_IN_IMAGE).name
    key_file.write_text(sentinel + '\n')
    key_file.chmod(0o644)
    keys.chmod(0o755)
    logs = ['/o2/out/netrec.log', '/o2/out/world.log', '/o2/out/indexer.log', '/o2/out/runner.log']
    spec = {'world_out': '/o2/out/world.json', 'result': '/o2/out/result.json', 'desks': desks,
            'desk_uuids': [str(uuid.uuid4()) for _ in desks], 'launches': launches, 'instrument': instrument,
            'key_sentinel': sentinel, 'scan_roots': ['/state', '/tmp'], 'settle_seconds': 15, 'extra_logs': logs,
            'seams': {**seams.as_json(), 'harness': seams.HARNESS}}
    (out / 'spec.json').write_text(json.dumps(spec))
    container = f'o2-{name}-{uuid.uuid4().hex[:10]}'
    indexer = ' '.join(seams.INDEXER_COMMAND)
    script = ('set -u; mkdir -p /state/o2 && cp /o2/bundle/runner.mjs /state/o2/runner.mjs '
              '&& ln -sfn /app/node_modules /state/o2/node_modules; '
              f'python3 /o2/tools/o2_container.py netrec /o2/out/net.jsonl {" ".join(map(str, seams.RECORDER_PORTS))} '
              '> /o2/out/netrec.log 2>&1 & '
              'python3 /o2/tools/o2_container.py world /o2/out/spec.json /o2/out/world.json > /o2/out/world.log 2>&1 '
              '|| { cat /o2/out/world.log; exit 3; }; '
              f'{indexer} > /o2/out/indexer.log 2>&1 & '
              'node /state/o2/runner.mjs run /o2/out/spec.json > /o2/out/runner.log 2>&1; code=$?; '
              'tail -c 3000 /o2/out/runner.log; exit $code')
    args = ['run', '--rm', '--pull', 'never', '--name', container, '--label', LABEL, '--network', 'none',
            '--add-host', f'{seams.NPM_REGISTRY_HOST}:127.0.0.1', '--add-host', f'{seams.MODELS_HOST}:127.0.0.1',
            '--sysctl', seams.UNPRIVILEGED_PORTS_SYSCTL,
            '-e', f'{seams.VOLUME_VARIABLE}={seams.VOLUME_VALUE}',
            '-e', f'{seams.KEY_FILE_VARIABLE}={seams.KEY_FILE_IN_IMAGE}',
            *[arg for key, value in seams.BOARD_HOST_ENV.items() for arg in ('-e', f'{key}={value}')],
            '--entrypoint', '/bin/sh',
            '-v', f'{(work / "bundle").resolve()}:/o2/bundle:ro',
            '-v', f'{tools.resolve()}:/o2/tools:ro',
            '-v', f'{out.resolve()}:/o2/out',
            '-v', f'{managed.resolve()}:{seams.STUB_PROVIDER_DIR}:ro',
            '-v', f'{keys.resolve()}:{KEY_DIR}:ro',
            *json.loads(os.environ.get(EXTRA_RUN_ARGS) or '[]'),
            image, '-c', script]
    try:
        completed = docker(*args, timeout=timeout)
    except subprocess.TimeoutExpired:
        completed = None
    finally:
        remove(container)
    result_path = out / 'result.json'
    result = json.loads(result_path.read_text()) if result_path.exists() else {}
    if completed is None:
        result['_runner'] = f'the container did not finish within {timeout}s'
    elif completed.returncode:
        result['_runner'] = f'container exit {completed.returncode}: {(completed.stdout + completed.stderr)[-3000:]}'
    result['_net'] = [json.loads(line) for line in (out / 'net.jsonl').read_text().splitlines() if line.strip()] \
        if (out / 'net.jsonl').exists() else []
    result['_world'] = json.loads((out / 'world.json').read_text()) if (out / 'world.json').exists() else {}
    result['_sentinel'] = sentinel
    result['_out'] = str(out)
    for name_ in ('indexer.log', 'runner.log', 'netrec.log'):
        path = out / name_
        result[f'_{name_}'] = path.read_text()[-4000:] if path.exists() else None
    return result


def launch(result: dict, launch_id: str) -> dict:
    found = (result.get('launches') or {}).get(launch_id)
    if found is None:
        pytest.fail(f'no result for launch {launch_id}; runner: {result.get("_runner", "finished")}; '
                    f'log tail: {result.get("_runner.log")}', pytrace=False)
    return found


def explain(result: dict, launch_id: str | None = None) -> str:
    shown = {k: v for k, v in result.items() if k not in ('stub',)}
    if launch_id is not None:
        entry = dict((result.get('launches') or {}).get(launch_id) or {})
        entry.pop('env', None)
        entry.pop('procEnv', None)
        shown = {'launch': entry, 'runner': result.get('_runner'), 'net': result.get('_net'),
                 'hooks': result.get('hooks'), 'stub': result.get('stub'), 'runner_log': result.get('_runner.log')}
    return json.dumps(shown, indent=1, default=str)[:12000]
