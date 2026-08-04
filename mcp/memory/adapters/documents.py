"""Plain-text / Markdown adapter (also serves news documents).

One file -> one :class:`SourceDocument`, prose-chunked.  ``source_type`` is
``document`` by default or ``news`` (which requires a ``published_at`` supplied
by the caller / dispatch layer).
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from ..chunking import chunk_prose
from .base import ChunkInput, SourceDocument, _doc_id, sha256_bytes


def _title_from(path: Path, text: str) -> str:
    """Prefer a leading Markdown H1, else the file stem."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or path.stem
        if stripped:
            break
    return path.stem


def load(
    path: Path,
    source_type: Optional[str] = None,
    published_at: Optional[str] = None,
) -> List[SourceDocument]:
    """Load one md/txt file into a single SourceDocument.

    The doc id is derived from the file path only (never the source_type label),
    so one file maps to exactly one document regardless of whether it is ingested
    as ``document`` or ``news`` — re-labeling updates the existing row rather than
    creating a duplicate.
    """
    st = source_type or "document"

    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    content_hash = sha256_bytes(raw)
    abspath = str(path.resolve())
    doc_id = _doc_id("doc", abspath)
    title = _title_from(path, text)

    chunks = [
        ChunkInput(seq=i, text=chunk_text, meta={})
        for i, chunk_text in enumerate(chunk_prose(text))
    ]

    return [
        SourceDocument(
            doc_id=doc_id,
            title=title,
            source_type=st,
            source_path=abspath,
            content_hash=content_hash,
            provenance="uploaded",
            published_at=published_at,
            chunks=chunks,
        )
    ]
