"""S1a P3 and falsifier (a): no network when not configured.

Order: docs/work/orders/S1a-scheduled-summarizer.md, P3 ("`not_configured`") and falsifier (a):
"With any ONE of these absent, the role reads `not_configured` and opens zero connections,
INCLUDING the model listing read: the approval; the route; the budget; the budget's estimate;
the key file; the desk's admission." Two instruments, as D0f did: the gateway's injected
transport records every request (the listing GET included), and a socket guard records every
connection attempt of the process, for a role built with the real HTTP transport.

Mutants these tests turn RED: dropping one check (the tick reaches the listing read or a
completion); fetching the listing before the checks pass (a GET in a not_configured tick);
fetching it from another host (`test_configured_tick_reads_the_listing_at_the_route_only`).
"""
from __future__ import annotations

import json

import pytest

from s1a_world import (BASE_URL, CAPABILITY, IMPL, KEY_VALUE, LISTING, MODEL, NetworkGuard, World,
                       write_private)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _absent_approval(world):
    world.approval_path.unlink()


def _absent_route(world):
    document = world.gateway_document()
    document['routes'] = {}
    world.write_gateway(document)


def _absent_gateway_file(world):
    world.gateway_path.unlink()


def _absent_budget(world):
    document = world.gateway_document()
    document['budgets'] = {}
    world.write_gateway(document)


def _absent_estimate(world):
    document = world.gateway_document()
    del document['budgets'][CAPABILITY]['estimated_usd_per_call']
    world.write_gateway(document)


def _absent_key_file(world):
    world.key_file.unlink()


def _permissive_key_file(world):
    world.key_file.chmod(0o644)


def _absent_admission(world):
    unadmitted = world.admit_summarizer(IMPL, 'summarizer-unadmitted', admit_it=False)
    document = world.approval_document()
    document['desks'][0]['memory_config'] = str(unadmitted)
    world.write_approval(document)


ABSENT = {
    'approval': (_absent_approval, 'approval'),
    'route': (_absent_route, 'route'),
    'gateway_file': (_absent_gateway_file, 'route'),
    'budget': (_absent_budget, 'budget'),
    'budget_estimate': (_absent_estimate, 'budget_estimate'),
    'key_file': (_absent_key_file, 'key_file'),
    'key_file_permissive': (_permissive_key_file, 'key_file'),
    'admission': (_absent_admission, 'admission'),
}


def _named(status, piece):
    names = set(status.get('missing') or [])
    for desk in status.get('desks') or []:
        names.update(desk.get('missing') or [])
    return piece in names


@pytest.mark.parametrize('case', sorted(ABSENT))
def test_one_absent_piece_reads_not_configured_with_zero_requests(world, case):
    job, _ = world.enqueue()
    make_absent, piece = ABSENT[case]
    make_absent(world)
    status = world.role().tick()
    assert status['status'] == 'not_configured', status
    assert _named(status, piece), f'the status does not name {piece!r}: {status}'
    assert world.provider.calls == [], f'not_configured tick made requests: {world.provider.calls}'
    assert world.profile_receipt() is None, 'the listing was read: a profile receipt was written'
    after = world.job(IMPL, job)
    assert after['state'] == 'queued' and after['attempts'] == [], after
    assert KEY_VALUE not in json.dumps(status)
    assert status['requests_sent'] == 0 and status['listing_reads'] == 0


@pytest.mark.parametrize('case', sorted(ABSENT))
def test_one_absent_piece_opens_no_connection_with_the_real_transport(world, monkeypatch, case):
    world.enqueue()
    ABSENT[case][0](world)
    guard = NetworkGuard(monkeypatch)
    status = world.role(transport=None).tick()
    assert status['status'] == 'not_configured', status
    assert guard.attempts == [], f'connection attempts while not configured: {guard.attempts}'


def test_status_file_names_the_missing_piece_and_never_the_key(world):
    world.key_file.chmod(0o644)
    world.role().tick()
    written = json.loads((world.role_state / 'status.json').read_text())
    assert written['status'] == 'not_configured' and 'key_file' in written['missing']
    assert KEY_VALUE not in (world.role_state / 'status.json').read_text()
    assert (world.role_state / 'status.json').stat().st_mode & 0o077 == 0


def test_configured_tick_reads_the_listing_at_the_route_only(world):
    world.enqueue()
    status = world.role().tick()
    assert status['status'] == 'ok', status
    gets = world.provider.gets()
    assert [call.target for call in gets] == [LISTING], gets
    assert world.provider.calls[0].method == 'GET', 'the listing read must precede the first completion'
    assert all(call.target.startswith(BASE_URL + '/') for call in world.provider.calls)
    assert all(call.key_matches for call in world.provider.calls)
    receipt = world.profile_receipt()
    assert receipt['profile']['model_id'] == MODEL and receipt['raw_model_entry']['id'] == MODEL


def test_listing_is_read_at_most_once_per_receipt_lifetime(world):
    world.enqueue(ref='a')
    world.role().tick()
    world.enqueue(ref='b')
    world.role().tick()
    assert len(world.provider.gets()) == 1, 'a fresh receipt was refreshed again'
    world.age_profile(hours=1)
    world.role().tick()
    assert len(world.provider.gets()) == 1, 'a receipt one hour old was refreshed'
    world.age_profile(hours=25)
    world.enqueue(ref='c')
    status = world.role().tick()
    assert len(world.provider.gets()) == 2 and status['listing_reads'] == 1, status
    assert world.job(IMPL, world.queue().list(world.capture_session(IMPL))['entries'][0]['id'])['state'] == 'succeeded'


@pytest.mark.parametrize('change', ['model_absent', 'parameter_dropped', 'effort_dropped'])
def test_changed_listing_reads_profile_changed_and_never_another_model(world, change):
    job, _ = world.enqueue()
    entries = world.provider.listing['data']
    if change == 'model_absent':
        world.provider.listing['data'] = [entry for entry in entries if entry['id'] != MODEL]
    elif change == 'parameter_dropped':
        entries[0]['supported_parameters'].remove('response_format')
    else:
        entries[0]['reasoning']['supported_efforts'] = ['high']
    status = world.role().tick()
    assert status['status'] == 'not_configured' and 'profile_changed' in status['missing'], status
    assert world.provider.posts() == []
    assert world.job(IMPL, job)['state'] == 'queued'
    world.role().tick()
    assert len(world.provider.gets()) == 1, 'a changed listing was read again within its 24 hours'


def test_listing_never_names_a_hard_coded_host(world, monkeypatch):
    """The listing and completions go through the gateway's transport only (no `openrouter.ai`)."""
    from kp_agent_tooling._impl.service import episodic_summarizer, openrouter_models
    def forbidden(*args, **kwargs):
        raise AssertionError('a hard-coded provider connection was used')
    monkeypatch.setattr(episodic_summarizer, '_request', forbidden)
    monkeypatch.setattr(openrouter_models, '_openrouter_models_json', forbidden)
    world.enqueue()
    assert world.role().tick()['status'] == 'ok'
    assert {call.target for call in world.provider.calls} <= {LISTING, BASE_URL + '/chat/completions'}


def test_route_model_differing_from_the_approved_model_is_not_configured(world):
    world.enqueue()
    world.write_gateway(route={'model': 'z-ai/glm-5.3-pro'})
    status = world.role().tick()
    assert status['status'] == 'not_configured' and 'model_pin' in status['missing'], status
    assert world.provider.calls == []


def test_invalid_approval_is_not_configured(world):
    write_private(world.approval_path, {'schema_version': 'agent-tooling.summarizer-approval.v1'})
    status = world.role().tick()
    assert status['status'] == 'not_configured' and 'approval' in status['missing']
    assert world.provider.calls == []
