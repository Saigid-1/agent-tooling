"""T5 interface surface: the new optional refresh request fields and their domains.

Out-of-domain values are refused as ValueError before any clone or publication
(the convention of semantic_deadline_seconds); the bounds themselves are accepted.
"""
from pathlib import Path

import pytest

from t5_rig import web

INVALID = [
    ('request', 'min_rebuild_interval_seconds', -1),
    ('request', 'min_rebuild_interval_seconds', 86401),
    ('request', 'min_rebuild_interval_seconds', 1.5),
    ('request', 'min_rebuild_interval_seconds', '60'),
    ('request', 'retain_generations', 0),
    ('request', 'retain_generations', 51),
    ('request', 'retain_generations', True),
    ('request', 'retain_generations', '3'),
    ('request', 'semantic_retry_after_seconds', 299),
    ('request', 'semantic_retry_after_seconds', 604801),
    ('request', 'semantic_retry_after_seconds', 86400.0),
    ('request', 'tenant_id', 7),
    ('request', 'tenant_id', ['platform-trial']),
    ('repository', 'python_index_timeout_seconds', 59),
    ('repository', 'python_index_timeout_seconds', 3601),
    ('repository', 'python_index_timeout_seconds', True),
    ('repository', 'python_index_timeout_seconds', 300.0),
    ('repository', 'python_index_timeout_seconds', '300'),
    ('repository', 'typescript_package_files', 'web/package.json'),
    ('repository', 'typescript_package_files', ['/abs/package.json']),
    ('repository', 'typescript_package_files', [7]),
]


@pytest.mark.parametrize('level,field,value', INVALID, ids=[f'{f}={v!r}' for _, f, v in INVALID])
def test_out_of_domain_field_is_refused_before_clone_or_publication(rig, level, field, value):
    web(rig)
    (rig.request if level == 'request' else rig.request['repositories']['web'])[field] = value
    with pytest.raises(ValueError):
        rig.refresh()
    assert rig.clones == [] and rig.index_runs == []
    assert not Path(rig.request['publication']).exists()


BOUNDS = {
    'lower': ({'min_rebuild_interval_seconds': 0, 'retain_generations': 1,
               'semantic_retry_after_seconds': 300, 'tenant_id': 'lower'},
              {'python_index_timeout_seconds': 60, 'typescript_package_files': ['web/package.json']}),
    'upper': ({'min_rebuild_interval_seconds': 86400, 'retain_generations': 50,
               'semantic_retry_after_seconds': 604800, 'tenant_id': 'upper'},
              {'python_index_timeout_seconds': 3600,
               'typescript_package_files': ['web/package.json', 'web/package-lock.json']}),
}


@pytest.mark.parametrize('bound', sorted(BOUNDS))
def test_field_bounds_are_accepted(rig, bound):
    request, repository = BOUNDS[bound]
    web(rig, **repository)
    rig.request.update(request)
    assert rig.refresh()['status'] == 'published'
