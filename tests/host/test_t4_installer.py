"""T4 installer: `kp-agent-install` renders the host adapter config, the spool and the launch profiles.

The order: "`kp-agent-install` renders `host/kp-agent-host.json` and a spool
directory. The compose `capture` role mounts the spool read-write and runs the
ingestion watcher. The other roles don't mount it." The adapter config is
`agent-tooling.host-adapter.v1`: `{runtime: {mode, container?, config_path},
spool_root, transcript_roots[]}`.

Coordinator clarification (2026-10-01):
- the capture role's bind of the spool may be read-only (ingestion cursors live
  under the writable /state), so its mode is not asserted;
- `kp-agent-install plan`/`apply` renders `config/launch/harness-profiles.json`
  (0600): the packaged default profiles with each `~/` root expanded against the
  invoking user's home (a recorded plan input); a profile whose expanded root is
  not equal to or under a planned `--transcript-root` is rendered
  `enabled: false` and named in `preview.warnings`; the plan writes nothing.

Readings (reported under AMBIGUITY):
- driven through the S3 contract harness (tests/install/s3_harness.py): a
  reviewed plan applied to an empty runtime root with the `capture` component;
  the installer runs with HOME pointing at the harness's scratch home, and "the
  invoking user's home" is that HOME (as Python's `Path.home()` resolves it);
- the rendered adapter config is a JSON object with those keys (and, when
  present, a `schema_version` naming the schema); its runtime is `docker`, its
  container is one the rendered compose names, its `config_path` is a container
  path (absolute, not under the host runtime root), its `spool_root` is an
  existing directory under the runtime root, and its `transcript_roots` are
  exactly the roots given;
- the capture role binds `spool_root` and its command runs `ingest-spool` with
  `--root` naming the bind's container target; no other tooling role binds the
  spool. The merged compose configuration is read with `docker compose config`
  when the CLI is present (S3's instrument);
- the rendered launch profiles equal the packaged defaults
  (`packages/tooling/src/kp_agent_tooling/assets/harness-profiles.json`) except
  for the expanded roots and the `enabled` flags; a warning "names" a profile when
  its text contains the profile id.
"""
import json
import os
import stat
from pathlib import Path

from s3_harness import INSTALLER, World as InstallWorld, binds, rendered_config, snapshot, snapshot_diff

HOST_SCHEMA = 'agent-tooling.host-adapter.v1'
PROFILES_SCHEMA = 'agent-tooling.harness-profiles.v1'
LAUNCH_PROFILES = 'config/launch/harness-profiles.json'
PACKAGED_PROFILES = (Path(__file__).resolve().parents[2]
                     / 'packages/tooling/src/kp_agent_tooling/assets/harness-profiles.json')


def _world(tmp_path):
    assert INSTALLER.exists(), 'console script kp-agent-install is not installed next to the interpreter'
    return InstallWorld.create(tmp_path / 'install', components=('tooling', 'capture'))


def test_installer_renders_the_host_adapter_config_spool_and_capture_watcher(tmp_path):
    world = _world(tmp_path)
    roots = [world.home / '.claude' / 'projects', world.home / '.codex' / 'sessions']
    argv = world.args()
    for root in roots:
        argv += ['--transcript-root', str(root)]
    world.apply_ok(argv)

    path = world.root / 'host' / 'kp-agent-host.json'
    assert path.is_file(), f'kp-agent-install did not render {path}'
    config = json.loads(path.read_text())
    assert isinstance(config, dict) and {'runtime', 'spool_root', 'transcript_roots'} <= set(config), config
    assert config.get('schema_version', HOST_SCHEMA) == HOST_SCHEMA, config
    runtime = config['runtime']
    assert runtime.get('mode') == 'docker', runtime
    config_path = runtime.get('config_path')
    assert isinstance(config_path, str) and os.path.isabs(config_path), runtime
    assert not Path(config_path).is_relative_to(world.root), f'config_path is a host path, not a container path: {runtime}'
    spool = Path(config['spool_root'])
    assert spool.is_absolute() and spool.is_dir() and spool.resolve().is_relative_to(world.root.resolve()), config
    assert sorted(config['transcript_roots']) == sorted(str(r) for r in roots), config

    compose, instrument = rendered_config(world)
    services = compose['services']
    containers = {svc.get('container_name') for svc in services.values() if svc.get('container_name')}
    assert runtime.get('container') in containers, (runtime, containers, instrument)
    spool_binds = {name: [b for b in binds(svc) if Path(b.get('source', '')).resolve() == spool.resolve()]
                   for name, svc in services.items()}
    capture = spool_binds.get('capture') or []
    assert len(capture) == 1, f'{instrument}: capture must bind the spool: {services["capture"].get("volumes")}'
    # The process the container runs is entrypoint + command. The capture role's `command`
    # is pinned to the workspace-capture watch by an existing contract test, so the watcher
    # may supervise it from the entrypoint (T4 meet classification: spec gap, not a defect).
    capture_service = services['capture']
    command = ' '.join([*(capture_service.get('entrypoint') or []), *(capture_service.get('command') or [])])
    assert 'ingest-spool' in command and '--root' in command and capture[0]['target'] in command, (
        f'{instrument}: capture does not run the ingestion watcher on its spool mount: {command}')
    others = {name: found for name, found in spool_binds.items() if name != 'capture' and found}
    assert not others, f'{instrument}: roles other than capture mount the spool: {others}'


def test_plan_renders_launch_profiles_for_the_planned_transcript_roots_and_writes_nothing(tmp_path):
    world = _world(tmp_path)
    claude_root, codex_root = world.home / '.claude' / 'projects', world.home / '.codex' / 'sessions'
    argv = world.args() + ['--transcript-root', str(claude_root)]

    before = snapshot(world.base)
    proc, value = world.plan(argv)
    assert proc.returncode == 0 and isinstance(value, dict), proc.stdout[-3000:] + proc.stderr[-2000:]
    assert snapshot_diff(before, snapshot(world.base)) == [], 'plan wrote something'
    assert value['files'].get(LAUNCH_PROFILES, {}).get('mode') == '0600', sorted(value['files'])
    warnings = value['preview']['warnings']
    assert any('codex' in str(w).lower() for w in warnings), f'the disabled codex profile is not named: {warnings}'

    world.apply_ok(argv)
    path = world.root / LAUNCH_PROFILES
    assert stat.S_IMODE(path.stat().st_mode) == 0o600, oct(path.stat().st_mode)
    rendered = json.loads(path.read_text())
    assert rendered.get('schema_version') == PROFILES_SCHEMA, rendered
    profiles = {p['harness']: p for p in rendered['profiles']}
    packaged = {p['harness']: p for p in json.loads(PACKAGED_PROFILES.read_text())['profiles']}
    assert set(profiles) == set(packaged), (sorted(profiles), sorted(packaged))
    expected_roots = {'claude': str(claude_root), 'codex': str(codex_root)}
    for harness, profile in profiles.items():
        if 'root' not in packaged[harness]['capture']:
            # An export-capture profile (OpenCode) has no transcript root to expand: it renders as packaged.
            assert ({k: v for k, v in profile.items() if k != 'enabled'} ==
                    {k: v for k, v in packaged[harness].items() if k != 'enabled'}), harness
            continue
        root = profile['capture']['root']
        assert root == expected_roots[harness], f'{harness} root is not the host-absolute expansion: {root}'
        same = {k: v for k, v in profile.items() if k not in ('enabled', 'capture')}
        assert same == {k: v for k, v in packaged[harness].items() if k not in ('enabled', 'capture')}, harness
        assert {k: v for k, v in profile['capture'].items() if k != 'root'} == {
            k: v for k, v in packaged[harness]['capture'].items() if k != 'root'}, harness
    assert profiles['claude']['enabled'] is True, profiles['claude']
    assert profiles['codex']['enabled'] is False, profiles['codex']
