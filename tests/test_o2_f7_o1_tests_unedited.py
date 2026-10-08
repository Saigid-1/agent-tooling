"""O2 F7: O1 stands (docs/work/orders/O2-opencode-third-harness.md, R2 "O1 is not weakened", F7).

O1's tests must pass UNEDITED on the merged tree, including the real-binary suite
(tests/image/test_o1_opencode_launch_image.py) and the A1 integration file that runs on the same image.
This guard holds every file O1 added for its tests byte-identical to the order's base (sha256), so a green
run of those files on the merged tree is the unedited run F7 names. The runs themselves are the suites:
`pytest tests/image/test_o1_opencode_launch_image.py -m image` with AGENT_TOOLING_TEST_IMAGE_OPENCODE set,
and vitest (test/runtime/terminal/o1-*.test.ts; test/integration/o1 with the same variable).

Guard: GREEN at base. Files O1 only extended (agent-session-adapters.test.ts, agent-registry.test.ts,
safe-launch.test.ts) are not held here: the order lets FEATURE extend the adapter's tests.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
O1_UNEDITED = {
    'tests/image/test_o1_opencode_launch_image.py': '594e9a09828f65b995078a810288f2a551d053aecd8f7ecfdb11e54e5eb40e01',
    'tests/image/o1_seams.py': 'dfd0973f0d8f8fc7293fee47a61820279e5c58dd9fd12d846422b0e3fea00925',
    'apps/kanban/test/runtime/terminal/o1-opencode-image-runner.ts':
        '47786c1d17406770f9322891ee796a366fc3a8b4519707fb8316995383957fb3',
    'apps/kanban/test/runtime/terminal/o1-opencode-launch-contract.test.ts':
        '187b99f4407f4a77f235165f9af16c674c10a94aa3bd58e2da5a89314beff620',
    'apps/kanban/test/runtime/terminal/o1-opencode-launch.test.ts':
        'c6d48126d53b665d4b0d0903fa8740496867b5ba442500635ede3cfa0aaa6d89',
    'apps/kanban/test/runtime/terminal/o1-opencode-launched-process.test.ts':
        'd3fcd348ef61c17368e15f794c51c0687f7253ee78e9db1db99928117a388be6',
    'apps/kanban/test/runtime/terminal/o1-seams.ts': '6b12a96e63fcda648b2215cc86c43d1b053f123dc59d5a76f01e615889312683',
    'apps/kanban/test/integration/o1/o1-a1-effective-policy.integration.test.ts':
        '1e8e4283f39f8b66295fca0875604c89ac94c06cc651c6d2a7f3865a62977e6e',
    'apps/kanban/test/integration/o1/fixtures/hook-recorder.mjs':
        '27c975e4cac00f26448c8188832b2f0c210c7a5871cc1060b1280739f8b057ff',
    'apps/kanban/test/integration/o1/fixtures/run-in-image.mjs':
        '75909e10eafe8bbf77c96e4e73e330048c3aedacd1cbc9410bda54a7b10080cb',
    'apps/kanban/test/integration/o1/fixtures/stub-provider.mjs':
        '4a03e2c75a638ed67a00ea970367584916585a20e69d893e9692b2cc02c72ddd',
}


@pytest.mark.parametrize('name', sorted(O1_UNEDITED))
def test_f7_o1_test_file_is_unedited(name):
    """Guard (GREEN at base): the O1 test file is byte-identical to the order's base."""
    path = REPO_ROOT / name
    assert path.is_file(), f'{name} is missing'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == O1_UNEDITED[name], f'{name} was edited'
