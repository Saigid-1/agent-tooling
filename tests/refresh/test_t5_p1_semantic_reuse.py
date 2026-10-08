"""T5 P1: semantic reuse follows its own identity, not the SCIP identity.

Falsifier: a dependency-only change that re-embeds an unchanged repository.
"""
from t5_rig import core_and_ops


def without_dir(entry):
    return {key: value for key, value in entry.items() if key != 'dir'}


def test_p1_dependency_only_change_keeps_the_unchanged_repository_semantic_index(rig):
    core_and_ops(rig)  # ops declares analysis_dependencies ["core"]
    rig.enable_semantic()
    assert rig.refresh()['status'] == 'published'
    assert sorted(rig.embeds) == ['core', 'ops']
    before = rig.published()

    rig.bump('core')
    second = rig.refresh()
    after = rig.published()
    # The scenario: ops's own revision and semantic identity hold; its SCIP identity moved.
    assert after['repos']['ops']['revision'] == before['repos']['ops']['revision']
    assert after['build_identities']['ops']['semantic'] == before['build_identities']['ops']['semantic']
    assert after['build_identities']['ops']['scip'] != before['build_identities']['ops']['scip']

    assert second['status'] == 'published'
    assert rig.embeds.count('ops') == 1, f'dependency-only change re-embedded ops: embeds={rig.embeds}'
    assert rig.embeds.count('core') == 2
    assert without_dir(after['semantic_indexes']['ops']) == without_dir(before['semantic_indexes']['ops'])
    assert second['repository_readiness']['ops']['semantic'] == 'validated_reuse'
    assert after['repository_readiness']['ops']['semantic'] == 'validated_reuse'
    entry = after['semantic_indexes']['ops']
    rig.validate_semantic(entry['dir'], 'ops', entry['revision'], expected_sha256=entry['index_sha256'])

    # Nothing moved since: the kept index serves the unchanged fast path.
    assert rig.refresh()['status'] == 'current'
    assert len(rig.embeds) == 3


def test_p1_own_revision_change_still_re_embeds(rig):
    core_and_ops(rig)
    rig.enable_semantic()
    rig.refresh()
    revision = rig.bump('ops')
    assert rig.refresh()['status'] == 'published'
    assert rig.embeds.count('ops') == 2 and rig.embeds.count('core') == 1
    assert rig.published()['semantic_indexes']['ops']['revision'] == revision
