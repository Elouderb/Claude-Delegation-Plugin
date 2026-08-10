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


# Cap for a derived title so it fits the central store's title column
# (memory.documents.title is NVARCHAR(1000), migration 008): an unbounded H1
# would otherwise overflow at INSERT and drop the whole document into
# documents_failed. The first-line fallback is capped tighter (a title, not a
# paragraph).
_MAX_TITLE_CHARS = 1000
_MAX_FIRST_LINE_TITLE_CHARS = 200


def title_from_content(text: str, fallback: str = "Untitled") -> str:
    """Derive a title for hub-ingested content that has no filename.

    Prefers a leading Markdown H1, else the first non-empty line, else
    ``fallback`` — always truncated to fit the store's NVARCHAR(1000) title
    column.  Distinct from :func:`_title_from` (whose non-H1 fallback is the file
    *stem*): the content path has no path to fall back to, so it uses the first
    line instead.  Used by :func:`memory_core.adapters.base.load_content`.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return (stripped[2:].strip() or fallback)[:_MAX_TITLE_CHARS]
        if stripped:
            return stripped[:_MAX_FIRST_LINE_TITLE_CHARS]
    return fallback


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
