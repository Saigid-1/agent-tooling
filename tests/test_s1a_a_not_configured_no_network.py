"""S1a falsifier (a): no network when not configured.

Order: docs/work/orders/S1a-scheduled-summarizer.md, P3 ("`not_configured` ... opens NO connection"), P3
Freshness (the listing read goes to the route's base URL only, after every check) and falsifier (a). Seams:
tests/s1a_seams.py. Instruments: tests/s1a_harness.py (the in-process network guard records every connection
attempt, name lookup and datagram, and the loopback stub provider records every request it receives).

The world: one desk (`alpha`) with one queued job; everything configured except the ONE piece a case removes.
One tick of the role runs in-process.

GREEN-IF, per test:
- `test_one_missing_piece_is_not_configured_with_zero_connections[piece]`, for each piece the order names
  (the approval, the route, the budget, the budget's estimate, the key file, the desk's admission), plus a
  key file the gateway refuses (mode 0644, "missing or invalid"): the role reports `not_configured` in a
  status node that names that piece (s1a_seams.MISSING_TOKENS); the guard saw ZERO network events (no
  connection attempt, no name lookup, no datagram), the stub received nothing (the model-listing read
  included); the job is untouched (queued, never leased, no attempt); the role started no child process;
  and the key's value appears in no output and no file the run wrote.
- `test_configured_world_reaches_the_stub` (the positive control that makes "zero" a measurement): with
  nothing removed, the tick connects to the stub, sends one completion and the job succeeds.
- `test_listing_read_goes_to_the_route_base_url_only`: with nothing removed, the tick makes exactly one
  model-listing read, a GET to `<route base_url>/models`, carrying the key file's key; every connection
  attempt the guard saw was to the stub, and none was refused (no other host, no lookup of another name).

Mutants that must be RED: drop any one check (its case connects or reports no `not_configured`); read the
listing before the checks pass (every case connects); read the listing from a host other than the route's
base URL, or from another path (the listing test).
"""
from __future__ import annotations

import pytest

import s1a_seams as seams
from s1a_harness import BASE_PATH, assert_no_child_process, make_world, run_role  # noqa: F401 - fixture

PIECES = ('approval', 'route', 'budget', 'estimate', 'key_file', 'key_file_permissive', 'admission')


def _remove(world, piece):
    if piece == 'approval':
        world.remove_approval()
    elif piece == 'route':
        world.write_gateway(route=False)
    elif piece == 'budget':
        world.write_gateway(budget=None)
    elif piece == 'estimate':
        world.write_gateway(budget={'max_calls_per_hour': 100, 'max_usd_per_day': 5.0})
    elif piece == 'key_file':
        world.remove_key()
    elif piece == 'key_file_permissive':
        world.write_key(mode=0o644)
    else:
        raise AssertionError(piece)


@pytest.mark.parametrize('piece', PIECES)
def test_one_missing_piece_is_not_configured_with_zero_connections(make_world, piece):
    world = make_world()
    if piece == 'admission':
        world.build(admitted=())
    else:
        world.build()
        _remove(world, piece)
    run = run_role(world)
    assert_no_child_process(run)
    nodes = run.status_nodes()
    assert nodes, f'no `{seams.NOT_CONFIGURED}` status with the {piece} missing:\n{run.describe()}'
    tokens = seams.MISSING_TOKENS['key_file' if piece.startswith('key_file') else piece]
    named = [n for n in nodes if any(t in str(n).lower() for t in tokens)]
    assert named, f'the `{seams.NOT_CONFIGURED}` status does not name the {piece} ({tokens}): {nodes}'
    assert not run.network(), f'network activity with the {piece} missing:\n{run.describe()}'
    assert not run.requests and run.accepted == 0, f'the stub was reached with the {piece} missing:\n{run.describe()}'
    job = world.job('alpha')
    assert world.untouched(job), f'the job was touched with the {piece} missing: {job} {world.attempts(job["id"])}'
    assert not run.leaked_key(), f'the key value appears in: {run.leaked_key()}'


def test_configured_world_reaches_the_stub(make_world):
    world = make_world().build()
    run = run_role(world)
    assert_no_child_process(run)
    assert run.connections() and run.accepted >= 1, f'the configured tick never connected:\n{run.describe()}'
    assert len(run.posts()) == 1, f'expected one completion request:\n{run.describe()}'
    job = world.job('alpha')
    assert job['state'] == 'succeeded', f'the configured job did not succeed: {job}\n{run.describe()}'


def test_listing_read_goes_to_the_route_base_url_only(make_world):
    world = make_world().build()
    run = run_role(world)
    assert_no_child_process(run)
    refused = run.guard.refused()
    assert not refused, f'the role tried to reach something other than the route: {refused}\n{run.describe()}'
    assert all(e.allowed for e in run.connections()), run.describe()
    gets = run.gets()
    assert len(gets) == 1, f'expected exactly one model-listing read on the first tick:\n{run.describe()}'
    assert gets[0].path == BASE_PATH + seams.LISTING_SUFFIX, (
        f'the listing read went to {gets[0].raw_path}, not <route base_url>{seams.LISTING_SUFFIX}')
    assert gets[0].credential == 'key-file', f'the listing read carried credential {gets[0].credential!r}'
