"""T5b P2: fail-safe.

Order: docs/work/orders/T5b-retention-references.md. A listed file that is missing,
unreadable or over the read budget means retention removes nothing in that pass.
The receipt says why. Publication is never failed.

Falsifier: a pruning pass with an unreadable reference file, or a refresh failure
caused by retention.

Readings (repeated in the arm report under AMBIGUITY):
- "the read budget" is the snapshot registry's per-file read budget at the base,
  16,000,000 bytes (the order reuses the registry's mention rules);
- "the receipt says why" follows T5's fail-safe convention: this cycle's retention
  receipt reports status "skipped" with a reason (as for an unreadable registry).

Every test configures a readable snapshot registry and ``retain_generations = 1``,
so without the listed file each pass would remove the superseded generation.
"""
import os
from pathlib import Path

import pytest

from t5_rig import solo
from t5b_rig import READ_BUDGET_BYTES, mention, mounts, publish_next  # noqa: F401

CATALOG = '/config/knowledge.json'
PROBLEMS = ['missing', 'unreadable', 'over-read-budget', 'deleted-after-a-good-pass']


@pytest.mark.parametrize('problem', PROBLEMS)
def test_p2_unusable_listed_file_removes_nothing_publication_succeeds_and_the_receipt_says_why(
        rig, mounts, problem):
    solo(rig)
    rig.configure_registry()
    rig.request['retain_generations'] = 1
    assert rig.refresh()['status'] == 'published'
    built = [rig.published_generation()]

    if problem == 'deleted-after-a-good-pass':
        mounts.write(CATALOG, mention(rig, built[0], 'json'))
        rig.request['retention_references'] = [CATALOG]
        result, generation = publish_next(rig)
        built.append(generation)
        assert built[0].exists(), 'setup: the readable listed file did not keep the generation it names'
        mounts.remove(CATALOG)
    else:
        rig.request['retention_references'] = [CATALOG]
        if problem == 'unreadable':
            mounts.write(CATALOG, mention(rig, built[0], 'json'))
            mounts.chmod(CATALOG, 0)
            if os.access(mounts.host(CATALOG), os.R_OK):
                mounts.chmod(CATALOG, 0o600)
                pytest.skip('this process reads mode-0 files (privileged); "unreadable" cannot be staged')
        elif problem == 'over-read-budget':
            head = mention(rig, built[0], 'text').encode()
            mounts.write(CATALOG, head + b' ' * (READ_BUDGET_BYTES + 1 - len(head)))
            assert mounts.host(CATALOG).stat().st_size == READ_BUDGET_BYTES + 1

    try:
        for _ in range(2):
            before = rig.generation_like_dirs()
            result, generation = publish_next(rig)  # publication is never failed
            built.append(generation)
            removed = sorted(p.name for p in before if not p.exists())
            assert not removed, (f'retention removed {removed} while the listed file is {problem}: '
                                 f'{result.get("retention")}')
            assert Path(rig.request['publication']).is_file() and rig.status()['status'] == 'published'
            assert rig.skip_receipts(result), (
                f'no retention receipt says why nothing was pruned (listed file {problem}): '
                f'{result.get("retention")}')
    finally:
        if problem == 'unreadable':
            mounts.chmod(CATALOG, 0o600)
