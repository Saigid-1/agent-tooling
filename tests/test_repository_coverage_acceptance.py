"""Acceptance boundaries for the committed repository inventory and paged search."""

import json
import subprocess

import pytest

from kp_agent_tooling._impl.repository_manifest import build_manifest
from kp_agent_tooling._impl.navigation_search_pages import search_pages


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'config', 'user.name', 'Test')
    git(root, 'config', 'user.email', 'test@example.invalid')
    (root / 'empty.txt').write_bytes(b'')
    (root / 'binary.bin').write_bytes(b'needle\0binary')
    (root / 'large.txt').write_text('x' * 8_100_000)
    (root / 'unicode.txt').write_text('caf\u00e9 needle\n', encoding='utf-8')
    (root / 'repeated.txt').write_text('needle\n' * 7)
    (root / 'linked.txt').symlink_to('repeated.txt')
    git(root, 'add', '.')
    git(root, 'commit', '-qm', 'original')
    gitlink_target = git(root, 'rev-parse', 'HEAD')
    git(root, 'update-index', '--add', '--cacheinfo', '160000', gitlink_target, 'subproject')
    git(root, 'commit', '-qm', 'tracked gitlink')
    revision = git(root, 'rev-parse', 'HEAD')
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({
        'schema_version': 'ops.navigation-profile.v1',
        'profile': 'dev-current',
        'repos': {'repo': {'path': str(root), 'revision': revision}},
        'published_at': 'fixture',
    }))
    config = {'repos': {'repo': {'path': str(root), 'revision': revision}},
              'navigation_profile': str(profile)}
    registry = tmp_path / 'search-registry'
    registry.mkdir()
    return root, profile, config, registry, revision


def test_manifest_inventories_all_tracked_entry_types_at_historical_revision(repository):
    root, profile, config, registry, revision = repository
    before = build_manifest(config, 'repo', revision)
    assert before['status'] == 'ok'
    entries = {row['path']: row for row in before['entries']}
    assert set(entries) == {'empty.txt', 'binary.bin', 'large.txt',
                            'unicode.txt', 'repeated.txt', 'linked.txt', 'subproject'}
    assert entries['empty.txt']['size'] == 0
    assert entries['empty.txt']['text_eligible']
    assert not entries['binary.bin']['text_eligible']
    assert entries['binary.bin']['exclusion_reason']
    assert not entries['large.txt']['text_eligible']
    assert entries['large.txt']['exclusion_reason']
    assert entries['linked.txt']['mode'] == '120000'
    assert not entries['linked.txt']['text_eligible']
    assert entries['subproject']['mode'] == '160000'
    assert entries['subproject']['size'] is None
    assert not entries['subproject']['text_eligible']
    assert all(row['blob_sha'] and row['artifact_id'] for row in entries.values())

    (root / 'untracked.txt').write_text('needle')
    (root / 'repeated.txt').write_text('dirty needle')
    profile_data = json.loads(profile.read_text())
    (root / 'later.txt').write_text('later')
    git(root, 'add', 'later.txt')
    git(root, 'commit', '-qm', 'later')
    profile_data['repos']['repo']['revision'] = git(root, 'rev-parse', 'HEAD')
    profile.write_text(json.dumps(profile_data))
    after = build_manifest(config, 'repo', revision)
    assert after['manifest_id'] == before['manifest_id']
    assert after['entries'] == before['entries']


def test_paged_search_resumes_inside_one_file_without_early_completion(repository):
    root, profile, config, registry, revision = repository
    token = None
    lines = []
    for page_number in range(10):
        page = search_pages(config, registry, 'repo', revision, 'needle',
                            path_pattern='repeated.txt', limit=2,
                            continuation_token=token)
        lines.extend(hit['line'] for hit in page['results'])
        if page_number == 0:
            assert page['search_complete'] is False
            assert page['continuation_token']
        token = page['continuation_token']
        if token is None:
            assert page['search_complete'] is True
            break
    else:
        pytest.fail('search never reached its final page')
    assert lines == list(range(1, 8))
    assert len(lines) == len(set(lines))


def test_search_token_is_bound_to_query_revision_and_integrity(repository):
    root, profile, config, registry, revision = repository
    first = search_pages(config, registry, 'repo', revision, 'needle',
                         path_pattern='repeated.txt', limit=1)
    token = first['continuation_token']
    assert token
    with pytest.raises(ValueError):
        search_pages(config, registry, 'repo', revision, 'different',
                     path_pattern='repeated.txt', limit=1,
                     continuation_token=token)
    with pytest.raises(ValueError):
        search_pages(config, registry, 'repo', revision, 'needle',
                     path_pattern='unicode.txt', limit=1,
                     continuation_token=token)
    with pytest.raises(ValueError):
        search_pages(config, registry, 'repo', revision, 'needle',
                     path_pattern='repeated.txt', limit=1,
                     continuation_token=token[:-1] + ('a' if token[-1] != 'a' else 'b'))

    (root / 'later.txt').write_text('needle')
    git(root, 'add', 'later.txt')
    git(root, 'commit', '-qm', 'later')
    later = git(root, 'rev-parse', 'HEAD')
    with pytest.raises(ValueError):
        search_pages(config, registry, 'repo', later, 'needle',
                     path_pattern='repeated.txt', limit=1,
                     continuation_token=token)


def test_no_matches_reports_exclusions_separately(repository):
    root, profile, config, registry, revision = repository
    page = search_pages(config, registry, 'repo', revision, 'absent', limit=2)
    while page['continuation_token']:
        page = search_pages(config, registry, 'repo', revision, 'absent', limit=2,
                            continuation_token=page['continuation_token'])
    assert page['traversal_complete']
    assert not page['search_complete']
    assert page['results'] == []
    assert page['coverage']['excluded_files'] >= 2
    assert page['coverage']['searched_files'] > 0
    assert page['absence_verdict'] == 'not-established'


def test_historical_cursor_ignores_dirty_tree_and_advanced_profile(repository):
    root, profile, config, registry, revision = repository
    first = search_pages(config, registry, 'repo', revision, 'needle',
                         path_pattern='repeated.txt', limit=2)
    token = first['continuation_token']
    (root / 'repeated.txt').write_text('dirty replacement\n')
    (root / 'later.txt').write_text('needle\n')
    git(root, 'add', 'later.txt')
    git(root, 'commit', '-qm', 'later')
    model = json.loads(profile.read_text())
    model['repos']['repo']['revision'] = git(root, 'rev-parse', 'HEAD')
    profile.write_text(json.dumps(model))
    second = search_pages(config, registry, 'repo', revision, 'needle',
                          path_pattern='repeated.txt', limit=2,
                          continuation_token=token)
    assert [hit['line'] for hit in second['results']] == [3, 4]
    assert second['source_revision'] == revision
    assert second['manifest_id'] == first['manifest_id']


def test_unicode_hits_and_mislabeled_binary_are_distinct(repository):
    root, profile, config, registry, revision = repository
    (root / 'mislabeled.txt').write_bytes(b'needle\0not text\n')
    git(root, 'add', 'mislabeled.txt')
    git(root, 'commit', '-qm', 'mislabeled binary')
    revision = git(root, 'rev-parse', 'HEAD')
    unicode_page = search_pages(config, registry, 'repo', revision, 'caf\u00e9',
                                path_pattern='unicode.txt')
    assert unicode_page['search_complete']
    assert unicode_page['results'][0]['excerpt'] == 'caf\u00e9 needle'
    binary_page = search_pages(config, registry, 'repo', revision, 'needle',
                               path_pattern='mislabeled.txt')
    assert binary_page['traversal_complete']
    assert not binary_page['search_complete']
    assert binary_page['results'] == []
    assert binary_page['coverage']['excluded_files'] == 1
    assert binary_page['absence_verdict'] == 'not-established'
