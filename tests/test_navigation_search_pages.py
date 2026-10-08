import json
import subprocess

import pytest

from kp_agent_tooling._impl.navigation_search_pages import SearchPageError, search_pages
import kp_agent_tooling._impl.navigation_search_pages as search_module


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    git(repo, 'config', 'user.name', 'Test')
    git(repo, 'config', 'user.email', 'test@example.invalid')
    (repo / 'hits.txt').write_text('needle\n' * 7)
    (repo / 'other.txt').write_text('other\n')
    (repo / 'binary.bin').write_bytes(b'needle\0')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'initial')
    revision = git(repo, 'rev-parse', 'HEAD')
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'schema_version': 'ops.navigation-profile.v1',
                                   'profile': 'fixture',
                                   'repos': {'repo': {'path': str(repo), 'revision': revision}}}))
    config = {'repos': {'repo': {'path': str(repo), 'revision': revision}},
              'navigation_profile': str(profile)}
    registry = tmp_path / 'registry'
    registry.mkdir()
    return repo, config, registry, revision


def test_pages_preserve_every_match_and_restart(source):
    repo, config, registry, revision = source
    token = None
    lines = []
    for _ in range(8):
        page = search_pages(config, registry, 'repo', revision, 'needle',
                            path_pattern='*.txt', limit=2, continuation_token=token)
        lines.extend(hit['line'] for hit in page['results'])
        if page['traversal_complete']:
            assert page['search_complete']
            assert page['coverage']['total_matches'] == 7
            assert page['continuation_token'] is None
            break
        token = page['continuation_token']
        assert token.startswith('navigation-search:sha256:')
    assert lines == list(range(1, 8))
    assert page['coverage']['selected_files'] == 2
    assert all(hit['artifact_id'] and hit['artifact_revision_id'] and hit['manifest_id']
               for hit in search_pages(config, registry, 'repo', revision, 'needle',
                                       path_pattern='hits.txt', limit=2)['results'])
    (repo / 'hits.txt').write_text('dirty text\n')
    assert search_pages(config, registry, 'repo', revision, 'needle',
                        path_pattern='hits.txt')['results'][0]['line'] == 1


def test_token_binds_request_and_detects_tamper(source):
    _, config, registry, revision = source
    first = search_pages(config, registry, 'repo', revision, 'needle', limit=2)
    token = first['continuation_token']
    with pytest.raises(SearchPageError, match='does not match'):
        search_pages(config, registry, 'repo', revision, 'other', limit=2,
                     continuation_token=token)
    with pytest.raises(SearchPageError, match='does not match'):
        search_pages(config, registry, 'repo', revision, 'needle', limit=3,
                     continuation_token=token)
    cursor_file = registry / ('search-' + token.split(':')[-1] + '.json')
    cursor_file.write_text('{}')
    with pytest.raises(SearchPageError, match='identity mismatch'):
        search_pages(config, registry, 'repo', revision, 'needle', limit=2,
                     continuation_token=token)


def test_scope_and_exclusions_do_not_assert_absence(source):
    _, config, registry, revision = source
    empty = search_pages(config, registry, 'repo', revision, 'absent', path_pattern='*.txt')
    assert empty['search_complete']
    assert empty['absence_verdict'] == 'no_matches_in_declared_scope'
    mixed = search_pages(config, registry, 'repo', revision, 'absent')
    assert mixed['traversal_complete']
    assert not mixed['search_complete']
    assert mixed['absence_verdict'] == 'not-established'
    assert mixed['coverage']['excluded_files'] == 1
    assert mixed['exclusions'][0]['path'] == 'binary.bin'


def test_large_file_resumes_after_line_budget(source, monkeypatch):
    repo, config, registry, _ = source
    monkeypatch.setattr(search_module, 'MAX_LINES', 1000)
    (repo / 'large.txt').write_text(('x' * 60 + '\n') * (search_module.MAX_LINES + 500) + 'late needle\n')
    git(repo, 'add', 'large.txt')
    git(repo, 'commit', '-qm', 'large')
    revision = git(repo, 'rev-parse', 'HEAD')
    first = search_pages(config, registry, 'repo', revision, 'needle', path_pattern='large.txt')
    assert not first['traversal_complete']
    assert first['page_usage']['lines'] == search_module.MAX_LINES
    second = search_pages(config, registry, 'repo', revision, 'needle', path_pattern='large.txt',
                          continuation_token=first['continuation_token'])
    assert second['traversal_complete']
    assert [hit['line'] for hit in second['results']] == [search_module.MAX_LINES + 501]


def test_no_progress_timeout_is_explicit(source, monkeypatch):
    _, config, registry, revision = source
    def timeout(*args, **kwargs):
        raise TimeoutError('forced')
    monkeypatch.setattr(search_module, '_blob', timeout)
    page = search_pages(config, registry, 'repo', revision, 'needle', path_pattern='hits.txt')
    assert page['status'] == 'unavailable'
    assert page['reason'] == 'operation_deadline_exceeded'
    assert page['retryable']
    assert not page['traversal_complete']


def test_operation_deadline_starts_before_manifest_and_is_forwarded(source, monkeypatch):
    _, config, registry, revision = source
    observed = {}

    def expired_manifest(*args, deadline=None, **kwargs):
        observed['remaining'] = deadline - search_module.time.monotonic()
        return {'status': 'incomplete', 'manifest_id': None,
                'reason': 'elapsed_time_budget_exceeded', 'entries': []}

    monkeypatch.setattr(search_module, 'build_manifest', expired_manifest)
    page = search_pages(config, registry, 'repo', revision, 'needle')
    assert 0 < observed['remaining'] <= search_module.MAX_OPERATION_SECONDS
    assert page['status'] == 'incomplete'
    assert page['reason'] == 'operation_deadline_exceeded'
    assert page['retryable'] is True
    assert page['operation_budget']['seconds'] == search_module.MAX_OPERATION_SECONDS


def test_inner_line_scan_checks_deadline_and_resumes_without_loss(source, monkeypatch):
    _, config, registry, revision = source
    from kp_agent_tooling._impl.repository_manifest import build_manifest
    manifest = build_manifest(config, 'repo', revision)
    monkeypatch.setattr(search_module, 'build_manifest', lambda *a, **kw: manifest)
    monkeypatch.setattr(search_module, '_blob', lambda *a, **kw: b'needle\n' * 7)
    ticks = iter([0.0, 0.0, 0.0, 0.0, 11.0, 11.0])
    monkeypatch.setattr(search_module.time, 'monotonic', lambda: next(ticks, 11.0))
    page = search_pages(config, registry, 'repo', revision, 'needle',
                        path_pattern='hits.txt', limit=100)
    assert page['reason'] == 'operation_deadline_exceeded'
    assert page['continuation_token']
    assert page['page_usage']['lines'] < 7
    monkeypatch.undo()
    resumed = search_pages(config, registry, 'repo', revision, 'needle',
                           path_pattern='hits.txt', limit=100,
                           continuation_token=page['continuation_token'])
    assert resumed['traversal_complete']
    assert [row['line'] for row in page['results'] + resumed['results']] == list(range(1, 8))


def test_expiration_cancels_before_next_blob_read(source, monkeypatch):
    _, config, registry, revision = source
    from kp_agent_tooling._impl.repository_manifest import build_manifest
    manifest = build_manifest(config, 'repo', revision)
    manifest['entries'] = [row for row in manifest['entries']
                           if row['path'] in {'hits.txt', 'other.txt'}]
    now = [0.0]
    reads = []
    monkeypatch.setattr(search_module, 'build_manifest', lambda *a, **kw: manifest)
    monkeypatch.setattr(search_module.time, 'monotonic', lambda: now[0])
    def one_read(*args, **kwargs):
        reads.append(args[1])
        now[0] = search_module.MAX_OPERATION_SECONDS + 1
        return b'needle\n'
    monkeypatch.setattr(search_module, '_blob', one_read)
    page = search_pages(config, registry, 'repo', revision, 'needle')
    assert len(reads) == 1
    assert page['reason'] == 'operation_deadline_exceeded'
    assert page['continuation_token']


def test_blob_timeout_after_progress_returns_resumable_deadline_reason(source, monkeypatch):
    _, config, registry, revision = source
    from kp_agent_tooling._impl.repository_manifest import build_manifest
    manifest = build_manifest(config, 'repo', revision)
    monkeypatch.setattr(search_module, 'build_manifest', lambda *a, **kw: manifest)
    calls = [0]
    def timeout_second(*args, **kwargs):
        calls[0] += 1
        if calls[0] == 2:
            raise TimeoutError('bounded read expired')
        return b'ordinary\n'
    monkeypatch.setattr(search_module, '_blob', timeout_second)
    page = search_pages(config, registry, 'repo', revision, 'needle',
                        path_pattern='*.txt')
    assert page['coverage']['searched_files'] == 1
    assert page['reason'] == 'operation_deadline_exceeded'
    assert page['continuation_token']


def test_manifest_git_deadline_is_retryable_operation_expiry(source, monkeypatch):
    _, config, registry, revision = source
    monkeypatch.setattr(search_module, 'build_manifest', lambda *a, **kw: {
        'status': 'incomplete', 'manifest_id': None,
        'reason': 'git_time_budget_exceeded', 'entries': []})
    page = search_pages(config, registry, 'repo', revision, 'needle')
    assert page['reason'] == 'operation_deadline_exceeded'
    assert page['retryable'] is True
    assert page['pages_remaining_estimate'] is None
    assert page['operation_elapsed_seconds'] >= 0


def test_resumed_cursor_survives_manifest_deadline_for_exact_retry(source, monkeypatch):
    _, config, registry, revision = source
    first = search_pages(config, registry, 'repo', revision, 'needle', limit=2)
    token = first['continuation_token']
    # The registry now serves a cached manifest; bypass it so the deadline path is exercised.
    monkeypatch.setattr(search_module, '_cached_manifest', lambda *a, **kw: None)
    monkeypatch.setattr(search_module, 'build_manifest', lambda *a, **kw: {
        'status': 'incomplete', 'manifest_id': None,
        'reason': 'elapsed_time_budget_exceeded', 'entries': []})
    interrupted = search_pages(config, registry, 'repo', revision, 'needle',
                               limit=2, continuation_token=token)
    assert interrupted['continuation_token'] == token
    assert interrupted['pages_remaining_estimate'] is None
    assert interrupted['absence_verdict'] == 'not-established'


def test_cursor_binds_search_policy_and_budget(source, monkeypatch):
    _, config, registry, revision = source
    page = search_pages(config, registry, 'repo', revision, 'needle',
                        path_pattern='hits.txt', limit=2)
    monkeypatch.setattr(search_module, 'MAX_LINES', 3999)
    with pytest.raises(SearchPageError, match='does not match'):
        search_pages(config, registry, 'repo', revision, 'needle',
                     path_pattern='hits.txt', limit=2,
                     continuation_token=page['continuation_token'])


def test_cursor_file_size_checked_before_read(source, monkeypatch):
    _, config, registry, revision = source
    page = search_pages(config, registry, 'repo', revision, 'needle',
                        path_pattern='hits.txt', limit=2)
    cursor = registry / ('search-' + page['continuation_token'].split(':')[-1] + '.json')
    cursor.write_bytes(b'x' * 65537)
    with pytest.raises(SearchPageError, match='byte budget'):
        search_pages(config, registry, 'repo', revision, 'needle',
                     path_pattern='hits.txt', limit=2,
                     continuation_token=page['continuation_token'])


def test_second_page_serves_manifest_from_registry_cache(source, monkeypatch):
    _, config, registry, revision = source
    first = search_pages(config, registry, 'repo', revision, 'needle', limit=2)
    assert first['manifest_source'] == 'built'
    calls = []
    real = search_module.build_manifest
    monkeypatch.setattr(search_module, 'build_manifest', lambda *a, **kw: calls.append(1) or real(*a, **kw))
    second = search_pages(config, registry, 'repo', revision, 'needle', limit=2,
                          continuation_token=first['continuation_token'])
    assert second['manifest_source'] == 'registry-hit'
    assert calls == []
    assert [row['line'] for row in first['results'] + second['results']] == list(range(1, 8))[:len(first['results']) + len(second['results'])]


def test_page_reads_every_blob_through_one_batch_process(source, monkeypatch):
    _, config, registry, revision = source
    spawns = []
    real_popen = search_module.subprocess.Popen
    def counting_popen(args, *a, **kw):
        spawns.append(args)
        return real_popen(args, *a, **kw)
    import kp_agent_tooling._impl.git_batch as batch_module
    monkeypatch.setattr(batch_module.subprocess, 'Popen', counting_popen)
    page = search_pages(config, registry, 'repo', revision, 'needle')
    # subprocess.run() calls Popen, so a per-file `cat-file blob <sha>` would be counted here.
    assert not any(list(args[:4]) == ['git', '--no-optional-locks', 'cat-file', 'blob'] for args in spawns)
    assert page['traversal_complete']
    assert [row['line'] for row in page['results']] == list(range(1, 8))
    assert [row['path'] for row in page['results']] == ['hits.txt'] * 7
    assert sum(1 for args in spawns if 'cat-file' in args and '--batch' in args) == 1
