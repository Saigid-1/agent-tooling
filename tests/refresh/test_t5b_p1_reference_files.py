"""T5b P1: listed reference files are kept.

Order: docs/work/orders/T5b-retention-references.md. The refresh request's optional
``retention_references`` lists up to 32 absolute paths of files under ``/config`` or
``/state``. Each run of retention reads the files as they are at that moment. Any
generation a listed file mentions (the snapshot registry's mention rules: absolute
paths under the generations root, in JSON values or text) is kept, and the
retention receipt lists it as ``referenced_by_reference_files``.

Falsifier: a generation a listed file references is removed, or a stale copy of an
earlier version of the file decides instead of the current one.

Every test configures a readable, empty snapshot registry (T5: pruning runs only
then) and ``retain_generations = 1``, so a superseded generation that nothing
references is removed as beyond retention. Each test also shows pruning ran in a
pass of its own: an unlisted superseded generation is removed. /config and /state
are the modelled bind mounts of t5b_rig.ContainerMounts.
"""
import json

import pytest

from t5_rig import solo
from t5b_rig import listed_by_reference_files, mention, mounts, publish_next, reported  # noqa: F401


@pytest.fixture
def pruning(rig):
    solo(rig)
    rig.configure_registry()
    rig.request['retain_generations'] = 1
    return rig


@pytest.mark.parametrize('form,location', [('json', '/config/knowledge.json'),
                                           ('text', '/state/pins/knowledge.env')],
                         ids=['json-catalog-under-config', 'text-file-under-state'])
def test_p1_generation_named_only_by_a_listed_file_survives_retain_1_and_is_receipted(
        pruning, mounts, form, location):
    rig = pruning
    assert rig.refresh()['status'] == 'published'
    g1 = rig.published_generation()
    mounts.write(location, mention(rig, g1, form))
    rig.request['retention_references'] = [location]

    result, g2 = publish_next(rig)
    assert g1.exists(), f'{g1.name}, named only by the listed {form} file {location}, was removed'
    values = listed_by_reference_files(result)
    assert reported(values, g1), (
        f'the retention receipt does not list {g1.name} as referenced_by_reference_files: '
        f'{result.get("retention")}')

    result, g3 = publish_next(rig)
    assert g1.exists(), f'{g1.name}, named only by the listed file, was removed on a later pass'
    assert reported(listed_by_reference_files(result), g1)
    assert not g2.exists(), (f'pruning did not run: unlisted superseded {g2.name} remains with '
                             f'retain_generations=1: {result.get("retention")}')


def test_p1_each_listed_file_keeps_the_generations_it_names(pruning, mounts):
    rig = pruning
    catalog, pins = '/config/knowledge.json', '/state/pins/review.json'
    mounts.write(catalog, '{}')
    mounts.write(pins, '{}')
    rig.request['retention_references'] = [catalog, pins]
    assert rig.refresh()['status'] == 'published'
    g1 = rig.published_generation()

    mounts.write(catalog, mention(rig, g1, 'json'))
    result, g2 = publish_next(rig)
    assert g1.exists(), f'{g1.name}, named by {catalog}, was removed'

    # A JSON value nested in a list, in the second listed file, under the other mount.
    mounts.write(pins, json.dumps({'review': [{'source': str(g2 / 'solo')}]}))
    result, g3 = publish_next(rig)
    assert g1.exists() and g2.exists(), (
        f'a generation a listed file names was removed: {g1.name} exists={g1.exists()}, '
        f'{g2.name} exists={g2.exists()}')
    values = listed_by_reference_files(result)
    assert reported(values, g1) and reported(values, g2), (
        f'referenced_by_reference_files does not list both {g1.name} and {g2.name}: {values}')

    result, g4 = publish_next(rig)
    assert g1.exists() and g2.exists()
    assert not g3.exists(), f'pruning did not run: unlisted superseded {g3.name} remains'


def test_p1_each_pass_reads_the_listed_file_as_it_is_then(pruning, mounts):
    rig = pruning
    catalog = '/config/knowledge.json'
    assert rig.refresh()['status'] == 'published'
    g1 = rig.published_generation()
    mounts.write(catalog, mention(rig, g1, 'json'))
    rig.request['retention_references'] = [catalog]

    result, g2 = publish_next(rig)
    assert g1.exists(), f'{g1.name}, named by {catalog}, was removed'

    # The operator moves the catalog's pin from G1 to G2 between passes.
    mounts.write(catalog, mention(rig, g2, 'json'))
    result, g3 = publish_next(rig)
    assert g2.exists(), f'{g2.name}, newly named by the listed file, was removed'
    assert not g1.exists(), (f'{g1.name}, no longer named by the listed file, was kept: an earlier '
                             f'version of the file decided: {result.get("retention")}')
    values = listed_by_reference_files(result)
    assert reported(values, g2), f'referenced_by_reference_files does not list {g2.name}: {values}'
    assert not reported(values, g1), f'referenced_by_reference_files still lists {g1.name}: {values}'
