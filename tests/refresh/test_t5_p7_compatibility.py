"""T5 P7: a request with none of the new fields keeps the published schema and the
rebuild decisions of the base (apart from P1, P2 and P3).

Falsifier: a schema or behaviour regression outside P1, P2 and P3.
"""
from t5_rig import (BASE_CATALOG_KEYS, BASE_CATALOG_REPOSITORY_KEYS, BASE_PROFILE_KEYS, PYPROJECT,
                    core_and_ops, dependency_artifacts)


def studio_repository(rig):
    rig.add_repo('ats', {'pyproject.toml': PYPROJECT, 'product/mod.py': 'def hello(): return 0\n',
                         'studio/package.json': '{"name": "studio"}\n',
                         'studio/package-lock.json': '{"lockfileVersion": 3}\n',
                         'studio/index.ts': 'export const x = 1;\n'},
                 anchor='product/mod.py', scope=['product'], typescript_prefix='studio',
                 analysis_dependencies=['core'])
    rig.request['platforms'] = {'ats': {'owner': 'OPS', 'profile': 'dev-current',
                                        'repositories': ['ats', 'core']}}


def test_p7_request_without_new_fields_keeps_published_schema_and_defaults(rig):
    core_and_ops(rig)
    studio_repository(rig)
    result = rig.refresh()
    assert result['status'] == 'published'
    profile = rig.published()
    assert BASE_PROFILE_KEYS <= set(profile), sorted(BASE_PROFILE_KEYS - set(profile))
    assert (profile['schema_version'], profile['profile']) == ('ops.navigation-profile.v1', 'dev-current')
    assert all({'scip', 'semantic'} <= set(row) for row in profile['repository_readiness'].values())
    catalog = rig.catalog()
    assert BASE_CATALOG_KEYS <= set(catalog) and catalog['schema_version'] == 'ops.knowledge-config.v1'
    for row in catalog['repositories'].values():
        assert BASE_CATALOG_REPOSITORY_KEYS <= set(row)
        assert row['tenant_ids'] == ['platform-trial']
    assert {row['path'] for row in dependency_artifacts(rig, 'ats')} == {
        'pyproject.toml', 'studio/package.json', 'studio/package-lock.json'}
    assert {row['path'] for row in dependency_artifacts(rig, 'core')} == {'pyproject.toml'}
    assert sorted((run['key'], run['language'], run['timeout']) for run in rig.index_runs) == [
        ('ats', 'python', 300), ('ats', 'typescript', 300), ('core', 'python', 300), ('ops', 'python', 300)]
    assert rig.status()['status'] == 'published'


def test_p7_request_without_new_fields_keeps_rebuild_decisions(rig):
    core_and_ops(rig)  # ops declares analysis_dependencies ["core"]

    def since(mark):
        return sorted((run['key'], run['language']) for run in rig.index_runs[mark:])

    assert rig.refresh()['status'] == 'published'
    assert sorted(rig.clones) == ['core', 'ops'] and since(0) == [('core', 'python'), ('ops', 'python')]

    clones, runs = len(rig.clones), len(rig.index_runs)
    assert rig.refresh()['status'] == 'current'
    assert (len(rig.clones), len(rig.index_runs)) == (clones, runs)

    rig.bump('ops')  # an own change rebuilds only that repository, immediately
    assert rig.refresh()['status'] == 'published'
    assert 'core' not in rig.clones[clones:] and since(runs) == [('ops', 'python')]

    runs = len(rig.index_runs)
    rig.bump('core')  # SCIP still follows analysis dependency pins
    assert rig.refresh()['status'] == 'published'
    assert since(runs) == [('core', 'python'), ('ops', 'python')]
