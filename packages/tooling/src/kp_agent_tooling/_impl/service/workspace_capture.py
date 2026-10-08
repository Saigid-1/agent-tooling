"""Opt-in, bounded capture of native sessions with verified project membership.

This worker has no admission or model transport. The native file remains the
source of truth; its byte cursor, prefix digest, and pending index IDs are durable.
"""
from __future__ import annotations

from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.leaf import sha256_hex as _sha
from kp_agent_tooling._impl.service.claude_episode_capture import (
    _parts, _bounded_events, ClaudeCaptureConflict,
)
from kp_agent_tooling._impl.service.episodic_memory import EpisodeConflict
from kp_agent_tooling._impl.service.episodic_search import drain_after_seal
from kp_agent_tooling._impl.service.native_history_import import (
    _claude_identity, _codex_events, _codex_identity, MAX_LINE_BYTES,
)
from kp_agent_tooling._impl.service.session_import_job import (
    _digest_prefix, MAX_SKIP_LINE_BYTES,
)
from kp_agent_tooling._impl.service.session_sources import SessionSources


SCHEMA = 'ops.workspace-capture.v1'
_DB = '''
CREATE TABLE IF NOT EXISTS files (
 path TEXT PRIMARY KEY, tenant TEXT NOT NULL, runtime TEXT NOT NULL,
 native_id TEXT NOT NULL, repo_key TEXT NOT NULL, device INTEGER NOT NULL,
 inode INTEGER NOT NULL, cursor INTEGER NOT NULL, prefix_sha256 TEXT NOT NULL,
 index_pending BLOB NOT NULL, counts BLOB NOT NULL, error TEXT,
 lease_until REAL NOT NULL DEFAULT 0, lease_owner TEXT);
CREATE TABLE IF NOT EXISTS inventory (
 path TEXT PRIMARY KEY, device INTEGER NOT NULL, inode INTEGER NOT NULL,
 size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
'''


class CaptureError(ValueError):
    pass


# Refusal reasons are fixed strings: a source's journal error, its `once` report and
# `preview` carry only these, never transcript text, a path or exception detail.
REASON_IDENTITY = 'source identity or project changed'
REASON_PREFIX = 'native source prefix changed or truncated'
REASON_WORKSPACE = 'native workspace identity changed'
REFUSAL_REASONS = (
    REASON_IDENTITY,
    REASON_PREFIX,
    REASON_WORKSPACE,
    'native row exceeds bounded read limit',
    'next native row exceeds batch bound',
    'source changed during batch',
    'source changed before cursor receipt',
    'capture lease ownership changed',
    'sealed native source row changed',
    'source project membership changed',
    'source reference already sealed with different evidence',
    'source session unavailable to this tenant',
    'source session integrity mismatch',
    'source episode unavailable',
    'source episode integrity mismatch',
    'Native source is shorter than its saved cursor.',
)
# Any other failure is reported by kind only.
REASON_STORE = 'capture store error'
REASON_FILE = 'file access error'
REASON_ROW = 'native row rejected'
REASON_OTHER = 'unclassified capture failure'
# `preview` shows free text recorded by an earlier version as this reason.
REASON_LEGACY = 'unclassified legacy error'
_RECORDED_REASONS = frozenset(REFUSAL_REASONS + (REASON_STORE, REASON_FILE, REASON_ROW, REASON_OTHER))
# A row whose cwd is in no approved repository is omitted under this counted name.
CWD_OUTSIDE_POLICY = 'cwd_outside_policy'


def refusal_reason(error):
    """The fixed reason recorded and reported for a source refusal."""
    text = str(error)
    if text in REFUSAL_REASONS:
        return text
    if isinstance(error, sqlite3.Error):
        return REASON_STORE
    if isinstance(error, OSError):
        return REASON_FILE
    if isinstance(error, ClaudeCaptureConflict):
        return REASON_ROW
    return REASON_OTHER


def recorded_reason(text):
    """A journal error as `preview` reports it; unknown text is never echoed."""
    return text if text in _RECORDED_REASONS else REASON_LEGACY


def _read_capture_line(source):
    """Read one complete row with the import job's four-megabyte hard bound."""
    line = source.readline(MAX_SKIP_LINE_BYTES + 1)
    if len(line) > MAX_SKIP_LINE_BYTES:
        raise CaptureError('native row exceeds bounded read limit')
    return line, len(line), line.endswith(b'\n')


def _git_common(path):
    try:
        result = subprocess.run(
            ['git', '-C', str(path), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
            check=True, capture_output=True, text=True, timeout=5,
            env={key: value for key, value in os.environ.items() if not key.startswith('GIT_')},
        )
        return str(Path(result.stdout.strip()).resolve(strict=True))
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def validate_policy(policy):
    required = {'schema_version', 'approval_record', 'approval_sha256', 'tenant_id', 'approved_repo_keys', 'repos', 'native_roots',
                'excluded_sessions', 'max_candidates', 'max_batch_bytes', 'max_batch_rows'}
    if not isinstance(policy, dict) or set(policy) != required or policy['schema_version'] != SCHEMA:
        raise CaptureError('exact workspace capture policy required')
    approval_path = Path(policy['approval_record'])
    if not approval_path.is_absolute() or approval_path.is_symlink() or not approval_path.is_file() or approval_path.stat().st_size > 100_000:
        raise CaptureError('approved scope record unavailable')
    raw = approval_path.read_bytes()
    if (not isinstance(policy['approval_sha256'], str) or
            _sha(raw) != policy['approval_sha256']):
        raise CaptureError('approved scope record digest changed')
    approval = json.loads(raw)
    if policy['tenant_id'] != approval['tenant_id'] or policy['approved_repo_keys'] != approval['repositories']:
        raise CaptureError('capture policy differs from the approved tenant or repository snapshot')
    keys, repos = policy['approved_repo_keys'], policy['repos']
    if (not isinstance(keys, list) or not 1 <= len(keys) <= 1000 or
            len(set(keys)) != len(keys) or not all(isinstance(k, str) and k for k in keys) or
            not isinstance(repos, dict) or set(repos) != set(keys)):
        raise CaptureError('policy must pin exact approved repository keys')
    common = {}
    for key, values in repos.items():
        if not isinstance(values, list) or len(values) > 32:
            raise CaptureError(f'bounded repository roots required: {key}')
        for value in values:
            root = Path(value)
            if not root.is_absolute() or not root.is_dir():
                raise CaptureError(f'repository root unavailable: {key}')
            git_dir = _git_common(root)
            if git_dir is None or (git_dir in common and common[git_dir] != key):
                raise CaptureError(f'repository Git identity missing or ambiguous: {key}')
            common[git_dir] = key
    roots = policy['native_roots']
    if not isinstance(roots, dict) or set(roots) != {'claude', 'codex'}:
        raise CaptureError('exact Claude and Codex native roots required')
    for root in roots.values():
        p = Path(root)
        if not p.is_absolute() or not p.is_dir() or p.is_symlink():
            raise CaptureError('native root unavailable; no local fallback')
    excluded = policy['excluded_sessions']
    if not isinstance(excluded, dict) or set(excluded) != {'claude', 'codex'} or any(
            not isinstance(v, list) or len(v) > 1000 or not all(isinstance(i, str) for i in v)
            for v in excluded.values()):
        raise CaptureError('exact live-hook exclusion lists required')
    for name, lo, hi in (('max_candidates', 1, 256), ('max_batch_bytes', 512_000, 8_000_000),
                         ('max_batch_rows', 1, 2000)):
        value = policy[name]
        if type(value) is not int or not lo <= value <= hi:
            raise CaptureError(f'{name} is outside its bound')
    return common


def _entries(folder, *, cap=4096):
    if folder.is_symlink():
        return []
    with os.scandir(folder) as scan:
        rows = []
        for row in scan:
            rows.append(row)
            if len(rows) > cap:
                raise CaptureError(f'native directory entry bound exceeded: {folder}')
    return sorted(rows, key=lambda row: row.name)


def _candidates(policy):
    """Enumerate native layouts with fixed depth and directory bounds."""
    result = []
    claude = Path(policy['native_roots']['claude'])
    for project in _entries(claude):
        if not project.is_dir(follow_symlinks=False):
            continue
        for file in _entries(Path(project.path)):
            if file.is_file(follow_symlinks=False) and file.name.endswith('.jsonl'):
                result.append(('claude', Path(file.path)))
            elif file.is_dir(follow_symlinks=False):
                subagents = Path(file.path) / 'subagents'
                if subagents.is_dir() and not subagents.is_symlink():
                    for child in _entries(subagents):
                        if child.is_file(follow_symlinks=False) and child.name.endswith('.jsonl'):
                            result.append(('claude', Path(child.path)))
    codex = Path(policy['native_roots']['codex'])
    for year in _entries(codex):
        if not year.is_dir(follow_symlinks=False):
            continue
        for month in _entries(Path(year.path)):
            if not month.is_dir(follow_symlinks=False):
                continue
            for day in _entries(Path(month.path)):
                if not day.is_dir(follow_symlinks=False):
                    continue
                for file in _entries(Path(day.path)):
                    if file.is_file(follow_symlinks=False) and file.name.endswith('.jsonl'):
                        result.append(('codex', Path(file.path)))
    return sorted(result, key=lambda item: str(item[1]))


def _metadata(runtime, path, native_id):
    """Read only a bounded prefix for native workspace evidence."""
    with path.open('rb') as source:
        for index in range(32):
            line = source.readline(MAX_LINE_BYTES + 1)
            if not line or not line.endswith(b'\n') or len(line) > MAX_LINE_BYTES:
                break
            try:
                row = json.loads(line)
            except (UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(row, dict):
                continue
            if runtime == 'codex' and index == 0:
                payload = row.get('payload')
                if (row.get('type') == 'session_meta' and isinstance(payload, dict)
                        and str(payload.get('id', '')).lower() == native_id
                        and isinstance(payload.get('cwd'), str)):
                    return payload['cwd']
                break
            parent = native_id.split('/subagents/', 1)[0]
            if runtime == 'claude' and isinstance(row.get('sessionId'), str) and row['sessionId'].lower() == parent:
                cwd = row.get('cwd')
                if isinstance(cwd, str) and cwd:
                    return cwd
    return None


class WorkspaceCapture:
    def __init__(self, store, policy):
        self.store, self.policy = store, policy
        self.common = validate_policy(policy)
        self.sources = SessionSources(store)
        self.path = leaf.store_path(store.path, leaf.WORKSPACE_CAPTURE_DB, sibling=True)

    def initialize(self):
        # Only the catalog schema capture's own writes need. Installing, rebuilding
        # or backfilling the scope projection, and the whole-store PRAGMA
        # quick_check, are the operator's `kp-agent-desk ... upgrade-sources`; a
        # pass never runs them. What the writers do on a store whose projection is
        # absent, incomplete or complete: SessionSources.ensure_catalog.
        self.sources.ensure_catalog()
        with closing(self._db(create=True)) as db:
            db.executescript(_DB)
            digest = leaf.sorted_sha256(self.policy)
            old = db.execute("SELECT value FROM state WHERE key='policy_sha256'").fetchone()
            if old and old[0] != digest:
                db.execute('DELETE FROM inventory')
            db.execute("INSERT OR REPLACE INTO state VALUES ('policy_sha256',?)", (digest,))
            db.commit()

    def _db(self, *, create=False):
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise CaptureError('capture journal unavailable')
        if not self.path.exists() and not create:
            db = leaf.sqlite_connect(':memory:')
            db.executescript(_DB)
            return db
        if not self.path.exists():
            # Created 0600 before SQLite opens it (T11b Q3); a concurrent creator wins the race.
            try:
                return leaf.sqlite_create(self.path, timeout=10)
            except FileExistsError:
                pass
        return leaf.sqlite_connect(self.path, mode='rwc', resolve=True, timeout=10)

    def _membership(self, cwd):
        if not cwd or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            return None, 'workspace_unavailable', None
        common = _git_common(Path(cwd))
        if common is None:
            return None, 'workspace_git_unavailable', None
        key = self.common.get(common)
        return key, 'eligible' if key else 'outside_scope', common

    def _approved_cwd(self, cwd, members):
        """Whether a moved row's cwd is in any approved repository, by the discovery rule.

        `members` caches the answer per distinct cwd for one batch. A cwd that is not
        a non-empty string, or cannot be resolved, is in no approved repository.
        """
        if not isinstance(cwd, str) or not cwd:
            return False
        if cwd not in members:
            try:
                members[cwd] = self._membership(cwd)[0] is not None
            except (OSError, ValueError):
                members[cwd] = False
        return members[cwd]

    def _inspect(self, runtime, path):
        try:
            native = (_codex_identity(path) if runtime == 'codex' else _claude_identity(path)[1])
        except ValueError:
            return None, 'native_identity_unavailable'
        if native in self.policy['excluded_sessions'][runtime] or native.split('/subagents/', 1)[0] in self.policy['excluded_sessions'][runtime]:
            return None, 'managed_by_exact_hook'
        cwd = _metadata(runtime, path, native)
        key, reason, common = self._membership(cwd)
        if key is None:
            return None, reason if cwd else 'workspace_evidence_missing'
        return {'runtime': runtime, 'path': str(path), 'native_id': native,
                'repo_key': key, 'cwd': cwd, 'git_common_dir': common}, 'eligible'

    def preview(self):
        return self._run(apply=False)

    def once(self):
        self.initialize()
        lock_path = self.path.with_name('workspace-capture.lock')
        if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
            raise CaptureError('capture lock unavailable')
        with leaf.open_fd_stream(lock_path, 'rb', access='rw', create=True) as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise CaptureError('another workspace capture worker is active') from error
            try:
                result = self._run(apply=True)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        if any(item.get('imported') for item in result['files']):
            # T12b: the seals' outbox rows carry the indexing. A host install drains once after the
            # pass, never waiting; inside Compose (the capture role) the indexer role drains.
            drain_after_seal(self.store)
        return result

    def _run(self, *, apply):
        candidates = _candidates(self.policy)
        selected, gaps = [], []
        with closing(self._db()) as db:
            position = int((db.execute("SELECT value FROM state WHERE key='next_position'").fetchone() or ('0',))[0])
            known = {row[0]: row[1:] for row in db.execute(
                'SELECT path,device,inode,size,mtime_ns,result FROM inventory')}
            changed = []
            for index, (_, path) in enumerate(candidates):
                previous = known.get(str(path))
                stat = path.stat()
                if previous is None or previous[:4] != (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns):
                    changed.append((stat.st_mtime_ns, index))
            changed.sort(reverse=True)
            quota = self.policy['max_candidates']
            priority = [index for _, index in changed[:max(1, quota // 2)]]
            fair = []
            fair_budget = quota - len(priority)
            if fair_budget:
                for step in range(len(candidates)):
                    index = (position + step) % len(candidates)
                    if index not in priority:
                        fair.append(index)
                    if len(fair) >= fair_budget:
                        break
            for index in priority + fair:
                runtime, path = candidates[index]
                stat = path.stat()
                fingerprint = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                prior = known.get(str(path))
                if prior and prior[:4] == fingerprint:
                    completed = db.execute('SELECT cursor,index_pending,error,counts FROM files WHERE path=?',
                                           (str(path),)).fetchone()
                    if (completed and completed[0] == stat.st_size and completed[1] == '[]' and
                            completed[2] is None and json.loads(completed[3]).get('quarantined_rows', 0) == 0 and
                            self._inspect(runtime, path)[1] == 'eligible'):
                        selected.append({'runtime': runtime, 'path': str(path), 'status': 'unchanged_complete'})
                        continue
                info, reason = self._inspect(runtime, path)
                if info:
                    selected.append(info)
                else:
                    gaps.append({'source_file': str(path), 'reason': reason})
                if apply:
                    db.execute('INSERT OR REPLACE INTO inventory VALUES (?,?,?,?,?,?)',
                               (str(path), *fingerprint, reason))
            if apply and candidates:
                db.execute("INSERT OR REPLACE INTO state VALUES ('next_position',?)",
                           (str((position + max(1, len(fair))) % len(candidates)),))
                db.commit()
            tracked = None if apply else db.execute('SELECT path,error FROM files ORDER BY path').fetchall()
        reports = []
        if apply:
            for item in selected:
                if item.get('status') == 'unchanged_complete':
                    reports.append({'source_file': item['path'], 'status': 'unchanged_complete'})
                else:
                    try:
                        reports.append(self._capture(item))
                    except Exception as error:
                        reports.append({'source_file': item['path'], 'status': 'error',
                                        'reason': refusal_reason(error)})
        else:
            reports = [
            {'source_file': item['path'], 'runtime': item['runtime'],
             'repo_key': item.get('repo_key'), 'status': item.get('status', 'eligible')} for item in selected]
        partial = any(item['status'] in ('error', 'incomplete') for item in reports)
        result = {'schema_version': SCHEMA, 'status': 'partial' if partial else 'ok', 'tenant_id': self.policy['tenant_id'],
                'candidate_count': len(candidates), 'inspected_count': len(selected) + len(gaps),
                'remaining_candidates': max(0, len(candidates) - len(selected) - len(gaps)),
                'files': reports, 'gaps': gaps,
                'selected_repos_without_roots': sorted(k for k, v in self.policy['repos'].items() if not v),
                'authority': 'verified local workspace evidence; no desk admission or outbound request'}
        if tracked is not None:
            # Every tracked source's current refusal, not only this pass's candidates.
            refusals = [{'source_file': path, 'reason': recorded_reason(error)}
                        for path, error in tracked if error is not None]
            counts = {}
            for refusal in refusals:
                counts[refusal['reason']] = counts.get(refusal['reason'], 0) + 1
            result.update(tracked_sources=len(tracked), refusals=refusals,
                          refusal_counts=dict(sorted(counts.items())))
        return result

    def _record_refusal(self, path, error):
        """Write a refusal raised before the lease as a tracked source's current error."""
        try:
            with closing(self._db()) as db:
                db.execute('UPDATE files SET error=? WHERE path=?', (refusal_reason(error), str(path)))
                db.commit()
        except (sqlite3.Error, OSError):
            pass  # an unwritable journal cannot hold the reason; the report still carries it

    def _capture(self, info):
        path = Path(info['path'])
        tenant = self.policy['tenant_id']
        owner = os.urandom(16).hex()
        try:
            stat = path.stat()
            with closing(self._db()) as db:
                db.execute('BEGIN IMMEDIATE')
                prior = db.execute('SELECT tenant,runtime,native_id,repo_key,device,inode,cursor,prefix_sha256,index_pending,counts,lease_until FROM files WHERE path=?',
                                   (str(path),)).fetchone()
                if prior:
                    # (device, inode) is a hint only: a recreated container renumbers the
                    # same host file. The prefix digest re-verified under the lease below is
                    # the integrity check; the record adopts the current pair on its receipt.
                    if prior[:4] != (tenant, info['runtime'], info['native_id'], info['repo_key']):
                        raise CaptureError(REASON_IDENTITY)
                    if prior[10] > time.time():
                        return {'source_file': str(path), 'status': 'worker_active'}
                    cursor, prefix, counts = prior[6], prior[7], json.loads(prior[9])
                else:
                    cursor, prefix, counts = 0, _sha(b''), {}
                    db.execute('INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,0,NULL)',
                               (str(path), tenant, info['runtime'], info['native_id'], info['repo_key'],
                                stat.st_dev, stat.st_ino, cursor, prefix, '[]', '{}'))
                db.execute('UPDATE files SET lease_until=?,lease_owner=? WHERE path=?',
                           (time.time() + 120, owner, str(path)))
                db.commit()
        except Exception as error:
            self._record_refusal(path, error)
            raise
        try:
            if stat.st_size < cursor or _digest_prefix(path, cursor) != prefix:
                raise CaptureError(REASON_PREFIX)
            snapshot_sha = _digest_prefix(path, stat.st_size)
            session = self.sources.register(tenant_id=tenant, runtime=info['runtime'], native_id=info['native_id'])
            self.sources.record_project(session_id=session, tenant_id=tenant, repo_key=info['repo_key'],
                evidence={'cwd': info['cwd'], 'git_common_dir': info['git_common_dir'], 'source_file': str(path)})
            staged, offset, partial, omitted, quarantined = [], cursor, 0, 0, 0
            outside, members = 0, {}
            with path.open('rb') as source:
                source.seek(cursor)
                for _ in range(self.policy['max_batch_rows']):
                    if offset >= stat.st_size or offset - cursor >= self.policy['max_batch_bytes']:
                        break
                    line, length, complete = _read_capture_line(source)
                    if not length:
                        break
                    if not complete or offset + length > stat.st_size:
                        partial = stat.st_size - offset
                        break
                    if offset > cursor and offset + length - cursor > self.policy['max_batch_bytes']:
                        break
                    if length > MAX_SKIP_LINE_BYTES:
                        raise CaptureError('native row exceeds bounded read limit')
                    try:
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError
                        if info['runtime'] == 'codex':
                            if row.get('type') == 'session_meta':
                                payload = row.get('payload')
                                # A moved cwd continues inside any approved repository; any
                                # other cwd stays a refusal (see docs/workspace-capture.md).
                                if (not isinstance(payload, dict) or
                                        str(payload.get('id', '')).lower() != info['native_id'] or
                                        (payload.get('cwd') != info['cwd'] and
                                         not self._approved_cwd(payload.get('cwd'), members))):
                                    raise CaptureError(REASON_WORKSPACE)
                            events, omissions = _codex_events(row, offset)
                            events = _bounded_events(events)
                        else:
                            header = row.get('sessionId')
                            parent = info['native_id'].split('/subagents/', 1)[0]
                            child = info['native_id'].split('/subagents/agent-', 1)[1] if '/subagents/agent-' in info['native_id'] else None
                            if ((header is not None and (not isinstance(header, str) or header.lower() != parent)) or
                                    (child is not None and row.get('agentId') not in (None, child))):
                                raise CaptureError(REASON_WORKSPACE)
                            moved = row.get('cwd', info['cwd'])
                            if moved != info['cwd'] and not self._approved_cwd(moved, members):
                                events, omissions = [], [CWD_OUTSIDE_POLICY]
                                outside += 1
                            elif row.get('type') in ('user', 'assistant') and header is None:
                                events, omissions = [], ['missing_native_session_identity']
                            else:
                                events, omissions = _parts(line, offset, parent,
                                                           allow_sidechain=child is not None)
                    except CaptureError:
                        raise
                    except (UnicodeError, json.JSONDecodeError, ValueError):
                        events, omissions = [], ['malformed_native_row']
                        quarantined += 1
                    if events and len(events) <= 125 and all(len(e['text'].encode()) <= 128000 for e in events):
                        staged.append((offset, line, row, events, omissions))
                    else:
                        if events:
                            quarantined += 1
                        omitted += 1
                    offset += length
            if offset == cursor and stat.st_size > cursor and not partial:
                raise CaptureError('next native row exceeds batch bound')
            after = path.stat()
            if ((after.st_dev, after.st_ino) != (stat.st_dev, stat.st_ino) or
                    after.st_size < stat.st_size or _digest_prefix(path, stat.st_size) != snapshot_sha):
                raise CaptureError('source changed during batch')
            verified_cursor_sha = _digest_prefix(path, offset)
            ids, imported = [], 0
            for start, line, row, events, omissions in staged:
                ref = f"native-jsonl:{info['runtime']}:{_sha(info['native_id'].encode())}:{start}"
                provenance = {'source_system': f"local:{info['runtime']}-jsonl",
                    'row_id': f'{path}:{start}:{start + len(line)}', 'row_digest': _sha(line),
                    'source_coordinates': {'path': str(path), 'start': start, 'end': start + len(line)},
                    'evidence_event_map': [{'event_start': 0, 'event_count': len(events),
                        'authored_at': row.get('timestamp', 'unknown'), 'row_sha256': _sha(line)}],
                    'omissions': omissions, 'import_actor': 'operator:workspace-capture'}
                with closing(self.store._connect()) as source_db:
                    old = source_db.execute('SELECT id FROM source_episodes WHERE session_id=? AND source_ref=?',
                                            (session, ref)).fetchone()
                if old:
                    sealed = self.sources.read(old[0])
                    if sealed['source_provenance']['row_digest'] != _sha(line) or sealed['events'] != events:
                        raise EpisodeConflict('sealed native source row changed')
                    ids.append(old[0])
                else:
                    receipt = self.sources.import_episode(session_id=session, source_ref=ref,
                                                         events=events, provenance=provenance)
                    ids.append(receipt['episode_id'])
                    imported += 1
            counts['rows'] = counts.get('rows', 0) + len(staged) + omitted
            counts['visible_rows'] = counts.get('visible_rows', 0) + len(staged)
            counts['omitted_rows'] = counts.get('omitted_rows', 0) + omitted
            counts['quarantined_rows'] = counts.get('quarantined_rows', 0) + quarantined
            counts['imported'] = counts.get('imported', 0) + imported
            if outside:
                named = dict(counts.get('omissions', {}))
                named[CWD_OUTSIDE_POLICY] = named.get(CWD_OUTSIDE_POLICY, 0) + outside
                counts['omissions'] = named
            final_stat = path.stat()
            if ((final_stat.st_dev, final_stat.st_ino) != (stat.st_dev, stat.st_ino) or
                    final_stat.st_size < stat.st_size or
                    _digest_prefix(path, stat.st_size) != snapshot_sha):
                raise CaptureError('source changed before cursor receipt')
            with closing(self._db()) as db:
                if db.execute('SELECT lease_owner FROM files WHERE path=?', (str(path),)).fetchone() != (owner,):
                    raise CaptureError('capture lease ownership changed')
                # index_pending is retired (T12b): the seals' outbox rows carry the indexing.
                db.execute('UPDATE files SET device=?,inode=?,cursor=?,prefix_sha256=?,index_pending=?,counts=?,error=NULL WHERE path=? AND lease_owner=?',
                           (stat.st_dev, stat.st_ino, offset, verified_cursor_sha, '[]', json.dumps(counts),
                            str(path), owner))
                db.commit()
            complete = offset == stat.st_size and not partial and counts['quarantined_rows'] == 0
            report = {'source_file': str(path), 'status': 'captured' if complete else 'incomplete', 'repo_key': info['repo_key'],
                    'runtime': info['runtime'], 'source_session_id': session,
                    'cursor': offset, 'observed_size': stat.st_size,
                    'remaining_bytes': stat.st_size - offset, 'partial_trailing_bytes': partial,
                    'imported': imported, 'omitted_rows': omitted,
                    'quarantined_rows': counts['quarantined_rows'], 'index_pending': 0}
            if outside:
                report['omissions'] = {CWD_OUTSIDE_POLICY: outside}
            return report
        except Exception as error:
            with closing(self._db()) as db:
                db.execute('UPDATE files SET error=? WHERE path=? AND lease_owner=?',
                           (refusal_reason(error), str(path), owner))
                db.commit()
            raise
        finally:
            with closing(self._db()) as db:
                db.execute('UPDATE files SET lease_until=0,lease_owner=NULL WHERE path=? AND lease_owner=?',
                           (str(path), owner))
                db.commit()
