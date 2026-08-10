"""Source adapters: turn files into :class:`SourceDocument` objects.

Pure Python (no heavy dependencies).  Each adapter owns the format-specific
parsing AND the chunking strategy appropriate to that format (prose vs.
message-boundary), producing documents + chunks the store can persist verbatim.
"""

from .base import (
    VALID_SOURCE_TYPES,
    ChunkInput,
    SourceDocument,
    detect_and_load,
    load_path,
    validate_iso_date,
)

__all__ = [
    "VALID_SOURCE_TYPES",
    "ChunkInput",
    "SourceDocument",
    "detect_and_load",
    "load_path",
    "validate_iso_date",
]
