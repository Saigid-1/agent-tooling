import subprocess

from kp_agent_tooling._impl.repository_manifest import build_manifest


def _git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root).decode().strip()


def _fixture(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    _git(root, 'init', '-q')
    _git(root, 'config', 'user.email', 'test@example.invalid')
    _git(root, 'config', 'user.name', 'Test')
    (root / 'README.md').write_text('committed\n')
    (root / 'docs').mkdir()
    (root / 'docs' / 'intent.md').write_text('intent\n')
    (root / 'config').mkdir()
    (root / 'config' / 'settings.yaml').write_text('key: value\n')
    (root / 'vendor').mkdir()
    (root / 'vendor' / 'library.py').write_text('x = 1\n')
    (root / 'generated').mkdir()
    (root / 'generated' / 'api.ts').write_text('export {}\n')
    (root / 'image.png').write_bytes(b'\x89PNG\x00')
    (root / 'pointer').symlink_to('docs/intent.md')
    _git(root, 'add', '.')
    _git(root, 'commit', '-qm', 'first')
    revision = _git(root, 'rev-parse', 'HEAD')
    config = {'repos': {'fixture': {'path': str(root), 'revision': revision}}}
    return root, config, revision


def test_manifest_covers_exact_committed_tree_with_explicit_exclusions(tmp_path):
    root, config, revision = _fixture(tmp_path)
    (root / 'README.md').write_text('dirty\n')
    (root / 'untracked.py').write_text('outside committed tree\n')
    result = build_manifest(config, 'fixture', revision)
    assert result['status'] == 'ok'
    assert result['complete'] is True
    entries = {row['path']: row for row in result['entries']}
    assert set(entries) == {'README.md', 'docs/intent.md', 'config/settings.yaml',
                            'vendor/library.py', 'generated/api.ts', 'image.png', 'pointer'}
    assert entries['README.md']['blob_sha'] == _git(root, 'rev-parse', revision + ':README.md')
    assert entries['docs/intent.md']['classification']['kind'] == 'documentation'
    assert entries['config/settings.yaml']['classification']['kind'] == 'configuration'
    assert entries['vendor/library.py']['classification']['kind'] == 'vendor'
    assert entries['generated/api.ts']['classification']['kind'] == 'generated'
    assert entries['image.png']['text_eligible'] is False
    assert entries['image.png']['exclusion_reason'] == 'binary_extension'
    assert entries['pointer']['mode'] == '120000'
    assert entries['pointer']['exclusion_reason'] == 'symlink'
    assert all(row['classification']['basis'] for row in entries.values())
    assert build_manifest(config, 'fixture', revision) == result


def test_artifact_id_tracks_path_while_manifest_id_tracks_revision(tmp_path):
    root, config, first = _fixture(tmp_path)
    (root / 'README.md').write_text('second\n')
    _git(root, 'add', 'README.md')
    _git(root, 'commit', '-qm', 'second')
    second = _git(root, 'rev-parse', 'HEAD')
    before = build_manifest(config, 'fixture', first)
    after = build_manifest(config, 'fixture', second)
    old = next(row for row in before['entries'] if row['path'] == 'README.md')
    new = next(row for row in after['entries'] if row['path'] == 'README.md')
    assert old['artifact_id'] == new['artifact_id']
    assert old['artifact_revision_id'] != new['artifact_revision_id']
    assert old['blob_sha'] != new['blob_sha']
    assert before['manifest_id'] != after['manifest_id']
    unchanged_old = next(row for row in before['entries'] if row['path'] == 'docs/intent.md')
    unchanged_new = next(row for row in after['entries'] if row['path'] == 'docs/intent.md')
    assert unchanged_old['artifact_revision_id'] == unchanged_new['artifact_revision_id']


def test_entry_budget_never_returns_partial_coverage(tmp_path):
    _, config, revision = _fixture(tmp_path)
    result = build_manifest(config, 'fixture', revision, entry_limit=2)
    assert result['status'] == 'incomplete'
    assert result['entries'] == []
    assert result['reason'] == 'entry_budget_exceeded'
    assert result['absence_verdict'] == 'not-established'


def test_gitlink_is_inventory_entry_with_explicit_exclusion(tmp_path):
    root, config, first = _fixture(tmp_path)
    _git(root, 'update-index', '--add', '--cacheinfo', '160000,' + first + ',linked')
    _git(root, 'commit', '-qm', 'gitlink')
    revision = _git(root, 'rev-parse', 'HEAD')
    result = build_manifest(config, 'fixture', revision)
    assert result['status'] == 'ok'
    linked = next(row for row in result['entries'] if row['path'] == 'linked')
    assert linked['mode'] == '160000'
    assert linked['blob_sha'] == first
    assert linked['size'] is None
    assert linked['classification'] == {'kind': 'gitlink', 'basis': 'git-tree-mode'}
    assert linked['text_eligible'] is False
    assert linked['exclusion_reason'] == 'gitlink'


def test_inventory_byte_budget_never_returns_partial_entries(tmp_path):
    root, config, _ = _fixture(tmp_path)
    for number in range(100):
        (root / f'extra-{number:03}.txt').write_text('a\n')
    _git(root, 'add', '.')
    _git(root, 'commit', '-qm', 'many entries')
    revision = _git(root, 'rev-parse', 'HEAD')
    result = build_manifest(config, 'fixture', revision, inventory_byte_limit=1024)
    assert result['status'] == 'incomplete'
    assert result['entries'] == []
    assert result['budget']['name'] == 'output_bytes'
