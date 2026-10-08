"""Pluggable deterministic and open-weights CP-3 embedders."""

from __future__ import annotations

import hashlib
import importlib
import math
from collections.abc import Sequence
from threading import Event, Lock, Thread
from time import monotonic, perf_counter
from typing import Protocol, runtime_checkable

from kp_agent_tooling._impl.embeddings.revision import (
    TEXT_EXTRACTION_REVISION,
    EmbeddingRevision,
)


DEFAULT_REAL_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_REAL_MODEL_DIM = 384
REAL_MODEL_WEIGHTS_DIGEST = (
    "sha256:53aa51172d142c89d9012cce15ae4d6cc0ca6895895114379cacb4fab128d9db"
)
DETERMINISTIC_MODEL_DIGEST = (
    "sha256:66676444224fbbff6f081aca738b2d6ba29dc2908c47ba08086109dd2b98afab"
)
INPUT_HYGIENE_REVISION = "strip-stamp-and-markdown-verbatim-lines-v1"


@runtime_checkable
class Embedder(Protocol):
    """One explicitly revisioned embedding instrument."""

    @property
    def revision(self) -> EmbeddingRevision: ...

    @property
    def dim(self) -> int: ...

    def embed(self, texts: list[str]) -> list[Sequence[float]]: ...


class DeterministicEmbedder:
    """Fixture-only SHA-256-to-unit-vector embedding instrument."""

    def __init__(self, dim: int = 32, *, chunking_spec: str = "coordinate-v1"):
        if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
            raise ValueError("deterministic embedder dim must be positive")
        self._dim = dim
        self._revision = EmbeddingRevision(
            model_id="kp-ops/deterministic-sha256-unit-vector",
            model_digest=DETERMINISTIC_MODEL_DIGEST,
            params={"algorithm": "sha256-counter-signed64-v1", "dim": dim},
            normalization={
                "input_hygiene": INPUT_HYGIENE_REVISION,
                "text_extraction": TEXT_EXTRACTION_REVISION,
                "vector": "l2-unit",
            },
            chunking_spec={"spec": chunking_spec},
        )

    @property
    def revision(self) -> EmbeddingRevision:
        return self._revision

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[Sequence[float]]:
        if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
            raise TypeError("embed expects a list of strings")
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> tuple[float, ...]:
        encoded = text.encode("utf-8")
        raw: list[float] = []
        for index in range(self._dim):
            digest = hashlib.sha256(
                b"kp-ops-cp3\0" + index.to_bytes(8, "big") + encoded
            ).digest()
            integer = int.from_bytes(digest[:8], "big", signed=False) - (1 << 63)
            raw.append(integer / float(1 << 63))
        norm = math.sqrt(sum(value * value for value in raw))
        if norm == 0.0:  # cryptographically unreachable; deterministic fallback
            raw[0] = 1.0
            norm = 1.0
        return tuple(value / norm for value in raw)


class RealEmbedder:
    """Lazy scaffold for the pinned open-weights production instrument.

    The caller must supply the independently verified weights digest. Import
    and model loading occur only on the first ``embed`` call, keeping tests
    model-free and preventing a descriptive model name from posing as a pin.
    """

    def __init__(
        self,
        *,
        model_digest: str,
        model_id: str = DEFAULT_REAL_MODEL_ID,
        dim: int = DEFAULT_REAL_MODEL_DIM,
        params: dict[str, object] | None = None,
        chunking_spec: str = "coordinate-v1",
    ) -> None:
        if not model_digest:
            raise ValueError("RealEmbedder requires a verified weights digest")
        if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
            raise ValueError("real embedder dim must be positive")
        instrument_params = {
            "dim": dim,
            "normalize_embeddings": True,
            **(params or {}),
        }
        self._model_id = model_id
        self._dim = dim
        self._model: object | None = None
        self._model_lock = Lock()
        self._model_load_count = 0
        self._model_load_ms: float | None = None
        self._embed_call_count = 0
        self._last_embed_ms: float | None = None
        self._last_batch_size = 0
        self._revision = EmbeddingRevision(
            model_id=model_id,
            model_digest=model_digest,
            params=instrument_params,
            normalization={
                "input_hygiene": INPUT_HYGIENE_REVISION,
                "text_extraction": TEXT_EXTRACTION_REVISION,
                "vector": "l2-unit",
            },
            chunking_spec={"spec": chunking_spec},
        )

    @property
    def revision(self) -> EmbeddingRevision:
        return self._revision

    @property
    def dim(self) -> int:
        return self._dim

    def _load_model(self) -> object:
        if self._model is None:
            with self._model_lock:
                if self._model is None:
                    started = perf_counter()
                    try:
                        module = importlib.import_module("sentence_transformers")
                    except ImportError as error:
                        raise RuntimeError(
                            "RealEmbedder requires the sentence-transformers package"
                        ) from error
                    model_type = getattr(module, "SentenceTransformer")
                    self._model = model_type(self._model_id)
                    self._model_load_count += 1
                    self._model_load_ms = round((perf_counter() - started) * 1000, 2)
        return self._model

    @property
    def diagnostics(self) -> dict[str, int | float | None]:
        """Content-free, bounded counters for the current embedder instance."""

        return {
            "model_load_count": self._model_load_count,
            "model_load_ms": self._model_load_ms,
            "embed_call_count": self._embed_call_count,
            "last_embed_ms": self._last_embed_ms,
            "last_batch_size": self._last_batch_size,
        }

    def embed(self, texts: list[str]) -> list[Sequence[float]]:
        if not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
            raise TypeError("embed expects a list of strings")
        if not texts:
            return []
        model = self._load_model()
        started = perf_counter()
        self._embed_call_count += 1
        self._last_batch_size = len(texts)
        encoded = model.encode(  # type: ignore[attr-defined]
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
        )
        self._last_embed_ms = round((perf_counter() - started) * 1000, 2)
        vectors = [tuple(float(value) for value in row) for row in encoded]
        if len(vectors) != len(texts) or any(
            len(vector) != self._dim for vector in vectors
        ):
            raise RuntimeError("real embedder returned an unexpected vector shape")
        return vectors


class ProductionLocalEmbedding:
    """Deadline-bounded local wrapper for the pinned all-MiniLM card space.

    ``RealEmbedder`` deliberately has a small, batch-oriented CP-3 interface.
    This adapter is the feature-intake boundary: it admits one bounded text at a
    time and returns to its caller no later than the authoritative monotonic
    deadline. The daemon worker is intentionally not joined after a timeout,
    so a stalled local inference cannot hold the intake request open.
    """

    production_ready = True
    bounds_enforced = True
    local_only = True
    hosted_billing_enabled = False
    hard_deadline_enforced = True

    def __init__(
        self,
        *,
        max_input_utf8_bytes: int = 65_536,
        runner: RealEmbedder | None = None,
    ) -> None:
        if (
            isinstance(max_input_utf8_bytes, bool)
            or not isinstance(max_input_utf8_bytes, int)
            or not 1 <= max_input_utf8_bytes <= 65_536
        ):
            raise ValueError("max_input_utf8_bytes must be a finite positive bound")
        self._runner = runner or RealEmbedder(model_digest=REAL_MODEL_WEIGHTS_DIGEST)
        if not isinstance(self._runner, RealEmbedder):
            raise TypeError("ProductionLocalEmbedding requires a RealEmbedder runner")
        if (
            self._runner.revision.model_id != DEFAULT_REAL_MODEL_ID
            or self._runner.revision.model_digest != REAL_MODEL_WEIGHTS_DIGEST
        ):
            raise ValueError("ProductionLocalEmbedding requires the verified MiniLM pin")
        self.max_input_utf8_bytes = max_input_utf8_bytes
        self.revision = self._runner.revision
        self.revision_id = self.revision.revision_id
        self.revision_digest = self.revision.model_digest

    def embed_one(
        self,
        text: str,
        *,
        deadline_monotonic: float,
    ) -> Sequence[float]:
        if not isinstance(text, str):
            raise TypeError("embedding input must be text")
        if len(text.encode("utf-8")) > self.max_input_utf8_bytes:
            raise ValueError("embedding input exceeds configured byte bound")
        if (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(deadline_monotonic)
            or monotonic() >= deadline_monotonic
        ):
            raise TimeoutError("embedding deadline exceeded")
        result: list[Sequence[float]] = []
        failure: list[BaseException] = []
        complete = Event()

        def run() -> None:
            try:
                result.extend(self._runner.embed([text]))
            except BaseException as error:
                failure.append(error)
            finally:
                complete.set()

        worker = Thread(target=run, name="kp-ops-local-embedding", daemon=True)
        worker.start()
        remaining = max(0.0, float(deadline_monotonic) - monotonic())
        if not complete.wait(remaining) or monotonic() >= deadline_monotonic:
            raise TimeoutError("embedding deadline exceeded")
        if failure:
            raise failure[0]
        if len(result) != 1:
            raise RuntimeError("local embedder returned an unexpected batch shape")
        return result[0]
