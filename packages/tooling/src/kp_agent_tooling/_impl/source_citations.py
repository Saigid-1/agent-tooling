"""Exact Git citations for navigation; source identity is not claim correctness."""
import ast
import hashlib
import re
import subprocess

def _git(repo, *args):
    return subprocess.check_output(['git', *args], cwd=repo, stderr=subprocess.PIPE)

def source(repo, revision, path, *, max_bytes=None):
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', revision):
        raise ValueError('full source commit required')
    if _git(repo, 'rev-parse', revision+'^{commit}').decode().strip() != revision:
        raise ValueError('revision must identify a commit')
    if not isinstance(path, str) or not path or path.startswith('/') or any(p in ('', '.', '..') for p in path.split('/')):
        raise ValueError('canonical repository-relative path required')
    entry = _git(repo, 'ls-tree', revision, '--', path).decode().rstrip('\n')
    if not entry or '\t' not in entry:
        raise ValueError('source not found')
    metadata, actual = entry.split('\t', 1)
    mode, kind, blob = metadata.split()
    if mode not in ('100644', '100755') or kind != 'blob' or actual != path:
        raise ValueError('regular source file required')
    if max_bytes is not None:
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError('nonnegative source byte budget required')
        size = int(_git(repo, 'cat-file', '-s', blob).decode().strip())
        if size > max_bytes:
            raise ValueError('source byte budget exceeded')
    return blob, _git(repo, 'cat-file', 'blob', blob).decode('utf-8')

def source_reference(repo_key, path, revision, blob_sha):
    """Typed identity of committed file content; not a provider invocation or a claim."""
    return {"kind": "source", "repo_key": repo_key, "path": path,
            "revision": revision, "blob_sha": blob_sha}

def cite(repo, revision, path, anchor, context=8, symbol=None):
    if not isinstance(anchor, str) or not anchor.strip(): raise ValueError('anchor required')
    if type(context) is not int or not 0 <= context <= 30: raise ValueError('context must be 0..30')
    blob, text = source(repo, revision, path)
    lines = text.splitlines()
    lower, upper = 1, len(lines)
    if symbol is not None:
        if not path.endswith('.py') or not isinstance(symbol, str): raise ValueError('Python qualified symbol required')
        node = ast.parse(text)
        for part in symbol.split('.'):
            found = [n for n in node.body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == part]
            if len(found) != 1: raise ValueError('symbol must resolve uniquely')
            node = found[0]
        lower, upper = node.lineno, node.end_lineno
    matches = [i for i,line in enumerate(lines) if lower <= i+1 <= upper and anchor in line]
    if len(matches) != 1: raise ValueError('anchor must resolve to exactly one line')
    start = matches[0]+1; end = min(upper, start+context)
    excerpt = '\n'.join(lines[start-1:end])
    return {'revision': revision, 'path': path, 'blob_sha': blob, 'anchor': anchor, 'symbol': symbol,
            'start_line': start, 'end_line': end, 'excerpt': excerpt,
            'excerpt_sha256': hashlib.sha256(excerpt.encode()).hexdigest(),
            'scope': 'committed-source', 'claim_semantics': 'not-assessed'}

def validate(repo, citation):
    try:
        if type(citation['start_line']) is not int or type(citation['end_line']) is not int:
            raise ValueError('integer line coordinates required')
        actual = cite(repo, citation['revision'], citation['path'], citation['anchor'],
                      citation['end_line']-citation['start_line'], citation.get('symbol'))
        mismatches = [k for k,v in actual.items() if citation.get(k) != v]
        return {'status': 'valid' if not mismatches else 'invalid', 'mismatches': mismatches}
    except (KeyError, ValueError, TypeError, SyntaxError, UnicodeError, subprocess.CalledProcessError) as error:
        return {'status': 'invalid', 'mismatches': ['unresolvable_source'], 'detail': str(error)}
