"""Bounded operator import of visible Claude and Codex JSONL history.

This converter does not assign desks, admit sessions, or summarize text.  A
preview reads only explicitly selected files (or one bounded project/date
directory); apply writes through SessionSources and refreshes its search index.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
from contextlib import closing

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.leaf import sha256_hex as _sha
from kp_agent_tooling._impl.service.claude_episode_capture import _parts, ClaudeCaptureConflict
from kp_agent_tooling._impl.service.episodic_memory import EpisodeConflict
from kp_agent_tooling._impl.service.episodic_search import drain_after_seal, reindex_counts, request_reindex
from kp_agent_tooling._impl.service.session_sources import SessionSources


MAX_FILES = 32
MAX_DIRECTORY_ENTRIES = 256
MAX_FILE_BYTES = 8_000_000
MAX_TOTAL_BYTES = 32_000_000
MAX_LINE_BYTES = 256_000
MAX_ROWS = 20_000
_UUID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
_AGENT = re.compile(r'^agent-([a-zA-Z0-9_-]{1,128})$')


def _regular(path):
    return leaf.checked_regular_file(path, suffix='.jsonl', max_bytes=MAX_FILE_BYTES,
                                     refused='absolute regular JSONL file required; symlinks refused',
                                     too_large='native history file exceeds eight-megabyte bound')


def _files(*, files, source_folder, project, day):
    if (files is None) == (source_folder is None):
        raise ValueError('select exact files or one source folder')
    if files is not None:
        if bool(project) != bool(day):
            raise ValueError('exact-file project and date filters must be supplied together')
        if not isinstance(files, (list, tuple)) or not 1 <= len(files) <= MAX_FILES:
            raise ValueError('select 1..32 exact files')
        chosen = [_regular(p) for p in files]
        if len({str(p.resolve()) for p in chosen}) != len(chosen):
            raise ValueError('duplicate source file')
    else:
        folder = Path(source_folder)
        if not folder.is_absolute() or folder.is_symlink() or not folder.is_dir():
            raise ValueError('absolute regular source folder required')
        if not project or not day:
            raise ValueError('folder selection requires project and date')
        date.fromisoformat(day)
        entries = []
        with os.scandir(folder) as iterator:
            for entry in iterator:
                entries.append(entry)
                if len(entries) > MAX_DIRECTORY_ENTRIES:
                    raise ValueError('source folder entry bound exceeded; select exact files')
        # A folder is deliberately shallow. Native subagents need exact file
        # selection so a recursive walk cannot silently sweep all projects.
        chosen = sorted((_regular(Path(entry.path)) for entry in entries
                         if entry.name.endswith('.jsonl')), key=str)
        if len(chosen) > MAX_FILES:
            raise ValueError('source folder file bound exceeded; select exact files')
    if sum(p.stat().st_size for p in chosen) > MAX_TOTAL_BYTES:
        raise ValueError('native history selection exceeds total byte bound')
    return chosen


def _claude_identity(path):
    stem = path.stem
    agent = _AGENT.fullmatch(stem)
    if agent:
        # Native Claude subagent path: <parent-session>/subagents/agent-<id>.jsonl.
        if path.parent.name != 'subagents' or not _UUID.fullmatch(path.parent.parent.name):
            raise ValueError('subagent path lacks native parent identity')
        parent = path.parent.parent.name.lower()
        return parent, f'{parent}/subagents/agent-{agent.group(1)}', agent.group(1)
    if not _UUID.fullmatch(stem):
        raise ValueError('Claude filename lacks native session identity')
    return stem.lower(), stem.lower(), None


def _codex_identity(path):
    match = re.search(r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$', path.stem)
    if not match:
        raise ValueError('Codex filename lacks native session identity')
    return match.group(1).lower()


def _timestamp_day(row):
    value = row.get('timestamp')
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).date().isoformat()
    except ValueError:
        return None


def _codex_events(row, offset):
    kind, payload = row.get('type'), row.get('payload')
    if not isinstance(payload, dict):
        return [], ['unsupported_payload']
    if kind == 'event_msg' and payload.get('type') in ('user_message', 'agent_message'):
        text = payload.get('message')
        if isinstance(text, str) and text:
            role = 'user' if payload['type'] == 'user_message' else 'assistant'
            return [dict(event_id=f'byte-{offset}-part-0', role=role, text=text)], []
        return [], ['unsupported_event_message']
    if kind != 'response_item' or payload.get('type') != 'message':
        return [], ['control_or_metadata']
    role = payload.get('role')
    if role not in ('user', 'assistant') or payload.get('phase') == 'analysis':
        return [], ['native_or_control_message']
    blocks = payload.get('content')
    if not isinstance(blocks, list):
        return [], ['unsupported_message_shape']
    events, omitted = [], []
    for i, block in enumerate(blocks):
        if isinstance(block, dict) and block.get('type') in ('input_text', 'output_text') and isinstance(block.get('text'), str) and block['text']:
            events.append(dict(event_id=f'byte-{offset}-part-{i}', role=role, text=block['text']))
        else:
            omitted.append(f'part-{i}:unsupported_or_empty')
    return events, omitted


def _existing(catalog, session_id, source_ref):
    with closing(catalog.store._connect()) as db:
        row = db.execute('SELECT id FROM source_episodes WHERE session_id=? AND source_ref=?',
                         (session_id, source_ref)).fetchone()
    return catalog.read(row[0]) if row else None


def _read_bounded(path):
    with path.open('rb') as source:
        data = source.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError('native history file exceeds eight-megabyte bound')
    return data


def import_native_history(*, store, tenant_id, runtime, files=None, source_folder=None,
                          project=None, day=None, apply=False, import_actor='operator:native-history',
                          max_rows=MAX_ROWS, cursors=None):
    """Preview or import selected files; rejected rows remain visible in the report.

    Replaying a source_ref verifies its row digest. A replaced/truncated file at
    the same coordinate cannot overwrite a sealed episode. The report's per-file
    next_offset is a resumable cursor for complete rows; replay from zero is safe.
    """
    if runtime not in ('claude', 'codex'):
        raise ValueError('runtime must be claude or codex')
    if not isinstance(tenant_id, str) or not tenant_id or len(tenant_id) > 512:
        raise ValueError('bounded tenant identity required')
    if not isinstance(import_actor, str) or not import_actor or len(import_actor) > 512:
        raise ValueError('bounded import actor required')
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_ROWS:
        raise ValueError('bounded row count required')
    if day is not None:
        date.fromisoformat(day)
    if source_folder is not None and (not project or not day):
        raise ValueError('folder selection requires project and date')
    if source_folder is not None and runtime == 'claude' and Path(source_folder).name != project:
        raise ValueError('Claude project must equal selected native project folder name')
    selected = _files(files=files, source_folder=source_folder, project=project, day=day)
    if cursors is None:
        cursors = {}
    if not isinstance(cursors, dict) or set(cursors) - {str(path) for path in selected}:
        raise ValueError('cursor keys must be selected source files')
    catalog = SessionSources(store)
    # Preview requires an initialized store but must not upgrade or mutate it.
    with closing(store._connect()) as db:
        if not catalog.available(db):
            raise ValueError('session source schema unavailable; run upgrade-sources')
    counts = Counter()
    reports = []
    staged = []
    snapshots = {}
    for path in selected:
        original = path.stat()
        data = _read_bounded(path)
        after_read = path.stat()
        if (len(data) != original.st_size or
                (after_read.st_dev, after_read.st_ino, after_read.st_size, after_read.st_mtime_ns) !=
                (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns)):
            raise ValueError('native source changed while reading')
        file_sha = _sha(data)
        cursor = cursors.get(str(path), {'offset': 0, 'prefix_sha256': _sha(b'')})
        if not isinstance(cursor, dict) or set(cursor) != {'offset', 'prefix_sha256'}:
            raise ValueError('cursor requires offset and prefix_sha256')
        resume_offset = cursor['offset']
        if (type(resume_offset) is not int or not 0 <= resume_offset <= len(data) or
                (resume_offset > 0 and data[resume_offset-1:resume_offset] != b'\n') or
                cursor['prefix_sha256'] != _sha(data[:resume_offset])):
            raise ValueError('source cursor prefix changed or offset is not a line boundary')
        snapshots[path] = (original.st_dev, original.st_ino, original.st_size,
                           original.st_mtime_ns, file_sha)
        file_report = {'file': str(path), 'sha256': file_sha, 'bytes': len(data),
                       'next_offset': resume_offset, 'partial_trailing_bytes': 0, 'rejections': []}
        reports.append(file_report)
        possible_mirrors = set()
        if runtime == 'codex':
            probe_offset = 0
            for probe_line in data.splitlines(keepends=True):
                if not probe_line.endswith(b'\n') or len(probe_line) > MAX_LINE_BYTES:
                    break
                try:
                    probe = json.loads(probe_line)
                except (UnicodeError, json.JSONDecodeError):
                    probe_offset += len(probe_line)
                    continue
                if isinstance(probe, dict) and probe.get('type') == 'response_item':
                    probe_events, _ = _codex_events(probe, probe_offset)
                    possible_mirrors.update((e['role'], e['text']) for e in probe_events)
                probe_offset += len(probe_line)
        try:
            parent_id, native_id, agent_id = _claude_identity(path) if runtime == 'claude' else (None, _codex_identity(path), None)
        except ValueError as exc:
            file_report['rejections'].append({'offset': 0, 'reason': str(exc)})
            counts['quarantined'] += 1
            continue
        if runtime == 'claude' and project is not None:
            native_project = path.parent.parent.parent.name if agent_id is not None else path.parent.name
            if native_project != project:
                file_report['rejections'].append({'offset': 0, 'reason': 'project_mismatch'})
                counts['quarantined'] += 1
                continue
        codex_meta = None
        codex_parent = None
        codex_tainted = False
        offset = 0
        for line in data.splitlines(keepends=True):
            if offset < resume_offset:
                if runtime == 'codex' and line.endswith(b'\n'):
                    try:
                        prefix_row = json.loads(line)
                    except (UnicodeError, json.JSONDecodeError):
                        prefix_row = None
                    if isinstance(prefix_row, dict) and prefix_row.get('type') == 'session_meta':
                        payload = prefix_row.get('payload')
                        if (isinstance(payload, dict) and str(payload.get('id', '')).lower() == native_id
                                and isinstance(payload.get('cwd'), str) and payload['cwd']):
                            codex_meta = payload
                            inherited = payload.get('session_id')
                            codex_parent = payload.get('parent_thread_id') or (
                                inherited if inherited != native_id else None)
                            codex_tainted = False
                        else:
                            codex_tainted = True
                offset += len(line)
                continue
            if offset + len(line) > len(data) or not line.endswith(b'\n'):
                file_report['partial_trailing_bytes'] = len(data) - offset
                counts['partial_trailing_lines'] += 1
                break
            if len(line) > MAX_LINE_BYTES:
                file_report['rejections'].append({'offset': offset, 'reason': 'oversized_line'})
                counts['quarantined'] += 1
                offset += len(line); file_report['next_offset'] = offset
                continue
            counts['complete_rows'] += 1
            if counts['complete_rows'] > max_rows:
                raise ValueError('native history row bound exceeded')
            try:
                row = json.loads(line.decode('utf-8', 'strict'))
                if not isinstance(row, dict):
                    raise ValueError('non_object')
            except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
                file_report['rejections'].append({'offset': offset, 'reason': 'malformed_json_or_utf8'})
                counts['quarantined'] += 1
                offset += len(line); file_report['next_offset'] = offset
                continue
            reason = None
            events = []
            omissions = []
            if runtime == 'claude':
                header = row.get('sessionId')
                if not isinstance(header, str) or header.lower() != parent_id:
                    reason = 'missing_or_conflicting_parent_session_identity'
                elif agent_id is not None and row.get('agentId') not in (None, agent_id):
                    reason = 'conflicting_child_identity'
                else:
                    try:
                        events, omissions = _parts(line, offset, parent_id)
                    except ClaudeCaptureConflict:
                        reason = 'invalid_claude_row'
            else:
                if row.get('type') == 'session_meta':
                    payload = row.get('payload')
                    if not isinstance(payload, dict) or str(payload.get('id', '')).lower() != native_id or not isinstance(payload.get('cwd'), str) or not payload['cwd']:
                        reason = 'missing_or_conflicting_session_metadata'
                        codex_tainted = True
                    elif codex_meta is not None and codex_meta != payload:
                        reason = 'conflicting_session_metadata'
                        codex_tainted = True
                    else:
                        inherited = payload.get('session_id')
                        stated_parent = payload.get('parent_thread_id')
                        if inherited is not None and (not isinstance(inherited, str) or not inherited):
                            reason = 'invalid_inherited_session_identity'
                        elif stated_parent is not None and (not isinstance(stated_parent, str) or not stated_parent):
                            reason = 'invalid_parent_thread_identity'
                        elif (inherited and inherited != native_id and stated_parent and
                              inherited != stated_parent):
                            reason = 'conflicting_parent_thread_identity'
                        else:
                            codex_meta = payload
                            codex_parent = stated_parent or (inherited if inherited != native_id else None)
                            codex_tainted = False
                            if project is not None and payload['cwd'] != project:
                                reason = 'project_mismatch'
                    if reason:
                        codex_tainted = True
                elif codex_meta is None:
                    reason = 'missing_session_metadata'
                elif codex_tainted:
                    reason = 'ambiguous_replayed_session_context'
                elif project is not None and codex_meta['cwd'] != project:
                    reason = 'project_mismatch'
                else:
                    events, omissions = _codex_events(row, offset)
            if reason:
                file_report['rejections'].append({'offset': offset, 'reason': reason})
                counts['quarantined'] += 1
            elif day is not None and _timestamp_day(row) != day:
                counts['outside_date'] += 1
            elif events:
                if len(events) > 125 or any(len(e['text'].encode('utf-8')) > 128000 for e in events):
                    file_report['rejections'].append({'offset': offset, 'reason': 'event_bound_exceeded'})
                    counts['quarantined'] += 1
                else:
                    # Host session plus byte coordinate survives a relocated
                    # or copied rollout; a different row at that coordinate
                    # is a conflict, not a replacement of sealed evidence.
                    session_key = _sha(native_id.encode())
                    source_ref = f'native-jsonl:{runtime}:{session_key}:{offset}'
                    row_sha = _sha(line)
                    # Session identity uses the host's native parent/child ID; the
                    # copied parent header is validated but never used as child ID.
                    possible_mirror = (runtime == 'codex' and row.get('type') == 'event_msg'
                                       and all((e['role'], e['text']) in possible_mirrors for e in events))
                    staged.append((path, native_id, source_ref, events, {
                        'source_system': f'local:{runtime}-jsonl',
                        'row_id': f'{path}:{offset}:{offset + len(line)}',
                        'row_digest': row_sha,
                        'source_artifact_sha256': file_sha,
                        'source_coordinates': {'path': str(path), 'start': offset, 'end': offset + len(line)},
                        'native_parent_id': parent_id if runtime == 'claude' else codex_parent,
                        'native_child_id': agent_id,
                        'legacy_host_session_id': native_id,
                        'evidence_event_map': [{'event_start': 0, 'event_count': len(events),
                                                'authored_at': row.get('timestamp', 'unknown'),
                                                'row_sha256': row_sha}],
                        'omissions': omissions,
                        'possible_mirror_of_response_item': possible_mirror,
                        'import_actor': import_actor,
                    }))
                    counts['visible_rows'] += 1
                    counts['visible_events'] += len(events)
                    if possible_mirror:
                        counts['possible_mirror_rows'] += 1
            else:
                counts['omitted_control_rows'] += 1
            offset += len(line)
            file_report['next_offset'] = offset
        file_report['resume_cursor'] = {'offset': file_report['next_offset'],
                                         'prefix_sha256': _sha(data[:file_report['next_offset']])}
    # Recheck all source snapshots before writing any row. Replay checks sealed
    # coordinates against the prior digest, while SessionSources enforces payload
    # immutability for the actual write.
    pending = []
    for path, native_id, source_ref, events, provenance in staged:
        source_session = leaf.content_id('source-session', {'schema_version':'ops.source-session.v1',
            'tenant_id':tenant_id, 'runtime':runtime, 'native_id':native_id}, ascii=True, allow_nan=True)
        prior = _existing(catalog, source_session, source_ref)
        if prior is not None:
            old = prior.get('source_provenance', {})
            old_coord = old.get('source_coordinates', {})
            new_coord = provenance['source_coordinates']
            if (old.get('row_digest') != provenance['row_digest'] or
                    (old_coord.get('start'), old_coord.get('end')) !=
                    (new_coord['start'], new_coord['end']) or
                    old.get('native_parent_id') != provenance['native_parent_id'] or
                    old.get('native_child_id') != provenance['native_child_id'] or
                    prior.get('events') != events):
                raise EpisodeConflict('native source coordinate changed after import')
            counts['already_present'] += 1
            continue
        pending.append((source_session, native_id, source_ref, events, provenance))
        if not apply:
            counts['would_import'] += 1
    if apply:
        for path, (dev, ino, size, mtime_ns, digest) in snapshots.items():
            if path.is_symlink():
                raise ValueError('native source changed before import')
            stat = path.stat()
            if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != (dev, ino, size, mtime_ns):
                raise ValueError('native source changed before import')
            if _sha(_read_bounded(path)) != digest:
                raise ValueError('native source changed before import')
        for source_session, native_id, source_ref, events, provenance in pending:
            actual = catalog.register(tenant_id=tenant_id, runtime=runtime, native_id=native_id)
            if actual != source_session:
                raise ValueError('source session identity mismatch')
            catalog.import_episode(session_id=source_session, source_ref=source_ref,
                                   events=events, provenance=provenance)
            counts['imported'] += 1
    if apply:
        # T12b: request a reindex (an outbox row); the drainer builds and swaps it. A host install
        # drains once now, never waiting; inside Compose the indexer role does.
        request_reindex(store)
        counts.update(reindex_counts(drain_after_seal(store)))
    return {'status': 'applied' if apply else 'preview', 'runtime': runtime,
            'tenant_id': tenant_id, 'counts': dict(counts), 'files': reports,
            'authority': 'visible source evidence only; no desk attribution, admission, or outbound consent'}
