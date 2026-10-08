"""Frozen instrument and run identities for CP-3 embeddings."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from kp_agent_tooling._impl import leaf


TEXT_EXTRACTION_REVISION = "jsonl-message-text-v1"
_NOT_FINITE = "embedding identity fields must be finite JSON values"


def _canonical_json(value: object) -> str:
    """Encode a JSON value in the single form used by every CP-3 digest."""

    try:
        return leaf.canonical_json(value, ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError(_NOT_FINITE) from error


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {str(key): _freeze_json(child) for key, child in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(child) for child in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def _frozen_mapping(value: Mapping[str, object], *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    canonical = _canonical_json(dict(value))
    decoded = json.loads(canonical)
    if not isinstance(decoded, dict):  # pragma: no cover - guarded by Mapping above
        raise TypeError(f"{field_name} must encode a JSON object")
    return _freeze_json(decoded)  # type: ignore[return-value]


def _sha256_identity(namespace: str, value: object) -> str:
    return leaf.canonical_identity(namespace, value, ascii=False, allow_nan=False,
                                   refuse=lambda: ValueError(_NOT_FINITE))


@dataclass(frozen=True, slots=True)
class EmbeddingRevision:
    """Complete, immutable identity of an embedding instrument.

    ``model_id`` is descriptive; ``model_digest`` pins the actual weights.
    The remaining mappings bind inference parameters, input/vector
    normalization, and the upstream chunking contract.
    """

    model_id: str
    model_digest: str
    params: Mapping[str, object] = field(default_factory=dict)
    normalization: Mapping[str, object] = field(default_factory=dict)
    chunking_spec: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.model_id:
            raise ValueError("model_id must not be empty")
        if not self.model_digest:
            raise ValueError("model_digest must not be empty")
        object.__setattr__(
            self, "params", _frozen_mapping(self.params, field_name="params")
        )
        object.__setattr__(
            self,
            "normalization",
            _frozen_mapping(self.normalization, field_name="normalization"),
        )
        object.__setattr__(
            self,
            "chunking_spec",
            _frozen_mapping(self.chunking_spec, field_name="chunking_spec"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "params": _thaw_json(self.params),
            "normalization": _thaw_json(self.normalization),
            "chunking_spec": _thaw_json(self.chunking_spec),
        }

    @property
    def revision_id(self) -> str:
        return _sha256_identity("embedding-revision", self.as_dict())


def embedding_identity(content_digest: str, revision: EmbeddingRevision) -> str:
    """Widen S5's ``digest(content_digest + model_version)`` lineage.

    The revision digest replaces the old descriptive model version with the
    complete instrument identity while preserving content-plus-instrument as
    the identity preimage.
    """

    if not content_digest:
        raise ValueError("content_digest must not be empty")
    digest = leaf.sha256_hex((content_digest + revision.revision_id).encode("utf-8"))
    return f"embedding:sha256:{digest}"
