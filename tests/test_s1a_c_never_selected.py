"""S1a falsifier (c): the summarizer is never selected unless asked.

Order: docs/work/orders/S1a-scheduled-summarizer.md, P3 (the component, its `profiles: [summarizer]`
service, egress) and falsifier (c). Seams: tests/s1a_seams.py (component, service and profile names, the
selecting `--components` value). O3's A1 pin (tests/test_o3_a1_default_plan_pin.py) is the default-plan half
and is run unedited, not repeated here.

How a plan is made: `kp-agent-install plan` in-process (`kp_agent_tooling.install_cli.main`), exactly as the
O3 pin makes it (a temporary runtime root, home and committed fixture repository, a synthetic digest,
uid/gid 10001), and its files are re-rendered and checked against the plan's recorded sha256 (the O3 pin's
`_rendered`), so the bytes read are the bytes the plan commits to.

Base measurement (the order's base): the rendered compose.yaml has the services below with the digests below
(sha256 of each service's canonical JSON after YAML anchors and merge keys resolve), top-level keys `name`,
`services`, `volumes` and the `x-` fragments; `capture` and `indexer` have `network_mode: none`.

GREEN-IF, per test:
- `test_selecting_plan_renders_the_service_and_its_profile`: the plan with `--components tooling,summarizer`
  exits 0; `inputs.components` and `compose.profiles` include `summarizer`; the rendered `.env` sets
  `COMPOSE_PROFILES` once and it includes `summarizer`; the rendered compose.yaml has a `summarizer` service
  whose `profiles` is exactly `[summarizer]` (a service without a profile always starts).
- `test_rendered_compose_adds_only_the_summarizer_service`: in that plan the rendered compose.yaml's services
  are the base services plus `summarizer`, each base service is byte-for-byte the base one (its digest), and
  no overlay (compose.workspaces.yaml, compose.capture.yaml) names the summarizer.
- `test_rendered_compose_top_level_is_unchanged`: in that plan the rendered compose.yaml's non-`x-` top-level
  keys are `name`, `services` and `volumes`, and `name` and `volumes` are the base ones.
- `test_capture_and_indexer_keep_network_none_beside_the_summarizer[capture|indexer]`: the plan with
  `--components tooling,summarizer,capture` (which also selects the indexer) exits 0 and renders that service
  with `network_mode: none`.
- `test_indexer_network_none_on_todays_live_like_plan` (a guard, GREEN at base): the live-like plan
  (`tooling,refresh,capture,board,indexer`) renders the indexer with `network_mode: none` (census §14 found a
  test for capture only).

Mutants that must be RED: the summarizer service without `profiles`; a plan that refuses the component; a
change to any other service (for example dropping `network_mode: none` from the indexer); a summarizer added
to an overlay; a new top-level volume.
"""
from __future__ import annotations

import contextlib
import io
import json

import pytest
import yaml

import s1a_seams as seams
from kp_agent_tooling import install_cli
from s1a_harness import content_digest
from test_o3_a1_default_plan_pin import (CONTAINER_ID, DIGEST, _committed_repository, _compose_profiles_lines,
                                         _rendered, _unquote)

BASE_SERVICE_DIGESTS = {
    # O2 adds one line to the board service, KANBAN_OPENCODE_OPENROUTER_KEY_FILE; without it the board's
    # digest is S1a's base value, 347a9877baee0ecfe7ddaf1b3797ff13bb8d5df52d5a8b951089100f3cfd48f0.
    'board': 'cd1e2b961ccef741179f7b7205ceec7da9ad35c69fd27c64b27f6820e79ae6ba',
    'capture': 'c2a8920a12203f3c8e20a8b051b7ef7d5e43147d0dc6975907644edfc3e77691',
    'collector': '6c6debb7dcb71ec5dce7266683f062f9971d63e1b0d703bd441d3998317e1291',
    'indexer': '01c7b16b0245c7c7acce82918917b31e6f9f106ac57b5a5526e0a5ff9bfdc3d6',
    'refresh': '8eb9d58a32fc2d1f5f9014d5d48e6ce5f11ad68a101d8c83ca40505cc54ce736',
    'tempo': '8248ceddd9203022cc01188c56fa7191d9b5bd4382b505c594cd181b6cec6ca3',
    'tooling': '7e7e51a82b76e683807c8b9c959c10980c96cc7738708bbf65ef7f6c26816391',
}
BASE_TOP_LEVEL_DIGESTS = {
    'name': 'c99dd612bbec74b4a6645da912e31f5a370846e716c72fa93436a8b094750ade',
    'volumes': 'b365787d017e0ea38370b88ac1930d0515a1a465f3bedc1bafa46a50904c7142',
}
BASE_OVERLAY_SERVICES = {'compose.workspaces.yaml': {'tooling', 'refresh'}, 'compose.capture.yaml': {'capture'}}
LIVE_LIKE = 'tooling,refresh,capture,board,indexer'


def _plan_files(base, components):
    """The O3 pin's in-process plan, with `--components <components>`; (document, rendered files)."""
    root, home = base / 'root', base / 'home'
    root.mkdir()
    home.mkdir()
    repository = _committed_repository(base / 'repos' / 'fixture')
    argv = ['plan', '--runtime-root', str(root.resolve()), '--image', DIGEST,
            '--repository', f'fixture={repository}', '--home', str(home.resolve()),
            '--uid', CONTAINER_ID, '--gid', CONTAINER_ID, '--components', components]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = install_cli.main(argv)
    text = out.getvalue()
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    assert code == 0 and isinstance(document, dict) and 'plan_sha256' in document, (
        f'the plan with --components {components} did not succeed (exit {code}):\n{text[-3000:]}')
    return document, _rendered(document)


def _compose(files):
    return yaml.safe_load(files['compose.yaml']) or {}


def test_selecting_plan_renders_the_service_and_its_profile(tmp_path):
    document, files = _plan_files(tmp_path, seams.SELECTING_COMPONENTS)
    assert seams.COMPONENT in document['inputs']['components'], document['inputs']['components']
    assert seams.PROFILE in document['compose']['profiles'], document['compose']['profiles']
    lines = _compose_profiles_lines(files['.env'])
    assert len(lines) == 1, f'.env sets COMPOSE_PROFILES {len(lines)} times: {lines}'
    profiles = _unquote(lines[0].split('=', 1)[1]).split(',')
    assert seams.PROFILE in profiles, f'COMPOSE_PROFILES={profiles}'
    services = _compose(files).get('services') or {}
    assert seams.SERVICE in services, f'no {seams.SERVICE} service in the rendered compose: {sorted(services)}'
    assert (services[seams.SERVICE] or {}).get('profiles') == [seams.PROFILE], (
        f'the {seams.SERVICE} service profiles are {(services[seams.SERVICE] or {}).get("profiles")!r}')


def test_rendered_compose_adds_only_the_summarizer_service(tmp_path):
    document, files = _plan_files(tmp_path, seams.SELECTING_COMPONENTS)
    services = _compose(files).get('services') or {}
    assert set(services) == set(BASE_SERVICE_DIGESTS) | {seams.SERVICE}, (
        f'services {sorted(services)}; expected the base services plus {seams.SERVICE}')
    changed = sorted(name for name, digest in BASE_SERVICE_DIGESTS.items()
                     if content_digest(services[name]) != digest)
    assert not changed, f'base services changed beside the summarizer: {changed}'
    for overlay, expected in BASE_OVERLAY_SERVICES.items():
        named = set((yaml.safe_load(files[overlay]) or {}).get('services') or {})
        assert named == expected, f'{overlay} names services {sorted(named)}; at base {sorted(expected)}'


def test_rendered_compose_top_level_is_unchanged(tmp_path):
    document, files = _plan_files(tmp_path, seams.SELECTING_COMPONENTS)
    model = _compose(files)
    keys = {key for key in model if not str(key).startswith('x-')}
    assert keys == {'name', 'services', 'volumes'}, f'top-level keys {sorted(keys)}'
    changed = sorted(key for key, digest in BASE_TOP_LEVEL_DIGESTS.items() if content_digest(model[key]) != digest)
    assert not changed, f'top-level keys changed beside the summarizer service: {changed}'


@pytest.mark.parametrize('service', ['capture', 'indexer'])
def test_capture_and_indexer_keep_network_none_beside_the_summarizer(tmp_path, service):
    document, files = _plan_files(tmp_path, seams.SELECTING_COMPONENTS + ',capture')
    assert seams.COMPONENT in document['inputs']['components'] and service in document['inputs']['components'], (
        document['inputs']['components'])
    services = _compose(files).get('services') or {}
    assert (services.get(service) or {}).get('network_mode') == 'none', (
        f'{service} network_mode is {(services.get(service) or {}).get("network_mode")!r}')


def test_indexer_network_none_on_todays_live_like_plan(tmp_path):
    document, files = _plan_files(tmp_path, LIVE_LIKE)
    services = _compose(files).get('services') or {}
    assert (services.get('indexer') or {}).get('network_mode') == 'none', services.get('indexer')
