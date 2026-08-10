"""Chunking for prose and for conversation message streams.

Pure Python — no heavy dependencies — so this is unit-testable in any
environment.  Token counts are *estimated* (a lightweight word/punctuation/CJK
regex, not a model tokenizer) which is sufficient for bounding chunk size; the
default budget targets the card's ~400-512 token window with ~15% overlap.

The hard invariant these functions guarantee: no returned chunk ever exceeds
``target_tokens`` as measured by :func:`estimate_tokens`.  That holds on every
path — greedily packed segments, the sentence/paragraph fallback, the
overlap-seeded next chunk, oversized single sentences, and whitespace-free
scripts (CJK) which are counted per-codepoint and hard-split at character
granularity when no sentence/word boundary is available.
"""

from __future__ import annotations

import re
from typing import List, Sequence, TypedDict

# Default prose budget: ~450 estimated tokens per chunk with ~15% overlap.
DEFAULT_TARGET_TOKENS = 450
DEFAULT_OVERLAP_RATIO = 0.15

# A token is a CJK/Japanese/Korean codepoint (whitespace-free scripts pack many
# "words" into one run, so each ideograph/kana/hangul syllable counts on its
# own), OR a maximal word run, OR a standalone punctuation mark.  Counting CJK
# per-codepoint is what lets the budget checks actually fire for those scripts.
_CJK = (
    r"　-〿"   # CJK symbols & punctuation (。、！？… etc. handled below too)
    r"぀-ヿ"   # Hiragana + Katakana
    r"㐀-䶿"   # CJK ext A
    r"一-鿿"   # CJK unified
    r"豈-﫿"   # CJK compatibility ideographs
    r"가-힯"   # Hangul syllables
)
_TOKEN_RE = re.compile(rf"[{_CJK}]|\w+|[^\w\s]")
_PARAGRAPH_RE = re.compile(r"\n\s*\n")
# Sentence break: Western end punctuation OR a CJK sentence terminator, followed
# by optional whitespace.  Deliberately simple; retrieval chunking does not need
# linguistically perfect sentence splitting.
_SENTENCE_RE = re.compile(r"(?<=[.!?。！？…])\s*")


def estimate_tokens(text: str) -> int:
    """Estimate a token count for ``text`` (words + punctuation + CJK codepoints)."""
    if not text:
        return 0
    return len(_TOKEN_RE.findall(text))


def _char_split(text: str, target_tokens: int) -> List[str]:
    """Split a boundary-free segment into <= target_tokens pieces by token unit.

    Works at the granularity of :data:`_TOKEN_RE` atoms (individual CJK
    codepoints, word runs, punctuation) so whitespace-free scripts still bound.
    Atoms are rejoined verbatim (they carried no interior whitespace), so a
    slice of ``target_tokens`` atoms re-estimates to <= target_tokens.
    """
    tokens = _TOKEN_RE.findall(text)
    if not tokens:
        return []
    return ["".join(tokens[i : i + target_tokens]) for i in range(0, len(tokens), target_tokens)]


def _hard_split(text: str, target_tokens: int) -> List[str]:
    """Split an oversized single segment into <= target_tokens pieces.

    Slices by *estimated tokens* (not word count) so punctuation-dense text
    cannot blow the budget, and falls back to :func:`_char_split` for segments
    with no usable whitespace (a single runaway "word" or a CJK run).
    """
    words = text.split()
    if len(words) <= 1:
        return _char_split(text, target_tokens)
    slices: List[str] = []
    current: List[str] = []
    current_tokens = 0
    for word in words:
        wt = estimate_tokens(word)
        if wt > target_tokens:
            # A single word exceeds the budget (punctuation-dense / no spaces):
            # flush the pack, then character-split the offending word.
            if current:
                slices.append(" ".join(current))
                current, current_tokens = [], 0
            slices.extend(_char_split(word, target_tokens))
            continue
        if current and current_tokens + wt > target_tokens:
            slices.append(" ".join(current))
            current, current_tokens = [], 0
        current.append(word)
        current_tokens += wt
    if current:
        slices.append(" ".join(current))
    return slices


def _segments(text: str, target_tokens: int) -> List[str]:
    """Break text into boundary-respecting segments no larger than target.

    Paragraph first, then sentence, then a hard token/character-slice fallback
    so a single runaway sentence (or a whitespace-free CJK run) can never exceed
    the budget.
    """
    segments: List[str] = []
    for para in _PARAGRAPH_RE.split(text):
        para = para.strip()
        if not para:
            continue
        if estimate_tokens(para) <= target_tokens:
            segments.append(para)
            continue
        for sentence in _SENTENCE_RE.split(para):
            sentence = sentence.strip()
            if not sentence:
                continue
            if estimate_tokens(sentence) <= target_tokens:
                segments.append(sentence)
            else:
                segments.extend(_hard_split(sentence, target_tokens))
    return segments


def chunk_prose(
    text: str,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_ratio: float = DEFAULT_OVERLAP_RATIO,
) -> List[str]:
    """Chunk prose into <= target_tokens pieces with sentence-aligned overlap.

    Segments (paragraph/sentence granularity, pre-bounded to the budget) are
    greedily packed up to the token budget; when a chunk is flushed, trailing
    segments totalling roughly ``overlap_ratio`` of the budget are carried into
    the next chunk so context is not lost at boundaries.  The overlap seed is
    strictly bounded and further trimmed against the incoming segment, so a
    chunk (seed + packed segments) never exceeds ``target_tokens``.
    """
    text = (text or "").strip()
    if not text:
        return []

    segments = _segments(text, target_tokens)
    if not segments:
        return []

    overlap_tokens = int(target_tokens * overlap_ratio)
    chunks: List[str] = []
    current: List[str] = []
    current_tokens = 0

    def flush() -> List[str]:
        """Emit the current chunk and return a bounded overlap seed (<= overlap)."""
        if not current:
            return []
        chunks.append(" ".join(current))
        if overlap_tokens <= 0:
            return []
        seed: List[str] = []
        seed_tokens = 0
        # Walk from the end, admitting trailing segments while they fit the
        # overlap budget.  Never admit a segment that would push the seed over
        # the overlap budget (the first candidate included), so seed_tokens
        # stays <= overlap_tokens whenever more than one segment exists.
        for seg in reversed(current):
            seg_tokens = estimate_tokens(seg)
            if seed and seed_tokens + seg_tokens > overlap_tokens:
                break
            if not seed and seg_tokens > overlap_tokens:
                # A single trailing segment already exceeds the overlap budget:
                # take no overlap here rather than blow it.
                break
            seed.insert(0, seg)
            seed_tokens += seg_tokens
            if seed_tokens >= overlap_tokens:
                break
        # Never let the overlap seed be the entire chunk (would loop forever).
        if len(seed) >= len(current):
            return []
        return seed

    for seg in segments:
        seg_tokens = estimate_tokens(seg)  # <= target_tokens (guaranteed by _segments)
        if current and current_tokens + seg_tokens > target_tokens:
            seed = flush()
            # Guarantee seed + this incoming segment still fits the budget by
            # dropping leading overlap segments until it does.
            while seed and sum(estimate_tokens(s) for s in seed) + seg_tokens > target_tokens:
                seed.pop(0)
            current = list(seed)
            current_tokens = sum(estimate_tokens(s) for s in current)
        current.append(seg)
        current_tokens += seg_tokens

    if current:
        chunks.append(" ".join(current))

    return chunks


class MessageChunk(TypedDict):
    """A conversation chunk: rendered text plus the roles/ids it spans."""

    text: str
    roles: List[str]
    message_ids: List[str]


def _render_message(role: str, text: str) -> str:
    return f"{role}: {text}".strip()


def chunk_messages(
    messages: Sequence[dict],
    target_tokens: int = DEFAULT_TARGET_TOKENS,
) -> List[MessageChunk]:
    """Chunk an ordered message stream respecting message boundaries.

    Consecutive messages are packed up to the token budget; a message is never
    split mid-message *unless it alone exceeds the budget*, in which case its
    text is prose-chunked and each piece becomes its own single-role chunk.
    Turn order is preserved (the caller assigns ``seq`` in returned order) and
    the speaker role(s) plus source message id(s) are retained per chunk.

    Each ``messages`` item is a dict with keys ``role`` (str), ``text`` (str),
    and optional ``id`` (str).  Empty-text messages are skipped.
    """
    out: List[MessageChunk] = []
    cur_parts: List[str] = []
    cur_roles: List[str] = []
    cur_ids: List[str] = []
    cur_tokens = 0

    def flush() -> None:
        nonlocal cur_parts, cur_roles, cur_ids, cur_tokens
        if cur_parts:
            out.append(
                MessageChunk(
                    text="\n\n".join(cur_parts),
                    roles=list(cur_roles),
                    message_ids=list(cur_ids),
                )
            )
        cur_parts, cur_roles, cur_ids, cur_tokens = [], [], [], 0

    for msg in messages:
        role = (msg.get("role") or "unknown").strip() or "unknown"
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        msg_id = str(msg.get("id") or "")
        rendered = _render_message(role, text)
        tokens = estimate_tokens(rendered)

        if tokens > target_tokens:
            # A single oversized message: flush the pack, then emit prose-split
            # pieces as their own single-role chunks (this is the only case that
            # splits within a message).
            flush()
            for piece in chunk_prose(text, target_tokens=target_tokens):
                out.append(
                    MessageChunk(
                        text=_render_message(role, piece),
                        roles=[role],
                        message_ids=[msg_id] if msg_id else [],
                    )
                )
            continue

        if cur_parts and cur_tokens + tokens > target_tokens:
            flush()

        cur_parts.append(rendered)
        cur_tokens += tokens
        if role not in cur_roles:
            cur_roles.append(role)
        if msg_id:
            cur_ids.append(msg_id)

    flush()
    return out
