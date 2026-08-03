"""ChatGPT ``conversations.json`` export adapter.

The export is a list of conversations, each with a ``mapping`` dict of message
nodes ``{id: {message, parent, children}}`` forming a tree.  One conversation
becomes one :class:`SourceDocument`; the active thread is linearized by walking
the parent/children chain, messages are chunked on message boundaries with the
speaker role retained, and turn order is preserved in ``seq``.  System messages
and messages with null/empty parts are dropped gracefully rather than crashing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from ..chunking import chunk_messages
from .base import ChunkInput, SourceDocument, _doc_id, sha256_text

# Roles whose turns carry conversational content worth retrieving.  System
# messages are hidden instructions/metadata (usually empty) and are skipped.
_KEEP_ROLES = {"user", "assistant", "tool"}


def _extract_text(message: dict) -> str:
    """Pull plain text out of a message's content.parts, tolerating anything.

    ``parts`` may be missing, null, a list of strings, or a list of dicts
    (multimodal / tool payloads).  Only string parts are kept; everything else
    is ignored so a malformed or non-text message yields an empty string.
    """
    content = message.get("content")
    if not isinstance(content, dict):
        return ""
    parts = content.get("parts")
    if not isinstance(parts, list):
        # Some content types use "text" instead of "parts".
        text = content.get("text")
        return text.strip() if isinstance(text, str) else ""
    collected = [p for p in parts if isinstance(p, str) and p.strip()]
    return "\n".join(collected).strip()


def _linearize(mapping: dict, current_node: Optional[str]) -> List[dict]:
    """Return message nodes in conversation order (root -> active leaf).

    Prefers walking parents up from ``current_node`` (the real active thread),
    reversing to root order.  Falls back to a forward walk from the root that
    follows the most-recent child when there is no usable current node.
    """
    if not isinstance(mapping, dict) or not mapping:
        return []

    # Path 1: current_node -> root via parent pointers, then reverse.  Non-dict
    # / null nodes are skipped gracefully (a malformed mapping must not crash the
    # whole ingest — see this module's docstring).
    if current_node and current_node in mapping:
        chain: List[dict] = []
        node_id: Optional[str] = current_node
        seen = set()
        while node_id and node_id in mapping and node_id not in seen:
            seen.add(node_id)
            node = mapping[node_id]
            if not isinstance(node, dict):
                break
            chain.append(node)
            node_id = node.get("parent")
        chain.reverse()
        return chain

    # Path 2: find root(s) and walk forward following the last child.
    roots = [
        nid
        for nid, node in mapping.items()
        if isinstance(node, dict) and not node.get("parent")
    ]
    if not roots:
        roots = list(mapping.keys())[:1]
    ordered: List[dict] = []
    node_id = roots[0]
    seen = set()
    while node_id and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        node = mapping[node_id]
        if not isinstance(node, dict):
            break
        ordered.append(node)
        children = node.get("children") or []
        node_id = children[-1] if children else None
    return ordered


def _iso_from_create_time(value) -> Optional[str]:
    """Convert a Unix create_time (float/int) to an ISO-8601 UTC string."""
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _conversation_to_document(
    conversation: dict,
    source_path: str,
    index: int,
) -> Optional[SourceDocument]:
    """Build one SourceDocument from one conversation dict (or None if empty)."""
    mapping = conversation.get("mapping")
    if not isinstance(mapping, dict):
        return None

    ordered_nodes = _linearize(mapping, conversation.get("current_node"))

    messages: List[dict] = []
    for node in ordered_nodes:
        message = node.get("message")
        if not isinstance(message, dict):
            continue
        author = message.get("author") or {}
        role = (author.get("role") or "").strip()
        if role not in _KEEP_ROLES:
            continue
        text = _extract_text(message)
        if not text:
            continue
        messages.append({"role": role, "text": text, "id": str(message.get("id") or node.get("id") or "")})

    if not messages:
        return None

    message_chunks = chunk_messages(messages)
    if not message_chunks:
        return None

    chunks = [
        ChunkInput(
            seq=i,
            text=mc["text"],
            meta={"roles": mc["roles"], "message_ids": mc["message_ids"]},
        )
        for i, mc in enumerate(message_chunks)
    ]

    conv_id = str(
        conversation.get("conversation_id")
        or conversation.get("id")
        or f"{source_path}#{index}"
    )
    title = (conversation.get("title") or "Untitled conversation").strip() or "Untitled conversation"
    published_at = _iso_from_create_time(conversation.get("create_time"))

    # Hash the linearized content so an edited conversation re-ingests cleanly.
    content_hash = sha256_text("\n\n".join(m["role"] + ": " + m["text"] for m in messages))

    return SourceDocument(
        doc_id=_doc_id("cg", conv_id),
        title=title,
        source_type="chatgpt_conversation",
        source_path=source_path,
        content_hash=content_hash,
        provenance="uploaded",
        published_at=published_at,
        chunks=chunks,
    )


def load(path: Path) -> List[SourceDocument]:
    """Parse a ChatGPT export file into one SourceDocument per conversation."""
    abspath = str(path.resolve())
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    if isinstance(data, dict):
        conversations = [data]
    elif isinstance(data, list):
        conversations = [c for c in data if isinstance(c, dict)]
    else:
        return []

    docs: List[SourceDocument] = []
    for index, conversation in enumerate(conversations):
        doc = _conversation_to_document(conversation, abspath, index)
        if doc is not None:
            docs.append(doc)
    return docs
