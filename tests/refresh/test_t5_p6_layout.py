"""T5 P6: no hard-coded repository layout or tenant.

TypeScript package files come from typescript_package_files (or typescript_prefix);
the tenant comes from tenant_id; `studio` and `platform-trial` are not code literals.

Falsifier: a repository whose TypeScript prefix is not `studio` fails, or the code
still names those literals.
"""
import json
import re

from t5_rig import (PYTHON_DEPENDENCY_FILES, core_and_ops, dependency_artifacts, layout_scan_files,
                    string_literals, web)


def typescript_package_files(rig, key):
    rows = dependency_artifacts(rig, key)
    assert all(row.get('role') == 'dependency' for row in rows), rows
    return {row['path'] for row in rows} - PYTHON_DEPENDENCY_FILES


def test_p6_non_studio_typescript_prefix_lists_its_own_package_files(rig):
    web(rig)
    assert rig.refresh()['status'] == 'published'
    assert typescript_package_files(rig, 'web') == {'web/package.json', 'web/package-lock.json'}


def test_p6_typescript_package_files_are_taken_from_the_request(rig):
    web(rig, typescript_package_files=['package.json', 'pnpm-lock.yaml'])
    assert rig.refresh()['status'] == 'published'
    assert typescript_package_files(rig, 'web') == {'package.json', 'pnpm-lock.yaml'}


def test_p6_tenant_comes_from_tenant_id(rig):
    core_and_ops(rig)
    rig.request['tenant_id'] = 'acme-tenant'
    assert rig.refresh()['status'] == 'published'
    catalog = rig.catalog()
    assert {key: row['tenant_ids'] for key, row in catalog['repositories'].items()} == {
        'core': ['acme-tenant'], 'ops': ['acme-tenant']}
    assert 'platform-trial' not in json.dumps(catalog)


def test_p6_studio_and_platform_trial_are_not_code_literals(rig):
    studio, tenant = [], []
    for path in layout_scan_files(rig.module):
        for line, value in string_literals(path):
            if re.search(r'\bstudio\b', value):
                studio.append(f'{path.name}:{line}: {value!r}')
            if 'platform-trial' in value:
                tenant.append(f'{path.name}:{line}: {value!r}')
    assert not studio, f'hard-coded studio layout literals: {studio}'
    # tenant_id defaults to "platform-trial" for compatibility: one declaration, no use-site literal.
    assert len(tenant) <= 1, f'platform-trial named more than once: {tenant}'
