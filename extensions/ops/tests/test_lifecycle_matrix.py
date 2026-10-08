"""Contract guards plus optional pinned cross-repository ASGI execution."""
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
from kp_agent_tooling_ops._impl.lifecycle_matrix import model, pinned_checkout, route_source, render_markdown
from kp_agent_tooling_ops._impl.behavior_model import validate


def test_authored_scenarios_bind_all_profiles():
    groups = validate(model())
    assert len(groups['scenarios']) == 14
    assert set(groups['profiles']) == {'baseline', 'eager-negative-control', 'ordered-pooled-candidate'}


def test_transformation_refuses_changed_resource_shape():
    with pytest.raises(ValueError, match='unsupported source shape'):
        route_source('async def graph_query():\n    return None\n', 'ordered-pooled-candidate')


def test_source_boundary_refuses_wrong_revision_and_untracked_file(tmp_path):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path/'entry.py').write_text('pass\n')
    subprocess.run(['git', '-C', str(tmp_path), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=Test', '-c', 'user.email=test@example.test',
                    'commit', '-qm', 'fixture'], check=True)
    revision = subprocess.check_output(['git', '-C', str(tmp_path), 'rev-parse', 'HEAD'], text=True).strip()
    assert pinned_checkout(tmp_path, revision, 'entry.py')[1] == 'pass\n'
    with pytest.raises(ValueError, match='HEAD'):
        pinned_checkout(tmp_path, '0'*40, 'entry.py')
    (tmp_path/'extra.py').write_text('pass\n')
    with pytest.raises(ValueError, match='clean source'):
        pinned_checkout(tmp_path, revision, 'entry.py')


@pytest.mark.skipif(not os.environ.get('OPS_LIFECYCLE_ATS'), reason='explicit isolated source checkouts required')
def test_crossrepo_matrix_negative_control_and_trace_join(tmp_path):
    output = tmp_path/'receipt.json'
    subprocess.run([sys.executable, 'scripts/lifecycle_matrix_cli.py',
      '--ats', os.environ['OPS_LIFECYCLE_ATS'], '--ats-revision', os.environ['OPS_LIFECYCLE_ATS_REVISION'],
      '--core', os.environ['OPS_LIFECYCLE_CORE'], '--core-revision', os.environ['OPS_LIFECYCLE_CORE_REVISION'],
      '--output', str(output)], check=True, timeout=90)
    receipt = json.loads(output.read_text())
    assert len(receipt['rows']) == 42
    failures = {(r['profile_id'], r['scenario_id']) for r in receipt['rows'] if r['status'] == 'failed'}
    assert failures == {('eager-negative-control', s) for s in
                        ('duplicate', 'duplicate-outage', 'missing-query', 'release-failure')}
    roots = {s['context']['trace_id'].removeprefix('0x'): s for s in receipt['telemetry']['spans']
             if s['name'] == 'lifecycle.request'}
    for row in receipt['rows']:
        root = roots[row['trace_id']]
        assert root['attributes']['lifecycle.scenario'] == row['scenario_id']
        assert root['attributes']['lifecycle.model_digest'] == row['model_digest']
    ordered = {r['scenario_id']: r for r in receipt['rows'] if r['profile_id'] == 'ordered-pooled-candidate'}
    assert ordered['duplicate-outage']['resource']['checkouts'] == 0
    assert ordered['executor-release-failure']['resource']['unresolved_leases'] == 1
    assert len(ordered['executor-release-failure']['exception_chain']) == 2

    assert 'ordered-pooled-candidate' in render_markdown(receipt)
    receipt['rows'][0]['scenario_digest'] = 'tampered'
    with pytest.raises(ValueError, match='incompatible'):
        render_markdown(receipt)


def test_candidate_module_preserves_concurrent_imports():
    import types
    from kp_agent_tooling_ops._impl.lifecycle_matrix import candidate_module
    candidate = types.ModuleType('_ops_lifecycle_candidate')
    exporter_import = types.ModuleType('_ops_exporter_test_import')
    try:
        with candidate_module(candidate):
            sys.modules[exporter_import.__name__] = exporter_import
        assert sys.modules[exporter_import.__name__] is exporter_import
        assert candidate.__name__ not in sys.modules
    finally:
        sys.modules.pop(exporter_import.__name__, None)
