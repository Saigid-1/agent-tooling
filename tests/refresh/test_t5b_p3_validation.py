"""T5b P3: validated before work; an absent field behaves exactly as today.

Order: docs/work/orders/T5b-retention-references.md. An invalid
``retention_references`` (a relative path, more than 32 entries, or a path outside
``/config`` or ``/state``) is refused before any source work, like the other T5
options: in process as ValueError (T5's convention, tests/refresh/test_t5_request_fields.py),
through the console script as a non-zero exit. When the field is absent, behaviour
is exactly as today.

Falsifier: a bad value accepted, or any change to the absent-field behaviour.

Readings (repeated in the arm report under AMBIGUITY):
- "wrong type" (the dispatcher's list) is any value other than a list of strings;
  JSON null is not tested (it could be read as absent);
- a path whose normal form leaves the mount (``/config/../etc/...``) and a sibling
  spelling (``/configuration/...``, ``/state-backup/...``) are outside /config and /state;
- "exactly as today" is compared on the retention decisions and on every field of
  today's retention receipt (returned and persisted); a receipt may carry additional
  fields. The absent-field test is RED at base only through its last step, which
  lists the same file and must change the decision.
"""
import json
from pathlib import Path
import subprocess

import pytest

from t5_rig import console_script, solo
from t5b_rig import (MAX_REFERENCES, listed_by_reference_files, mention, mounts,  # noqa: F401
                     publish_next, reported)

HOST_PATH = object()  # replaced by an absolute path in the test's own temporary directory

INVALID = {
    'relative': ['config/knowledge.json'],
    'relative-dot': ['./knowledge.json'],
    'empty-string': [''],
    'one-relative-among-valid': ['/config/knowledge.json', 'pins.json'],
    'outside-absolute': ['/etc/knowledge.json'],
    'outside-host-path': [HOST_PATH],
    'sibling-of-config': ['/configuration/knowledge.json'],
    'sibling-of-state': ['/state-backup/pins.json'],
    'dotdot-escape': ['/config/../etc/knowledge.json'],
    'more-than-32': [f'/config/refs/{number:02d}.json' for number in range(MAX_REFERENCES + 1)],
    'type-string': '/config/knowledge.json',
    'type-object': {'/config/knowledge.json': True},
    'type-number': 7,
    'type-bool': True,
    'entry-number': [7],
    'entry-null': [None],
    'entry-list': [['/config/knowledge.json']],
}
CONSOLE_CASES = ['relative', 'more-than-32', 'outside-absolute', 'type-string']


def invalid_value(rig, case):
    value = INVALID[case]
    if isinstance(value, list):
        value = [str(rig.tmp / 'knowledge.json') if item is HOST_PATH else item for item in value]
    return value


@pytest.mark.parametrize('case', sorted(INVALID))
def test_p3_invalid_retention_references_refused_before_any_source_work(rig, case):
    solo(rig)
    rig.configure_registry()
    rig.request['retention_references'] = invalid_value(rig, case)
    with pytest.raises(ValueError):
        rig.refresh()
    assert rig.clones == [] and rig.index_runs == [], (
        f'source work before refusing retention_references ({case}): clones={rig.clones}, '
        f'index runs={rig.index_runs}')
    assert not Path(rig.request['publication']).exists()


@pytest.mark.parametrize('case', CONSOLE_CASES)
def test_p3_console_script_refuses_invalid_retention_references_before_any_source_work(rig, case):
    solo(rig)
    rig.configure_registry()
    rig.request['retention_references'] = invalid_value(rig, case)
    completed = subprocess.run([str(console_script()), '--request', str(rig.write_request())],
                               capture_output=True, text=True, timeout=300)
    assert completed.returncode != 0, (completed.stdout[-2000:], completed.stderr[-2000:])
    assert rig.clones == [] and rig.bypass_runs == [], (
        f'kp-agent-refresh did source work before refusing retention_references ({case}): '
        f'clones={rig.clones}, toolchain runs={rig.bypass_runs}')
    assert not Path(rig.request['publication']).exists()


def test_p3_thirty_two_listed_files_are_accepted_and_each_is_read(rig, mounts):
    solo(rig)
    rig.configure_registry()
    rig.request['retain_generations'] = 1
    assert rig.refresh()['status'] == 'published'
    g1 = rig.published_generation()
    listed = [f'/config/refs/{number:02d}.json' for number in range(MAX_REFERENCES - 1)]
    listed.append('/state/pins/catalog.json')
    for path in listed[:-1]:
        mounts.write(path, '{}')
    mounts.write(listed[-1], mention(rig, g1, 'json'))  # only the 32nd file names G1
    rig.request['retention_references'] = listed

    result, g2 = publish_next(rig)  # 32 entries, both mounts: accepted
    assert g1.exists(), f'{g1.name}, named by the 32nd listed file, was removed'
    result, g3 = publish_next(rig)
    assert g1.exists(), f'{g1.name}, named by the 32nd listed file, was removed'
    assert not g2.exists(), f'pruning did not run: unlisted superseded {g2.name} remains'
    assert reported(listed_by_reference_files(result), g1)


def todays_receipt(*, removed, published):
    """Today's retention receipt for one pruning pass of this test (retain 1, empty registry)."""
    return {'schema_version': 'agent-tooling.refresh-retention-receipt.v1', 'retain_generations': 1,
            'status': 'pruned', 'removed': [{'generation': name, 'reason': 'beyond_retention'} for name in removed],
            'errors': [], 'published': [published], 'referenced_by_registry': [],
            'retained_unreferenced': [], 'retained_unknown': [], 'retained_unknown_count': 0}


def test_p3_absent_field_keeps_todays_retention_and_only_listing_the_file_changes_it(rig, mounts):
    solo(rig)
    rig.configure_registry()
    rig.request['retain_generations'] = 1
    assert 'retention_references' not in rig.request
    catalog = '/config/knowledge.json'
    assert rig.refresh()['status'] == 'published'
    g1 = rig.published_generation()
    # An operator catalog naming G1 exists at the live path, but the request does not list it.
    mounts.write(catalog, mention(rig, g1, 'json'))

    result, g2 = publish_next(rig)
    rig.assert_retention(retain=1, built=[g1, g2])  # T5 P3's expectation
    assert not g1.exists(), f'{g1.name} was kept although the request lists no reference file'
    persisted = json.loads((rig.output_root / 'retention-receipt.json').read_text())
    today = todays_receipt(removed=[g1.name], published=g2.name)
    for receipt in (result['retention'], persisted):
        assert {key: receipt.get(key) for key in today} == today
        assert receipt['surfaces']['published_profile'] == str(rig.request['publication'])
        assert receipt['surfaces']['snapshot_registry'] == str(rig.registry)

    # The same file, now listed, is what keeps a generation (RED at base: the field is ignored).
    mounts.write(catalog, mention(rig, g2, 'json'))
    rig.request['retention_references'] = [catalog]
    result, g3 = publish_next(rig)
    assert g2.exists(), f'{g2.name}, named by the listed file, was removed'
