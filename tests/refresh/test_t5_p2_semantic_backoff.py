"""T5 P2: failure backoff follows the failed build's own (revision, semantic identity).

Falsifier: a retry inside the window caused by a dependency-only change, or a gap
that is not reported.
"""
import json
from pathlib import Path

from t5_rig import core_and_ops, instant

SLACK = 2


def failing_ops(rig, **request):
    core_and_ops(rig)  # ops declares analysis_dependencies ["core"]
    rig.enable_semantic()
    rig.semantic_failures['ops'] = 'build_deadline'
    rig.request.update(request)


def assert_gap(readiness, retry_after):
    assert readiness['semantic'] == 'unavailable'
    assert readiness['semantic_reason'] == 'build_deadline'
    assert abs(instant(readiness['semantic_retry_after']) - retry_after) <= 1, (
        f"backoff moved: {readiness['semantic_retry_after']}")


def test_p2_dependency_only_change_inside_window_does_not_retry_and_reports_gap(rig):
    failing_ops(rig)
    first = rig.refresh()
    assert first['status'] == 'published'
    before = rig.published()
    retry_after = instant(first['repository_readiness']['ops']['semantic_retry_after'])
    assert retry_after is not None

    rig.bump('core')
    second = rig.refresh()
    after = rig.published()
    assert after['build_identities']['ops']['semantic'] == before['build_identities']['ops']['semantic']
    assert after['build_identities']['ops']['scip'] != before['build_identities']['ops']['scip']

    assert second['status'] == 'published'
    assert rig.embeds.count('ops') == 1, f'dependency-only change retried ops inside its window: {rig.embeds}'
    assert rig.embeds.count('core') == 2
    assert_gap(second['repository_readiness']['ops'], retry_after)
    assert_gap(after['repository_readiness']['ops'], retry_after)
    assert 'ops' not in after['semantic_indexes']


def test_p2_default_backoff_window_is_86400_seconds(rig):
    failing_ops(rig)
    result = rig.refresh()
    for readiness in (result['repository_readiness']['ops'], rig.published()['repository_readiness']['ops']):
        retry_after = instant(readiness['semantic_retry_after'])
        assert rig.started + 86400 - SLACK <= retry_after <= rig.finished + 86400 + SLACK, (
            f"retry at {readiness['semantic_retry_after']} is not failure time + 86400s")


def test_p2_backoff_window_is_semantic_retry_after_seconds(rig):
    failing_ops(rig, semantic_retry_after_seconds=300)
    result = rig.refresh()
    retry_after = instant(result['repository_readiness']['ops']['semantic_retry_after'])
    assert rig.started + 300 - SLACK <= retry_after <= rig.finished + 300 + SLACK, (
        f"retry at {result['repository_readiness']['ops']['semantic_retry_after']} ignores the configured 300s")


def test_p2_elapsed_window_allows_retry_after_dependency_only_change(rig):
    failing_ops(rig)
    rig.refresh()
    publication = Path(rig.request['publication'])
    profile = json.loads(publication.read_text())
    profile['repository_readiness']['ops']['semantic_retry_after'] = '2000-01-01T00:00:00+00:00'
    publication.write_text(json.dumps(profile))
    rig.bump('core')
    assert rig.refresh()['status'] == 'published'
    assert rig.embeds.count('ops') == 2


def test_p2_own_revision_change_retries_inside_window(rig):
    failing_ops(rig)
    rig.refresh()
    rig.bump('ops')
    assert rig.refresh()['status'] == 'published'
    assert rig.embeds.count('ops') == 2
