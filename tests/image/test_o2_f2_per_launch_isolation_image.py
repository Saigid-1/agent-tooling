"""O2 F2, per-launch isolation, on O3's image (docs/work/orders/O2-opencode-third-harness.md, R2 and F2).
Seams: tests/o2_seams.py (O2-S13 is this test's instrument). The container: tests/image/o2_image_run.py.

Image, by variable (unset FAILS; never skips): AGENT_TOOLING_TEST_IMAGE_OPENCODE. Precondition: `npm ci` in
apps/kanban.

Two OpenCode tasks are launched from ONE board process, at once, each bound to a different desk; each runs
one turn (so each session is bound by its first Stop). While both are alive, the runner asks the pinned
binary what each launch would load (`opencode debug config` with that launch's own environment, argv
minus --prompt, cwd and PWD=cwd) and starts every local MCP server listed there exactly as OpenCode would,
asking it `memory.connection_status` and `memory.bindings`.

GREEN-IF, for each launch: it was bound to its own desk; at least one listed server answers as a desk memory
for its own session (status `ready`, own binding = its desk's); and no listed server answers for the
other launch's desk.
Mutant (the order's): the MCP entry is written into the board-wide opencode.json, so the launch prepared
first sees the second launch's server.
RED at base: there is no `opencode` profile and no per-launch binding, so no launch lists a desk memory.
"""
from __future__ import annotations

import pytest

import o2_seams as seams
from o2_image_run import explain, launch, run_container

pytestmark = pytest.mark.image

LAUNCHES = [
    {'id': 'first', 'desk': 0, 'prompt': 'o2-isolation-first remember the Sedge lantern',
     'answer': 'Kept the Sedge lantern for the first desk'},
    {'id': 'second', 'desk': 1, 'prompt': 'o2-isolation-second remember the Rush beacon',
     'answer': 'Kept the Rush beacon for the second desk'},
]


@pytest.fixture(scope='module')
def run(tmp_path_factory) -> dict:
    return run_container(tmp_path_factory, 'f2', desks=['First desk', 'Second desk'], launches=LAUNCHES,
                         instrument=True)


def _servers(run, launch_id) -> list[dict]:
    return [check for check in launch(run, launch_id).get('serverChecks') or [] if isinstance(check, dict)]


@pytest.mark.parametrize('launch_id,other_id', [('first', 'second'), ('second', 'first')])
def test_f2_each_launch_sees_only_its_own_desk_memory(run, launch_id, other_id):
    """GREEN-IF the launch is bound to its own desk under `opencode`, and among the MCP servers its OpenCode
    would load, at least one serves its own desk (ready, own binding = its desk's key) and none serves the
    other launch's desk."""
    world = run['_world']
    entry = launch(run, launch_id)
    assert entry.get('launched') and entry.get('answered'), f'the launch or its turn failed:\n{explain(run, launch_id)}'
    index = next(item['desk'] for item in LAUNCHES if item['id'] == launch_id)
    other = next(item['desk'] for item in LAUNCHES if item['id'] == other_id)
    own_desk, other_desk = world['desks'][index], world['desks'][other]
    own_key, other_key = world['binding_keys'][own_desk], world['binding_keys'][other_desk]
    bound = [(b.get('harness'), b.get('desk_id')) for b in (entry.get('final') or {}).get('bindings') or []]
    assert bound == [(seams.HARNESS, own_desk)], f'{launch_id} is not bound to its own desk: {bound}'
    debug = entry.get('debugConfig') or {}
    assert debug.get('code') == 0, f'instrument: `opencode debug config` failed for {launch_id}: {debug}'
    servers = _servers(run, launch_id)
    serving_own = [s for s in servers if s.get('status') == 'ready' and s.get('own') == [own_key]]
    serving_other = [s for s in servers if other_key in (s.get('own') or [])]
    assert serving_own, f'no MCP server {launch_id} would load serves its own desk ({seams.MCP_SERVER_NAME}?): ' \
                        f'{servers}\nmcp: {entry.get("mcp")}'
    assert not serving_other, f'{launch_id} would load the other desk\'s memory server: {serving_other}'
