"""T9: workspace capture continues across file renumbering and directory moves.

Order: docs/work/orders/T9-capture-continuity.md (P1-P4), frozen at its merge
commit. These tests drive the public surface:

- the `WorkspaceCapture` service (`preview`, `once`), as the existing capture tests do;
- the `kp-agent-workspace-capture` CLI (`preview`), with a private policy file.

The inputs are synthetic Claude and Codex transcripts and Git checkouts in temporary
directories. Results are observed in four places:

- source episodes, in the episode store;
- source sessions and their sealed project membership, through `SessionSources`;
- the per-source capture record, in the worker's journal, as the existing tests read it;
- the reports that `once` and `preview` return.

Readings (repeated in the arm report under AMBIGUITY):

- **Renumbering.** A renumbered source is the same path atomically replaced by a new
  file with a new inode: the same bytes plus appended rows.
- **Counting `cwd_outside_policy`.** The omission must be counted under exactly
  that name. It may be counted in the `once` file entry for the pass, or cumulatively
  in the source's journal record counts.
- **The reason `preview` reports.** For a source, `preview` must report the
  source's recorded error string. A count per reason is either a `{reason: n}`
  mapping or a mapping that names the reason beside an integer `*count*` field.
- **Codex rows after `session_meta`.** Only a Codex `session_meta` move between
  approved checkouts is tested here. A `session_meta` whose `cwd` is outside every
  approved checkout is not tested: the existing test
  `test_copied_native_file_replays_original_episode_and_changed_identity_is_error`
  requires that case to stay a refusal, and P4 keeps every existing test unmodified
  and green.
- **P4 guards.** These pass at base by design: they pin unchanged behaviour. Each
  also fails against the null stub.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from test_portable_desk_memory import config, episode_store
from test_workspace_capture_adversarial import _git, _line, _repo, _worker
from kp_agent_tooling._impl.service.session_sources import SessionSources
from kp_agent_tooling._impl.service.workspace_capture import WorkspaceCapture
from kp_agent_tooling.workspace_capture_cli import main as capture_cli


PARENT = 'c95869b4-6ab4-4e5c-b028-8d4b31d27b71'
SECOND = 'd95869b4-6ab4-4e5c-b028-8d4b31d27b71'
CODEX = 'e95869b4-6ab4-4e5c-b028-8d4b31d27b71'
THIRD = 'a75869b4-6ab4-4e5c-b028-8d4b31d27b71'
FOREIGN = 'f95869b4-6ab4-4e5c-b028-8d4b31d27b71'
OUTSIDE = 'cwd_outside_policy'
SEALED = 'sealed native source row changed'


# ---------------------------------------------------------------------------
# Synthetic native transcripts
# ---------------------------------------------------------------------------

def _said(text, cwd, *, role='user', session=PARENT, **extra):
    """One visible Claude row."""
    return _line({'type': role, 'sessionId': session, 'cwd': str(cwd),
                  'message': {'role': role, 'content': text}, **extra})


def _child(text, cwd, *, agent='sub1'):
    """One visible Claude sidechain row of the native subagent `agent-sub1`."""
    return _said(text, cwd, role='assistant', agentId=agent, isSidechain=True)


def _meta(cwd, native=CODEX):
    return _line({'type': 'session_meta', 'payload': {'id': native, 'cwd': str(cwd)}})


def _codex_said(text, kind='user_message'):
    return _line({'type': 'event_msg', 'payload': {'type': kind, 'message': text}})


def _claude_path(worker, session=PARENT, project='project-folder'):
    folder = Path(worker.policy['native_roots']['claude']) / project
    folder.mkdir(exist_ok=True)
    return folder / f'{session}.jsonl'


def _child_path(worker, project='project-folder'):
    folder = Path(worker.policy['native_roots']['claude']) / project / PARENT / 'subagents'
    folder.mkdir(parents=True, exist_ok=True)
    return folder / 'agent-sub1.jsonl'


def _codex_path(worker, native=CODEX, day='29'):
    folder = Path(worker.policy['native_roots']['codex']) / '2026' / '09' / day
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f'rollout-2026-09-{day}T12-00-00-{native}.jsonl'


def _append(path, data):
    with path.open('ab') as stream:
        stream.write(data)


def _renumber(path, data):
    """Atomically replace `path` with `data` written to a new file, so its inode changes."""
    before = path.stat()
    staging = path.with_name(path.name + '.renumber')  # not a `.jsonl` capture candidate
    staging.write_bytes(data)
    os.replace(staging, path)
    after = path.stat()
    assert after.st_dev == before.st_dev
    assert after.st_ino != before.st_ino, 'precondition: the renumbered source has a new inode'
    return after


# ---------------------------------------------------------------------------
# Workers, observations
# ---------------------------------------------------------------------------

def _setup(tmp_path):
    store = episode_store(tmp_path)
    repo = _repo(tmp_path / 'repo')
    codex = tmp_path / 'codex'
    codex.mkdir()
    return store, repo, _worker(tmp_path, store, repo, codex)


def _scoped(tmp_path, store, worker, repos, *, name, tenant=None):
    """A worker on the same native roots and journal with an explicit approved repository map."""
    tenant = tenant or worker.policy['tenant_id']
    approval = tmp_path / f'approval-{name}.json'
    approval.write_text(json.dumps({'tenant_id': tenant, 'repositories': list(repos)}))
    policy = {**worker.policy, 'tenant_id': tenant, 'approval_record': str(approval),
              'approval_sha256': hashlib.sha256(approval.read_bytes()).hexdigest(),
              'approved_repo_keys': list(repos),
              'repos': {key: [str(root) for root in roots] for key, roots in repos.items()}}
    return WorkspaceCapture(store, policy)


def _tenant(worker):
    return worker.policy['tenant_id']


def _sid(store, worker, native, runtime='claude'):
    return SessionSources(store).register(tenant_id=_tenant(worker), runtime=runtime, native_id=native)


def _episodes(store):
    with closing(store._connect()) as db:
        rows = db.execute('SELECT session_id,source_ref,payload FROM source_episodes').fetchall()
    return [(session, ref, json.loads(payload)) for session, ref, payload in rows]


def _texts(store, session=None):
    """Every visible event text in the store (or one source session), in sorted order."""
    return sorted(event['text'] for sid, _, payload in _episodes(store)
                  if session is None or sid == session for event in payload['events'])


def _assert_no_duplicates(store):
    refs = [(session, ref) for session, ref, _ in _episodes(store)]
    assert len(refs) == len(set(refs))
    texts = _texts(store)
    assert len(texts) == len(set(texts)), texts


def _record(worker, path):
    with closing(worker._db()) as db:
        row = db.execute('SELECT device,inode,cursor,prefix_sha256,error,counts,repo_key,'
                         'lease_until,lease_owner FROM files WHERE path=?', (str(path),)).fetchone()
    assert row is not None, f'no capture record for {path}'
    names = ('device', 'inode', 'cursor', 'prefix_sha256', 'error', 'counts', 'repo_key',
             'lease_until', 'lease_owner')
    record = dict(zip(names, row))
    record['counts'] = json.loads(record['counts'])
    return record


def _entry(report, path):
    rows = [row for row in report['files'] if row.get('source_file') == str(path)]
    assert len(rows) == 1, report
    return rows[0]


def _projects(store, session):
    with closing(store._connect()) as db:
        return db.execute('SELECT repo_key FROM source_projects WHERE session_id=?', (session,)).fetchall()


def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _strings_for(report, path):
    """Strings a report attaches to one source file: any mapping that names the file."""
    found = set()
    for node in _walk(report):
        if not isinstance(node, dict):
            continue
        if str(path) in node.values():
            found |= {value for value in node.values() if isinstance(value, str)}
        if str(path) in node:
            found |= {value for value in _walk(node[str(path)]) if isinstance(value, str)}
    found.discard(str(path))
    return found


def _reason_counts(report, reason):
    """Counts a report attaches to one reason: {reason: n}, or a mapping naming it beside a count."""
    found = []
    for node in _walk(report):
        if not isinstance(node, dict):
            continue
        if type(node.get(reason)) is int:
            found.append(node[reason])
        if reason in node.values():
            found += [value for key, value in node.items()
                      if type(value) is int and 'count' in str(key).lower()]
    return found


def _omission_counts(node):
    """Counts attached to the `cwd_outside_policy` omission: {name: n} or repeated list entries."""
    found = []
    for item in _walk(node):
        if isinstance(item, dict) and type(item.get(OUTSIDE)) is int:
            found.append(item[OUTSIDE])
        elif isinstance(item, list) and OUTSIDE in item:
            found.append(item.count(OUTSIDE))
    return found


def _cli_preview(tmp_path, worker, capsys, name):
    policy = tmp_path / f'policy-{name}.json'
    policy.write_text(json.dumps(worker.policy))
    policy.chmod(0o600)
    capsys.readouterr()
    capture_cli(['--config', str(config(tmp_path, session='session-1', instance='fixture')),
                 '--policy', str(policy), 'preview'])
    return json.loads(capsys.readouterr().out)


def _outside_git(path):
    result = subprocess.run(['git', '-C', str(path), 'rev-parse', '--git-common-dir'],
                            capture_output=True, text=True,
                            env={k: v for k, v in os.environ.items() if not k.startswith('GIT_')})
    return result.returncode != 0


# ---------------------------------------------------------------------------
# P1: continuity across file renumbering
# ---------------------------------------------------------------------------

def _two_rows(runtime, worker, repo):
    if runtime == 'claude':
        path = _claude_path(worker)
        rows = [_said('renumber cedar one', repo), _said('renumber cedar two', repo, role='assistant')]
        appended = [_said('renumber cedar three', repo), _said('renumber cedar four', repo, role='assistant')]
    else:
        path = _codex_path(worker)
        rows = [_meta(repo), _codex_said('renumber cedar one'), _codex_said('renumber cedar two', 'agent_message')]
        appended = [_codex_said('renumber cedar three'), _codex_said('renumber cedar four', 'agent_message')]
    path.write_bytes(b''.join(rows))
    return path, b''.join(appended)


@pytest.mark.parametrize('runtime', ['claude', 'codex'])
def test_p1_renumbered_source_continues_from_its_cursor(tmp_path, runtime):
    store, repo, worker = _setup(tmp_path)
    path, appended = _two_rows(runtime, worker, repo)
    first = worker.once()
    assert _entry(first, path).get('imported') == 2
    captured = _record(worker, path)
    visible_before = captured['counts']['visible_rows']
    original = path.read_bytes()
    assert captured['cursor'] == len(original)

    renumbered = _renumber(path, original + appended)
    report = worker.once()
    entry = _entry(report, path)
    assert report['status'] == 'ok', report
    assert entry['status'] == 'captured', entry
    assert entry.get('imported') == 2
    assert _texts(store) == sorted(f'renumber cedar {n}' for n in ('one', 'two', 'three', 'four'))
    _assert_no_duplicates(store)

    record = _record(worker, path)
    assert (record['device'], record['inode']) == (renumbered.st_dev, renumbered.st_ino)
    assert record['cursor'] == renumbered.st_size
    assert record['prefix_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert record['error'] is None
    # The prefix was not processed again: only the two appended rows were added.
    assert record['counts']['visible_rows'] == visible_before + 2

    replay = worker.once()
    assert _entry(replay, path)['status'] == 'unchanged_complete'
    assert len(_texts(store)) == 4


def test_p1_renumbered_source_with_changed_prefix_stays_refused_until_restored(tmp_path):
    store, repo, worker = _setup(tmp_path)
    path = _claude_path(worker)
    path.write_bytes(_said('prefix cedar alpha', repo) + _said('prefix cedar beta', repo, role='assistant'))
    assert _entry(worker.once(), path).get('imported') == 2
    original, captured = path.read_bytes(), _record(worker, path)
    appended = _said('prefix cedar gamma', repo)

    tampered = original.replace(b'prefix cedar alpha', b'prefix cidar alpha')
    assert len(tampered) == len(original) and tampered != original
    _renumber(path, tampered + appended)
    refused = worker.once()
    assert _entry(refused, path)['status'] == 'error'
    assert refused['status'] == 'partial'
    assert _texts(store) == ['prefix cedar alpha', 'prefix cedar beta']
    held = _record(worker, path)
    assert (held['cursor'], held['prefix_sha256']) == (captured['cursor'], captured['prefix_sha256'])
    assert _entry(worker.once(), path)['status'] == 'error'
    assert _texts(store) == ['prefix cedar alpha', 'prefix cedar beta']

    # Control: the refusal is about the prefix, not the renumbering.
    restored = _renumber(path, original + appended)
    entry = _entry(worker.once(), path)
    assert entry['status'] == 'captured', entry
    assert entry.get('imported') == 1
    assert _texts(store) == ['prefix cedar alpha', 'prefix cedar beta', 'prefix cedar gamma']
    _assert_no_duplicates(store)
    record = _record(worker, path)
    assert (record['device'], record['inode'], record['cursor']) == (
        restored.st_dev, restored.st_ino, restored.st_size)


def test_p1_renumbered_source_shorter_than_its_cursor_stays_refused(tmp_path):
    store, repo, worker = _setup(tmp_path)
    path = _claude_path(worker)
    head = _said('short cedar alpha', repo)
    path.write_bytes(head + _said('short cedar beta', repo, role='assistant'))
    assert _entry(worker.once(), path).get('imported') == 2
    original, captured = path.read_bytes(), _record(worker, path)

    _renumber(path, head)
    assert path.stat().st_size < captured['cursor']
    assert _entry(worker.once(), path)['status'] == 'error'
    assert _texts(store) == ['short cedar alpha', 'short cedar beta']
    held = _record(worker, path)
    assert (held['cursor'], held['prefix_sha256']) == (captured['cursor'], captured['prefix_sha256'])

    restored = _renumber(path, original + _said('short cedar gamma', repo))
    entry = _entry(worker.once(), path)
    assert entry['status'] == 'captured', entry
    assert entry.get('imported') == 1
    assert _texts(store) == ['short cedar alpha', 'short cedar beta', 'short cedar gamma']
    _assert_no_duplicates(store)
    assert _record(worker, path)['inode'] == restored.st_ino


def test_p1_renumbered_source_with_changed_tenant_or_repository_key_stays_refused(tmp_path):
    store, repo, worker = _setup(tmp_path)
    second = _repo(tmp_path / 'second')
    both = _scoped(tmp_path, store, worker, {'included': [repo], 'second': [second]}, name='both')
    swapped = _scoped(tmp_path, store, worker, {'included': [second], 'second': [repo]}, name='swapped')
    foreign = _scoped(tmp_path, store, worker, {'included': [repo], 'second': [second]},
                      name='foreign', tenant='another-tenant')
    path = _claude_path(worker)
    path.write_bytes(_said('identity cedar alpha', repo))
    assert _entry(both.once(), path).get('imported') == 1
    captured = _record(both, path)
    assert captured['repo_key'] == 'included'
    renumbered = _renumber(path, path.read_bytes() + _said('identity cedar beta', repo))

    # Same prefix, new inode, but the repository key differs: refused.
    assert _entry(swapped.once(), path)['status'] == 'error'
    # Same prefix, new inode, but the tenant differs: refused.
    assert _entry(foreign.once(), path)['status'] == 'error'
    assert _texts(store) == ['identity cedar alpha']
    held = _record(both, path)
    assert (held['repo_key'], held['cursor']) == ('included', captured['cursor'])

    entry = _entry(both.once(), path)
    assert entry['status'] == 'captured', entry
    assert entry.get('imported') == 1
    assert _texts(store) == ['identity cedar alpha', 'identity cedar beta']
    _assert_no_duplicates(store)
    assert _record(both, path)['inode'] == renumbered.st_ino


# ---------------------------------------------------------------------------
# P2: a directory move does not end capture
# ---------------------------------------------------------------------------

def test_p2_claude_move_into_a_subdirectory_is_captured_and_continues(tmp_path):
    store, repo, worker = _setup(tmp_path)
    (repo / 'sub' / 'deeper').mkdir(parents=True)
    path = _claude_path(worker)
    path.write_bytes(_said('move cedar start', repo)
                     + _said('move cedar sub', repo / 'sub', role='assistant')
                     + _said('move cedar deeper', repo / 'sub' / 'deeper')
                     + _said('move cedar back', repo, role='assistant'))
    report = worker.once()
    entry = _entry(report, path)
    assert report['status'] == 'ok', report
    assert entry['status'] == 'captured', entry
    assert entry.get('imported') == 4
    sid = _sid(store, worker, PARENT)
    assert _texts(store, sid) == sorted(['move cedar start', 'move cedar sub', 'move cedar deeper', 'move cedar back'])
    assert _record(worker, path)['cursor'] == path.stat().st_size

    _append(path, _said('move cedar later', repo / 'sub'))
    entry = _entry(worker.once(), path)
    assert entry['status'] == 'captured', entry
    assert entry.get('imported') == 1
    assert 'move cedar later' in _texts(store, sid)
    assert _projects(store, sid) == [('included',)]


@pytest.mark.parametrize('kind', ['other approved repository', 'second root of the same key', 'linked worktree'])
def test_p2_claude_move_into_another_approved_checkout_keeps_starting_membership(tmp_path, kind):
    store, repo, worker = _setup(tmp_path)
    if kind == 'other approved repository':
        other = _repo(tmp_path / 'other')
        repos = {'included': [repo], 'second': [other]}
    elif kind == 'second root of the same key':
        other = _repo(tmp_path / 'clone')
        repos = {'included': [repo, other]}
    else:
        other = tmp_path / 'linked'
        _git('-C', repo, 'worktree', 'add', '-qb', 'linked', other)
        repos = {'included': [repo]}
    scoped = _scoped(tmp_path, store, worker, repos, name='approved')
    path = _claude_path(worker)
    path.write_bytes(_said('approved cedar start', repo)
                     + _said('approved cedar elsewhere', other, role='assistant')
                     + _said('approved cedar after', other))
    report = scoped.once()
    entry = _entry(report, path)
    assert report['status'] == 'ok', report
    assert entry['status'] == 'captured', entry
    assert entry.get('imported') == 3
    sid = _sid(store, worker, PARENT)
    assert _texts(store, sid) == sorted(['approved cedar start', 'approved cedar elsewhere', 'approved cedar after'])

    _append(path, _said('approved cedar later', other, role='assistant'))
    assert _entry(scoped.once(), path).get('imported') == 1
    assert 'approved cedar later' in _texts(store, sid)
    # The sealed project membership stays the starting repository.
    assert _projects(store, sid) == [('included',)]
    sources = SessionSources(store)
    assert sid in sources.project_ids(tenant_id=_tenant(worker), repo_key='included')
    if 'second' in repos:
        assert sid not in sources.project_ids(tenant_id=_tenant(worker), repo_key='second')


@pytest.mark.parametrize('kind', ['unapproved Git checkout', 'missing directory', 'directory outside Git'])
def test_p2_claude_row_outside_policy_is_omitted_counted_and_capture_continues(tmp_path, kind):
    store, repo, worker = _setup(tmp_path)
    if kind == 'unapproved Git checkout':
        outside = _repo(tmp_path / 'unapproved')
    elif kind == 'missing directory':
        outside = tmp_path / 'missing-directory'
        assert not outside.exists()
    else:
        outside = tmp_path / 'plain-directory'
        outside.mkdir()
        assert _outside_git(outside), 'precondition: the directory is outside every Git checkout'
    path = _claude_path(worker)
    path.write_bytes(_said('policy cedar alpha', repo)
                     + _said('outside oak one', outside, role='assistant')
                     + _said('policy cedar omega', repo))
    report = worker.once()
    entry = _entry(report, path)
    assert report['status'] == 'ok', report
    assert entry['status'] == 'captured', entry
    assert entry.get('imported') == 2
    assert entry['quarantined_rows'] == 0
    assert _texts(store) == ['policy cedar alpha', 'policy cedar omega']
    record = _record(worker, path)
    assert record['cursor'] == path.stat().st_size
    assert record['error'] is None
    assert 1 in _omission_counts(entry) + _omission_counts(record['counts']), (entry, record)

    _append(path, _said('outside oak two', outside) + _said('outside oak three', outside, role='assistant')
            + _said('policy cedar after', repo))
    entry = _entry(worker.once(), path)
    assert entry['status'] == 'captured', entry
    assert entry.get('imported') == 1
    record = _record(worker, path)
    # Two omissions in this pass, three in total.
    assert 2 in _omission_counts(entry) or 3 in _omission_counts(record['counts']), (entry, record)
    assert _texts(store) == ['policy cedar after', 'policy cedar alpha', 'policy cedar omega']
    assert not any('oak' in text for text in _texts(store))
    sid = _sid(store, worker, PARENT)
    assert _projects(store, sid) == [('included',)]


def test_p2_foreign_session_or_agent_row_still_refused_after_a_move(tmp_path):
    store, repo, worker = _setup(tmp_path)
    (repo / 'sub').mkdir()
    parent = _claude_path(worker)
    child = _child_path(worker)
    parent.write_bytes(_said('session cedar first', repo))
    child.write_bytes(_child('agent cedar first', repo))
    first = worker.once()
    assert [_entry(first, path).get('imported') for path in (parent, child)] == [1, 1], first

    _append(parent, _said('session cedar moved', repo / 'sub', role='assistant'))
    _append(child, _child('agent cedar moved', repo / 'sub'))
    moved = worker.once()
    assert [_entry(moved, path).get('imported') for path in (parent, child)] == [1, 1], moved
    texts = _texts(store)

    foreign_at = parent.stat().st_size
    _append(parent, _said('foreign session maple', repo / 'sub', session=FOREIGN)
            + _said('session after maple', repo / 'sub'))
    child_at = child.stat().st_size
    _append(child, _child('foreign agent maple', repo / 'sub', agent='sub2')
            + _child('agent after maple', repo / 'sub'))
    for _ in range(2):
        refused = worker.once()
        assert _entry(refused, parent)['status'] == 'error'
        assert _entry(refused, child)['status'] == 'error'
        assert _texts(store) == texts
    assert _record(worker, parent)['cursor'] <= foreign_at
    assert _record(worker, child)['cursor'] <= child_at


def test_p2_codex_session_meta_move_continues_and_foreign_id_still_refused(tmp_path):
    store, repo, worker = _setup(tmp_path)
    (repo / 'sub').mkdir()
    other = _repo(tmp_path / 'other')
    scoped = _scoped(tmp_path, store, worker, {'included': [repo], 'second': [other]}, name='codex')
    path = _codex_path(worker)
    path.write_bytes(_meta(repo) + _codex_said('codex cedar first'))
    assert _entry(scoped.once(), path).get('imported') == 1

    _append(path, _meta(repo / 'sub') + _codex_said('codex cedar sub', 'agent_message')
            + _meta(other) + _codex_said('codex cedar other'))
    report = scoped.once()
    entry = _entry(report, path)
    assert report['status'] == 'ok', report
    assert entry['status'] == 'captured', entry
    assert entry.get('imported') == 2
    sid = _sid(store, worker, CODEX, 'codex')
    assert _texts(store, sid) == ['codex cedar first', 'codex cedar other', 'codex cedar sub']
    assert _projects(store, sid) == [('included',)]

    _append(path, _meta(repo, native=FOREIGN) + _codex_said('codex foreign maple'))
    for _ in range(2):
        assert _entry(scoped.once(), path)['status'] == 'error'
    assert _texts(store) == ['codex cedar first', 'codex cedar other', 'codex cedar sub']


# ---------------------------------------------------------------------------
# P3: refusals are recorded and visible
# ---------------------------------------------------------------------------

def test_p3_pre_lease_identity_refusal_is_recorded_previewed_and_cleared(tmp_path, capsys):
    store, repo, worker = _setup(tmp_path)
    second = _repo(tmp_path / 'second')
    both = _scoped(tmp_path, store, worker, {'included': [repo], 'second': [second]}, name='both')
    swapped = _scoped(tmp_path, store, worker, {'included': [second], 'second': [repo]}, name='swapped')
    path = _claude_path(worker)
    path.write_bytes(_said('recorded cedar quartz-7731', repo))
    assert _entry(both.once(), path).get('imported') == 1

    refused = _entry(swapped.once(), path)
    assert refused['status'] == 'error'
    reason = _record(swapped, path)['error']
    assert reason is not None, 'the refusal before the lease is not recorded'
    assert reason == refused['reason']
    assert 'quartz-7731' not in reason and 'cedar' not in reason
    for preview in (swapped.preview(), _cli_preview(tmp_path, swapped, capsys, 'swapped')):
        assert reason in _strings_for(preview, path), preview
        assert set(_reason_counts(preview, reason)) == {1}, preview

    _append(path, _said('recorded cedar after', repo))
    entry = _entry(both.once(), path)
    assert entry['status'] == 'captured', entry
    assert _record(both, path)['error'] is None
    for preview in (both.preview(), _cli_preview(tmp_path, both, capsys, 'both')):
        assert reason not in _strings_for(preview, path), preview
        assert set(_reason_counts(preview, reason)) <= {0}, preview


def test_p3_renumbered_refusals_are_counted_per_reason_and_cleared_by_success(tmp_path):
    store, repo, worker = _setup(tmp_path)
    first, second = _claude_path(worker), _claude_path(worker, SECOND)
    first.write_bytes(_said('count cedar first jade-1', repo))
    second.write_bytes(_said('count cedar second jade-2', repo, session=SECOND))
    report = worker.once()
    assert [_entry(report, path).get('imported') for path in (first, second)] == [1, 1]
    originals = {path: path.read_bytes() for path in (first, second)}

    _renumber(first, originals[first].replace(b'jade', b'jadx') + _said('count cedar later', repo))
    _renumber(second, originals[second].replace(b'jade', b'jadx')
              + _said('count cedar later two', repo, session=SECOND))
    refused = worker.once()
    reasons = {}
    for path in (first, second):
        entry = _entry(refused, path)
        assert entry['status'] == 'error'
        reasons[path] = _record(worker, path)['error']
        assert reasons[path] is not None, 'the renumbered prefix refusal is not recorded'
        assert reasons[path] == entry['reason']
        assert 'jade' not in reasons[path] and 'jadx' not in reasons[path] and 'cedar' not in reasons[path]
    reason = reasons[first]
    assert reasons[second] == reason, 'one cause, one fixed reason'
    preview = worker.preview()
    assert all(reason in _strings_for(preview, path) for path in (first, second)), preview
    assert set(_reason_counts(preview, reason)) == {2}, preview

    _renumber(first, originals[first] + _said('count cedar later', repo))
    entry = _entry(worker.once(), first)
    assert entry['status'] == 'captured', entry
    assert _record(worker, first)['error'] is None
    preview = worker.preview()
    assert reason not in _strings_for(preview, first), preview
    assert reason in _strings_for(preview, second), preview
    assert set(_reason_counts(preview, reason)) == {1}, preview

    _renumber(second, originals[second] + _said('count cedar later two', repo, session=SECOND))
    assert _entry(worker.once(), second)['status'] == 'captured'
    preview = worker.preview()
    assert _record(worker, second)['error'] is None
    assert reason not in json.dumps(preview), preview
    assert set(_reason_counts(preview, reason)) <= {0}, preview
    _assert_no_duplicates(store)


def test_p3_refusal_reasons_are_fixed_and_carry_no_transcript_text(tmp_path, capsys):
    store, repo, worker = _setup(tmp_path)
    secrets = ['onyx-session-5521', 'onyx-session-9934', 'onyx-agent-4410', 'onyx-codex-7782',
               'onyx-text-3187', 'onyx-prefix-6620']
    first, second = _claude_path(worker), _claude_path(worker, SECOND)
    child, codex = _child_path(worker), _codex_path(worker)
    prefixed = _claude_path(worker, THIRD)
    first.write_bytes(_said('fixed cedar one', repo))
    second.write_bytes(_said('fixed cedar two', repo, session=SECOND))
    child.write_bytes(_child('fixed cedar child', repo))
    codex.write_bytes(_meta(repo) + _codex_said('fixed cedar codex'))
    prefixed.write_bytes(_said('fixed cedar prefix', repo, session=THIRD))
    assert worker.once()['status'] == 'ok'

    _append(first, _said('onyx-text-3187 first', repo, session='onyx-session-5521'))
    _append(second, _said('onyx-text-3187 second', repo, session='onyx-session-9934'))
    _append(child, _child('onyx-text-3187 child', repo, agent='onyx-agent-4410'))
    _append(codex, _meta(repo, native='onyx-codex-7782') + _codex_said('onyx-text-3187 codex'))
    _renumber(prefixed, prefixed.read_bytes().replace(b'fixed cedar prefix', b'fixed cedar onyx-prefix-6620')
              + _said('onyx-text-3187 later', repo, session=THIRD))
    report = worker.once()
    paths = (first, second, child, codex, prefixed)
    reasons = {}
    for path in paths:
        assert _entry(report, path)['status'] == 'error'
        reasons[path] = _record(worker, path)['error']
        assert reasons[path] is not None, f'refusal not recorded: {path.name}'
    assert reasons[first] == reasons[second], 'one cause, one fixed reason'
    previews = [worker.preview(), _cli_preview(tmp_path, worker, capsys, 'fixed')]
    for preview in previews:
        assert all(reasons[path] in _strings_for(preview, path) for path in paths), preview
    published = json.dumps([report, previews, list(reasons.values())])
    leaked = [secret for secret in secrets if secret in published]
    assert leaked == [], leaked


# ---------------------------------------------------------------------------
# P4: everything else is unchanged
# ---------------------------------------------------------------------------

def _fresh_claude(worker, repo):
    rows = [
        _line({'type': 'file-history-snapshot', 'cwd': str(repo)}),
        _said('fresh cedar question', repo),
        _line({'type': 'assistant', 'sessionId': PARENT, 'cwd': str(repo),
               'message': {'role': 'assistant', 'content': [
                   {'type': 'text', 'text': 'fresh cedar answer'},
                   {'type': 'tool_use', 'id': 'tool-1', 'name': 'Read', 'input': {}}]}}),
        _line({'type': 'user', 'sessionId': PARENT, 'cwd': str(repo),
               'message': {'role': 'user', 'content': [
                   {'type': 'tool_result', 'tool_use_id': 'tool-1', 'content': 'fresh cedar tool'}]}}),
        _said('fresh meta row', repo, isMeta=True),
        _line({'type': 'summary', 'summary': 'fresh summary row'}),
        _line({'type': 'user', 'cwd': str(repo), 'message': {'role': 'user', 'content': 'fresh no session'}}),
        b'{not json\n',
    ]
    trailing = b'{"type":"user","sessionId":"' + PARENT.encode()
    path = _claude_path(worker)
    path.write_bytes(b''.join(rows) + trailing)
    visible = {1: [('user', 'fresh cedar question')],
               2: [('assistant', 'fresh cedar answer')],
               3: [('tool', 'fresh cedar tool')]}
    return path, rows, trailing, visible, PARENT


def _fresh_codex(worker, repo):
    rows = [
        _meta(repo),
        _codex_said('fresh codex question'),
        _line({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
               'content': [{'type': 'output_text', 'text': 'fresh codex answer'}]}}),
        _line({'type': 'response_item', 'payload': {'type': 'reasoning', 'summary': []}}),
        _line({'type': 'turn_context', 'payload': {'cwd': str(repo)}}),
        _codex_said('fresh codex agent', 'agent_message'),
    ]
    path = _codex_path(worker)
    path.write_bytes(b''.join(rows))
    visible = {1: [('user', 'fresh codex question')],
               2: [('assistant', 'fresh codex answer')],
               5: [('assistant', 'fresh codex agent')]}
    return path, rows, b'', visible, CODEX


@pytest.mark.parametrize('runtime', ['claude', 'codex'])
def test_p4_never_seen_source_captures_exactly_as_before(tmp_path, runtime):
    store, repo, worker = _setup(tmp_path)
    path, rows, trailing, visible, native = (_fresh_claude if runtime == 'claude' else _fresh_codex)(worker, repo)
    offsets = [sum(len(row) for row in rows[:index]) for index in range(len(rows))]
    complete = sum(len(row) for row in rows)
    report = worker.once()
    entry = _entry(report, path)
    expected = {
        'claude': dict(status='incomplete', imported=3, omitted_rows=5, quarantined_rows=1,
                       cursor=complete, observed_size=complete + len(trailing),
                       remaining_bytes=len(trailing), partial_trailing_bytes=len(trailing)),
        'codex': dict(status='captured', imported=3, omitted_rows=3, quarantined_rows=0,
                      cursor=complete, observed_size=complete, remaining_bytes=0, partial_trailing_bytes=0),
    }[runtime]
    assert {key: entry.get(key) for key in expected} == expected, entry
    assert (entry['runtime'], entry['repo_key'], entry['index_pending']) == (runtime, 'included', 0)
    assert report['status'] == ('partial' if runtime == 'claude' else 'ok')
    counts = _record(worker, path)['counts']
    assert {key: counts.get(key) for key in ('rows', 'visible_rows', 'omitted_rows', 'quarantined_rows', 'imported')} == {
        'rows': len(rows), 'visible_rows': 3, 'omitted_rows': expected['omitted_rows'],
        'quarantined_rows': expected['quarantined_rows'], 'imported': 3}

    sid = _sid(store, worker, native, runtime)
    episodes = sorted((ref, payload) for session, ref, payload in _episodes(store) if session == sid)
    native_digest = hashlib.sha256(native.encode()).hexdigest()
    assert [ref for ref, _ in episodes] == sorted(
        f'native-jsonl:{runtime}:{native_digest}:{offsets[index]}' for index in visible)
    by_offset = {payload['source_provenance']['source_coordinates']['start']: payload for _, payload in episodes}
    for index, events in visible.items():
        payload = by_offset[offsets[index]]
        assert [(event['role'], event['text']) for event in payload['events']] == events
        provenance = payload['source_provenance']
        assert provenance['row_digest'] == hashlib.sha256(rows[index]).hexdigest()
        assert provenance['source_system'] == f'local:{runtime}-jsonl'
        assert provenance['import_actor'] == 'operator:workspace-capture'
        assert provenance['source_coordinates'] == {'path': str(path), 'start': offsets[index],
                                                    'end': offsets[index] + len(rows[index])}
    assert _projects(store, sid) == [('included',)]


@pytest.mark.parametrize('runtime', ['claude', 'codex'])
def test_p4_sealed_row_conflict_is_still_refused(tmp_path, runtime):
    store, repo, worker = _setup(tmp_path)
    if runtime == 'claude':
        path = _claude_path(worker)
        path.write_bytes(_said('sealed cedar original', repo))
        copy = _claude_path(worker, project='another-project-folder')
    else:
        path = _codex_path(worker)
        path.write_bytes(_meta(repo) + _codex_said('sealed cedar original'))
        copy = _codex_path(worker, day='30')
    assert _entry(worker.once(), path).get('imported') == 1
    copy.write_bytes(path.read_bytes().replace(b'sealed cedar original', b'sealed cedar replaced'))
    for _ in range(2):
        entry = _entry(worker.once(), copy)
        assert entry['status'] == 'error'
        assert SEALED in entry['reason']
    assert _texts(store) == ['sealed cedar original']


def test_p4_row_bound_holds_across_renumbering_and_moves(tmp_path):
    store, repo, worker = _setup(tmp_path)
    (repo / 'sub').mkdir()
    bounded = WorkspaceCapture(store, {**worker.policy, 'max_batch_rows': 2})
    path = _claude_path(worker)
    head = [_said('bound cedar one', repo), _said('bound cedar two', repo, role='assistant')]
    path.write_bytes(b''.join(head))
    assert _entry(bounded.once(), path).get('imported') == 2
    tail = [_said('bound cedar three', repo / 'sub'),
            _said('outside oak bound', tmp_path / 'missing-directory', role='assistant'),
            _said('bound cedar five', repo),
            _said('bound cedar six', repo / 'sub', role='assistant'),
            _said('bound cedar seven', repo)]
    _renumber(path, b''.join(head + tail))
    boundaries = [sum(len(row) for row in (head + tail)[:index]) for index in range(len(head + tail) + 1)]
    cursor, passes = _record(bounded, path)['cursor'], 0
    while cursor < boundaries[-1] and passes < 6:
        entry = _entry(bounded.once(), path)
        assert entry['status'] != 'error', entry
        passes += 1
        advanced = _record(bounded, path)['cursor']
        assert advanced in boundaries
        assert 0 < boundaries.index(advanced) - boundaries.index(cursor) <= 2, (cursor, advanced)
        cursor = advanced
    assert passes == 3
    assert _texts(store) == sorted(f'bound cedar {n}' for n in ('one', 'two', 'three', 'five', 'six', 'seven'))
    _assert_no_duplicates(store)


def test_p4_active_lease_on_a_renumbered_source_is_respected(tmp_path):
    store, repo, worker = _setup(tmp_path)
    path = _claude_path(worker)
    path.write_bytes(_said('lease cedar one', repo))
    assert _entry(worker.once(), path).get('imported') == 1
    _renumber(path, path.read_bytes() + _said('lease cedar two', repo))
    with closing(worker._db()) as db:
        db.execute("UPDATE files SET lease_until=?,lease_owner='another-worker' WHERE path=?",
                   (time.time() + 600, str(path)))
        db.commit()
    assert _entry(worker.once(), path)['status'] == 'worker_active'
    assert _texts(store) == ['lease cedar one']
    assert _record(worker, path)['lease_owner'] == 'another-worker'

    with closing(worker._db()) as db:
        db.execute('UPDATE files SET lease_until=0,lease_owner=NULL WHERE path=?', (str(path),))
        db.commit()
    entry = _entry(worker.once(), path)
    assert entry['status'] == 'captured', entry
    assert entry.get('imported') == 1
    assert _texts(store) == ['lease cedar one', 'lease cedar two']
