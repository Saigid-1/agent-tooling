"""Claude native parent and subagent capture boundaries."""
from contextlib import closing
import fcntl
import os

import pytest

from test_portable_desk_memory import episode_store
from test_workspace_capture_adversarial import _line, _repo, _worker
from kp_agent_tooling._impl.service.session_sources import SessionSources
from kp_agent_tooling._impl.service.workspace_capture import CaptureError


PARENT = 'a45869b4-6ab4-4e5c-b028-8d4b31d27b71'


def _claude_sources(worker, repo):
    project = worker.policy['native_roots']['claude']
    from pathlib import Path
    root = Path(project) / 'project-folder-slug-is-not-authority'
    root.mkdir()
    parent = root / f'{PARENT}.jsonl'
    parent.write_bytes(
        _line({'type': 'file-history-snapshot', 'cwd': str(repo)})
        + _line({'type': 'user', 'sessionId': PARENT, 'cwd': str(repo),
                 'message': {'role': 'user', 'content': 'parent cedar'}})
    )
    sub = root / PARENT / 'subagents'
    sub.mkdir(parents=True)
    child = sub / 'agent-sub1.jsonl'
    child.write_bytes(_line({'type': 'assistant', 'sessionId': PARENT,
                             'agentId': 'sub1', 'isSidechain': True, 'cwd': str(repo),
                             'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'child cedar'}]}}))
    return parent, child


def test_claude_parent_control_child_and_replay(tmp_path):
    store = episode_store(tmp_path)
    repo = _repo(tmp_path / 'repo')
    codex = tmp_path / 'codex'
    codex.mkdir()
    worker = _worker(tmp_path, store, repo, codex)
    parent, child = _claude_sources(worker, repo)
    assert not worker.path.exists()
    preview = worker.preview()
    assert preview['candidate_count'] == 2
    assert not worker.path.exists()

    first = worker.once()
    assert first['status'] == 'ok'
    assert {row['runtime'] for row in first['files']} == {'claude'}
    assert sum(row.get('imported', 0) for row in first['files']) == 2
    tenant = worker.policy['tenant_id']
    with closing(store._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_episodes WHERE tenant=?', (tenant,)).fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM session_claims').fetchone()[0] == 0
    sources = SessionSources(store)
    parent_id = sources.register(tenant_id=tenant, runtime='claude', native_id=PARENT)
    child_id = sources.register(tenant_id=tenant, runtime='claude', native_id=f'{PARENT}/subagents/agent-sub1')
    assert sources.metadata(parent_id, tenant)['attribution_status'] == 'unresolved'
    assert sources.metadata(child_id, tenant)['attribution_status'] == 'unresolved'

    replay = worker.once()
    assert replay['status'] == 'ok'
    assert all(row['status'] == 'unchanged_complete' for row in replay['files'])
    with closing(store._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_episodes WHERE tenant=?', (tenant,)).fetchone()[0] == 2
    with parent.open('ab') as stream:
        stream.write(_line({'type': 'assistant', 'sessionId': PARENT, 'cwd': str(repo),
                            'message': {'role': 'assistant', 'content': 'appended cedar'}}))
    appended = worker.once()
    assert appended['status'] == 'ok'
    assert sum(row.get('imported', 0) for row in appended['files']) == 1
    with closing(store._connect()) as db:
        assert db.execute('SELECT COUNT(*) FROM source_episodes WHERE tenant=?', (tenant,)).fetchone()[0] == 3


def test_separate_worker_lock_refuses_overlap(tmp_path):
    store = episode_store(tmp_path)
    repo = _repo(tmp_path / 'repo')
    codex = tmp_path / 'codex'
    codex.mkdir()
    worker = _worker(tmp_path, store, repo, codex)
    _claude_sources(worker, repo)
    worker.initialize()
    lock_path = worker.path.with_name('workspace-capture.lock')
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(CaptureError, match='another workspace capture worker'):
            worker.once()
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
