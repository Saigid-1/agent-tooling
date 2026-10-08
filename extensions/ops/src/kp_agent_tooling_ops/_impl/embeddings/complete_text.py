"""Document-only full-input encoding; source/citation windows remain unchanged."""
from __future__ import annotations
import math
from kp_agent_tooling._impl.embeddings.embedders import RealEmbedder
from kp_agent_tooling._impl.embeddings.revision import EmbeddingRevision

ENCODING = 'char-split-token-budget-weighted-mean-v1'

class CompleteTextEmbedder:
    """Pool bounded model inputs while retaining every original source character.

    This is a separate instrument revision, not a change to other vector partitions.
    Token counts include model special tokens; split boundaries preserve Unicode.
    Pooling guarantees input coverage, not relevance or equal influence of all facts.
    """
    def __init__(self, base: RealEmbedder):
        self._base = base
        revision = base.revision
        self._revision = EmbeddingRevision(
            model_id=revision.model_id, model_digest=revision.model_digest,
            params={**dict(revision.params), 'complete_text_encoding': ENCODING,
                    'complete_text_max_tokens': revision.params.get('max_seq_length', 256)},
            normalization=revision.normalization, chunking_spec=revision.chunking_spec)

    @property
    def revision(self): return self._revision

    @property
    def dim(self): return self._base.dim

    def embed(self, texts):
        if not isinstance(texts, list) or any(not isinstance(t, str) for t in texts):
            raise TypeError('embed expects a list of strings')
        if not texts: return []
        model = self._base._load_model()
        limit = model.max_seq_length
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 2:
            raise ValueError('model token budget must exceed special-token overhead')
        if limit != self.revision.params['complete_text_max_tokens']:
            raise ValueError('model token budget differs from declared instrument')
        def count(text):
            return len(model.tokenizer(text, add_special_tokens=True, truncation=False)['input_ids'])
        def split(text):
            tokens = count(text)
            if tokens <= limit:
                return [(text, max(1, tokens - count('')))]
            if len(text) <= 1:
                raise ValueError('one source character exceeds model token budget')
            middle = len(text)//2
            # Prefer a nearby word boundary while always making progress.
            boundary = text.rfind(' ', max(1, middle//2), middle+1)
            if boundary > 0: middle = boundary+1
            return split(text[:middle]) + split(text[middle:])
        groups = [split(text) for text in texts]
        vectors = self._base.embed([part for group in groups for part, _ in group])
        result = []; offset = 0
        for group in groups:
            pooled = [0.0]*self.dim
            for (_, weight), vector in zip(group, vectors[offset:offset+len(group)], strict=True):
                for i, value in enumerate(vector): pooled[i] += weight*value
            offset += len(group)
            norm = math.sqrt(sum(value*value for value in pooled))
            if not math.isfinite(norm) or norm == 0:
                raise ValueError('complete-text encoding produced an invalid pooled vector')
            result.append(tuple(value/norm for value in pooled))
        return result

def document_embedder(embedder):
    """Only the real document instrument needs the token-complete adapter."""
    return CompleteTextEmbedder(embedder) if isinstance(embedder, RealEmbedder) else embedder
