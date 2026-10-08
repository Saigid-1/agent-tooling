"""D0a-3's named exceptions hold: the example catalog and the T8 golden carry the derived values.

See tests/d0a3_ruled_exceptions.py. A binding key must be the product's own derivation of
the example's coordinates, and the T8 golden must be the bytes its generator wrote.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import d0a3_ruled_exceptions as ruled  # noqa: E402
from kp_agent_tooling._impl.service.desk_identity import binding_key  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CATALOGS = ('config/desk-context/catalog.example.json',
            'packages/tooling/src/kp_agent_tooling/assets/desk-context/catalog.example.json')


def test_example_catalogs_carry_the_derived_coordinates_and_binding_keys():
    for name in CATALOGS:
        catalog = json.loads((ROOT / name).read_text())
        project = catalog['project']
        assert (project['tenant_id'], project['repo_key'], project['source_revision']) == (
            ruled.EXAMPLE_TENANT, ruled.EXAMPLE_REPO, ruled.EXAMPLE_REVISION), name
        for desk in catalog['desks']:
            derived = binding_key(tenant_id=project['tenant_id'], role=desk['role_id'], repo_key=project['repo_key'])
            assert desk['binding_id'] == desk['memory_binding_id'] == derived == ruled.NEW_BINDINGS[desk['role_id']]
    memory = json.loads((ROOT / 'config/desk-context/memory.example.json').read_text())
    assert set(memory['bindings']) == set(ruled.NEW_BINDINGS.values())


def test_t8_golden_is_the_generated_bytes_at_the_new_coordinates():
    raw = (ROOT / ruled.T8_GOLDEN).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ruled.T8_GOLDEN_SHA256
    golden = json.loads(raw)
    assert golden['capsule_row']['binding'] == ruled.T8_NEW_IDS['binding']
    assert golden['episode_id'] == ruled.T8_NEW_IDS['episode_id']
    assert golden['capsule_row']['id'] == ruled.T8_NEW_IDS['capsule_id']


def test_t11a_identity_corpus_records_the_new_example_binding():
    # Not pinned by sha256: later slices regenerate the T11a goldens at their own bases (T12b).
    # tests/test_t11a_p1_identity_corpus.py already checks the golden equals its generator's output.
    raw = (ROOT / ruled.T11A_CORPUS).read_bytes()
    assert ruled.NEW_BINDINGS['implementation'].encode() in raw
