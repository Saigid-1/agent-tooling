"""Deterministic document-to-code reference extraction and resolution."""

from .extraction import EXTRACTION_POLICY_VERSION, extract_code_references
from .models import ChunkRange, ReferenceCandidate, Resolution
from .resolution import RESOLUTION_POLICY_VERSION, ReferenceResolver

__all__ = [
    "ChunkRange", "EXTRACTION_POLICY_VERSION", "RESOLUTION_POLICY_VERSION",
    "ReferenceCandidate", "ReferenceResolver", "Resolution",
    "extract_code_references",
]
