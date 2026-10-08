"""Transport a fully materialized reference generation without a graph reader."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path

from kp_agent_tooling._impl import leaf

from .manifest import FrozenReferenceRetrieval, ManifestShard


def generation_document(frozen):
    shards = tuple(frozen._shards.values())
    return {'schema_version':'agent-tooling.reference-generation.v1',
            'metadata':frozen.generation_metadata,
            'shards':[asdict(s) for s in shards],
            'views':{s.shard_id:frozen.retrieval_view(s,None) for s in shards}}


def load_generation(path, *, sha256):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or leaf.shared_bits(path.stat().st_mode):
        raise ValueError('reference generation requires an owner-private regular file')
    if path.stat().st_size > 64*1024*1024:
        raise ValueError('reference generation exceeds 64 MiB')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError('reference generation digest mismatch')
    doc = json.loads(raw)
    if set(doc) != {'schema_version','metadata','shards','views'} or doc['schema_version'] != 'agent-tooling.reference-generation.v1':
        raise ValueError('invalid reference generation envelope')
    shards = tuple(ManifestShard(**{**row,'outcomes':tuple(row['outcomes'])}) for row in doc['shards'])
    if len({s.shard_id for s in shards}) != len(shards) or set(doc['views']) != {s.shard_id for s in shards}:
        raise ValueError('reference shard/view coverage mismatch')
    metadata = doc['metadata']
    frozen = FrozenReferenceRetrieval(shards,doc['views'],source_repo_key=metadata['source_repo_key'],target_revisions=metadata['target_revisions'])
    if frozen.generation_metadata != metadata:
        raise ValueError('reference serving generation identity mismatch')
    return frozen
