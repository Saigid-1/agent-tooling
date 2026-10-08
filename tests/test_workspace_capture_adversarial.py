"""Adversarial synthetic checks for automatic, unassigned workspace capture."""
import hashlib
import json
import subprocess
from contextlib import closing
from unittest.mock import patch

import pytest

from t12b_seams import drain  # T12b B4: the one drain helper
from test_portable_desk_memory import config, episode_store
from kp_agent_tooling._impl.service.desk_profiles import DeskProfiles
from kp_agent_tooling._impl.service.episodic_memory_tools import EpisodicMemoryTools
from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
from kp_agent_tooling._impl.service.session_sources import SessionSources
from kp_agent_tooling._impl.service.workspace_capture import CaptureError, WorkspaceCapture
from kp_agent_tooling.workspace_capture_cli import main as capture_cli


NATIVE_INCLUDED = 'a55869b4-6ab4-4e5c-b028-8d4b31d27b71'
NATIVE_OUTSIDE = 'b55869b4-6ab4-4e5c-b028-8d4b31d27b71'


def _git(*args):
    subprocess.run(['git', *map(str, args)], check=True, capture_output=True)


def _repo(path):
    path.mkdir()
    _git('-C', path, 'init', '-q')
    _git('-C', path, 'config', 'user.email', 'fixture@example.invalid')
    _git('-C', path, 'config', 'user.name', 'Fixture')
    (path / 'tracked.txt').write_text('fixture')
    _git('-C', path, 'add', 'tracked.txt')
    _git('-C', path, 'commit', '-qm', 'fixture')
    return path


def _line(row):
    return (json.dumps(row, separators=(',', ':')) + '\n').encode()


def _rollout(root, native_id, cwd, text):
    day = root / '2026' / '09' / '29'
    day.mkdir(parents=True, exist_ok=True)
    path = day / f'rollout-2026-09-29T12-00-00-{native_id}.jsonl'
    path.write_bytes(
        _line({'type': 'session_meta', 'payload': {'id': native_id, 'cwd': str(cwd)}})
        + _line({'type': 'event_msg', 'payload': {'type': 'user_message', 'message': text}})
    )
    return path


def _worker(tmp_path, store, included, codex_root, *, max_candidates=8):
    tenant = store.sessions.resolve('session-1', store.registry).tenant_id
    approval = tmp_path / 'approval.json'
    approval.write_text(json.dumps({'tenant_id': tenant, 'repositories': ['included']}))
    claude_root = tmp_path / 'claude'
    claude_root.mkdir(exist_ok=True)
    policy = {
        'schema_version': 'ops.workspace-capture.v1',
        'approval_record': str(approval),
        'approval_sha256': hashlib.sha256(approval.read_bytes()).hexdigest(),
        'tenant_id': tenant,
        'approved_repo_keys': ['included'],
        'repos': {'included': [str(included)]},
        'native_roots': {'claude': str(claude_root), 'codex': str(codex_root)},
        'excluded_sessions': {'claude': [], 'codex': []},
        'max_candidates': max_candidates,
        'max_batch_bytes': 512_000,
        'max_batch_rows': 50,
    }
    return WorkspaceCapture(store, policy)


def test_worktree_membership_unassigned_and_outside_project_excluded(tmp_path, capsys):
    store = episode_store(tmp_path)
    main = _repo(tmp_path / 'main')
    worktree = tmp_path / 'linked-worktree'
    _git('-C', main, 'worktree', 'add', '-qb', 'linked', worktree)
    outside = _repo(tmp_path / 'outside')
    codex_root = tmp_path / 'codex'
    _rollout(codex_root, NATIVE_INCLUDED, worktree, 'selected workspace cedar')
    _rollout(codex_root, NATIVE_OUTSIDE, outside, 'outside workspace oak')
    worker = _worker(tmp_path, store, main, codex_root)

    private_policy = tmp_path / 'capture-policy.json'
    private_policy.write_text(json.dumps(worker.policy))
    private_policy.chmod(0o600)
    assert capture_cli(['--config', str(config(tmp_path, session='session-1', instance='fixture')),
                        '--policy', str(private_policy), 'once']) == 0
    first = json.loads(capsys.readouterr().out)
    assert first['candidate_count'] == 2
    assert any(gap['reason'] == 'outside_scope' for gap in first['gaps'])
    with closing(store._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_episodes').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM session_claims').fetchone()[0] == 0

    drain(store)  # T12b B4: the indexer, not the capture, indexes
    sources = SessionSources(store)
    tenant = store.sessions.resolve('session-1', store.registry).tenant_id
    sid = sources.register(tenant_id=tenant, runtime='codex', native_id=NATIVE_INCLUDED)
    assert sources.metadata(sid, tenant)['attribution_status'] == 'unresolved'
    assert DeskProfiles(store, 'session-1').source_ids({'kind': 'repo', 'value': 'included'}) == {sid}
    tools = EpisodicMemoryTools(store, 'session-1')
    repo_result = tools.call('memory.search', {'query': 'cedar',
                                             'view': {'kind': 'repo', 'value': 'included'}})
    assert repo_result['results']
    selected = tools.call('memory.search', {'query': 'cedar', 'view': {'kind': 'global'}})
    assert selected['total_episodes'] == 1
    assert selected['results']
    excluded = tools.call('memory.search', {'query': 'oak', 'view': {'kind': 'global'}})
    assert excluded['results'] == []

    second = worker.once()
    assert any(row['status'] == 'unchanged_complete' for row in second['files'])
    with closing(store._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_episodes').fetchone()[0] == 1


def test_missing_worktree_recovers_without_native_append(tmp_path):
    store = episode_store(tmp_path)
    main = _repo(tmp_path / 'main')
    worktree = tmp_path / 'later-worktree'
    codex_root = tmp_path / 'codex'
    _rollout(codex_root, NATIVE_INCLUDED, worktree, 'mounted later cedar')
    worker = _worker(tmp_path, store, main, codex_root)

    first = worker.once()
    assert first['gaps'][0]['reason'] == 'workspace_unavailable'
    _git('-C', main, 'worktree', 'add', '-qb', 'later', worktree)
    second = worker.once()
    assert second['files'][0]['imported'] == 1


def test_rewritten_source_conflicts_and_project_projection_tamper_fails(tmp_path):
    store = episode_store(tmp_path)
    main = _repo(tmp_path / 'main')
    codex_root = tmp_path / 'codex'
    source = _rollout(codex_root, NATIVE_INCLUDED, main, 'original cedar')
    worker = _worker(tmp_path, store, main, codex_root)
    assert worker.once()['files'][0]['imported'] == 1

    tenant = store.sessions.resolve('session-1', store.registry).tenant_id
    sid = SessionSources(store).register(tenant_id=tenant, runtime='codex', native_id=NATIVE_INCLUDED)
    with closing(store._connect()) as db, db:
        db.execute("UPDATE source_projects SET repo_key='forged' WHERE session_id=?", (sid,))
    with pytest.raises(Exception, match='projection'):
        DeskProfiles(store, 'session-1').source_ids({'kind': 'repo', 'value': 'forged'})

    source.write_bytes(source.read_bytes().replace(b'original cedar', b'rewritten cedar'))
    rewrite = worker.once()
    assert rewrite['files'][0]['status'] == 'error'
    assert 'prefix changed' in rewrite['files'][0]['reason']
    with closing(store._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_episodes').fetchone()[0] == 1


def test_exact_hook_exclusion_after_capture_blocks_append(tmp_path):
    store = episode_store(tmp_path)
    main = _repo(tmp_path / 'main')
    codex_root = tmp_path / 'codex'
    source = _rollout(codex_root, NATIVE_INCLUDED, main, 'before hook cedar')
    worker = _worker(tmp_path, store, main, codex_root)
    assert worker.once()['files'][0]['imported'] == 1
    with source.open('ab') as output:
        output.write(_line({'type': 'event_msg',
                            'payload': {'type': 'agent_message', 'message': 'after hook maple'}}))

    excluded_policy = {**worker.policy,
                       'excluded_sessions': {'claude': [], 'codex': [NATIVE_INCLUDED]}}
    excluded = WorkspaceCapture(store, excluded_policy)
    preview = excluded.preview()
    assert preview['gaps'][0]['reason'] == 'managed_by_exact_hook'
    assert all('hook_health' not in str(item) for item in preview['gaps'])
    try:
        result = excluded.once()
    except CaptureError:
        # A changed policy may require operator review; it must never continue
        # the old approved capture path while that review is pending.
        pass
    else:
        assert result['gaps'][0]['reason'] == 'managed_by_exact_hook'
    with closing(store._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_episodes').fetchone()[0] == 1


def test_copied_native_file_replays_original_episode_and_changed_identity_is_error(tmp_path):
    store = episode_store(tmp_path)
    main = _repo(tmp_path / 'main')
    codex_root = tmp_path / 'codex'
    source = _rollout(codex_root, NATIVE_INCLUDED, main, 'copied evidence cedar')
    worker = _worker(tmp_path, store, main, codex_root)
    first = worker.once()
    assert first['files'][0]['imported'] == 1

    copied = codex_root / '2026' / '09' / '30' / source.name
    copied.parent.mkdir(parents=True)
    copied.write_bytes(source.read_bytes())
    replay = worker.once()
    assert any(item.get('imported') == 0 for item in replay['files'] if item['source_file'] == str(copied))
    with closing(store._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_episodes').fetchone()[0] == 1

    with copied.open('ab') as output:
        output.write(_line({'type': 'session_meta',
                            'payload': {'id': NATIVE_INCLUDED, 'cwd': str(tmp_path / 'other')}}))
        output.write(_line({'type': 'event_msg',
                            'payload': {'type': 'user_message', 'message': 'must not capture'}}))
    changed = worker.once()
    error = next(item for item in changed['files'] if item['source_file'] == str(copied))
    assert error['status'] == 'error'
    assert 'identity changed' in error['reason']
    with closing(store._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_episodes').fetchone()[0] == 1


def test_cli_refuses_foreign_tenant_policy_before_capture(tmp_path, capsys):
    store = episode_store(tmp_path)
    main = _repo(tmp_path / 'main')
    codex_root = tmp_path / 'codex'
    _rollout(codex_root, NATIVE_INCLUDED, main, 'foreign tenant cedar')
    worker = _worker(tmp_path, store, main, codex_root)
    foreign_policy = {**worker.policy, 'tenant_id': 'another-tenant'}
    approval = tmp_path / 'foreign-approval.json'
    approval.write_text(json.dumps({'tenant_id': 'another-tenant', 'repositories': ['included']}))
    foreign_policy['approval_record'] = str(approval)
    foreign_policy['approval_sha256'] = hashlib.sha256(approval.read_bytes()).hexdigest()
    private_policy = tmp_path / 'private-policy.json'
    private_policy.write_text(json.dumps(foreign_policy))
    private_policy.chmod(0o600)
    result = capture_cli(['--config', str(config(tmp_path, session='session-1', instance='fixture')),
                          '--policy', str(private_policy), 'once'])
    report = json.loads(capsys.readouterr().out)
    assert result == 1
    assert report['status'] == 'error'
    assert 'tenant' in report['message']
    with closing(store._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_projects').fetchone()[0] == 0


def test_large_visible_row_is_reconstructable_and_index_failure_repairs(tmp_path):
    """T12b R3 replacement (ruled exception, Verification, 2026-10-04; tests/t12b_ruled_exceptions.py): the large
    visible row stays reconstructable; an index outage during a drain loses no seal, and the next drain indexes it.
    The outage is an index path that is not a file (the index cannot be opened) while the pass and its host drain run.
    GREEN-IF the pass reports no error, the row is sealed once and reconstructs exactly, its seal waits in the outbox
    after the failed drain, the retired `index_pending` stays empty, and once the index is back the next drain
    indexes it and a search finds it."""
    from t12b_seams import drain, outbox_rows
    store = episode_store(tmp_path)
    main = _repo(tmp_path / 'main')
    codex_root = tmp_path / 'codex'
    visible = 'cedar-' * 25_000
    _rollout(codex_root, NATIVE_INCLUDED, main, visible)
    worker = _worker(tmp_path, store, main, codex_root)
    index = store.path.with_name('episode-search.sqlite3')
    index.mkdir()  # the index outage: the index file cannot be opened

    passed = worker.once()
    assert passed['files'][0]['status'] != 'error', f'an index outage failed the capture pass: {passed["files"][0]}'
    with closing(store._connect()) as db:
        rows = db.execute('SELECT id, payload FROM source_episodes').fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0][1])
    assert ''.join(event['text'] for event in payload['events']) == visible
    assert len(payload['events']) > 1
    waiting = [row for row in outbox_rows(store.path) if row['reason'] == 'seal' and row['episode_id'] == rows[0][0]]
    assert len(waiting) == 1, f'the seal is not waiting in the outbox after the failed drain: {waiting}'
    with closing(worker._db()) as db:
        assert json.loads(db.execute('SELECT index_pending FROM files').fetchone()[0]) == []

    index.rmdir()  # the index is back
    repaired = worker.once()
    assert repaired['files'][0]['status'] != 'error'
    with closing(store._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_episodes').fetchone()[0] == 1
    drain(store)
    found = EpisodicMemoryTools(store, 'session-1').call('memory.search', {'query': 'cedar', 'view': {'kind': 'global'}})
    assert [r['episode_id'] for r in found['results']][:1] == [rows[0][0]], f'the next drain did not index it: {found}'
