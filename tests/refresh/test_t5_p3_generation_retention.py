"""T5 P3: generations are retained and bounded (as clarified by the dispatcher).

With ``snapshot_registry`` configured and readable, after each successful publish
only the published generation, generations referenced by the published profile or
the registry, and at most retain_generations - 1 most recent unreferenced
generations remain; removals are listed in a receipt. Without a readable registry
nothing is pruned and the retention receipt reports status "skipped" with a
reason. The current cycle's own failed build directory is always removed. A
generation without profile.json provenance is never pruned.

Falsifiers: unreferenced generations beyond the bound, a referenced one removed,
the current cycle's partial directory left behind, any generation removed while
the registry is unset or unreadable, or a profile.json-less generation removed.
"""
from pathlib import Path

import pytest

from t5_rig import core_and_ops, git, solo


def build_series(rig, key, count, *, retain):
    built = []
    for number in range(count):
        if number:
            rig.bump(key)
        assert rig.refresh()['status'] == 'published'
        built.append(rig.published_generation())
        rig.assert_retention(retain=retain, built=built)
    assert len(set(built)) == count  # every rebuild produced its own generation
    return built


def test_p3_default_retention_bounds_unreferenced_generations(rig):
    solo(rig)
    rig.configure_registry()
    build_series(rig, 'solo', 5, retain=3)


@pytest.mark.parametrize('retain', [1, 2])
def test_p3_retain_generations_bounds_unreferenced_generations(rig, retain):
    solo(rig)
    rig.configure_registry()
    rig.request['retain_generations'] = retain
    build_series(rig, 'solo', 4, retain=retain)


def test_p3_generation_referenced_through_reused_scip_is_never_removed(rig):
    core_and_ops(rig, dependent=False)
    rig.configure_registry()
    rig.request['retain_generations'] = 1
    built = []
    for number in range(3):
        if number:
            rig.bump('ops')
        assert rig.refresh()['status'] == 'published'
        built.append(rig.published_generation())
    profile = rig.published()
    assert Path(profile['repos']['core']['path']).resolve().is_relative_to(built[0])  # core reused from G1
    rig.assert_retention(retain=1, built=built)
    assert rig.refresh()['status'] == 'current'  # the reused source and index still validate


def test_p3_generation_referenced_through_reused_semantic_index_is_never_removed(rig):
    core_and_ops(rig)  # ops declares analysis_dependencies ["core"]
    rig.enable_semantic()
    rig.configure_registry()
    rig.request['retain_generations'] = 1
    built = []
    for number in range(3):
        if number:
            rig.bump('core')
        assert rig.refresh()['status'] == 'published'
        built.append(rig.published_generation())
        rig.assert_retention(retain=1, built=built)
    for key, entry in rig.published()['semantic_indexes'].items():
        rig.validate_semantic(entry['dir'], key, entry['revision'], expected_sha256=entry['index_sha256'])
    assert rig.refresh()['status'] == 'current'


def test_p3_generation_referenced_by_the_snapshot_registry_is_never_removed(rig):
    from kp_agent_tooling._impl.navigation_snapshot import select
    solo(rig)
    rig.configure_registry()
    rig.request['retain_generations'] = 1
    assert rig.refresh()['status'] == 'published'
    built = [rig.published_generation()]
    config, snapshot = rig.capture_snapshot()
    assert built[0] in rig.registry_generations()  # the registry names G1
    for _ in range(2):
        rig.bump('solo')
        assert rig.refresh()['status'] == 'published'
        built.append(rig.published_generation())
        rig.assert_retention(retain=1, built=built)
    assert built[0].exists()
    select(config, rig.registry, snapshot['snapshot_id'])  # the frozen review snapshot still resolves


@pytest.mark.parametrize('registry', ['unset', 'missing', 'unreadable'])
def test_p3_without_a_readable_registry_nothing_is_pruned_and_the_skip_is_receipted(rig, registry):
    solo(rig)  # no semantic selection: nothing else in this cycle reports "skipped"
    rig.request['retain_generations'] = 1
    if registry == 'missing':
        rig.request['snapshot_registry'] = str(rig.tmp / 'absent-registry')
    elif registry == 'unreadable':
        locked = rig.tmp / 'locked-registry'
        locked.mkdir()
        locked.chmod(0)
        rig.request['snapshot_registry'] = str(locked)
    try:
        built = []
        for number in range(3):
            if number:
                rig.bump('solo')
            result = rig.refresh()
            assert result['status'] == 'published'
            built.append(rig.published_generation())
            removed = [g.name for g in built if not g.exists()]
            assert not removed, f'generations removed while the registry is {registry}: {removed}'
            assert rig.skip_receipts(result), (
                f'no retention receipt reports status "skipped" with a reason (registry {registry})')
    finally:
        if registry == 'unreadable':
            locked.chmod(0o700)


@pytest.mark.parametrize('registry', ['unset', 'configured'])
def test_p3_current_cycle_failed_build_directory_is_removed(rig, registry):
    core_and_ops(rig, dependent=False)
    if registry == 'configured':
        rig.configure_registry()
    assert rig.refresh()['status'] == 'published'
    before = rig.generation_like_dirs()
    rig.bump('ops')
    rig.failing_index.add('ops')
    with pytest.raises(Exception):
        rig.refresh()
    left = sorted(p.name for p in rig.generation_like_dirs() - before)
    assert not left, f'the failed cycle left its partial build directory behind: {left}'
    assert all(p.exists() for p in before)


def test_p3_generation_without_profile_provenance_is_never_pruned(rig):
    solo(rig)
    rig.configure_registry()
    rig.request['retain_generations'] = 1
    legacy = rig.output_root / 'snapshot-legacy0'
    (legacy / 'solo').mkdir(parents=True)
    git(legacy / 'solo', 'init', '-q')
    (legacy / 'solo-python.scip').write_bytes(b'scip')
    assert legacy in rig.generation_like_dirs()
    built = []
    for number in range(3):
        if number:
            rig.bump('solo')
        assert rig.refresh()['status'] == 'published'
        built.append(rig.published_generation())
        rig.assert_retention(retain=1, built=built, kept=[legacy])
    assert (legacy / 'solo-python.scip').read_bytes() == b'scip'


def test_p3_removals_are_listed_in_a_receipt(rig):
    solo(rig)
    rig.configure_registry()
    rig.request['retain_generations'] = 1
    removed_all = []
    for number in range(3):
        if number:
            rig.bump('solo')
        before = rig.generation_like_dirs()
        result = rig.refresh()
        assert result['status'] == 'published'
        removed = sorted(g for g in before if not g.exists())
        text = rig.receipt_text(result)
        unlisted = [g.name for g in removed if g.name not in text]
        assert not unlisted, f'removed generations missing from the receipt: {unlisted}'
        removed_all += removed
    assert removed_all, 'no unreferenced generation was removed, so no removal was receipted'
