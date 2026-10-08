"""Bounded path and text discovery from an exact committed tree."""
import fnmatch
import hashlib
import os
import subprocess
import time

from kp_agent_tooling._impl.git_batch import BlobBatch, BatchReadTimeout
from kp_agent_tooling._impl.navigation_workspace import active, SHA


DISCOVERY_SECONDS = 20


class DiscoveryBudgetExceeded(ValueError):
    """Safe, structured bounds failure; never evidence of absent matches."""

    def __init__(self, name, limit, observed, message):
        super().__init__(message)
        self.budget = {'name': name, 'limit': limit, 'observed': observed}

    def result(self, repo_key, revision, path_pattern, *, query=None, mode='literal', limit=100, snapshot_id=None):
        result = {'status': 'error', 'reason': 'search_budget_exceeded',
                'message': str(self), 'budget': self.budget,
                'repo_key': repo_key, 'source_revision': revision,
                'path_pattern': path_pattern, 'search_complete': False,
                'absence_verdict': 'not-established',
                'next_action': 'Narrow path_pattern to a relevant directory or file type; '
                               'navigation.paths can identify candidate paths. '
                               'Reducing limit only bounds returned matches, not scanned input.'}
        if query is not None:
            arguments = {'repo_key':repo_key,'target_revision':revision,'query':query,
                         'mode':mode,'path_pattern':path_pattern,'limit':min(limit,100)}
            if snapshot_id:
                arguments['snapshot_id']=snapshot_id
            result.update(
                pattern_status='accepted',
                next_call={'name':'navigation.search_page','arguments':arguments},
                next_action='Use navigation.search_page with the same scope and follow its continuation_token. '
                            'This requires the configured persistent navigation registry. '
                            'Inspect final coverage/exclusions before claiming absence. '
                            'Alternatively narrow path_pattern if reduced scope answers the question; '
                            'reducing limit only bounds returned matches, not scanned input.')
        return result


class DiscoveryInputError(ValueError):
    def result(self):
        return {'status':'error','reason':'invalid_path_pattern',
                'message':'Use a repository-relative fnmatch glob with forward slashes, '
                          '1..256 characters, no parent traversal, NUL or backslash escapes.',
                'examples':{'all_files':'*','all_python_including_root':'*.py',
                            'nested_python':'**/*.py','package_python':'kp_core/**/*.py'},
                'search_complete':False,'absence_verdict':'not-established'}


def validate_path_pattern(pattern):
    if (not isinstance(pattern,str) or not 1 <= len(pattern) <= 256
            or pattern.startswith('/') or '..' in pattern.split('/')
            or '\\' in pattern or '\0' in pattern):
        raise DiscoveryInputError('bounded repository-relative glob required')


def _elapsed_budget(deadline, message):
    # The deadline is the budget; report how much of it was spent, never the
    # remainder at the moment of failure (a remainder reads as a misconfigured limit).
    observed = None if deadline is None else round(DISCOVERY_SECONDS - (deadline - time.monotonic()), 3)
    return DiscoveryBudgetExceeded('elapsed_seconds', DISCOVERY_SECONDS, observed, message)


def _git(root, *args, deadline=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    remaining = 15 if deadline is None else deadline-time.monotonic()
    if remaining <= 0: raise _elapsed_budget(deadline, 'discovery elapsed-time budget exceeded')
    try:
        data = subprocess.check_output(['git', '--no-optional-locks', *args], cwd=root,
                                       env=env, stderr=subprocess.DEVNULL, timeout=min(15,remaining))
    except subprocess.TimeoutExpired as error:
        raise _elapsed_budget(deadline, 'discovery elapsed-time budget exceeded during a git call') from error
    if len(data) > 8_000_000:
        raise DiscoveryBudgetExceeded('git_output_bytes', 8_000_000, len(data), 'Git discovery output exceeds byte budget')
    return data


def _select(config, repo_key, target_revision):
    profile = active(config)
    if repo_key not in profile['repos'] or not isinstance(target_revision, str) or not SHA.fullmatch(target_revision):
        raise ValueError('configured repository and explicit full commit required')
    root = profile['repos'][repo_key]['path']
    if _git(root, 'rev-parse', target_revision + '^{commit}').decode().strip() != target_revision:
        raise ValueError('target revision must be a resolvable commit')
    return profile, root


def _paths(root, revision, scan_limit, deadline=None):
    # Git's tree, rather than the checkout, is the only discovery input.
    raw = _git(root, 'ls-tree', '-r', '-z', revision, deadline=deadline)
    entries = raw.split(b'\0')
    if len(entries) - 1 > scan_limit:
        raise DiscoveryBudgetExceeded('scan_limit', scan_limit, len(entries)-1, 'committed tree exceeds discovery scan budget')
    rows = []; excluded = 0
    for entry in entries:
        if not entry:
            continue
        meta, path = entry.split(b'\t', 1)
        mode, kind, blob = meta.decode().split()
        if kind == 'blob' and mode in ('100644', '100755'):
            rows.append((path.decode('utf-8'), blob))
        else: excluded += 1
    return rows, excluded


def _common(profile, repo_key, revision, result, omitted, scope):
    return {'schema_version': 'ops.navigation-discovery.v1', 'status': 'ok',
            'repo_key': repo_key, 'source_revision': revision,
            'default_revision': profile['repos'][repo_key]['revision'],
            'profile': profile['profile'], 'profile_sha256': profile.get('profile_sha256'),
            'scope': scope, 'results': result, 'omitted_results': omitted,
            'truncated': bool(omitted), 'runtime_execution': 'not-assessed'}


def paths(config, repo_key, target_revision, *, pattern='*', limit=100, scan_limit=20000):
    validate_path_pattern(pattern)
    if type(limit) is not int or not 1 <= limit <= 500 or type(scan_limit) is not int or not 1 <= scan_limit <= 50000:
        raise ValueError('invalid discovery budget')
    profile, root = _select(config, repo_key, target_revision)
    tree, excluded = _paths(root, target_revision, scan_limit)
    matches = [p for p, _ in tree if fnmatch.fnmatchcase(p, pattern)]
    result = [{'path': p, 'source_revision': target_revision} for p in matches[:limit]]
    answer = _common(profile, repo_key, target_revision, result, max(0, len(matches)-limit),
                     'regular files in exact committed tree; path glob=' + pattern)
    answer['excluded_nonregular_entries'] = excluded
    return answer


def search(config, repo_key, target_revision, query, *, mode='literal', path_pattern='*',
           limit=100, scan_limit=20000, file_limit=2000, max_blob_bytes=250000,
           line_limit=200000):
    if not isinstance(query, str) or not 1 <= len(query) <= 256 or '\0' in query:
        raise ValueError('bounded nonempty query required')
    if mode not in ('literal', 'glob'):
        raise ValueError('search mode must be literal or glob')
    validate_path_pattern(path_pattern)
    if any(type(x) is not int for x in (limit, scan_limit, file_limit, max_blob_bytes, line_limit)) or not (1 <= limit <= 500 and 1 <= scan_limit <= 50000 and 1 <= file_limit <= 5000 and 1 <= max_blob_bytes <= 1000000 and 1 <= line_limit <= 500000):
        raise ValueError('invalid search budget')
    profile, root = _select(config, repo_key, target_revision)
    deadline=time.monotonic()+DISCOVERY_SECONDS
    tree, excluded = _paths(root,target_revision,scan_limit,deadline)
    selected = [(p,b) for p,b in tree if fnmatch.fnmatchcase(p,path_pattern)]
    if len(selected) > file_limit:
        raise DiscoveryBudgetExceeded('file_limit', file_limit, len(selected), 'selected files exceed search budget; narrow path_pattern')
    # Two streamed processes replace two spawns per file: sizes first, so blobs
    # over the per-file cap are never read, then every eligible blob in tree order.
    exclusions = []
    exclusion_counts = {}
    def exclude(path, reason):
        exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
        if len(exclusions) < 20:
            exclusions.append({'path': path, 'reason': reason})
    matched_rows=[]; scanned_lines=0; skipped_blobs=0; shortened_excerpts=0; read_bytes=0; scanned_files=0
    try:
        with BlobBatch(root, [b for _, b in selected], deadline=deadline, check_only=True) as sizes:
            eligible = []
            for path, blob in selected:
                kind, size, _ = sizes.get(blob)
                if kind != 'blob' or size > max_blob_bytes:
                    exclude(path, 'not_blob' if kind != 'blob' else 'max_blob_bytes')
                    skipped_blobs += 1; continue
                eligible.append((path, blob))
        with BlobBatch(root, [b for _, b in eligible], deadline=deadline) as blobs:
            scanned = {}
            for path_index, (path, blob) in enumerate(eligible):
                if blob not in scanned:
                    _, size, data = blobs.get(blob)
                    read_bytes += size
                    if b'\0' in data:
                        scanned[blob] = ('binary_nul', None)
                    else:
                        try: scanned[blob] = (None, data.decode('utf-8'))
                        except UnicodeDecodeError: scanned[blob] = ('invalid_utf8', None)
                exclusion_reason, content = scanned[blob]
                if content is None:
                    exclude(path, exclusion_reason)
                    skipped_blobs += 1; continue
                scanned_files += 1
                for number,line in enumerate(content.splitlines(),1):
                    scanned_lines += 1
                    if scanned_lines > line_limit:
                        raise DiscoveryBudgetExceeded('line_limit', line_limit, scanned_lines, 'search line budget exceeded; narrow path_pattern')
                    matched = query in line if mode == 'literal' else fnmatch.fnmatchcase(line, query)
                    if matched:
                        matched_rows.append((path_index, number, path, blob, line))
    except BatchReadTimeout as error:
        raise _elapsed_budget(deadline, 'discovery elapsed-time budget exceeded while reading blobs') from error
    rows=[]; omitted=0
    for _, number, path, blob, line in matched_rows:
        if len(rows) < limit:
            excerpt=line[:500]
            shortened_excerpts += len(excerpt)<len(line)
            rows.append({'path':path,'line':number,'blob_sha':blob,'excerpt':excerpt,
                         'excerpt_sha256':hashlib.sha256(excerpt.encode()).hexdigest(),
                         'excerpt_truncated':len(excerpt)<len(line),
                         'source_revision':target_revision,'citation_scope':'committed-source-line'})
        else: omitted += 1
    result=_common(profile,repo_key,target_revision,rows,omitted,
                   'UTF-8 regular files in exact committed tree; '+mode+' search; path glob='+path_pattern)
    result['skipped_blobs']=skipped_blobs
    result['coverage'] = {
        'traversal_complete': True,
        'selected_files_complete': skipped_blobs == 0,
        'exclusions': exclusions,
        'exclusion_counts': exclusion_counts,
        'omitted_exclusion_details': max(0, skipped_blobs - len(exclusions)),
        'max_blob_bytes': max_blob_bytes,
        'details_status': 'reported',
    }
    result['search_complete'] = not (skipped_blobs or omitted or shortened_excerpts)
    result['absence_verdict'] = 'not-established'
    if skipped_blobs or omitted or shortened_excerpts:
        result['message'] = (
            f'Partial search result: {scanned_files} of {len(selected)} selected files searched; '
            f'{skipped_blobs} files excluded, {omitted} matching lines omitted, '
            f'{shortened_excerpts} excerpts shortened. See coverage for exclusion reasons. '
            'The legacy hit_budget flag includes file exclusions and shortened excerpts; '
            'it does not establish time-budget exhaustion.')
        result['next_action'] = (
            'Use navigation.search_page for paged results and inspect its exclusions. '
            'For excluded files use bounded navigation.source reads where supported; '
            'binary or non-UTF8 content needs an appropriate reader. Narrowing the glob '
            'does not remove a per-file size or encoding exclusion.')
    result['shortened_excerpts']=shortened_excerpts
    result['excluded_nonregular_entries']=excluded
    # `truncated` keeps its historical meaning (anything omitted or shortened);
    # the two named flags say which kind so absence reasoning can tell them apart.
    result['hit_limit'] = bool(omitted)
    result['hit_budget'] = bool(skipped_blobs or shortened_excerpts)
    result['truncated'] = result['truncated'] or bool(skipped_blobs or shortened_excerpts)
    result['scan'] = {'selected_files': len(selected), 'scanned_files': scanned_files,
                      'scanned_lines': scanned_lines, 'read_bytes': read_bytes,
                      'read_model': 'git cat-file --batch (two processes per request)'}
    return result
