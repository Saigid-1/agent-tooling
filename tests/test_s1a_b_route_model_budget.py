"""S1a falsifier (b): only the route's provider, at the pinned model, under the budget.

Order: docs/work/orders/S1a-scheduled-summarizer.md, P2 and falsifier (b). Seams: tests/s1a_seams.py.
Instruments: tests/s1a_harness.py (network guard, loopback stub provider, queue and store reads).

The world: one desk (`alpha`) approved and admitted, with one queued job of one small episode (one packet).
The gateway route sends `memory.summarize` to the stub at a distinctive base path, with a priced budget.

GREEN-IF, per test:
- `test_one_request_per_packet_to_the_route_at_the_approved_model_with_privacy`: one tick sends exactly as
  many completion POSTs as the job has attempts (one packet here: one POST, one attempt); each POST goes to
  `<route base_url>/chat/completions`, carries the key file's key, has `model` equal to the approved model, no
  `models` fallback list, and `provider` with `zdr: true`, `data_collection: 'deny'` and
  `require_parameters: true`; every connection attempt was to the stub; the job succeeded with a capsule.
- `test_route_params_weakening_privacy_are_refused_before_the_network[params]`: with a route `params` entry
  that weakens privacy (zdr off, data collection allowed, require_parameters off, or a `models` fallback
  list) the role reports `not_configured`, the guard saw zero network events, and the job is untouched.
- `test_reported_model_other_than_requested_gives_needs_review_without_capsule`: the stub reports another
  model; one POST was sent, the attempt is `failed_or_uncertain`, the job is `needs_review`, no capsule.
- `test_budget_refusal_leaves_the_job_claimable_without_requests`: the calls-per-hour cap is already reached
  in the gateway's own ledger; the tick sends zero POSTs and the job is not `needs_review`; after the cap is
  raised, the next tick sends one POST and the job succeeds (it was claimable).
- `test_budget_refusal_records_the_attempt_as_not_sent`: in the same refusal, the queue records the attempt
  in the not-sent state (s1a_seams.NOT_SENT_STATE), and no attempt as `failed_or_uncertain`.
- `test_usd_cap_reached_by_the_estimate_sends_nothing`: `max_usd_per_day` 0.01 with
  `estimated_usd_per_call` 0.05 and an empty ledger: zero POSTs and the job is not `needs_review`; control:
  with `max_usd_per_day` 5.0 the next tick sends one POST and the job succeeds.

Mutants that must be RED: a fallback list or a weakening `params` reaching the wire; no model comparison;
a budget refusal treated as uncertain (needs_review) or as a retry-later without a record; the hard-coded
`openrouter.ai` transport (every connection is refused and the job does not succeed); privacy routing removed.
"""
from __future__ import annotations

import pytest

import s1a_seams as seams
from s1a_harness import BASE_PATH, OTHER_MODEL, assert_no_child_process, make_world, run_role  # noqa: F401


def _post_problems(post):
    problems = []
    body = post.json()
    if post.path != BASE_PATH + seams.COMPLETION_SUFFIX:
        problems.append(f'path {post.raw_path}')
    if post.credential != 'key-file':
        problems.append(f'credential {post.credential}')
    if not isinstance(body, dict):
        return problems + ['body is not a JSON object']
    if body.get('model') != seams.APPROVED_MODEL:
        problems.append(f'model {body.get("model")!r}')
    if 'models' in body:
        problems.append(f'fallback list models={body["models"]!r}')
    provider = body.get('provider') if isinstance(body.get('provider'), dict) else {}
    for key, value in (('zdr', True), ('data_collection', 'deny'), ('require_parameters', True)):
        if provider.get(key) != value:
            problems.append(f'provider.{key}={provider.get(key)!r}')
    return problems


def test_one_request_per_packet_to_the_route_at_the_approved_model_with_privacy(make_world):
    world = make_world().build()
    run = run_role(world)
    assert_no_child_process(run)
    assert not run.guard.refused(), f'the role tried to reach something other than the route:\n{run.describe()}'
    job = world.job('alpha')
    attempts = world.attempts(job['id'])
    posts = run.posts()
    assert len(posts) == 1 and len(attempts) == 1, (
        f'one packet: expected one POST and one attempt, got {len(posts)} POSTs, attempts {attempts}\n'
        f'{run.describe()}')
    problems = [(i, p) for i, post in enumerate(posts) for p in _post_problems(post)]
    assert not problems, f'completion requests off the route, model or privacy routing: {problems}'
    assert job['state'] == 'succeeded' and job['capsule_id'], f'job {job}\n{run.describe()}'
    assert world.capsule_count('alpha') == 1


WEAKENING = {
    'zdr_off': {'provider': {'zdr': False}},
    'data_collection_allow': {'provider': {'data_collection': 'allow'}},
    'require_parameters_off': {'provider': {'require_parameters': False}},
    'fallback_list': {'models': [seams.APPROVED_MODEL, OTHER_MODEL]},
}


@pytest.mark.parametrize('case', sorted(WEAKENING))
def test_route_params_weakening_privacy_are_refused_before_the_network(make_world, case):
    world = make_world().build()
    world.write_gateway(params=WEAKENING[case])
    run = run_role(world)
    assert_no_child_process(run)
    assert not run.network() and not run.requests, (
        f'a route weakening privacy ({case}) reached the network:\n{run.describe()}')
    assert run.status_nodes(), f'no `{seams.NOT_CONFIGURED}` status for a weakening route ({case}):\n{run.describe()}'
    job = world.job('alpha')
    assert world.untouched(job), f'the job was touched: {job} {world.attempts(job["id"])}'


def test_reported_model_other_than_requested_gives_needs_review_without_capsule(make_world):
    world = make_world().build()
    world.stub.reported_model = OTHER_MODEL
    run = run_role(world)
    assert_no_child_process(run)
    assert len(run.posts()) == 1, f'expected the one request to be sent:\n{run.describe()}'
    job = world.job('alpha')
    attempts = world.attempts(job['id'])
    assert job['state'] == 'needs_review', f'a response from {OTHER_MODEL} left the job {job}\n{run.describe()}'
    assert [a['state'] for a in attempts] == ['failed_or_uncertain'], f'attempts {attempts}'
    assert not job['capsule_id'] and world.capsule_count('alpha') == 0, 'a capsule was written from another model'


def _capped_world(make_world):
    world = make_world().build()
    world.write_gateway(budget={'max_calls_per_hour': 1, 'max_usd_per_day': 5.0, 'estimated_usd_per_call': 0.01})
    world.prefill_ledger(1)
    return world


def test_budget_refusal_leaves_the_job_claimable_without_requests(make_world):
    world = _capped_world(make_world)
    run = run_role(world)
    assert_no_child_process(run)
    assert not run.posts(), f'a completion was sent over the calls-per-hour cap:\n{run.describe()}'
    job = world.job('alpha')
    assert job['state'] != 'needs_review', f'a budget refusal sent the job to review: {job}'
    assert not job['capsule_id'], job
    world.write_gateway(budget={'max_calls_per_hour': 100, 'max_usd_per_day': 5.0, 'estimated_usd_per_call': 0.01})
    later = run_role(world)
    job = world.job('alpha')
    assert len(later.posts()) == 1 and job['state'] == 'succeeded', (
        f'the refused job was not claimable on a later tick: {job}\n{later.describe()}')


def test_budget_refusal_records_the_attempt_as_not_sent(make_world):
    world = _capped_world(make_world)
    run = run_role(world)
    assert not run.posts(), run.describe()
    job = world.job('alpha')
    states = [a['state'] for a in world.attempts(job['id'])]
    assert states == [seams.NOT_SENT_STATE], (
        f'expected the refused attempt recorded as {seams.NOT_SENT_STATE!r}, got attempts {states} (job {job})')


def test_usd_cap_reached_by_the_estimate_sends_nothing(make_world):
    world = make_world().build()
    world.write_gateway(budget={'max_calls_per_hour': 100, 'max_usd_per_day': 0.01, 'estimated_usd_per_call': 0.05})
    run = run_role(world)
    assert_no_child_process(run)
    assert not run.posts(), f'a completion was sent although the estimate exceeds the USD cap:\n{run.describe()}'
    job = world.job('alpha')
    assert job['state'] != 'needs_review' and not job['capsule_id'], f'job {job}'
    # Control: the same world under a cap the estimate fits does send.
    world.write_gateway(budget={'max_calls_per_hour': 100, 'max_usd_per_day': 5.0, 'estimated_usd_per_call': 0.05})
    later = run_role(world)
    assert len(later.posts()) == 1 and world.job('alpha')['state'] == 'succeeded', (
        f'control: under a cap the estimate fits, the job was not sent:\n{later.describe()}')
