"""Adapter dispatch and the shared document/chunk data model.

``detect_and_load`` walks a file or directory, routes each file to the right
adapter (ChatGPT ``conversations.json`` export vs. plain md/txt), and returns a
flat list of :class:`SourceDocument` plus a list of per-file errors.  doc ids
are deterministic and derived from the absolute path (prose) or the conversation
id (ChatGPT) — never from the source_type label — so re-ingesting or re-labeling
the same source maps to the same document and replaces it idempotently.

Directory ingestion is fault-tolerant per file: a single malformed/unreadable
file is recorded in the returned error list and the walk continues, rather than
aborting the whole batch mid-directory.  A single explicitly-named file that is
unsupported or unparseable surfaces a clear error instead of a silent no-op.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# Extensions handled as prose documents / news.
_PROSE_SUFFIXES = {".md", ".markdown", ".txt", ".text"}

# The single source of truth for stored source_type values (the documents table
# validates against this in app code — there is no DB CHECK constraint, so new
# Phase-1 types can be added here without a schema rebuild).
VALID_SOURCE_TYPES = ("document", "chatgpt_conversation", "news")
# source_type values a *caller* may request for a prose file.  The
# ``chatgpt_conversation`` type is adapter-assigned only — md/txt must never be
# labeled a conversation (it would poison the source_type filter).
USER_SOURCE_TYPES = ("document", "news")

# Bytes of a candidate .json to sniff for the ChatGPT export shape without
# fully parsing arbitrary (possibly huge) files.
_SNIFF_BYTES = 65536
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


@dataclass
class ChunkInput:
    """One chunk of a source document, pre-chunking-strategy applied."""

    seq: int
    text: str
    meta: dict = field(default_factory=dict)


@dataclass
class SourceDocument:
    """A document plus its ordered chunks, ready for the store to persist."""

    doc_id: str
    title: str
    source_type: str
    source_path: Optional[str]
    content_hash: str
    provenance: str
    published_at: Optional[str]
    chunks: List[ChunkInput]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _doc_id(prefix: str, key: str) -> str:
    """Deterministic, colon-free doc id (chunk ids are ``mem:<doc_id>:<seq>``)."""
    return f"{prefix}_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"


def validate_iso_date(value: Optional[str], field_name: str) -> Optional[str]:
    """Validate an ISO ``YYYY-MM-DD`` (prefix) date, or raise ValueError.

    Requires zero-padded ``YYYY-MM-DD`` so string-prefix comparisons (used by
    the date filters) are correct, and confirms it is a real calendar date.  A
    trailing time component (e.g. ``2023-11-14T09:00:00+00:00``) is accepted.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not _ISO_DATE_RE.match(s):
        raise ValueError(
            f"{field_name} must be an ISO date (YYYY-MM-DD prefix), got {value!r}"
        )
    try:
        datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid calendar date: {value!r}") from exc
    return s


def _looks_like_chatgpt_export(path: Path) -> bool:
    """Cheaply decide whether a .json file is a ChatGPT conversations export.

    Uses the filename when it is unambiguous, else sniffs only a bounded prefix
    for the ``"mapping"`` key rather than fully parsing an arbitrary file.  A
    false positive is harmless: the ChatGPT adapter yields no documents for a
    file that is not actually an export.
    """
    if path.suffix.lower() != ".json":
        return False
    if path.name.lower() == "conversations.json":
        return True
    try:
        with path.open("rb") as fh:
            prefix = fh.read(_SNIFF_BYTES)
    except OSError:
        return False
    return b'"mapping"' in prefix


def load_path(
    path: Path,
    source_type: Optional[str],
    published_at: Optional[str],
) -> List[SourceDocument]:
    """Load a single file into one-or-more SourceDocuments via the right adapter.

    May raise (e.g. malformed JSON) — callers walking a directory catch and
    record per-file, and single-file callers surface the error.
    """
    # Imported here to keep the package import graph acyclic and lazy.
    from . import chatgpt, documents

    if _looks_like_chatgpt_export(path):
        return chatgpt.load(path)
    if path.suffix.lower() in _PROSE_SUFFIXES:
        return documents.load(path, source_type=source_type, published_at=published_at)
    return []


def _content_looks_like_chatgpt(content: str) -> bool:
    """Cheaply decide whether raw content is a ChatGPT conversations export.

    The content analog of :func:`_looks_like_chatgpt_export` (which sniffs a
    file): the payload must start like JSON (``{``/``[``) and contain the
    ``"mapping"`` key within a bounded prefix.  A false positive is harmless —
    :func:`chatgpt.load_export` yields no documents for a non-export JSON.
    """
    head = content.lstrip()[:1]
    if head not in ("{", "["):
        return False
    return '"mapping"' in content[:_SNIFF_BYTES]


def load_content(
    content: str,
    *,
    source_type: Optional[str] = None,
    source_path: Optional[str] = None,
    published_at: Optional[str] = None,
    mint_doc_id: Optional[Callable[[], str]] = None,
) -> List[SourceDocument]:
    """Build SourceDocuments from raw in-memory ``content`` (the hub ingest path).

    The content analog of :func:`detect_and_load` / :func:`load_path`: the central
    API receives document content over HTTP rather than a filesystem path, so this
    reuses the SAME adapters/chunking (``chatgpt.load_export``, ``chunk_prose``,
    ``sha256_text``, ``_doc_id``) with no re-implementation or behavioral drift.

    Source-type validation mirrors :func:`detect_and_load` exactly (up-front,
    before detection): ``source_type`` must be one of :data:`USER_SOURCE_TYPES`
    (``chatgpt_conversation`` is adapter-assigned, never a caller value), ``news``
    requires ``published_at``, and ``published_at`` is validated as an ISO date.

    Identity (the hub's dual-key policy):

    * A ChatGPT export keeps each conversation's own conversation-derived id
      (``cg_...``), exactly like the file path.
    * Prose derives a deterministic ``_doc_id("doc", source_path)`` when a
      ``source_path`` identity key is supplied (so re-ingesting the same source
      replaces it), else ``mint_doc_id()`` when provided (the API injects
      ``contracts.identifiers.new_id(MEMORY)`` → a fresh ``mem_<ULID>``), else a
      deterministic content-hash-derived id (standalone fallback).
    """
    if not isinstance(content, str):
        raise ValueError("content must be a string")

    # Up-front source_type validation, byte-identical to detect_and_load's.
    if source_type is not None and source_type not in USER_SOURCE_TYPES:
        raise ValueError(
            f"invalid source_type {source_type!r}; expected one of {USER_SOURCE_TYPES} "
            "('chatgpt_conversation' is adapter-assigned, not a caller option)"
        )
    if source_type == "news" and not published_at:
        raise ValueError("source_type 'news' requires published_at")
    published_at = validate_iso_date(published_at, "published_at")

    # Imported here (like load_path) to keep the package import graph acyclic.
    from . import chatgpt, documents

    if _content_looks_like_chatgpt(content):
        if source_path:
            # Stable identity: a conversation with no id of its own keys off
            # "<source_path>#<index>", so re-ingesting the same export replaces it.
            return chatgpt.load_export(content, source_path)
        # Anonymous ingest (no source_path) has NO stable identity. A conversation
        # WITH its own conversation_id still keys off that id (re-ingesting the same
        # conversation dedups by content hash at the store), but an ID-LESS
        # conversation must NOT collapse onto a constant sentinel-derived id — that
        # bug made two unrelated anonymous ingests share a doc_id and the second
        # silently OVERWRITE the first. Route a FRESH per-request token through the
        # adapter's "<base>#<index>" fallback so each id-less conversation gets a
        # unique id (mirroring the prose path, which mints a fresh mem_<ULID> when
        # source_path is None), then blank the synthetic source_path back to None
        # (it is not a real client path). The store's global content-hash UNIQUE
        # then dedups genuinely-identical re-ingests without ever overwriting.
        token = mint_doc_id() if mint_doc_id is not None else sha256_text(content)
        docs = chatgpt.load_export(content, token)
        for doc in docs:
            doc.source_path = None
        return docs

    st = source_type or "document"
    content_hash = sha256_text(content)
    if source_path:
        doc_id = _doc_id("doc", source_path)
    elif mint_doc_id is not None:
        doc_id = mint_doc_id()
    else:
        doc_id = _doc_id("doc", content_hash)
    chunks = [
        ChunkInput(seq=i, text=chunk_text, meta={})
        for i, chunk_text in enumerate(documents.chunk_prose(content))
    ]
    return [
        SourceDocument(
            doc_id=doc_id,
            title=documents.title_from_content(content),
            source_type=st,
            source_path=source_path,
            content_hash=content_hash,
            provenance="uploaded",
            published_at=published_at,
            chunks=chunks,
        )
    ]


def detect_and_load(
    path: str,
    source_type: Optional[str] = None,
    published_at: Optional[str] = None,
) -> Tuple[List[SourceDocument], List[dict]]:
    """Load a file or directory of files into SourceDocuments.

    ``source_type`` applies to prose files (``document`` default, or ``news``
    which requires ``published_at``); ChatGPT exports are always
    ``chatgpt_conversation`` regardless of the label.  Directories are walked
    recursively in sorted order for determinism; unsupported file types are
    skipped and per-file failures are collected (never abort the whole walk).

    Returns ``(documents, errors)`` where ``errors`` is a list of
    ``{"path", "error"}`` for files that could not be parsed.
    """
    if source_type is not None and source_type not in USER_SOURCE_TYPES:
        raise ValueError(
            f"invalid source_type {source_type!r}; expected one of {USER_SOURCE_TYPES} "
            "('chatgpt_conversation' is adapter-assigned, not a caller option)"
        )
    if source_type == "news" and not published_at:
        raise ValueError("source_type 'news' requires published_at")
    published_at = validate_iso_date(published_at, "published_at")

    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"path not found: {path}")

    docs: List[SourceDocument] = []
    errors: List[dict] = []
    if p.is_dir():
        for child in sorted(p.rglob("*")):
            if not child.is_file():
                continue
            try:
                docs.extend(load_path(child, source_type, published_at))
            except Exception as exc:  # per-file fault tolerance: record and continue
                errors.append({"path": str(child), "error": str(exc)})
    else:
        # A single explicitly-named file: surface a clear error rather than a
        # success-shaped no-op when it is unsupported (load_path raises for
        # malformed content, which callers propagate).
        loaded = load_path(p, source_type, published_at)
        if not loaded:
            raise ValueError(
                f"unsupported source file (expected .md/.txt/.markdown or a "
                f"ChatGPT conversations .json): {path}"
            )
        docs.extend(loaded)
    return docs, errors
