"""Bounded reuse of committed declaration facts and existing SCIP observations.

SCIP identifier spans corroborate declarations; they never replace body coordinates
or establish execution. This adapter consumes the existing normalized SCIP format.
"""
from collections import OrderedDict
from dataclasses import dataclass
import ast
import hashlib
import re
import sys

from .models import canonical_digest

POLICY = 'python-ast-declarations.v1'
PARSER = f'{sys.version_info.major}.{sys.version_info.minor}'


@dataclass(frozen=True)
class Declaration:
    name: str
    start: int
    length: int
    identifier_start: int
    identifier_length: int


@dataclass(frozen=True)
class DeclarationFacts:
    repo_key: str
    revision: str
    entries: tuple
    documents: tuple
    errors: tuple
    fingerprint: str


class DeclarationFactCache:
    """Process-local bounded immutable generations; graph writes remain verified."""
    def __init__(self, max_generations=2, max_declarations=100000):
        if type(max_generations) is not int or not 1 <= max_generations <= 16:
            raise ValueError('bounded generation count required')
        if type(max_declarations) is not int or not 1 <= max_declarations <= 500000:
            raise ValueError('bounded declaration count required')
        self.max_generations, self.max_declarations = max_generations, max_declarations
        self._values = OrderedDict()

    def get(self, key):
        value = self._values.get(key)
        if value is not None:
            self._values.move_to_end(key)
        return value

    def put(self, key, value):
        if (len(value.entries) > 10000 or
                sum(len(rows) for _, _, rows in value.documents) > self.max_declarations):
            return  # Do not retain an oversized generation; caller can still use it.
        self._values[key] = value
        self._values.move_to_end(key)
        while len(self._values) > self.max_generations:
            self._values.popitem(last=False)


DEFAULT_CACHE = DeclarationFactCache()


def committed_declarations(*, root, repo_key, revision, git, cache=DEFAULT_CACHE):
    key = (str(root.resolve()), repo_key, revision, POLICY, PARSER)
    cached = cache.get(key) if cache is not None else None
    if cached is not None:
        return cached
    if git(root, 'rev-parse', revision + '^{commit}').decode().strip() != revision:
        raise ValueError('commit identity mismatch')
    entries = []
    for row in git(root, 'ls-tree', '-rz', '--full-tree', revision).split(b'\0'):
        if row:
            header, name = row.split(b'\t', 1)
            mode, kind, blob = header.decode('ascii').split()
            if mode in {'100644', '100755'} and kind == 'blob':
                entries.append((name.decode('utf-8'), blob))
    documents, errors = [], []
    for path, blob in sorted(entries):
        if not path.endswith('.py'):
            continue
        raw = git(root, 'cat-file', 'blob', blob)
        try:
            tree = ast.parse(raw.decode('utf-8'), filename='committed_source.py')
        except (UnicodeDecodeError, SyntaxError, RecursionError):
            errors.append((hashlib.sha256(path.encode()).hexdigest(), 'python_parse_failed'))
            documents.append((path, blob, ()))
            continue
        lines = raw.splitlines(keepends=True)
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        module = path[:-3].replace('/', '.')
        if module.endswith('.__init__'):
            module = module[:-9]
        declarations = []
        for node, names in _declarations(tree):
            start = offsets[node.lineno-1] + node.col_offset
            end = offsets[node.end_lineno-1] + node.end_col_offset
            header = lines[node.lineno-1][node.col_offset:]
            prefix = re.match(rb'(?:async\s+def|def|class)\s+', header)
            identifier = start + prefix.end() if prefix else -1
            length = len(node.name.encode())
            if identifier < 0 or raw[identifier:identifier+length] != node.name.encode():
                identifier = -1  # Still a declaration; no corroboration claim.
            declarations.append(Declaration('.'.join((module, *names)), start, end-start,
                                            identifier, length))
        documents.append((path, blob, tuple(declarations)))
    fingerprint = canonical_digest('python-declarations', {
        'repo_key':repo_key, 'revision':revision, 'policy':POLICY, 'parser':PARSER,
        'files':[(p,b) for p,b,_ in documents],
        'errors':[{'path_digest':p,'reason':r} for p,r in errors]})
    facts = DeclarationFacts(repo_key, revision, tuple(sorted(entries)), tuple(documents),
                             tuple(errors), fingerprint)
    if cache is not None:
        cache.put(key, facts)
    return facts


def corroborate_scip(facts, envelope):
    """Join exact identifier coordinates only; preserve narrower SCIP coverage."""
    from kp_agent_tooling._impl.scip_navigation import validate_index
    data = validate_index(envelope, facts.revision)
    if data.get('repo_key') != facts.repo_key:
        raise ValueError('SCIP repository mismatch')
    entries = dict(facts.entries)
    for path, blob in data['blobs'].items():
        if entries.get(path) != blob:
            raise ValueError('SCIP committed blob mismatch')
    identifiers = {(p,b,d.identifier_start,d.identifier_length):d.name
                   for p,b,rows in facts.documents for d in rows if d.identifier_start >= 0}
    matches, unmatched = [], 0
    for occurrence in data['occurrences']:
        if not occurrence.get('definition'):
            continue
        path, blob = occurrence['path'], occurrence['blob_sha']
        if data['blobs'].get(path) != blob or entries.get(path) != blob:
            raise ValueError('SCIP definition outside verified inventory')
        if (type(occurrence.get('byte_offset')) is not int or occurrence['byte_offset'] < 0
                or type(occurrence.get('byte_length')) is not int or occurrence['byte_length'] <= 0
                or not isinstance(occurrence.get('symbol'), str) or not occurrence['symbol']):
            raise ValueError('invalid SCIP definition coordinates')
        key = (path,blob,occurrence['byte_offset'],occurrence['byte_length'])
        if key in identifiers:
            matches.append((path,identifiers[key],occurrence['symbol']))
        else:
            unmatched += 1
    return {'index_sha256':envelope['sha256'], 'repo_key':facts.repo_key,
            'revision':facts.revision,'declaration_fingerprint':facts.fingerprint,
            'matched_definitions':len(matches),'unmatched_definitions':unmatched,
            'indexed_files':len(data['blobs']),'index_gaps':len(data.get('gaps',[])),
            'matches':tuple(sorted(set(matches))),
            'authority':'matching static identifier coordinates; not invocation or complete repository coverage'}


def _declarations(root, owners=()):
    for child in ast.iter_child_nodes(root):
        if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = (*owners, child.name)
            yield child, scope
            yield from _declarations(child, scope)
        else:
            yield from _declarations(child, owners)
