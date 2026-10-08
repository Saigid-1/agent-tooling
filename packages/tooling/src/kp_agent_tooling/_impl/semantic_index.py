"""Concept-to-location ranking over committed source: a separately labelled layer.

Literal search (``navigation.search``) is exhaustive and cites coverage; this layer
answers a different question — *where is the code that does X* when the caller does
not know an identifier — and it can only say what it found. Every hit is a citation
the existing envelope can express (path, blob_sha, line range, excerpt digest) with a
``navigation.source`` continuation; ``absence_verdict`` is always ``not-established``.

The index is built at publish time from a committed tree (never a worktree): Python
files are chunked by symbol span (``ast``: functions, classes, methods); every other
text-eligible file by overlapping line windows. Vectors come from the pinned embedder
(``RealEmbedder`` in ``_impl.embeddings.embedders``) and are stored beside a JSON metadata
envelope that records the revision, the embedding revision and the chunking policy, so
a query is refused when any of them disagree with the caller's snapshot.
"""
from __future__ import annotations

import ast
import fnmatch
import re
import json
import math
import time
from pathlib import Path

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.leaf import sha256_hex as _digest
from kp_agent_tooling._impl.git_batch import BlobBatch, BatchReadTimeout
from kp_agent_tooling._impl.repository_manifest import build_manifest

SCHEMA = 'ops.navigation-semantic-index.v1'
CHUNKING = {'spec': 'python-ast-symbols+line-windows-v1', 'window_lines': 60, 'overlap_lines': 10,
            'max_chunk_chars': 1400, 'symbol_kinds': ['function', 'class', 'method']}
INDEXABLE_KINDS = frozenset({'source', 'documentation', 'configuration', 'unknown'})
MAX_FILES = 4000
BUILD_SECONDS = 900
PART_SOURCE_BYTES = 16_000_000
PART_CHUNKS = 8_000
PART_FILE_BYTES = 16_000_000
ROOT_FILE_BYTES = 20_000_000
MAX_PARTS = 64


class SemanticIndexError(ValueError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def _canonical(value) -> bytes:
    return leaf.canonical_bytes(value, ascii=True, allow_nan=True)


# --- chunking -----------------------------------------------------------------

def _python_symbol_chunks(path, text, lines):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    chunks = []

    def add(node, kind, qualname):
        start, end = node.lineno, getattr(node, 'end_lineno', node.lineno)
        body = '\n'.join(lines[start - 1:end])
        doc = ast.get_docstring(node) or ''
        head = f'{path} {kind} {qualname}\n'
        content = (head + body)[:CHUNKING['max_chunk_chars']]
        if doc and doc not in content:
            content = (head + doc[:300] + '\n' + body)[:CHUNKING['max_chunk_chars']]
        chunks.append({'kind': kind, 'symbol': qualname, 'start_line': start, 'end_line': end, 'text': content})

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node, 'function', node.name)
        elif isinstance(node, ast.ClassDef):
            add(node, 'class', node.name)
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(child, 'method', f'{node.name}.{child.name}')
    return chunks


def _window_chunks(path, lines):
    window, overlap = CHUNKING['window_lines'], CHUNKING['overlap_lines']
    chunks = []
    start = 0
    while start < len(lines):
        end = min(len(lines), start + window)
        body = '\n'.join(lines[start:end])
        chunks.append({'kind': 'window', 'symbol': None, 'start_line': start + 1, 'end_line': end,
                       'text': (f'{path}\n' + body)[:CHUNKING['max_chunk_chars']]})
        if end == len(lines):
            break
        start = end - overlap
    return chunks


def chunk_text(path: str, text: str) -> list[dict]:
    lines = text.splitlines()
    if not lines:
        return []
    if path.endswith('.py'):
        symbols = _python_symbol_chunks(path, text, lines)
        if symbols:
            return symbols
    return _window_chunks(path, lines)


# --- build ----------------------------------------------------------------------

def build_semantic_index(config, repo_key, revision, *, embedder, out_dir, path_pattern='*',
                         batch_size=64, deadline_seconds=BUILD_SECONDS):
    """Build from committed blobs while bounding source, chunk, and vector batches."""
    started = time.monotonic()
    deadline = started + deadline_seconds
    manifest = build_manifest(config, repo_key, revision, deadline=deadline)
    if manifest.get('status') != 'ok':
        raise SemanticIndexError('manifest_incomplete', str(manifest.get('reason')))
    override = config.get('_active_override')
    if override is not None:
        root = override['repos'][repo_key]['path']
    else:
        from kp_agent_tooling._impl.navigation_workspace import active
        root = active(config)['repos'][repo_key]['path']
    selected = [e for e in manifest['entries'] if e['text_eligible']
                and e['classification']['kind'] in INDEXABLE_KINDS
                and fnmatch.fnmatchcase(e['path'], path_pattern)]
    if len(selected) > MAX_FILES * MAX_PARTS:
        raise SemanticIndexError('file_budget', 'semantic partition file budget exceeded')
    groups, group, size = [], [], 0
    for entry in selected:
        entry_size = int(entry['size'])
        if entry_size > PART_SOURCE_BYTES:
            raise SemanticIndexError('byte_budget', f'{entry["path"]} exceeds partition source budget')
        if group and (size + entry_size > PART_SOURCE_BYTES or len(group) >= MAX_FILES):
            groups.append(group); group, size = [], 0
        group.append(entry); size += entry_size
    if group:
        groups.append(group)
    if len(groups) > MAX_PARTS:
        raise SemanticIndexError('byte_budget', 'semantic source partition count budget exceeded')
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    stem = f'{repo_key}-{revision}.semantic'
    revision_row = embedder.revision
    embedding = {'model_id': revision_row.model_id, 'model_digest': revision_row.model_digest,
                 'dim': embedder.dim, 'revision_id': getattr(revision_row, 'revision_id', None)
                 or leaf.canonical_sha256([revision_row.model_id, revision_row.model_digest, embedder.dim], ascii=True, allow_nan=True)}
    if embedder.dim < 1 or embedder.dim * 4 > PART_FILE_BYTES:
        raise SemanticIndexError('partition_budget', 'embedding dimension exceeds vector partition budget')
    part_limit = min(PART_CHUNKS, PART_FILE_BYTES // (embedder.dim * 4))
    pending, parts = [], []
    skipped = {'binary': 0, 'invalid_utf8': 0, 'empty': 0}
    indexed = chunk_count = 0
    kinds = {}

    def encode_pending():
        import numpy as np
        vectors = np.zeros((len(pending), embedder.dim), dtype=np.float32)
        for offset in range(0, len(pending), batch_size):
            if time.monotonic() > deadline:
                raise SemanticIndexError('build_deadline', 'embedding exceeded the build deadline')
            for i, vector in enumerate(embedder.embed([row['text'] for row in pending[offset:offset + batch_size]])):
                vectors[offset + i] = np.asarray(vector, dtype=np.float32)
        rows = [{key: value for key, value in row.items() if key != 'text'} for row in pending]
        raw = _canonical(rows)
        vector_bytes = vectors.tobytes()
        if len(raw) > PART_FILE_BYTES or len(vector_bytes) > PART_FILE_BYTES:
            raise SemanticIndexError('partition_budget', 'semantic partition exceeds file budget')
        return rows, raw, vector_bytes

    def write_part(raw, vector_bytes, count):
        if len(parts) >= MAX_PARTS:
            raise SemanticIndexError('partition_budget', 'semantic partition count budget exceeded')
        number = len(parts)
        chunk_file = f'{stem}.{number:03d}.{_digest(raw)[:16]}.chunks.json'
        vector_file = f'{stem}.{number:03d}.{_digest(vector_bytes)[:16]}.vectors.f32'
        _write_once(out / chunk_file, raw)
        _write_once(out / vector_file, vector_bytes)
        parts.append({'chunks_file': chunk_file, 'chunks_sha256': _digest(raw),
                      'vectors_file': vector_file, 'vectors_sha256': _digest(vector_bytes),
                      'chunk_count': count, 'chunks_bytes': len(raw), 'vectors_bytes': len(vector_bytes)})

    try:
        for source_group in groups:
            with BlobBatch(root, [e['blob_sha'] for e in source_group], deadline=deadline) as blobs:
                for entry in source_group:
                    _, _, data = blobs.get(entry['blob_sha'])
                    if b'\0' in data:
                        skipped['binary'] += 1; continue
                    try:
                        text = data.decode('utf-8')
                    except UnicodeDecodeError:
                        skipped['invalid_utf8'] += 1; continue
                    rows = chunk_text(entry['path'], text)
                    if not rows:
                        skipped['empty'] += 1; continue
                    indexed += 1
                    lines = text.splitlines()
                    for row in rows:
                        if len(pending) >= part_limit:
                            _, raw, vectors = encode_pending()
                            write_part(raw, vectors, len(pending))
                            pending.clear()
                        excerpt = '\n'.join(lines[row['start_line'] - 1:row['end_line']])
                        pending.append({**row, 'path': entry['path'], 'blob_sha': entry['blob_sha'],
                                        'excerpt_sha256': _digest(excerpt.encode())})
                        chunk_count += 1
                        kinds[row['kind']] = kinds.get(row['kind'], 0) + 1
    except BatchReadTimeout as error:
        raise SemanticIndexError('build_deadline', str(error)) from error
    _, raw, vectors = encode_pending()
    envelope = {'schema_version': SCHEMA, 'repo_key': repo_key, 'revision': revision,
                'manifest_id': manifest['manifest_id'], 'chunking': CHUNKING, 'embedding': embedding,
                'built_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'build_seconds': round(time.monotonic() - started, 3),
                'files_indexed': indexed, 'files_skipped': skipped, 'chunk_count': chunk_count,
                'chunk_kinds': dict(sorted(kinds.items())),
                'scope': 'committed text-eligible source, documentation and configuration; worktree excluded',
                'limitations': ['A ranker returns what it found; it never establishes absence.',
                                'Chunks are symbol spans for Python and line windows elsewhere; a concept split across chunks ranks lower.',
                                'Scores are cosine similarities under the pinned model, not confidence probabilities.']}
    if parts:
        if pending:
            write_part(raw, vectors, len(pending))
        envelope.update(schema_version='ops.navigation-semantic-index.partitioned.v1', parts=parts)
    else:
        _write_once(out / (stem + '.vectors.f32'), vectors)
        envelope.update(vectors_file=stem + '.vectors.f32', vectors_sha256=_digest(vectors),
                        chunks=json.loads(raw))
    envelope['index_sha256'] = leaf.canonical_sha256(envelope, ascii=True, allow_nan=True)
    _write_once(out / (stem + '.json'), _canonical(envelope))
    return {key: value for key, value in envelope.items() if key != 'chunks'}


def _write_once(path: Path, data: bytes):
    return leaf.replace_file(path, data, temp_prefix='.semantic-', skip_if_equal=True, cleanup=None)


# --- query ------------------------------------------------------------------------

RANKING = 'cosine+lexical-path-symbol-v1'
LEXICAL_WEIGHT = 0.15
TEST_PENALTY = 0.05


def _terms(text):
    return {t for t in re.findall(r'[a-z0-9]+', text.lower().replace('_', ' ')) if len(t) >= 4}


def _lexical_boost(chunk, query_terms):
    """Fraction of query words whose 5-char stem appears in the chunk's path or symbol."""
    if not query_terms:
        return 0.0
    hay = _terms(chunk['path'] + ' ' + (chunk['symbol'] or ''))
    matched = sum(1 for t in query_terms if any(h[:5] == t[:5] for h in hay))
    return LEXICAL_WEIGHT * matched / len(query_terms)


def _is_test_path(path):
    parts = path.lower().split('/')
    return any(p in ('tests', 'test', '__tests__', 'e2e') for p in parts[:-1]) or parts[-1].startswith('test_') or '.test.' in parts[-1]

def _load_root(index_dir, repo_key, revision):
    path = Path(index_dir) / f'{repo_key}-{revision}.semantic.json'
    if not path.is_file():
        raise SemanticIndexError('index_unavailable', f'no semantic index for {repo_key}@{revision[:8]} under {index_dir}')
    if path.stat().st_size > ROOT_FILE_BYTES:
        raise SemanticIndexError('index_integrity', 'semantic root exceeds reader budget')
    raw = path.read_bytes()
    envelope = json.loads(raw)
    recorded = envelope.pop('index_sha256', None)
    if envelope.get('schema_version') not in {SCHEMA, 'ops.navigation-semantic-index.partitioned.v1'} or recorded != leaf.canonical_sha256(envelope, ascii=True, allow_nan=True):
        raise SemanticIndexError('index_integrity', 'semantic index envelope digest mismatch')
    envelope['index_sha256'] = recorded
    if envelope['repo_key'] != repo_key or envelope['revision'] != revision:
        raise SemanticIndexError('index_mismatch', 'semantic index names another repository or revision')
    return path, envelope


def _part(path, envelope, descriptor):
    for field, suffix, budget in (('chunks_file', '.chunks.json', PART_FILE_BYTES),
                                  ('vectors_file', '.vectors.f32', PART_FILE_BYTES)):
        name = descriptor[field]
        if not isinstance(name, str) or '\\' in name or Path(name).name != name or not name.endswith(suffix):
            raise SemanticIndexError('index_integrity', 'invalid semantic partition path')
        item = path.with_name(name)
        try:
            size = item.stat().st_size
        except OSError as error:
            raise SemanticIndexError('index_integrity', 'semantic partition missing') from error
        if size != descriptor[field.replace('_file', '_bytes')]:
            raise SemanticIndexError('index_integrity', 'semantic partition byte count mismatch')
        if size > budget:
            raise SemanticIndexError('index_integrity', 'semantic partition exceeds reader budget')
    chunk_bytes = path.with_name(descriptor['chunks_file']).read_bytes()
    vector_bytes = path.with_name(descriptor['vectors_file']).read_bytes()
    if _digest(chunk_bytes) != descriptor['chunks_sha256'] or _digest(vector_bytes) != descriptor['vectors_sha256']:
        raise SemanticIndexError('index_integrity', 'semantic partition digest mismatch')
    chunks = json.loads(chunk_bytes)
    if len(chunks) != descriptor['chunk_count']:
        raise SemanticIndexError('index_integrity', 'semantic partition coverage mismatch')
    for chunk in chunks:
        source_path = chunk.get('path')
        if (not isinstance(source_path, str) or not source_path or '\\' in source_path
                or any(part in {'', '.', '..'} for part in source_path.split('/'))
                or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', chunk.get('blob_sha', ''))):
            raise SemanticIndexError('index_integrity', 'invalid semantic source identity')
    import numpy as np
    try:
        vectors = np.frombuffer(vector_bytes, dtype=np.float32).reshape(len(chunks), envelope['embedding']['dim'])
    except ValueError as error:
        raise SemanticIndexError('index_integrity', 'semantic vectors shape mismatch') from error
    return chunks, vectors


def validate_semantic_index(index_dir, repo_key, revision, *, expected_sha256=None):
    """Verify complete partition coverage without joining chunks or vectors."""
    path, envelope = _load_root(index_dir, repo_key, revision)
    if expected_sha256 is not None and envelope['index_sha256'] != expected_sha256:
        raise SemanticIndexError('index_mismatch', 'configured semantic digest mismatch')
    count, kinds = 0, {}
    for chunks, _ in _iter_parts(path, envelope):
        count += len(chunks)
        for chunk in chunks:
            source_path = chunk.get('path')
            if (not isinstance(source_path, str) or not source_path or '\\' in source_path
                    or any(part in {'', '.', '..'} for part in source_path.split('/'))
                    or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', chunk.get('blob_sha', ''))):
                raise SemanticIndexError('index_integrity', 'invalid semantic source identity')
            kind = chunk['kind']
            kinds[kind] = kinds.get(kind, 0) + 1
    if count != envelope['chunk_count'] or kinds != envelope['chunk_kinds']:
        raise SemanticIndexError('index_integrity', 'semantic indexed coverage mismatch')
    return {key: value for key, value in envelope.items() if key not in {'chunks', 'parts'}}


def _iter_parts(path, envelope):
    if envelope['schema_version'] == SCHEMA:
        vector_bytes = path.with_name(envelope['vectors_file']).read_bytes()
        if _digest(vector_bytes) != envelope['vectors_sha256']:
            raise SemanticIndexError('index_integrity', 'semantic vectors digest mismatch')
        import numpy as np
        yield envelope['chunks'], np.frombuffer(vector_bytes, dtype=np.float32).reshape(len(envelope['chunks']), envelope['embedding']['dim'])
        return
    parts = envelope.get('parts')
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS or sum(p['chunk_count'] for p in parts) != envelope['chunk_count']:
        raise SemanticIndexError('index_integrity', 'semantic partition coverage mismatch')
    for descriptor in parts:
        yield _part(path, envelope, descriptor)


def load_semantic_index(index_dir, repo_key, revision):
    path, envelope = _load_root(index_dir, repo_key, revision)
    if envelope['schema_version'] != SCHEMA:
        raise SemanticIndexError('partitioned_index', 'use validate_semantic_index or query_semantic_index for partitioned indexes')
    parts = list(_iter_parts(path, envelope))
    import numpy as np
    chunks = [chunk for rows, _ in parts for chunk in rows]
    vectors = np.concatenate([vectors for _, vectors in parts]) if parts else np.empty((0, envelope['embedding']['dim']))
    return {**envelope, 'chunks': chunks}, vectors


def query_semantic_index(index_dir, repo_key, revision, query, *, embedder, limit=8, path_pattern='*',
                         kinds=None):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 512:
        raise SemanticIndexError('invalid_request', 'bounded nonempty query required')
    if type(limit) is not int or not 1 <= limit <= 20:
        raise SemanticIndexError('invalid_request', 'limit must be 1..20')
    path, root = _load_root(index_dir, repo_key, revision)
    if root['schema_version'] == 'ops.navigation-semantic-index.partitioned.v1':
        return _query_partitioned(path, root, repo_key, revision, query, embedder, limit, path_pattern, kinds)
    envelope, vectors = load_semantic_index(index_dir, repo_key, revision)
    expected = envelope['embedding']
    if (embedder.revision.model_id, embedder.revision.model_digest, embedder.dim) != (
            expected['model_id'], expected['model_digest'], expected['dim']):
        raise SemanticIndexError('embedder_mismatch', 'query embedder differs from the index embedding revision')
    import numpy as np
    started = time.monotonic()
    q = np.asarray(embedder.embed([query.strip()])[0], dtype=np.float32)
    norm = float(np.linalg.norm(q)) or 1.0
    cosine = vectors @ (q / norm)
    # Hybrid ranking: a query word appearing in the path or symbol is strong evidence
    # a small general-purpose embedder misses; test files rank behind the code they
    # exercise unless the caller asks about tests. Both signals are reported per hit.
    query_terms = _terms(query)
    wants_tests = 'test' in query.lower()
    adjust = np.zeros(len(cosine), dtype=np.float32)
    for i, chunk in enumerate(envelope['chunks']):
        adjust[i] = _lexical_boost(chunk, query_terms) - (0.0 if wants_tests or not _is_test_path(chunk['path']) else TEST_PENALTY)
    scores = cosine + adjust
    mask = np.ones(len(scores), dtype=bool)
    if path_pattern != '*' or kinds:
        for i, chunk in enumerate(envelope['chunks']):
            if (path_pattern != '*' and not fnmatch.fnmatchcase(chunk['path'], path_pattern)) or (kinds and chunk['kind'] not in kinds):
                mask[i] = False
    candidates = np.flatnonzero(mask)
    order = candidates[np.argsort(-scores[candidates])][:limit]
    results = []
    for i in order:
        chunk = envelope['chunks'][int(i)]
        results.append({**{k: chunk[k] for k in ('path', 'blob_sha', 'start_line', 'end_line', 'kind', 'symbol', 'excerpt_sha256')},
                        'score': round(float(scores[i]), 4), 'cosine': round(float(cosine[i]), 4),
                        'lexical_boost': round(float(max(adjust[i], 0.0)), 4),
                        'test_penalty': bool(adjust[i] < 0 or (adjust[i] == 0 and not wants_tests and _is_test_path(chunk['path']))),
                        'source_revision': revision,
                        'evidence_kind': 'semantic_ranking', 'citation_scope': 'committed-source-span',
                        'retrieval': {'operation': 'navigation.source', 'arguments': {
                            'repo_key': repo_key, 'target_revision': revision, 'path': chunk['path'],
                            'start_line': chunk['start_line'],
                            'line_count': min(200, chunk['end_line'] - chunk['start_line'] + 1)}}})
    return {'schema_version': 'ops.navigation-semantic.v1', 'status': 'ok' if results else 'no_candidates',
            'repo_key': repo_key, 'source_revision': revision, 'query': query.strip(), 'limit': limit,
            'path_pattern': path_pattern, 'results': results, 'candidates_considered': int(mask.sum()),
            'ranking': {'spec': RANKING, 'lexical_weight': LEXICAL_WEIGHT, 'test_penalty': TEST_PENALTY,
                        'test_penalty_applied': not wants_tests},
            'index': {k: envelope[k] for k in ('index_sha256', 'manifest_id', 'chunk_count', 'chunk_kinds',
                                               'files_indexed', 'built_at', 'embedding', 'chunking')},
            'query_seconds': round(time.monotonic() - started, 3),
            'evidence_kind': 'semantic_ranking', 'absence_verdict': 'not-established',
            'runtime_execution': 'not-assessed',
            'limitations': envelope['limitations'] + [
                'Use navigation.search for exhaustive literal coverage; this ranking is a suggestion list over indexed chunks.']}


def _query_partitioned(path, envelope, repo_key, revision, query, embedder, limit, path_pattern, kinds):
    """Keep only one verified partition and the global top K in memory."""
    expected = envelope['embedding']
    if (embedder.revision.model_id, embedder.revision.model_digest, embedder.dim) != (
            expected['model_id'], expected['model_digest'], expected['dim']):
        raise SemanticIndexError('embedder_mismatch', 'query embedder differs from the index embedding revision')
    import numpy as np
    started = time.monotonic()
    q = np.asarray(embedder.embed([query.strip()])[0], dtype=np.float32)
    q /= float(np.linalg.norm(q)) or 1.0
    terms, wants_tests = _terms(query), 'test' in query.lower()
    top = []
    considered = scanned = 0
    for chunks, vectors in _iter_parts(path, envelope):
        scanned += len(chunks)
        cosine = vectors @ q
        for i, chunk in enumerate(chunks):
            if (path_pattern != '*' and not fnmatch.fnmatchcase(chunk['path'], path_pattern)) or (kinds and chunk['kind'] not in kinds):
                continue
            considered += 1
            boost = _lexical_boost(chunk, terms)
            penalty = 0.0 if wants_tests or not _is_test_path(chunk['path']) else TEST_PENALTY
            score = float(cosine[i]) + boost - penalty
            top.append((score, chunk['path'], chunk['start_line'], chunk, float(cosine[i]), boost, penalty))
            if len(top) > limit * 2:
                top.sort(key=lambda row: (-row[0], row[1], row[2]))
                del top[limit:]
    if scanned != envelope['chunk_count']:
        raise SemanticIndexError('index_integrity', 'semantic indexed chunk coverage mismatch')
    top.sort(key=lambda row: (-row[0], row[1], row[2]))
    results = []
    for score, _, _, chunk, cosine, boost, penalty in top[:limit]:
        results.append({**{key: chunk[key] for key in ('path', 'blob_sha', 'start_line', 'end_line', 'kind', 'symbol', 'excerpt_sha256')},
                        'score': round(score, 4), 'cosine': round(cosine, 4),
                        'lexical_boost': round(boost, 4), 'test_penalty': bool(penalty),
                        'source_revision': revision, 'evidence_kind': 'semantic_ranking',
                        'citation_scope': 'committed-source-span',
                        'retrieval': {'operation': 'navigation.source', 'arguments': {
                            'repo_key': repo_key, 'target_revision': revision, 'path': chunk['path'],
                            'start_line': chunk['start_line'],
                            'line_count': min(200, chunk['end_line'] - chunk['start_line'] + 1)}}})
    return {'schema_version': 'ops.navigation-semantic.v1', 'status': 'ok' if results else 'no_candidates',
            'repo_key': repo_key, 'source_revision': revision, 'query': query.strip(), 'limit': limit,
            'path_pattern': path_pattern, 'results': results, 'candidates_considered': considered,
            'ranking': {'spec': RANKING, 'lexical_weight': LEXICAL_WEIGHT, 'test_penalty': TEST_PENALTY,
                        'test_penalty_applied': not wants_tests},
            'index': {key: envelope[key] for key in ('index_sha256', 'manifest_id', 'chunk_count', 'chunk_kinds',
                                                    'files_indexed', 'built_at', 'embedding', 'chunking')},
            'query_seconds': round(time.monotonic() - started, 3), 'evidence_kind': 'semantic_ranking',
            'absence_verdict': 'not-established', 'runtime_execution': 'not-assessed',
            'limitations': envelope['limitations'] + [
                'Use navigation.search for exhaustive literal coverage; this ranking is a suggestion list over indexed chunks.']}
