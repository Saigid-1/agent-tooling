"""T5 P5: scip-python runs with the repository's python_index_timeout_seconds.

Falsifier: the configured value is ignored.
"""
import pytest

from t5_rig import core_and_ops


@pytest.mark.parametrize('seconds', [60, 1234, 3600])
def test_p5_scip_python_runs_with_the_repository_timeout(rig, seconds):
    core_and_ops(rig)
    rig.request['repositories']['ops']['python_index_timeout_seconds'] = seconds
    assert rig.refresh()['status'] == 'published'
    timeouts = {(run['key'], run['language']): run['timeout'] for run in rig.index_runs}
    assert timeouts == {('core', 'python'): 300, ('ops', 'python'): seconds}, timeouts
