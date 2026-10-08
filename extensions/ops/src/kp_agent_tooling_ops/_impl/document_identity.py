"""Validate exact document removal coordinates without persistence dependencies."""
import re


def document_identity(repo_key, path, blob_sha=None):
    if not isinstance(repo_key, str) or not repo_key.strip():
        raise ValueError('repository scope required')
    if (not isinstance(path, str) or path.startswith('/') or '\\' in path
            or any(p in {'', '.', '..'} for p in path.split('/'))
            or any(ord(c)<32 for c in path)):
        raise ValueError('normalized relative document path required')
    if blob_sha is not None and (not isinstance(blob_sha, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', blob_sha)):
        raise ValueError('full blob digest required')
    return repo_key, path, blob_sha or ''
