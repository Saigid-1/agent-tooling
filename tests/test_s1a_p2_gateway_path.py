"""S1a P2 and falsifier (b): through the gateway, to the route's provider only, at the pinned
model, under a priced dollar-per-day budget.

Order: docs/work/orders/S1a-scheduled-summarizer.md, P2 and falsifier (b). Gateway budget refusal
before the network is covered by the EXISTING tests, named there and not rewritten here
(tests/models/test_m1_p3_budgets.py, test_m1_p2_secrets.py, test_m1_p1_routing.py). This file
adds the role's path: the USD cap reached by the estimate gives zero requests and a claimable
job, and the first test of `budget_unconfigured`.

Mutants these tests turn RED: a fallback list or a route `params` entry weakening ZDR (refused
before the network); removing the model comparison (a response naming another model must give
`needs_review`); treating a budget refusal as uncertain (the job must stay claimable, with zero
requests).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from s1a_world import (BASE_URL, CAPABILITY, COMPLETIONS, IMPL, KEY_VALUE, MODEL, OTHER_MODEL, VERIF,
                       StubProvider, World)
from kp_agent_tooling._impl.service.model_gateway import ModelGateway


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_configured_tick_sends_one_request_per_packet_to_the_route_at_the_pinned_model(world):
    job, _ = world.enqueue()
    status = world.role().tick()
    assert status['status'] == 'ok' and status['requests_sent'] == 1, status
    posts = world.provider.posts()
    assert [call.target for call in posts] == [COMPLETIONS]
    body = posts[0].body
    assert body['model'] == MODEL
    assert body['provider']['zdr'] is True
    assert body['provider']['data_collection'] == 'deny'
    assert body['provider']['require_parameters'] is True
    assert 'models' not in body and 'route' not in body, 'a fallback model list reached the wire'
    result = world.job(IMPL, job)
    assert result['state'] == 'succeeded' and result['capsule_id']
    attempt = result['attempts'][0]
    assert attempt['state'] == 'citations_validated'
    assert attempt['receipt']['model_reported'] == MODEL
    assert attempt['receipt']['zdr_required'] is True and attempt['receipt']['data_collection'] == 'deny'
    assert KEY_VALUE not in json.dumps(result) and KEY_VALUE not in json.dumps(status)


@pytest.mark.parametrize('params', [
    {'provider': {'zdr': False}},
    {'provider': {'data_collection': 'allow'}},
    {'models': [MODEL, OTHER_MODEL]},
    {'route': 'fallback'},
])
def test_route_params_that_weaken_privacy_or_add_fallbacks_are_refused_before_the_network(world, params):
    job, _ = world.enqueue()
    world.write_gateway(params=params)
    status = world.role().tick()
    assert status['status'] == 'not_configured' and 'route_params' in status['missing'], status
    assert world.provider.calls == []
    assert world.job(IMPL, job)['state'] == 'queued'


@pytest.mark.parametrize('params', [{'provider': {'zdr': False}}, {'models': [OTHER_MODEL]}])
def test_the_gateway_content_free_mode_refuses_weakening_params_itself(world, params):
    """Defence in depth: the gateway's own pre-network order refuses them, whatever the caller checked."""
    world.write_gateway(params=params)
    provider = StubProvider()
    gateway = ModelGateway(world.gateway_path, transport=provider)
    request = {'model': MODEL, 'stream': False, 'max_tokens': 16,
               'provider': {'allow_fallbacks': True, 'require_parameters': True, 'zdr': True,
                            'data_collection': 'deny'},
               'messages': [{'role': 'user', 'content': 'x'}]}
    result = gateway.complete_content_free(CAPABILITY, request)
    assert result['status'] == 'refused' and result['requests_sent'] == 0, result
    assert provider.calls == []


@pytest.mark.parametrize('routing', [
    {'allow_fallbacks': True, 'require_parameters': True, 'zdr': False, 'data_collection': 'deny'},
    {'allow_fallbacks': True, 'require_parameters': True, 'zdr': True},
    {'allow_fallbacks': True, 'require_parameters': False, 'zdr': True, 'data_collection': 'deny'},
])
def test_the_gateway_content_free_mode_refuses_a_request_without_privacy_routing(world, routing):
    provider = StubProvider()
    gateway = ModelGateway(world.gateway_path, transport=provider)
    request = {'model': MODEL, 'stream': False, 'max_tokens': 16, 'provider': routing,
               'messages': [{'role': 'user', 'content': 'x'}]}
    result = gateway.complete_content_free(CAPABILITY, request)
    assert result['status'] == 'refused' and result['category'] == 'privacy_routing_required', result
    assert provider.calls == []


def test_the_gateway_content_free_mode_refuses_another_model(world):
    provider = StubProvider()
    gateway = ModelGateway(world.gateway_path, transport=provider)
    request = {'model': OTHER_MODEL, 'stream': False, 'max_tokens': 16,
               'provider': {'allow_fallbacks': True, 'require_parameters': True, 'zdr': True,
                            'data_collection': 'deny'},
               'messages': [{'role': 'user', 'content': 'x'}]}
    result = gateway.complete_content_free(CAPABILITY, request)
    assert result['status'] == 'refused' and result['category'] == 'model_pin_mismatch', result
    assert provider.calls == []


@pytest.mark.parametrize('reported', [OTHER_MODEL, None, MODEL + ':free'])
def test_a_response_naming_another_model_goes_to_review_without_a_capsule(world, reported):
    job, _ = world.enqueue()
    world.provider.reported_model = reported
    before = world.capsules()
    status = world.role().tick()
    assert status['requests_sent'] == 1, status
    result = world.job(IMPL, job)
    assert result['state'] == 'needs_review', result
    assert result['capsule_id'] is None and world.capsules() == before
    attempt = result['attempts'][0]
    assert attempt['state'] == 'failed_or_uncertain'
    assert attempt['receipt']['failure_category'] == 'model_mismatch'
    world.provider.reported_model = MODEL
    world.role().tick()
    assert len(world.provider.posts()) == 1, 'a job in review was resent'


@pytest.mark.parametrize('budget', [
    {'max_usd_per_day': 0.001, 'estimated_usd_per_call': 0.002},
    {'max_calls_per_hour': 0},
    {'confirm_over_usd': 0.001},
], ids=['usd_cap_by_estimate', 'calls_per_hour', 'confirmation_required'])
def test_a_budget_refusal_sends_nothing_and_the_job_stays_claimable(world, budget):
    job, _ = world.enqueue()
    world.write_gateway(budget=budget)
    status = world.role().tick()
    assert status['status'] == 'refused', status
    assert world.provider.posts() == [], 'a refused call reached the provider'
    result = world.job(IMPL, job)
    assert result['state'] == 'queued', f'a refusal before the network was treated as uncertain: {result}'
    assert [attempt['state'] for attempt in result['attempts']] == ['not_sent']
    world.write_gateway()
    status = world.role().tick()
    assert status['status'] == 'ok', status
    result = world.job(IMPL, job)
    assert result['state'] == 'succeeded', result
    assert [attempt['state'] for attempt in result['attempts']] == ['not_sent', 'citations_validated']
    assert len(world.provider.posts()) == 1


def test_a_budget_refusal_ends_the_tick_for_every_desk(world):
    world.write_approval(desks=(IMPL, VERIF))
    jobs = {desk: world.enqueue(desk)[0] for desk in (IMPL, VERIF)}
    world.write_gateway(budget={'max_calls_per_hour': 0})
    world.role().tick()
    assert world.provider.posts() == []
    attempted = [desk for desk, job in jobs.items() if world.job(desk, job)['attempts']]
    assert len(attempted) == 1, f'the tick went on after the gateway refused: {attempted}'


def test_budget_unconfigured_refuses_before_the_network(world):
    document = world.gateway_document()
    document['budgets'] = {}
    world.write_gateway(document)
    provider = StubProvider()
    result = ModelGateway(world.gateway_path, transport=provider).complete_content_free(CAPABILITY, {})
    assert result['status'] == 'refused' and result['category'] == 'budget_unconfigured', result
    assert result['requests_sent'] == 0 and provider.calls == []
    called = ModelGateway(world.gateway_path, transport=provider).call('model.' + CAPABILITY, {'prompt': 'x'})
    assert called['status'] == 'refused' and called['category'] == 'budget_unconfigured', called
    assert provider.calls == []


def test_unpriced_budget_refuses_in_the_content_free_mode(world):
    document = world.gateway_document()
    del document['budgets'][CAPABILITY]['estimated_usd_per_call']
    world.write_gateway(document)
    provider = StubProvider()
    request = {'model': MODEL, 'stream': False, 'max_tokens': 16,
               'provider': {'allow_fallbacks': True, 'require_parameters': True, 'zdr': True,
                            'data_collection': 'deny'},
               'messages': [{'role': 'user', 'content': 'x'}]}
    result = ModelGateway(world.gateway_path, transport=provider).complete_content_free(CAPABILITY, request)
    assert result['status'] == 'refused' and result['category'] == 'budget_unpriced', result
    assert provider.calls == []


def test_the_gateway_writes_nothing_content_bearing_for_the_role(world):
    world.enqueue()
    episodes_before = _count(world, 'episodes')
    world.role().tick()
    names = sorted(str(path.relative_to(world.role_state)) for path in world.role_state.rglob('*') if path.is_file())
    assert names == ['.model-gateway/ledger.sqlite3', 'model-profile.json', 'status.json'], names
    for path in world.role_state.rglob('*'):
        if path.is_file():
            assert b'Choose pool B' not in path.read_bytes(), f'{path.name} holds summary or source content'
    assert _count(world, 'episodes') == episodes_before, 'the gateway recorded a desk memory event'
    import sqlite3
    db = sqlite3.connect(f'file:{world.role_state / ".model-gateway" / "ledger.sqlite3"}?mode=ro', uri=True)
    try:
        rows = db.execute('SELECT capability, model, status, estimate_usd, cost_usd FROM calls').fetchall()
    finally:
        db.close()
    assert rows == [(CAPABILITY, MODEL, 'succeeded', '0.002', '0.0004')], rows


def _count(world, table):
    import sqlite3
    db = sqlite3.connect(f'file:{world.state / "episodes.sqlite3"}?mode=ro', uri=True)
    try:
        return db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
    finally:
        db.close()


class _Provider(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        from s1a_world import model_entry
        self.seen.append(('GET', self.path))
        self._reply({'data': [model_entry()]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.seen.append(('POST', self.path, body['model'], body['provider']))
        wire = json.loads(body['messages'][1]['content'])
        proposal = {'items': [{'kind': 'decision', 'text': 'Choose pool B',
                               'source_ids': [wire['sources'][0]['source_id']]}], 'unresolved_questions': []}
        self._reply({'id': 'gen-loopback', 'model': body['model'], 'usage': {'cost': 0.0001},
                     'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(proposal)}}]})

    def _reply(self, value):
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_the_real_transport_reaches_only_the_route_base_url(world):
    """Through the gateway's own HTTP transport, to a loopback provider named only by the route."""
    _Provider.seen = []
    server = ThreadingHTTPServer(('127.0.0.1', 0), _Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        document = world.gateway_document()
        document['providers']['openrouter']['base_url'] = f'http://127.0.0.1:{server.server_port}/api/v1'
        world.write_gateway(document)
        job, _ = world.enqueue()
        status = world.role(transport=None).tick()
    finally:
        server.shutdown()
        server.server_close()
    assert status['status'] == 'ok', status
    assert [entry[:2] for entry in _Provider.seen] == [('GET', '/api/v1/models'),
                                                        ('POST', '/api/v1/chat/completions')]
    assert _Provider.seen[1][2] == MODEL
    assert _Provider.seen[1][3]['zdr'] is True and _Provider.seen[1][3]['data_collection'] == 'deny'
    assert world.job(IMPL, job)['state'] == 'succeeded'
