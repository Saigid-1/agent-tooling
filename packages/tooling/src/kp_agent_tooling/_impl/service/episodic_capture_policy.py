"""Pure decision policy for incremental episodic source capture.

The caller owns durable cursor and last-trigger state. A decision is never a
receipt of capture or enqueue success; persist state only after those succeed.
"""

from datetime import datetime, timezone
from collections.abc import Mapping
import math


def _observed_fraction(usage, *, session_id, model_id, context_epoch, now,
                       max_usage_age_seconds):
    if model_id == 'unknown' or context_epoch == 'unknown':
        return None
    if not isinstance(usage, Mapping):
        return None
    if any(usage.get(key) != expected for key, expected in (
            ('session_id', session_id), ('model_id', model_id),
            ('context_epoch', context_epoch))):
        return None
    observed_at = usage.get('observed_at')
    if isinstance(observed_at, str):
        try:
            observed_at = datetime.fromisoformat(observed_at)
        except ValueError:
            return None
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
        return None
    age = (now - observed_at).total_seconds()
    if not 0 <= age <= max_usage_age_seconds:
        return None
    current, capacity = usage.get('current_window_tokens'), usage.get('context_window_tokens')
    if (type(current) is not int or type(capacity) is not int or
            current < 0 or capacity <= 0 or current > capacity):
        return None
    return current / capacity


def evaluate_capture_trigger(*, session_id: str, model_id: str,
                             context_epoch: str, pending_source_bytes: int,
                             usage: Mapping | None = None,
                             last_trigger_epoch: str | None = None,
                             event: str | None = None, threshold: float = .5,
                             source_batch_bytes: int = 32768,
                             now: datetime | None = None,
                             max_usage_age_seconds: int = 300) -> dict:
    """Choose a capture trigger from trusted occupancy, source size, or lifecycle.

    ``usage`` must describe the *current window* and match session, model, and
    context epoch. Cumulative billing counters are deliberately unrecognized.
    An unavailable observation activates the declared source-byte fallback.
    """
    if any(not isinstance(v, str) or not v for v in (session_id, model_id, context_epoch)):
        raise ValueError('session, model and context epoch required')
    if type(pending_source_bytes) is not int or not 0 <= pending_source_bytes <= 100_000_000:
        raise ValueError('bounded pending source bytes required')
    if last_trigger_epoch is not None and (not isinstance(last_trigger_epoch, str) or not last_trigger_epoch):
        raise ValueError('valid last trigger epoch required')
    if event not in {None, 'pre_compact', 'session_end'}:
        raise ValueError('unsupported capture lifecycle event')
    if type(threshold) not in (float, int) or not math.isfinite(threshold) or not 0 < threshold < 1:
        raise ValueError('occupancy threshold must be between zero and one')
    if type(source_batch_bytes) is not int or not 1000 <= source_batch_bytes <= 200000:
        raise ValueError('bounded source batch threshold required')
    if type(max_usage_age_seconds) is not int or not 1 <= max_usage_age_seconds <= 3600:
        raise ValueError('bounded observation freshness required')
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError('aware evaluation time required')

    fraction = _observed_fraction(usage, session_id=session_id, model_id=model_id,
                                  context_epoch=context_epoch, now=now,
                                  max_usage_age_seconds=max_usage_age_seconds)
    unknowns = [] if fraction is not None else ['current_window_occupancy_unavailable']
    measurement = 'current_window' if fraction is not None else 'source_bytes'
    reason = None
    if pending_source_bytes:
        if event == 'session_end':
            reason, measurement = 'session_end', 'lifecycle'
        elif event == 'pre_compact':
            reason, measurement = 'batch', 'lifecycle'
        elif fraction is not None and fraction >= threshold and last_trigger_epoch != context_epoch:
            reason = 'context_threshold'
        elif pending_source_bytes >= source_batch_bytes:
            reason, measurement = 'batch', 'source_bytes'
    return {'trigger': reason is not None, 'reason': reason,
            'measurement_kind': measurement, 'occupancy_fraction': fraction,
            'unknowns': unknowns, 'pending_source_bytes': pending_source_bytes,
            'context_epoch': context_epoch, 'trigger_event': event if reason else None}
