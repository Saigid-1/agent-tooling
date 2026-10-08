from datetime import datetime, timedelta, timezone

import pytest

from kp_agent_tooling._impl.service.episodic_capture_policy import evaluate_capture_trigger


NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
BASE = dict(session_id='session-1', model_id='model-1', context_epoch='epoch-1',
            pending_source_bytes=100, now=NOW)


def observed(current=500, **changes):
    value = dict(session_id='session-1', model_id='model-1', context_epoch='epoch-1',
                 current_window_tokens=current, context_window_tokens=1000,
                 observed_at=NOW.isoformat())
    value.update(changes)
    return value


def test_fresh_bound_current_window_crosses_configurable_threshold_once_per_epoch():
    assert not evaluate_capture_trigger(**BASE, usage=observed(499))['trigger']
    hit = evaluate_capture_trigger(**BASE, usage=observed(), threshold=.5)
    assert (hit['reason'], hit['measurement_kind'], hit['occupancy_fraction']) == (
        'context_threshold', 'current_window', .5)
    repeat = evaluate_capture_trigger(**BASE, usage=observed(900),
                                      last_trigger_epoch='epoch-1')
    assert not repeat['trigger']
    assert evaluate_capture_trigger(**BASE, usage=observed(900),
                                    last_trigger_epoch='prior')['trigger']


@pytest.mark.parametrize('untrusted', [
    {'session_id': 'other'}, {'model_id': 'other'}, {'context_epoch': 'other'},
    {'observed_at': (NOW-timedelta(seconds=301)).isoformat()},
    {'current_window_tokens': -1}, {'current_window_tokens': 1001},
    {'current_window_tokens': None},
])
def test_untrusted_or_stale_usage_is_not_occupancy(untrusted):
    result = evaluate_capture_trigger(**BASE, usage=observed(800, **untrusted))
    assert not result['trigger']
    assert result['occupancy_fraction'] is None
    assert result['unknowns'] == ['current_window_occupancy_unavailable']


def test_cumulative_billing_is_not_occupancy_and_source_bytes_trigger_batch():
    billing = dict(session_id='session-1', model_id='model-1', context_epoch='epoch-1',
                   observed_at=NOW.isoformat(), input_tokens=90000,
                   context_window_tokens=1000)
    result = evaluate_capture_trigger(**BASE | {'pending_source_bytes': 32768},
                                      usage=billing)
    assert result['reason'] == 'batch'
    assert result['measurement_kind'] == 'source_bytes'
    assert result['occupancy_fraction'] is None


@pytest.mark.parametrize(('event', 'reason'), [
    ('pre_compact', 'batch'), ('session_end', 'session_end')])
def test_lifecycle_flushes_pending_source_below_threshold(event, reason):
    result = evaluate_capture_trigger(**BASE, usage=observed(100), event=event)
    assert (result['reason'], result['measurement_kind'], result['trigger_event']) == (
        reason, 'lifecycle', event)


def test_no_pending_source_never_claims_capture_or_success():
    result = evaluate_capture_trigger(**BASE | {'pending_source_bytes': 0},
                                      usage=observed(900), event='session_end')
    assert result['trigger'] is False and result['reason'] is None
    assert 'captured' not in result and 'committed' not in result


def test_source_batches_continue_after_epoch_threshold_fired():
    result = evaluate_capture_trigger(**BASE | {'pending_source_bytes': 40000},
                                      usage=observed(900), last_trigger_epoch='epoch-1')
    assert result['reason'] == 'batch'
    assert result['measurement_kind'] == 'source_bytes'


def test_unknown_model_or_epoch_cannot_establish_occupancy():
    for field in ('model_id', 'context_epoch'):
        result = evaluate_capture_trigger(**BASE | {field: 'unknown'},
                                          usage=observed(900, **{field: 'unknown'}))
        assert not result['trigger']
        assert result['occupancy_fraction'] is None
