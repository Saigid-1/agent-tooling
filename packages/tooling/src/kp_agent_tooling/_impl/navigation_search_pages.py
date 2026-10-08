"""Resumable, bounded search of an exact committed repository manifest."""
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import math

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.git_batch import BlobBatch, BatchReadTimeout
from kp_agent_tooling._impl.navigation_workspace import active, SHA
from kp_agent_tooling._impl.repository_manifest import build_manifest


SCHEMA = 'ops.navigation-search-page.v1'
SEARCH_POLICY_VERSION = 'ops.navigation-search-policy.v1'
CURSOR = re.compile(r'navigation-search:sha256:([0-9a-f]{64})\Z')
# One streamed `cat-file --batch` per page makes bytes and time the binding
# limits; the file cap only bounds cursor arithmetic and record retention.
MAX_FILES = 4000
MAX_LINES = 1_000_000
# Prefetched per page by one batch read; a large product tree is ~9 MB of text.
MAX_BYTES = 32_000_000
MAX_SECONDS = 10
MAX_OPERATION_SECONDS = 10
MAX_EXCERPT = 500


class SearchPageError(ValueError):
    """Expected invalid search request or continuation (safe for callers)."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def _canonical(value):
    return leaf.canonical_bytes(value, ascii=True, allow_nan=True)


def _registry(path):
    root = Path(path)
    if root.is_symlink() or not root.is_dir():
        raise SearchPageError('registry_unavailable', 'server-owned search registry must already exist')
    return root.resolve()


def _save(root, state):
    return leaf.publish_content_addressed(
        root, state, ascii=True, allow_nan=True, id_prefix='navigation-search', name_prefix='search-',
        temp_prefix='.search-',
        refuse=lambda message: SearchPageError('invalid_cursor', message),
        symlink_message='search cursor registry symlink refused', collision_message='search cursor collision')


def _load(root, token):
    match = CURSOR.fullmatch(token) if isinstance(token, str) else None
    if not match:
        raise SearchPageError('invalid_cursor', 'invalid search continuation token')
    path = root / ('search-' + match.group(1) + '.json')
    try:
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
            raise SearchPageError('invalid_cursor', 'regular search cursor required')
        if path.stat().st_size > 65536:
            raise SearchPageError('invalid_cursor', 'search continuation exceeds byte budget')
        raw = path.read_bytes()
    except FileNotFoundError as error:
        raise SearchPageError('invalid_cursor', 'unknown search continuation token') from error
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != match.group(1):
        raise SearchPageError('invalid_cursor', 'search continuation identity mismatch')
    state = json.loads(raw)
    if not isinstance(state, dict) or state.get('schema_version') != SCHEMA:
        raise SearchPageError('invalid_cursor', 'invalid search cursor schema')
    return state


def _page_reader(root, selected, index, deadline):
    """One `cat-file --batch` for the blobs this page can reach, in manifest order."""
    ids = []
    files = byte_count = 0
    for entry in selected[index:]:
        if files >= MAX_FILES:
            break
        files += 1
        size = entry.get('size')
        if not entry['text_eligible'] or type(size) is not int or size < 0 or size > MAX_BYTES:
            continue
        if byte_count + size > MAX_BYTES:
            break
        byte_count += size
        ids.append(entry['blob_sha'])
    return BlobBatch(root, ids, deadline=deadline)


def _blob(root, blob, size, deadline, reader=None):
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', blob):
        raise ValueError('invalid manifest blob')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('search page time budget exhausted')
    if reader is not None:
        try:
            kind, read_size, data = reader.get(blob)
        except BatchReadTimeout as error:
            raise TimeoutError('search page time budget exhausted') from error
        if kind != 'blob' or read_size != size or len(data) != size:
            raise ValueError('manifest blob size changed')
        return data
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    result = subprocess.run(['git', '--no-optional-locks', 'cat-file', 'blob', blob],
                            cwd=root, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=min(5, remaining), check=True)
    if len(result.stdout) != size:
        raise ValueError('manifest blob size changed')
    return result.stdout


def _manifest_cache_path(root, repo_key, target_revision):
    from kp_agent_tooling._impl.repository_manifest import SCHEMA_VERSION, POLICY_VERSION
    identity = leaf.sha256_hex('\0'.join((SCHEMA_VERSION, POLICY_VERSION, repo_key, target_revision)).encode())
    return root / ('manifest-' + identity + '.json')


def _cached_manifest(root, repo_key, target_revision):
    """A complete manifest for an exact commit is immutable; serve it from the registry."""
    path = _manifest_cache_path(root, repo_key, target_revision)
    try:
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > 64_000_000:
            return None
        manifest = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    if (not isinstance(manifest, dict) or manifest.get('status') != 'ok'
            or manifest.get('repo_key') != repo_key
            or manifest.get('source_revision') != target_revision
            or not isinstance(manifest.get('entries'), list)):
        return None
    manifest['cache'] = 'registry-hit'
    return manifest


def _store_manifest(root, manifest):
    return leaf.publish_if_absent(_manifest_cache_path(root, manifest['repo_key'], manifest['source_revision']),
                                  lambda: _canonical(manifest), temp_prefix='.manifest-', temp_dir=root)


def search_pages(config, registry, repo_key, target_revision, query, *, mode='literal',
                 path_pattern='*', continuation_token=None, limit=100):
    """Return one bounded page and a durable cursor for the remaining committed text."""
    if not isinstance(query, str) or not 1 <= len(query) <= 256 or '\0' in query:
        raise SearchPageError('invalid_request', 'bounded nonempty query required')
    if mode not in ('literal', 'glob'):
        raise SearchPageError('invalid_request', 'search mode must be literal or glob')
    if (not isinstance(path_pattern, str) or not 1 <= len(path_pattern) <= 256
            or '\\' in path_pattern or path_pattern.startswith('/')
            or '..' in path_pattern.split('/')):
        raise SearchPageError('invalid_request', 'bounded repository-relative path glob required')
    if type(limit) is not int or not 1 <= limit <= 500:
        raise SearchPageError('invalid_request', 'invalid search result limit')
    if not isinstance(target_revision, str) or not SHA.fullmatch(target_revision):
        raise SearchPageError('invalid_request', 'explicit full commit required')
    operation_started = time.monotonic()
    deadline = operation_started + MAX_OPERATION_SECONDS
    registry_root = _registry(registry)
    manifest = _cached_manifest(registry_root, repo_key, target_revision)
    if manifest is None:
        manifest = build_manifest(config, repo_key, target_revision, deadline=deadline)
        if manifest.get('status') == 'ok' and manifest.get('complete'):
            _store_manifest(registry_root, manifest)
    if manifest['status'] != 'ok':
        manifest_deadline = manifest.get('reason') in {
            'elapsed_time_budget_exceeded', 'git_time_budget_exceeded'}
        return {'schema_version': SCHEMA, 'status': 'incomplete',
                'repo_key': repo_key, 'source_revision': target_revision,
                'manifest_id': manifest.get('manifest_id'), 'manifest': manifest,
                'results': [], 'continuation_token': continuation_token,
                'pages_remaining_estimate': None, 'traversal_complete': False,
                'search_complete': False, 'reason': (
                    'operation_deadline_exceeded'
                    if manifest_deadline
                    else manifest.get('reason')),
                'retryable': manifest_deadline,
                'operation_budget': {'seconds': MAX_OPERATION_SECONDS},
                'operation_elapsed_seconds': round(time.monotonic() - operation_started, 6),
                'absence_verdict': 'not-established'}
    selected = [entry for entry in manifest['entries']
                if fnmatch.fnmatchcase(entry['path'], path_pattern)]
    binding = {'repo_key': repo_key, 'source_revision': target_revision,
               'manifest_id': manifest['manifest_id'],
               'policy_version': manifest['policy_version'],
               'search_policy_version': SEARCH_POLICY_VERSION, 'query': query,
               'mode': mode, 'path_pattern': path_pattern, 'limit': limit,
               'scan_limits': {'files': MAX_FILES, 'lines': MAX_LINES,
                               'source_bytes': MAX_BYTES, 'seconds': MAX_SECONDS,
                               'excerpt_chars': MAX_EXCERPT}}
    if continuation_token is None:
        state = {'schema_version': SCHEMA, 'binding': binding, 'entry_index': 0,
                 'next_line': 1, 'searched_files': 0, 'total_matches': 0,
                 'excluded_files': 0, 'failed_files': 0}
    else:
        state = _load(registry_root, continuation_token)
        if state.get('binding') != binding:
            raise SearchPageError('cursor_mismatch', 'search continuation does not match source or request')
    index, next_line = state['entry_index'], state['next_line']
    if (type(index) is not int or type(next_line) is not int or
            not 0 <= index <= len(selected) or next_line < 1):
        raise SearchPageError('invalid_cursor', 'invalid search cursor position')
    start_position = (index, next_line)
    source_root = Path(active(config)['repos'][repo_key]['path'])
    # The manifest resolver validates the configured source and exact revision.
    # The same deadline covers manifest construction, reads and line scanning.
    files = lines = byte_count = 0
    results = []
    page_exclusions = []
    page_failures = []
    deadline_exceeded = False
    interruption_reason = None
    reader = _page_reader(source_root, selected, index, deadline)
    try:
      while index < len(selected):
          if files >= MAX_FILES or lines >= MAX_LINES or len(results) >= limit or time.monotonic() >= deadline:
              deadline_exceeded = time.monotonic() >= deadline
              break
          entry = selected[index]
          if not entry['text_eligible']:
              page_exclusions.append({'path': entry['path'],
                                      'reason': entry.get('exclusion_reason') or entry['classification']['kind']})
              state['excluded_files'] += 1
              index += 1
              next_line = 1
              files += 1
              continue
          size = entry['size']
          if type(size) is not int or size < 0:
              page_failures.append({'path': entry['path'], 'reason': 'invalid_manifest_size'})
              state['failed_files'] += 1
              index += 1
              next_line = 1
              files += 1
              continue
          if size > MAX_BYTES:
              page_exclusions.append({'path': entry['path'], 'reason': 'blob_exceeds_page_byte_budget',
                                      'size': size, 'limit': MAX_BYTES})
              state['excluded_files'] += 1
              index += 1
              next_line = 1
              files += 1
              continue
          if byte_count + size > MAX_BYTES:
              break
          try:
              data = _blob(source_root, entry['blob_sha'], size, deadline, reader)
              byte_count += size
              if b'\0' in data:
                  page_exclusions.append({'path': entry['path'], 'reason': 'binary_blob'})
                  state['excluded_files'] += 1
                  index += 1
                  next_line = 1
                  files += 1
                  continue
              try:
                  content = data.decode('utf-8')
              except UnicodeDecodeError:
                  page_exclusions.append({'path': entry['path'], 'reason': 'invalid_utf8'})
                  state['excluded_files'] += 1
                  index += 1
                  next_line = 1
                  files += 1
                  continue
          except (TimeoutError, subprocess.TimeoutExpired):
              deadline_exceeded = True
              interruption_reason = 'operation_deadline_exceeded'
              break
          except (UnicodeError, ValueError, OSError, subprocess.SubprocessError) as error:
              page_failures.append({'path': entry['path'], 'reason': type(error).__name__})
              state['failed_files'] += 1
              index += 1
              next_line = 1
              files += 1
              continue
          source_lines = content.splitlines()
          while next_line <= len(source_lines) and lines < MAX_LINES and len(results) < limit:
              if time.monotonic() >= deadline:
                  deadline_exceeded = True
                  break
              line = source_lines[next_line - 1]
              matched = query in line if mode == 'literal' else fnmatch.fnmatchcase(line, query)
              lines += 1
              if matched:
                  excerpt = line[:MAX_EXCERPT]
                  results.append({'path': entry['path'], 'line': next_line,
                                  'blob_sha': entry['blob_sha'], 'excerpt': excerpt,
                                  'artifact_id': entry['artifact_id'],
                                  'artifact_revision_id': entry['artifact_revision_id'],
                                  'manifest_id': manifest['manifest_id'],
                                  'excerpt_sha256': hashlib.sha256(excerpt.encode()).hexdigest(),
                                  'excerpt_truncated': len(excerpt) < len(line),
                                  'source_revision': target_revision,
                                  'citation_scope': 'committed-source-line'})
                  state['total_matches'] += 1
              next_line += 1
          if deadline_exceeded:
              break
          if next_line > len(source_lines):
              state['searched_files'] += 1
              index += 1
              next_line = 1
              files += 1
          else:
              break
    finally:
        reader.close()
    state['entry_index'], state['next_line'] = index, next_line
    traversal_complete = index == len(selected)
    search_complete = traversal_complete and not state['excluded_files'] and not state['failed_files']
    coverage = {'selected_files': len(selected),
                'eligible_files': sum(1 for entry in selected if entry['text_eligible']),
                'searched_files': state['searched_files'], 'excluded_files': state['excluded_files'],
                'failed_files': state['failed_files'], 'remaining_files': len(selected) - index,
                'total_matches': state['total_matches']}
    # This estimate is deliberately advisory: file traversal is predictable, while
    # result-limit and time pages depend on content and host speed. It never changes
    # completion or absence semantics.
    if traversal_complete:
        pages_remaining_estimate = 0
    elif index > start_position[0]:
        completed_this_page = index - start_position[0]
        pages_remaining_estimate = max(
            1, math.ceil((len(selected) - index) / completed_this_page))
    else:
        pages_remaining_estimate = None
    no_progress = not traversal_complete and (index, next_line) == start_position
    if deadline_exceeded:
        reason = interruption_reason or 'operation_deadline_exceeded'
    else:
        reason = 'page_time_budget_no_progress' if no_progress else None
    return {'schema_version': SCHEMA,
            'status': 'unavailable' if no_progress else 'ok',
            'reason': reason,
            'retryable': no_progress or deadline_exceeded, **binding,
            'results': results, 'continuation_token': None if traversal_complete else _save(registry_root, state),
            'pages_remaining_estimate': pages_remaining_estimate,
            'traversal_complete': traversal_complete, 'search_complete': search_complete,
            'absence_verdict': ('no_matches_in_declared_scope'
                                if search_complete and not state['total_matches'] else 'not-established'),
            'coverage': coverage, 'exclusions': page_exclusions, 'failures': page_failures,
            'page_budget': {'files': MAX_FILES, 'lines': MAX_LINES,
                            'source_bytes': MAX_BYTES, 'seconds': MAX_SECONDS},
            'operation_budget': {'seconds': MAX_OPERATION_SECONDS,
                                 'scope': 'manifest, committed blob reads, and scanning'},
            'operation_elapsed_seconds': round(time.monotonic() - operation_started, 6),
            'page_usage': {'files': files, 'lines': lines, 'source_bytes': byte_count},
            'manifest_source': manifest.get('cache', 'built'),
            'scope': 'manifest-selected committed UTF-8 regular text; path glob=' + path_pattern,
            'runtime_execution': 'not-assessed'}
