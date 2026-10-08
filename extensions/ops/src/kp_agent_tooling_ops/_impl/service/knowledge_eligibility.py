"""Shared, fail-closed document admission, independent of ranking and transport."""
from dataclasses import asdict, is_dataclass


def eligible_paths(repository, *, paths=None):
    selected = set(paths) if paths is not None else None
    return tuple(sorted(path for path, declaration in repository.get('artifacts', {}).items()
                        if declaration.get('status') == 'maintained'
                        and declaration.get('owner')
                        and (selected is None or path in selected)))


def eligible_rows(repository, hits, *, paths=None):
    allowed = set(eligible_paths(repository, paths=paths))
    rows = []
    for hit in hits:
        row = asdict(hit) if is_dataclass(hit) else dict(hit)
        if not isinstance(row.get('path'), str) or not isinstance(row.get('repo_key'), str):
            raise ValueError('document candidate lacks source identity')
        if row.get('repo_key') == repository['corpus_scope'] and row.get('path') in allowed:
            rows.append(row)
    return rows


def matching_revision_rows(source, rows):
    """Admit only indexed bytes proved to be the file at the requested commit.

    A blob ID is content identity, not an ingestion revision. Keep the two facts
    separate, and fail closed when the path or blob cannot be verified.
    """
    admitted, excluded = [], []
    observed = {}
    for row in rows:
        path, indexed_blob = row['path'], row.get('blob_sha')
        if path not in observed:
            observed[path] = source.entry(path)
        entry = observed[path]
        target_blob = entry[1] if entry and entry[0] in {'100644', '100755'} else None
        if (isinstance(indexed_blob, str) and target_blob is not None
                and indexed_blob == target_blob):
            admitted.append({**row, 'document_revision': source.revision,
                             'indexed_blob_sha': indexed_blob,
                             'target_blob_sha': target_blob,
                             'revision_proof': 'exact_path_blob_match',
                             'corpus_lineage': 'unknown'})
        else:
            excluded.append({'path': path, 'indexed_blob_sha': indexed_blob,
                             'target_blob_sha': target_blob,
                             'reason': ('target_path_unavailable' if target_blob is None
                                        else 'indexed_blob_differs_from_target')})
    return admitted, excluded
