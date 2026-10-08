"""Revision-pinned inventory of every leaf in a committed Git tree.

Classification is a path/mode heuristic for routing searches, not a claim about
the contents or meaning of an artifact. Consumers must check candidate bytes.
"""

import os
import select
import subprocess
import threading
import time

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.navigation_workspace import active, SHA


SCHEMA_VERSION = 'ops.repository-manifest.v1'
POLICY_VERSION = 'ops.repository-manifest-policy.v1'
MAX_TEXT_BYTES = 8_000_000
_TEXT_EXTENSIONS = frozenset({
    '.bat', '.c', '.cc', '.cfg', '.clj', '.conf', '.cpp', '.css', '.csv', '.dart',
    '.env', '.go', '.graphql', '.h', '.hpp', '.html', '.ini', '.java', '.js',
    '.json', '.jsonl', '.jsx', '.kt', '.lock', '.lua', '.md', '.mjs', '.php',
    '.properties', '.proto', '.py', '.rb', '.rs', '.rst', '.scss', '.sh', '.sql',
    '.svg', '.swift', '.toml', '.ts', '.tsx', '.txt', '.xml', '.yaml', '.yml',
})
_BINARY_EXTENSIONS = frozenset({
    '.7z', '.a', '.bin', '.class', '.db', '.dll', '.doc', '.docx', '.exe', '.gif',
    '.gz', '.ico', '.jar', '.jpeg', '.jpg', '.mp3', '.mp4', '.o', '.pdf', '.png',
    '.pyc', '.so', '.sqlite', '.ttf', '.wav', '.webp', '.woff', '.woff2', '.zip',
})
_GENERATED_PARTS = frozenset({'__pycache__', 'build', 'dist', 'generated', 'gen',
                              'node_modules', 'target'})
_VENDOR_PARTS = frozenset({'vendor', 'third_party', 'third-party', 'external'})
_DOC_PARTS = frozenset({'docs', 'doc', 'documentation'})
_CONFIG_PARTS = frozenset({'config', '.github', '.circleci'})


class ManifestIncomplete(RuntimeError):
    def __init__(self, reason, budget=None):
        super().__init__(reason)
        self.reason = reason
        self.budget = budget


def _identity(*parts):
    return leaf.joined_sha256(parts, separator='\0', errors='surrogateescape')


def _git(root, args, *, input_bytes=None, deadline=None, output_limit=16_000_000):
    remaining = 20 if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise ManifestIncomplete('elapsed_time_budget_exceeded',
                                 {'name': 'elapsed_seconds', 'limit': 20})
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    process = None
    call_limit = min(20, remaining)
    call_deadline = time.monotonic() + call_limit
    try:
        process = subprocess.Popen(['git', '--no-optional-locks', *args], cwd=root,
                                   env=env, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if input_bytes is not None:
            # A batch-check may emit output while consuming input. Feed it on a
            # separate thread so neither pipe can deadlock the bounded reader.
            def feed():
                try:
                    process.stdin.write(input_bytes)
                    process.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass
            threading.Thread(target=feed, daemon=True).start()
        chunks = []
        observed = 0
        while True:
            wait = call_deadline - time.monotonic()
            if wait <= 0:
                raise ManifestIncomplete('git_time_budget_exceeded',
                                         {'name': 'git_call_seconds', 'limit': call_limit})
            if not select.select([process.stdout], [], [], wait)[0]:
                raise ManifestIncomplete('git_time_budget_exceeded',
                                         {'name': 'git_call_seconds', 'limit': call_limit})
            chunk = os.read(process.stdout.fileno(), min(65_536, output_limit + 1 - observed))
            if not chunk:
                break
            chunks.append(chunk)
            observed += len(chunk)
            if observed > output_limit:
                raise ManifestIncomplete('git_output_budget_exceeded',
                                         {'name': 'output_bytes', 'limit': output_limit,
                                          'observed': observed})
        process.wait(timeout=max(0.001, call_deadline - time.monotonic()))
        if process.returncode:
            raise ManifestIncomplete('git_object_unavailable')
        return b''.join(chunks)
    except subprocess.TimeoutExpired as exc:
        raise ManifestIncomplete('git_time_budget_exceeded',
                                 {'name': 'git_call_seconds', 'limit': call_limit}) from exc
    except OSError as exc:
        raise ManifestIncomplete('repository_unavailable') from exc
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if process.stdin is not None:
                process.stdin.close()


def _classification(path, mode, size):
    if mode == '160000':
        return {'kind': 'gitlink', 'basis': 'git-tree-mode'}, False, 'gitlink'
    if mode == '120000':
        return {'kind': 'symlink', 'basis': 'git-tree-mode'}, False, 'symlink'
    parts = path.lower().split('/')
    name = parts[-1]
    dot = name.rfind('.')
    ext = name[dot:] if dot >= 0 else ''
    if any(part in _VENDOR_PARTS for part in parts[:-1]):
        kind, basis = 'vendor', 'path-component-heuristic'
    elif any(part in _GENERATED_PARTS for part in parts[:-1]):
        kind, basis = 'generated', 'path-component-heuristic'
    elif any(part in _DOC_PARTS for part in parts[:-1]) or ext in ('.md', '.rst'):
        kind, basis = 'documentation', 'path-or-extension-heuristic'
    elif any(part in _CONFIG_PARTS for part in parts[:-1]) or name.startswith('.'):
        kind, basis = 'configuration', 'path-component-heuristic'
    elif ext in _BINARY_EXTENSIONS:
        kind, basis = 'binary', 'extension-heuristic'
    elif ext in _TEXT_EXTENSIONS:
        kind, basis = 'source', 'extension-heuristic'
    else:
        kind, basis = 'unknown', 'no-matching-path-rule'
    if ext in _BINARY_EXTENSIONS:
        return {'kind': kind, 'basis': basis}, False, 'binary_extension'
    if size > MAX_TEXT_BYTES:
        return {'kind': kind, 'basis': basis}, False, 'text_size_limit'
    # Unknown extensions remain candidates. The content reader must reject NUL
    # bytes and invalid UTF-8; a suffix cannot establish that a blob is binary.
    return {'kind': kind, 'basis': basis}, True, None


def build_manifest(config, repo_key, target_revision, *, entry_limit=100_000,
                   inventory_byte_limit=16_000_000, deadline=None):
    """Return a complete committed-tree inventory or an explicit incomplete result.

    ``artifact_id`` identifies a repository-relative path across revisions;
    ``manifest_id`` identifies this repository, exact commit and policy version.
    Neither says that two versions of a path have identical content.
    """
    if (not isinstance(repo_key, str) or not isinstance(target_revision, str)
            or not SHA.fullmatch(target_revision)):
        raise ValueError('configured repository and explicit full commit required')
    if (type(entry_limit) is not int or not 1 <= entry_limit <= 500_000
            or type(inventory_byte_limit) is not int
            or not 1024 <= inventory_byte_limit <= 64_000_000):
        raise ValueError('invalid manifest budget')
    profile = active(config)
    if repo_key not in profile['repos']:
        raise ValueError('configured repository required')
    root = profile['repos'][repo_key]['path']
    manifest_id = _identity(SCHEMA_VERSION, POLICY_VERSION, repo_key, target_revision)
    common = {'schema_version': SCHEMA_VERSION, 'policy_version': POLICY_VERSION,
              'manifest_id': manifest_id, 'repo_key': repo_key,
              'source_revision': target_revision,
              'scope': 'exact committed Git tree; worktree excluded',
              'classification_authority': 'heuristic-not-content-truth',
              'text_eligibility_authority': 'candidate-only; consumers must validate bytes'}
    deadline = time.monotonic() + 20 if deadline is None else deadline
    try:
        resolved = _git(root, ['rev-parse', target_revision + '^{commit}'], deadline=deadline)
        if resolved.decode().strip() != target_revision:
            raise ManifestIncomplete('target_revision_unavailable')
        tree = _git(root, ['ls-tree', '-r', '-z', target_revision], deadline=deadline,
                    output_limit=inventory_byte_limit)
        if len(tree) > inventory_byte_limit:
            raise ManifestIncomplete('inventory_byte_budget_exceeded',
                                     {'name': 'inventory_bytes', 'limit': inventory_byte_limit,
                                      'observed': len(tree)})
        raw_entries = [item for item in tree.split(b'\0') if item]
        if len(raw_entries) > entry_limit:
            raise ManifestIncomplete('entry_budget_exceeded',
                                     {'name': 'entries', 'limit': entry_limit,
                                      'observed': len(raw_entries)})
        parsed = []
        blob_ids = []
        for raw in raw_entries:
            if time.monotonic() >= deadline:
                raise ManifestIncomplete('elapsed_time_budget_exceeded',
                                         {'name': 'elapsed_seconds'})
            try:
                meta, raw_path = raw.split(b'\t', 1)
                mode, object_type, object_id = meta.decode('ascii').split()
                path = raw_path.decode('utf-8', 'surrogateescape')
            except (ValueError, UnicodeDecodeError) as exc:
                raise ManifestIncomplete('invalid_tree_entry') from exc
            if object_type not in ('blob', 'commit') or (object_type == 'commit') != (mode == '160000'):
                raise ManifestIncomplete('unsupported_tree_entry')
            parsed.append((path, mode, object_type, object_id))
            if object_type == 'blob':
                blob_ids.append(object_id)
        sizes = {}
        if blob_ids:
            size_output = _git(root, ['cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)'],
                               input_bytes=('\n'.join(blob_ids) + '\n').encode('ascii'),
                               deadline=deadline, output_limit=max(inventory_byte_limit, len(blob_ids) * 100))
            lines = size_output.decode('ascii').splitlines()
            if len(lines) != len(blob_ids):
                raise ManifestIncomplete('blob_size_inventory_incomplete')
            for expected, line in zip(blob_ids, lines):
                fields = line.split()
                if len(fields) != 3 or fields[0] != expected or fields[1] != 'blob':
                    raise ManifestIncomplete('blob_size_inventory_incomplete')
                sizes[expected] = int(fields[2])
        entries = []
        for path, mode, object_type, object_id in parsed:
            if time.monotonic() >= deadline:
                raise ManifestIncomplete('elapsed_time_budget_exceeded',
                                         {'name': 'elapsed_seconds'})
            size = sizes[object_id] if object_type == 'blob' else None
            classification, eligible, reason = _classification(path, mode, size)
            entries.append({'path': path, 'blob_sha': object_id, 'mode': mode,
                            'size': size, 'artifact_id': _identity('artifact.v1', repo_key, path),
                            'artifact_revision_id': _identity('artifact-revision.v1', repo_key,
                                                               path, mode, object_id),
                            'classification': classification, 'text_eligible': eligible,
                            'exclusion_reason': reason})
        return dict(common, status='ok', complete=True, entries=entries,
                    entry_count=len(entries), excluded_count=sum(not e['text_eligible'] for e in entries))
    except ManifestIncomplete as exc:
        return dict(common, status='incomplete', complete=False, entries=[], entry_count=None,
                    reason=exc.reason, budget=exc.budget,
                    absence_verdict='not-established')
