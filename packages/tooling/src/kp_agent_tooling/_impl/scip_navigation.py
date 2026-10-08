"""Revision-pinned SCIP source facts. Exact symbols are not runtime calls."""
import hashlib
import json
from pathlib import Path
import re

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.source_citations import source

MAX_PART_BYTES = 16 * 1024 * 1024
MAX_INDEX_BYTES = 128 * 1024 * 1024
MAX_PARTS = 64


def _bytes(value):
    return leaf.canonical_bytes(value, ascii=True, allow_nan=True)


def _atomic(path, raw):
    return leaf.replace_file(path, raw, temp_prefix='.scip-', cleanup='if-exists')


def write_partitioned_index(path, envelope):
    """Publish a small identity manifest and bounded occurrence files atomically.

    A published manifest is written last, after every named partition exists. Readers
    verify all parts before accepting coverage, including parts not queried.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = validate_index(envelope, envelope['data']['revision'])
    base = {key: value for key, value in data.items() if key != 'occurrences'}
    rows, parts, batch, batch_bytes = data['occurrences'], [], [], 2
    for row in rows:
        row_bytes = len(_bytes(row))
        if row_bytes + 2 > MAX_PART_BYTES:
            raise ValueError('single SCIP occurrence exceeds partition budget')
        if batch and batch_bytes + row_bytes + 1 > MAX_PART_BYTES:
            parts.append(batch)
            batch, batch_bytes = [], 2
        batch.append(row); batch_bytes += row_bytes + (1 if len(batch) > 1 else 0)
    if batch:
        parts.append(batch)
    if len(parts) > MAX_PARTS:
        raise ValueError('SCIP partition count budget exceeded')
    descriptors = []
    for number, batch in enumerate(parts):
        raw = _bytes(batch)
        sha = leaf.sha256_hex(raw)
        name = f'{path.name}.{number:03d}.{sha[:16]}.part.json'
        _atomic(path.with_name(name), raw)
        descriptors.append({'file': name, 'sha256': sha, 'bytes': len(raw), 'occurrences': len(batch)})
    manifest_data = {**base, 'schema': 'ops.scip-navigation.partitioned.v1',
                     'occurrence_count': len(rows), 'parts': descriptors}
    manifest = {'sha256': digest(manifest_data), 'data': manifest_data}
    if len(_bytes(manifest)) > MAX_INDEX_BYTES:
        raise ValueError('SCIP manifest budget exceeded')
    _atomic(path, _bytes(manifest) + b'\n')
    return manifest


def _partition_rows(path, parts):
    for part in parts:
        name = part['file']
        if not isinstance(name, str) or '\\' in name or Path(name).name != name or not name.endswith('.part.json'):
            raise ValueError('invalid SCIP partition path')
        item = path.with_name(name)
        if item.stat().st_size != part['bytes'] or part['bytes'] > MAX_PART_BYTES:
            raise ValueError('SCIP partition byte mismatch')
        raw = item.read_bytes()
        if hashlib.sha256(raw).hexdigest() != part['sha256']:
            raise ValueError('SCIP partition digest mismatch')
        batch = json.loads(raw)
        if not isinstance(batch, list) or len(batch) != part['occurrences']:
            raise ValueError('SCIP partition coverage mismatch')
        yield batch


def load_partitioned_index(path, *, expected_sha256=None, hydrate=True):
    path = Path(path)
    if path.stat().st_size > MAX_INDEX_BYTES:
        raise ValueError('index budget exceeded')
    envelope = json.loads(path.read_bytes())
    if expected_sha256 is not None and envelope['sha256'] != expected_sha256:
        raise ValueError('configured index digest mismatch')
    data = envelope['data']
    if data.get('schema') != 'ops.scip-navigation.partitioned.v1':
        validate_index(envelope, data['revision'])
        return envelope
    if envelope['sha256'] != digest(data):
        raise ValueError('index digest mismatch')
    parts = data['parts']
    if not isinstance(parts, list) or len(parts) > MAX_PARTS:
        raise ValueError('SCIP partition count budget exceeded')
    rows = [] if hydrate else None
    count = 0
    for batch in _partition_rows(path, parts):
        for row in batch:
            if data['blobs'].get(_path(row['path'])) != row['blob_sha']:
                raise ValueError('SCIP partition source identity mismatch')
        count += len(batch)
        if hydrate:
            rows.extend(batch)
    if count != data['occurrence_count']:
        raise ValueError('SCIP occurrence coverage mismatch')
    return {'sha256': envelope['sha256'], 'data': {**data, **({'occurrences': rows} if hydrate else {})},
            **({} if hydrate else {'_parts_root': str(path)})}


def _occurrences(envelope):
    data = envelope['data']
    if data.get('schema') != 'ops.scip-navigation.partitioned.v1' or '_parts_root' not in envelope:
        yield from data['occurrences']
        return
    count = 0
    for batch in _partition_rows(Path(envelope['_parts_root']), data['parts']):
        count += len(batch)
        yield from batch
    if count != data['occurrence_count']:
        raise ValueError('SCIP occurrence coverage mismatch')


def digest(value):
    return leaf.canonical_sha256(value, ascii=True, allow_nan=True)


def _path(value):
    if not isinstance(value, str) or not value or '\\' in value or any(
            p in {'', '.', '..'} for p in value.split('/')):
        raise ValueError('canonical relative source path required')
    return value


def _span(text, positions):
    # Pinned indexers use UTF-16 source columns. Do not split a surrogate pair.
    if len(positions) == 3:
        start_line, start_col, end_col = positions
        end_line = start_line
    elif len(positions) == 4:
        start_line, start_col, end_line, end_col = positions
    else:
        raise ValueError('unsupported SCIP range')
    if any(type(n) is not int or n < 0 for n in positions):
        raise ValueError('invalid SCIP coordinate')
    lines = text.splitlines(keepends=True)
    def offset(line, column):
        if line == len(lines) and column == 0:
            return len(text.encode())
        if line >= len(lines):
            raise ValueError('line outside blob')
        raw = lines[line].encode('utf-16-le')
        if column * 2 > len(raw):
            raise ValueError('column outside line')
        prefix = raw[:column * 2].decode('utf-16-le')
        return len(''.join(lines[:line]).encode()) + len(prefix.encode())
    start, end = offset(start_line, start_col), offset(end_line, end_col)
    if end <= start:
        raise ValueError('empty or reversed occurrence')
    return start, end - start


def build_index(repo, revision, repo_key, decoded, *, provenance, prefix=''):
    if not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', revision):
        raise ValueError('full commit required')
    instrument = decoded.get('metadata', {}).get('tool_info', {})
    if (instrument.get('name'), instrument.get('version')) not in {
            ('scip-python', '0.6.6'), ('scip-typescript', '0.4.0')}:
        raise ValueError('unreviewed SCIP producer')
    if prefix:
        _path(prefix)
    rows, gaps, blobs = [], [], {}
    documents = decoded.get('documents', [])
    if len(documents) > 10000:
        raise ValueError('document budget exceeded')
    for doc in documents:
        path = _path((prefix + '/' if prefix else '') + _path(doc['relative_path']))
        blob, text = source(repo, revision, path)
        if (Path(repo)/path).read_bytes() != text.encode():
            raise ValueError('indexed checkout differs from declared blob: ' + path)
        blobs[path] = blob
        for occurrence in doc.get('occurrences', []):
            symbol = occurrence.get('symbol', '')
            if not isinstance(symbol, str) or not symbol or len(symbol) > 4096:
                continue
            try:
                start, length = _span(text, occurrence['range'])
            except (KeyError, ValueError, UnicodeError):
                gaps.append({'path':path, 'reason':'unsupported_or_synthetic_range'})
                continue
            rows.append({'symbol':symbol, 'path':path, 'blob_sha':blob,
                'byte_offset':start, 'byte_length':length,
                'definition': bool(occurrence.get('symbol_roles',0) & 1),
                'local':symbol.startswith('local ')})
            if len(rows) > 500000:
                raise ValueError('occurrence budget exceeded')
    data = {'schema':'ops.scip-navigation.v1','repo_key':repo_key,'revision':revision,
            'producer':instrument,'provenance':provenance,'blobs':blobs,'occurrences':rows,
            'gaps':gaps,'limitations':['Static occurrence/definition evidence; not invocation, reachability or interface compatibility.',
                'Only indexed committed documents are covered; unresolved dependencies are not absence proof.']}
    return {'sha256':digest(data),'data':data}


def validate_index(envelope, revision):
    data = envelope['data']
    identity = {k:v for k,v in data.items() if k != 'occurrences'} if data.get('schema') == 'ops.scip-navigation.partitioned.v1' else data
    if envelope['sha256'] != digest(identity):
        raise ValueError('index digest mismatch')
    if data.get('schema') not in {'ops.scip-navigation.v1', 'ops.scip-navigation.partitioned.v1'} or data.get('revision') != revision:
        raise ValueError('index revision mismatch')
    if data.get('schema') == 'ops.scip-navigation.partitioned.v1' and '_parts_root' not in envelope and len(data.get('occurrences', [])) != data['occurrence_count']:
        raise ValueError('SCIP occurrence coverage mismatch')
    return data


def lookup(envelope, *, revision, repo, path, line, limit=20):
    data = validate_index(envelope, revision)
    _path(path)
    if type(line) is not int or line < 1 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('bounded positive line/limit required')
    blob, text = source(repo, revision, path)
    if data['blobs'].get(path) != blob:
        raise ValueError('SCIP source blob mismatch or not indexed')
    lines = text.splitlines(keepends=True)
    if line > len(lines):
        raise ValueError('line outside source')
    start = len(''.join(lines[:line-1]).encode()); end = start + len(lines[line-1].encode())
    matches, total = [], 0
    for row in _occurrences(envelope):
        if row['path']==path and start <= row['byte_offset'] < end:
            total += 1
            if len(matches) < limit:
                matches.append(row)
    return {'status':'ok' if total else 'no_results','repo_key':data['repo_key'],
        'revision':revision,'index_sha256':envelope['sha256'],'occurrences':matches,
        'omitted':total-len(matches),'absence_verdict':'not-established',
        'runtime':'not-assessed','limitations':data['limitations']}


def definitions(symbol, catalogs, *, limit=20):
    """Catalog entries explicitly pin each allowed repository revision and index.

    Package/version equality produces candidates, never automatic package-to-commit
    equivalence. Multiple definitions remain ambiguous; local symbols never join.
    """
    if not isinstance(symbol,str) or not symbol or len(symbol)>4096 or symbol.startswith('local '):
        raise ValueError('global SCIP symbol required')
    if type(limit) is not int or not 1 <= limit <= 50 or len(catalogs)>32:
        raise ValueError('bounded catalog required')
    results = []
    total = 0
    for item in catalogs:
        data = validate_index(item['index'], item['revision'])
        for row in _occurrences(item['index']):
            if row['symbol']==symbol and row['definition'] and not row['local']:
                blob, text = source(item['repo'], item['revision'], row['path'])
                if blob != row['blob_sha']:
                    raise ValueError('definition blob mismatch')
                line = text.encode()[:row['byte_offset']].count(b'\n') + 1
                total += 1
                if len(results) < limit:
                    results.append({**row,'line':line,'repo_key':data['repo_key'],'revision':item['revision'],
                                    'index_sha256':item['index']['sha256']})
    return {'status':'source_candidates' if total else 'unresolved','definitions':results,
        'omitted':total-len(results),'ambiguous':total>1,
        'runtime':'not-assessed','package_build_equivalence':'not-established',
        'boundary_inference':'not-performed'}
