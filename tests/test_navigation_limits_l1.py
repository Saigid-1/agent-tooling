"""L1 acceptance: useful bounded pages and an advisory completion estimate.

GREEN-IF: a 929-file low-density literal search finishes in at most three pages,
while result limits still resume mid-file without duplicates and exclusions keep
absence unestablished.
"""
import json
import subprocess

from kp_agent_tooling._impl.navigation_search_pages import search_pages


def _git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def _fixture(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'config', 'user.name', 'Test')
    _git(repo, 'config', 'user.email', 'test@example.invalid')
    for index in range(929):
        lines = ['ordinary line\n'] * 20
        if index == 800:
            lines[12] = 'run_import()\n'
        (repo / f'f{index:04}.py').write_text(''.join(lines))
    (repo / 'excluded.bin').write_bytes(b'run_import\0')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'fixture')
    revision = _git(repo, 'rev-parse', 'HEAD')
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'schema_version': 'ops.navigation-profile.v1',
        'profile': 'fixture', 'repos': {'repo': {'path': str(repo), 'revision': revision}}}))
    registry = tmp_path / 'registry'
    registry.mkdir()
    return {'repos': {'repo': {'path': str(repo), 'revision': revision}},
            'navigation_profile': str(profile)}, registry, revision


def test_low_density_repository_finishes_within_three_pages_and_estimates(tmp_path):
    config, registry, revision = _fixture(tmp_path)
    token = None
    pages = []
    hits = []
    while True:
        page = search_pages(config, registry, 'repo', revision, 'run_import',
                            continuation_token=token, limit=30)
        pages.append(page)
        hits.extend((row['path'], row['line']) for row in page['results'])
        if page['traversal_complete']:
            break
        token = page['continuation_token']
    assert len(pages) <= 3
    assert hits == [('f0800.py', 13)]
    assert pages[0]['pages_remaining_estimate'] is not None
    assert pages[-1]['pages_remaining_estimate'] == 0
    assert pages[-1]['coverage']['excluded_files'] == 1
    assert pages[-1]['absence_verdict'] == 'not-established'
    # One streamed batch read per page: a 929-file tree no longer needs paging.
    assert all(page['page_budget']['files'] > 0 for page in pages)
    assert len(pages) == 1
    assert all(0 < page['page_budget']['lines'] for page in pages)


def test_high_match_result_limit_resumes_mid_file_without_loss(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q', '-b', 'main')
    _git(repo, 'config', 'user.name', 'Test')
    _git(repo, 'config', 'user.email', 'test@example.invalid')
    (repo / 'many.py').write_text('call()\n' * 75)
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-qm', 'fixture')
    revision = _git(repo, 'rev-parse', 'HEAD')
    profile = tmp_path / 'profile.json'
    profile.write_text(json.dumps({'schema_version': 'ops.navigation-profile.v1',
        'profile': 'fixture', 'repos': {'repo': {'path': str(repo), 'revision': revision}}}))
    config = {'repos': {'repo': {'path': str(repo), 'revision': revision}},
              'navigation_profile': str(profile)}
    registry = tmp_path / 'registry'
    registry.mkdir()
    token = None
    lines = []
    estimates = []
    while True:
        page = search_pages(config, registry, 'repo', revision, 'call(', limit=30,
                            continuation_token=token)
        lines.extend(row['line'] for row in page['results'])
        estimates.append(page['pages_remaining_estimate'])
        if page['traversal_complete']:
            break
        token = page['continuation_token']
    assert lines == list(range(1, 76))
    assert estimates[0] is None
    assert estimates[-1] == 0


def test_estimate_uses_observed_page_throughput_when_line_budget_binds(tmp_path, monkeypatch):
    config, registry, revision = _fixture(tmp_path)
    monkeypatch.setattr('kp_agent_tooling._impl.navigation_search_pages.MAX_LINES', 1000)
    page = search_pages(config, registry, 'repo', revision, 'absent', limit=30)
    assert page['page_usage']['lines'] == 1000
    assert page['coverage']['searched_files'] == 50
    assert page['pages_remaining_estimate'] == 18
