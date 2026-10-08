"""Read immutable document coordinates/vectors without a graph runtime.

The operator pins the corpus bytes and repository roots. Rankings reuse the
existing cosine implementation; selected text is recovered from Git and checked
against the original chunk digest. This reader does not ingest or authorize.
"""
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path

from kp_agent_tooling._impl import leaf

from ..embeddings.desk_docs import build_desk_doc_embedding_input, desk_doc_embedding_revision
from ..embeddings.store import MemoryVectorStore
from ..embeddings.complete_text import document_embedder


class DocumentCorpus:
    def __init__(self, path, *, sha256, repositories, embedder):
        self.path = Path(path)
        if (not self.path.is_absolute() or self.path.is_symlink()
                or not self.path.is_file() or self.path.stat().st_uid != os.getuid()
                or leaf.shared_bits(self.path.stat().st_mode)):
            raise ValueError('document corpus requires an owner-private regular file')
        if self.path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('document corpus exceeds 64 MiB')
        raw = self.path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha256:
            raise ValueError('document corpus digest mismatch')
        data = json.loads(raw)
        if set(data) != {'schema_version', 'chunks', 'vectors'} or data['schema_version'] != 'agent-tooling.document-corpus.v1':
            raise ValueError('invalid document corpus envelope')
        if not isinstance(data['chunks'], list) or not isinstance(data['vectors'], list) or max(len(data['chunks']), len(data['vectors'])) > 10000:
            raise ValueError('bounded document corpus required')
        self.sha256, self.embedder = sha256, document_embedder(embedder)
        self.repositories = {key: str(Path(value).resolve(strict=True)) for key, value in repositories.items()}
        self.chunks = {}
        for row in data['chunks']:
            if row.get('entity_type') != 'DeskDocumentChunk' or row['id'] in self.chunks:
                raise ValueError('invalid or duplicate document chunk')
            self.chunks[row['id']] = row
        self.vectors = MemoryVectorStore()
        for row in data['vectors']:
            if row['content_kind'] != 'desk_doc':
                raise ValueError('non-document vector in corpus')
            vector = json.loads(row['vector']) if isinstance(row['vector'], str) else row['vector']
            self.vectors.upsert(row['chunk_content_id'], row['revision_id'], vector,
                content_digest=row['content_digest'], provenance=row['provenance'],
                content_kind='desk_doc', embedding_model_id=row['embedding_model_id'])
        self.reconciliation = {
            'chunks':len(self.chunks), 'vectors':len(self.vectors.records),
            'orphan_vector_ids':sorted({v.chunk_content_id for v in self.vectors.records} - self.chunks.keys()),
            'chunks_without_vectors':sorted(self.chunks.keys() - {v.chunk_content_id for v in self.vectors.records}),
        }

    def recall(self, *, query, top_k, repo_keys=None, paths=None):
        if not isinstance(query, str) or not query.strip() or type(top_k) is not int or not 1 <= top_k <= 400:
            raise ValueError('bounded document query required')
        # A replacement/withdrawal must never silently continue using cached data.
        if self.path.is_symlink() or self.path.stat().st_size > 64*1024*1024 or hashlib.sha256(self.path.read_bytes()).hexdigest() != self.sha256:
            raise ValueError('document corpus changed; repin required')
        revision = desk_doc_embedding_revision(self.embedder)
        self.vectors.assert_embedding_model(revision.model_id)
        encoded = self.embedder.embed([query])
        if len(encoded) != 1 or len(encoded[0]) != self.embedder.dim or any(not math.isfinite(v) for v in encoded[0]):
            raise ValueError('invalid document query vector')
        candidates = self.vectors.ann_candidates(encoded[0], top_k=max(1,len(self.vectors.records)),
            revision_id=revision.revision_id, content_kind='desk_doc')
        hits = []
        for candidate in candidates:
            node = self.chunks.get(candidate.chunk_content_id)
            if node is None:
                continue
            attr = node['attributes']
            if attr.get('is_current') is not True or (repo_keys is not None and attr['repo_key'] not in repo_keys) or (paths is not None and attr['path'] not in paths):
                continue
            coordinates = {k:attr.get(k) for k in ('repo_key','path','blob_sha','byte_offset','byte_length','heading','local_path')}
            if coordinates != dict(candidate.provenance):
                raise ValueError('document vector coordinate differs from chunk')
            if attr['repo_key'] not in self.repositories:
                raise ValueError('document repository root is not configured')
            relocated = deepcopy(node)
            relocated['attributes']['local_path'] = self.repositories[attr['repo_key']]
            item = build_desk_doc_embedding_input(relocated)
            record = self.vectors.get(candidate.chunk_content_id, candidate.revision_id, content_kind='desk_doc')
            if item.content_digest != record.content_digest:
                raise ValueError('document vector digest differs from source')
            hits.append({k:attr[k] for k in ('repo_key','path','blob_sha','byte_offset','byte_length','heading')})
            hits[-1].update(text=item.text, score=float(candidate.vector_score))
            if len(hits) == top_k:
                break
        return tuple(hits)
