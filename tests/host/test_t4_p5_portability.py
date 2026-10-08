"""T4 P5: portability.

`local` and `docker` modes produce the same binding and capture results for the
same inputs. The image bakes in no host path.
Falsifier: a mode-dependent result, or a host path in the image.

Readings (reported under AMBIGUITY):
- "the same inputs": two worlds, one per mode, each with the shipped profiles
  given absolute transcript roots, a desk of the same name and role, the same
  launch arguments and the same transcript text; results are compared after
  normalising session ids, desk ids, times and the world's own root out of paths;
- "results": every recorded binding field (harness, provider, model, source,
  desk, workspace, parent), admission, the number of captured episodes and of
  search hits for each captured turn, for a Claude launch, a Codex launch and an
  operator `bind`;
- docker mode is the docker stand-in (host_harness.py); it shares paths with the
  host, so it cannot show that a path is visible inside a real container;
- "no host path in the image" is checked on a built image named by
  AGENT_TOOLING_TEST_IMAGE (an `image` test, deselected by default; it fails,
  never skips, when the variable is unset): the image configuration, the
  installed package and the `kp-agent-host`/`kp-agent-launch` console scripts
  carry no build-host path (`/Users/`, `/Volumes/`, `/private/var/folders/`, the
  build user's home, this checkout), and `kp-agent-host` is on the image PATH.
"""
import json
import os
import shlex
import subprocess
import uuid
from pathlib import Path

import pytest

from host_harness import HostWorld, codex_meta, new_session_id

REPO_ROOT = Path(__file__).resolve().parents[2]


def _normalised(hw, session):
    return [{**{k: b[k] for k in ('harness', 'provider', 'model', 'source', 'parent_session_id')},
             'desk': hw.desk_name(b['desk_id']), 'workspace': os.path.relpath(b['workspace'], hw.root)}
            for b in hw.binding_for(session)]


def _scenario(hw):
    desk = hw.save_desk(name='Portable desk')
    claude_marker, codex_marker = 'Hazel portable claude marker', 'Linden portable codex marker'
    claude = hw.launch('claude', desk, provider='anthropic', model='fixture-model', user_args=['--verbose'], plan=[
        hw.claude_turn('stop', f'Remember {claude_marker}.', f'Recorded {claude_marker}.')]).ok()
    session = new_session_id()
    codex = hw.launch('codex', desk, provider='openai', model='fixture-model', plan=[
        hw.codex_turn('prompt', session, f'Remember {codex_marker}.', event='UserPromptSubmit',
                      meta=codex_meta(session, hw.workspace)),
        hw.codex_turn('stop', session, f'Recorded {codex_marker}.')]).ok()
    operator = new_session_id()
    hw.host('bind', 'codex', '--native-session-id', operator, '--desk', desk).ok()
    hw.ingest(lambda: hw.hits_now(claude.native, claude_marker) >= 1 and hw.bound_now(session)
              and hw.hits_now(session, codex_marker) >= 1)
    return {
        'claude': {'binding': _normalised(hw, claude.native), 'ready': hw.ready(claude.native),
                   'episodes': hw.episodes(claude.native), 'hits': hw.hits(claude.native, claude_marker)},
        'codex': {'binding': _normalised(hw, session), 'ready': hw.ready(session),
                  'episodes': hw.episodes(session), 'hits': hw.hits(session, codex_marker)},
        'operator': {'binding': _normalised(hw, operator), 'ready': hw.ready(operator)},
        'sessions': len(hw.bindings()),
    }


def test_local_and_docker_modes_bind_and_capture_identically(tmp_path):
    results = {}
    for mode in ('local', 'docker'):
        hw = HostWorld.create(tmp_path / mode, mode=mode, profiles='absolute')
        results[mode] = _scenario(hw)
        calls = [c for c in hw.docker_calls() if c.get('command')]
        if mode == 'docker':
            assert calls, 'docker mode never ran `docker exec`'
        else:
            assert hw.docker_calls() == [], 'local mode called docker'
    assert results['local']['claude']['hits'] >= 1 and results['local']['codex']['hits'] >= 1, results
    assert results['local'] == results['docker'], json.dumps(results, indent=1)


# ------------------------------------------------------------------------------- image

HOST_MARKERS = ('/Users/', '/Volumes/', '/private/var/folders/')


def _docker(*args, timeout=300):
    return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)


def _strings(value):
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (str(k), *_strings(v))]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return [value] if isinstance(value, str) else []


@pytest.mark.image
def test_image_ships_the_host_adapter_and_bakes_in_no_host_path():
    image = os.environ.get('AGENT_TOOLING_TEST_IMAGE', '').strip()
    if not image:
        pytest.fail('AGENT_TOOLING_TEST_IMAGE is unset; this image test fails rather than skips', pytrace=False)
    markers = (*HOST_MARKERS, str(Path.home()), str(REPO_ROOT), str(REPO_ROOT.resolve()))
    inspected = _docker('image', 'inspect', image)
    assert inspected.returncode == 0, f'image {image!r} is not present locally: {inspected.stderr}'
    config = json.loads(inspected.stdout)[0].get('Config') or {}
    leaks = [s for s in _strings({k: config.get(k) for k in ('Env', 'Cmd', 'Entrypoint', 'WorkingDir', 'Labels',
                                                               'Volumes', 'User')})
             if any(m in s for m in markers)]
    assert not leaks, f'the image configuration carries host paths: {leaks}'

    pattern = '|'.join(shlex.quote(m) for m in markers)
    script = (
        'set -u; host=$(command -v kp-agent-host) || { echo "kp-agent-host is not on PATH"; exit 3; }; '
        'launch=$(command -v kp-agent-launch) || { echo "kp-agent-launch is not on PATH"; exit 3; }; '
        'pkg=$(python3 -c "import kp_agent_tooling,os;print(os.path.dirname(kp_agent_tooling.__file__))") || exit 4; '
        'found=0; for m in "$@"; do if grep -rIlF -- "$m" "$pkg" "$host" "$launch"; then found=1; fi; done; '
        'exit $found')
    name = f'agent-tooling-t4-test-{uuid.uuid4().hex[:10]}'
    try:
        done = _docker('run', '--rm', '--pull', 'never', '--network', 'none', '--name', name, '--entrypoint',
                       '/bin/sh', image, '-c', script, 'sh', *markers)
    finally:
        _docker('rm', '-f', name, timeout=120)
    assert done.returncode == 0, (f'the image lacks the host adapter or bakes in a host path '
                                  f'(markers {pattern}): exit {done.returncode}\n{done.stdout[-3000:]}'
                                  f'{done.stderr[-2000:]}')
