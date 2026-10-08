"""T11a P1: content addresses do not move (docs/work/orders/T11a-leaf-consolidation.md).

The golden corpus (tests/fixtures/t11a/identity_corpus.json, and identity_corpus_ops.json for
the schemes that live in extensions/ops) was generated at base by
``python tests/fixtures/t11a/generate_identity_corpus.py`` (its docstring lists every scheme
and site). These tests recompute it in this tree through the same public or old names and
compare the texts byte for byte, on every CPython >= 3.11 (measured identical on 3.11, 3.12 and
3.14). The ops part skips when extensions/ops is not installed (the CI core job).

GREEN-IF: every identity scheme the order lists -- episode/claim ``_id``, ``binding_key``,
``canonical_digest`` (models, review_ledger), ``behavior_model.digest``, ``observations.digest``,
``finding_digest``, ``_sha256_identity``/``embedding_identity``, ``scip_navigation.digest``, the
``_digest`` helpers of desk_write_scope/episodic_queue/memory_budget/session_bindings,
``workspace_setup.digest``, ``refresh_cli.digest_json``, the ``claude_episode_capture`` hash
chain, both ``deskdoc:`` chunk-ID implementations, ``change_lineage_key``/``change_occurrence_id``,
``codex_trust_entry`` (timeout 5 and 30, matcher present and absent, both source forms), every
sorted_json digest site and ``scip_entry.py:41`` -- yields at head exactly what it yielded at
base, outputs and errors alike.

refresh_cli.py:460 hashes inputs that include refresh_cli.py's and scip_navigation.py's own
bytes, which T11a edits, so no golden can hold it; the second test pins its scheme instead
(an oracle: sort_keys-only JSON of the inputs, accepted by the reuse check).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from t11a_golden import assert_matches_golden, load_generator


def test_p1_identity_corpus_is_byte_identical_to_base():
    generator = load_generator('generate_identity_corpus')
    assert_matches_golden(generator.build('core'), generator.GOLDENS['core'])


def test_p1_ops_identity_corpus_is_byte_identical_to_base():
    pytest.importorskip('kp_agent_tooling_ops', reason='the ops schemes need extensions/ops installed')
    generator = load_generator('generate_identity_corpus')
    assert_matches_golden(generator.build('ops'), generator.GOLDENS['ops'])


def _refresh_request(tmp_path):
    toolchain = tmp_path / 'toolchain'
    toolchain.mkdir()
    (toolchain / 'package-lock.json').write_bytes('{"lockfileVersion": 3, "note": "é"}'.encode())
    return {'repositories': {}, 'publication': str(tmp_path / 'profile.json'),
            'output_root': str(tmp_path / 'out'), 'toolchain': str(toolchain), 'tenant_id': 'ténant ☃',
            'check_status': str(tmp_path / 'status.json'), 'node': '/usr/bin/node',
            'x-t11a': {'nested': [1.5, None, 'ü'], 'empty': {}}}


def _inputs(request):
    from kp_agent_tooling import refresh_cli
    here = Path(refresh_cli.__file__)
    return {'request': request, 'script': hashlib.sha256(here.read_bytes()).hexdigest(),
            'index_builder': hashlib.sha256((here.parent / '_impl/scip_navigation.py').read_bytes()).hexdigest(),
            'toolchain': hashlib.sha256((Path(request['toolchain']) / 'package-lock.json').read_bytes()).hexdigest(),
            'templates': {}, 'tenant_id': request['tenant_id']}


def test_p1_refresh_request_sha256_is_the_sorted_json_digest_of_its_inputs(tmp_path):
    from kp_agent_tooling import refresh_cli
    request = _refresh_request(tmp_path)
    inputs = _inputs(request)
    sorted_json = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    compact = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert sorted_json != compact
    Path(request['publication']).write_text(json.dumps({'request_sha256': sorted_json}))
    result = refresh_cli.refresh(request)
    assert result['status'] == 'current', (
        'the reuse check refused the sort_keys-only digest of the request inputs: '
        f'refresh_cli.py:460 no longer hashes json.dumps(inputs, sort_keys=True): {result}')
