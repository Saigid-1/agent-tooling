"""T11b Q3, Verification U1 (meet, 2026-10-04): a private write ends exactly 0600.

Order (Q3): "Every private file is created with its mode, so there is no create-then-chmod window."
The leaf's writers promise more than the create: ``overwrite_private`` sets an existing file with
another mode to 0600 before writing, and ``touch_new_private`` and ``write_new_json`` "end exactly
0600". Base had that guarantee too: write-then-chmod and 'xb'-then-chmod always ended 0600, whatever
the umask. T11a's P3 golden pins the umask at 0o022 and never overwrites a wider file, so it cannot
see the guarantee go. Verification's surviving mutant MV8 (``_settle_mode``'s body replaced by
``return``) left all 21 Q1/Q2/Q3/regression tests green.

GREEN-IF:
- an existing 0644 file that ``overwrite_private`` writes ends 0600, with the new content;
- a file that ``touch_new_private`` creates under umask 0o277 ends 0600 (created 0400 by the umask);
- a file that ``write_new_json`` creates under umask 0o277 ends 0600 and holds the value.

Under umask 0o022 each case is a control that passes with or without the settle. Under MV8: the
overwrite case ends 0644, and the two 0o277 cases end 0400.
"""
from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager

import pytest

leaf = pytest.importorskip('kp_agent_tooling._impl.leaf', reason='no leaf module at base')


@contextmanager
def umask(mask):
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.parametrize('existing', [0o644, 0o600], ids=['existing-0644', 'existing-0600'])
def test_u1_overwrite_private_leaves_an_existing_file_exactly_0600(tmp_path, existing):
    path = tmp_path / 'private.json'
    path.write_text('old')
    os.chmod(path, existing)
    with umask(0o022):
        leaf.overwrite_private(path, 'new')
    assert (mode(path), path.read_text()) == (0o600, 'new'), (
        f'overwrite_private over a {existing:#o} file left it {mode(path):#o}')


@pytest.mark.parametrize('mask', [0o277, 0o022], ids=['umask-0277', 'umask-0022'])
def test_u1_touch_new_private_ends_exactly_0600_under_any_umask(tmp_path, mask):
    path = tmp_path / 'marker'
    with umask(mask):
        leaf.touch_new_private(path)
    assert mode(path) == 0o600, f'touch_new_private under umask {mask:#o} left {mode(path):#o}'


@pytest.mark.parametrize('mask', [0o277, 0o022], ids=['umask-0277', 'umask-0022'])
def test_u1_write_new_json_ends_exactly_0600_under_any_umask(tmp_path, mask):
    path = tmp_path / 'record.json'
    with umask(mask):
        leaf.write_new_json(path, {'k': 1})
    assert mode(path) == 0o600, f'write_new_json under umask {mask:#o} left {mode(path):#o}'
    assert json.loads(path.read_text()) == {'k': 1}
