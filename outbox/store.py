"""Durable append-only outbox store + drain-side reader API (proposal §5.5).

This is the storage substrate under the emission layer (``outbox.emit``) and the
substrate a Phase 1c drain worker reads from. It is deliberately transport-free:
nothing here talks to a network, and it imports no pydantic at module load
(``EventEnvelope`` is imported lazily inside :func:`read_pending`) so it stays
cheap to import on the producer hot path. Two concerns:

**Producer side** — :func:`append_envelope` / :func:`append_line`:
  Each event is one UTF-8 JSON object on its own ``\\n``-terminated line in
  ``outbox.jsonl``, appended under an ``O_APPEND`` file descriptor via the shared
  :func:`outbox._fsutil.append_bytes` helper.

  *Atomicity posture (documented honestly, per platform).* On POSIX, ``O_APPEND``
  makes each append land at the current end of file as one step, so two processes
  appending small lines to the same outbox do not corrupt each other's lines.
  This is *regular-file* append atomicity — NOT the pipe/FIFO ``PIPE_BUF``
  guarantee, which is a distinct (though related) rule; we do not rely on
  ``PIPE_BUF`` here. To keep every real line comfortably inside the safe regime
  (and to bound a pathological input), :func:`append_line` enforces
  :data:`MAX_LINE_BYTES`: an oversized line is **dead-lettered rather than
  appended**, so the outbox only ever holds small, single-write lines. The
  appender uses a write-all loop as a correctness net for the rare short write
  (interrupted syscall / ``ENOSPC``); under a genuine short write with a
  concurrent writer, POSIX no longer guarantees non-interleaving, but the size
  cap makes a short write practically unreachable for these payloads.

  *Windows posture (honest).* On Windows ``os.open`` with ``O_APPEND`` maps to the
  CRT ``_O_APPEND`` mode; each write still lands at end-of-file, but the strict
  concurrent-append non-interleaving guarantee is POSIX's, not Windows'. This
  epic's delegation model *does* run concurrent subagent/hook processes against
  the same repo, so cross-process contention on Windows is not rare; a stronger
  cross-process guarantee there (advisory locking) is a later-phase hardening
  item. Single-writer-per-process appends are safe on both.

  *Durability posture.* No ``fsync`` per append by default — the outbox is a
  hot-path buffer written on every card/graph/agent event, and a per-event fsync
  would be the dominant cost. A crash can lose the last few not-yet-flushed
  lines; acceptable because the outbox is a best-effort local buffer whose only
  consumer (the 1c drain worker) tolerates gaps. Set ``AGENT_OS_EVENTS_FSYNC=1``
  to force an ``fsync`` after every append.

**Drain side** — :func:`read_cursor`, :func:`read_pending`, :func:`write_cursor`,
  :func:`dead_letter`:
  The cursor is a **byte offset** into ``outbox.jsonl`` recording how far the
  drain has consumed, plus a **generation token** (a hash of the outbox head)
  so a deleted-and-recreated outbox (rotation, ``git clean -fdx`` + re-mint) is
  detected and the cursor resets to 0 instead of resuming at a byte offset that
  now means something else. :func:`read_cursor`
  is defensive: a non-dict cursor file, a stored offset beyond the current file
  size, or a generation mismatch all reset to 0 rather than raising or stalling.
  :func:`read_pending` reads from the cursor to EOF but only yields lines up to
  the *last* newline (a line still mid-append is never handed out half-written),
  bounds each batch by ``max_events`` / ``max_bytes`` so a huge backlog cannot be
  slurped into memory at once, and flags a malformed line (``envelope=None`` +
  ``error``) instead of raising so the drain can dead-letter it. Cursor writes
  are atomic (temp file + ``os.replace``).

  Rotation/compaction of a fully-drained outbox is **out of scope for Phase 1b**
  (noted as a 1c / operational-hardening follow-up); the file only grows here.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Union

from . import _fsutil, paths

if TYPE_CHECKING:  # type-only: keeps store importable without pydantic at runtime
    from contracts import EventEnvelope

_PathLike = Union[str, Path]

_FSYNC_ENV = "AGENT_OS_EVENTS_FSYNC"

# Maximum size (bytes, including the trailing newline) of a single outbox line.
# Ids/titles/statuses envelopes are a few hundred bytes; this cap is generous
# headroom for a legitimate event while keeping every appended line small enough
# to stay in the single-write atomic regime on every platform. A line over the
# cap is anomalous (e.g. a pathological card title); rather than risk a torn
# multi-write append, :func:`append_line` dead-letters it (preserved for
# diagnosis) and does not append it to the outbox.
MAX_LINE_BYTES = 65536

# Bytes of the outbox head hashed into the generation token (see below). The
# head holds the oldest events; append-only never rewrites it, so it is stable
# within one file incarnation and differs across incarnations.
_GENERATION_SAMPLE_BYTES = 4096


def _fsync_enabled() -> bool:
    return _fsutil.env_flag(_FSYNC_ENV)


def _outbox_generation(repo_root: _PathLike) -> Optional[str]:
    """A token identifying the current outbox file *incarnation*.

    A deleted-and-recreated outbox (rotation, ``git clean -fdx`` + re-mint) must
    be detected even when the new file grows *past* the old cursor offset (so the
    offset-vs-filesize guard alone can't catch it). The file's ``(device,
    inode)`` identity is unreliable here — filesystems routinely reuse a
    just-freed inode for the replacement file. Instead we hash the first
    :data:`_GENERATION_SAMPLE_BYTES` of the outbox: because the head holds the
    oldest events (append-only never rewrites it) it is stable within one
    incarnation, and because those first events carry unique ULID ``event_id``s a
    different incarnation produces a different head hash. Returns ``None`` when
    the file is absent or empty, in which case generation matching is skipped and
    the offset-vs-filesize guard alone protects the cursor.
    """
    try:
        with open(paths.outbox_path(repo_root), "rb") as handle:
            head = handle.read(_GENERATION_SAMPLE_BYTES)
    except OSError:
        return None
    if not head:
        return None
    return hashlib.sha256(head).hexdigest()


# --------------------------------------------------------------------------- #
# Producer side
# --------------------------------------------------------------------------- #
def append_line(repo_root: _PathLike, line: str) -> None:
    """Append one logical line (no trailing newline) to ``outbox.jsonl``.

    The newline is added here; the whole ``line + "\\n"`` is appended with the
    shared ``O_APPEND`` write-all helper (see the module docstring for the
    atomicity / durability posture). ``line`` must not itself contain a newline.
    An oversized line (> :data:`MAX_LINE_BYTES`) is **dead-lettered instead of
    appended** so the outbox only holds small, atomically-appendable lines.
    """
    if "\n" in line:
        raise ValueError("outbox line must not contain a newline")
    data = (line + "\n").encode("utf-8")
    if len(data) > MAX_LINE_BYTES:
        # Too large to guarantee an atomic single append — preserve it for
        # diagnosis in the dead-letter log rather than risk a torn write.
        dead_letter(
            repo_root,
            line,
            f"line exceeds MAX_LINE_BYTES ({len(data)} > {MAX_LINE_BYTES})",
        )
        return
    paths.ensure_sync_dir(repo_root)
    _fsutil.append_bytes(paths.outbox_path(repo_root), data, fsync=_fsync_enabled())


def append_envelope(repo_root: _PathLike, envelope) -> None:
    """Serialize a contracts.EventEnvelope to one compact JSON line and append it."""
    # model_dump_json() emits compact (no-indent) JSON, keeping lines small.
    append_line(repo_root, envelope.model_dump_json())


# --------------------------------------------------------------------------- #
# Drain side
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PendingEvent:
    """One line read from the outbox during a drain pass.

    ``envelope`` is the parsed :class:`contracts.EventEnvelope`, or ``None`` when
    the line could not be parsed/validated (``error`` then holds the reason and
    ``raw`` the original text — the drain worker dead-letters it). ``end_offset``
    is the byte offset in ``outbox.jsonl`` immediately after this line's newline;
    pass it to :func:`write_cursor` once the event is durably handled.
    """

    envelope: "Optional[EventEnvelope]"
    raw: str
    end_offset: int
    error: Optional[str] = None


def read_cursor(repo_root: _PathLike) -> int:
    """Return the drain cursor (byte offset), or 0 if none/unreadable/stale.

    Defensive against every corruption/rotation shape: an unreadable or
    non-object cursor file, a non-integer offset, a stored offset beyond the
    current outbox size (the file shrank/was truncated/recreated smaller), or a
    generation mismatch (the outbox was deleted and recreated) all reset to 0.
    Resetting to 0 re-reads from the start — safe because central ingest is
    idempotent on ``event_id`` (ops.events PK / ``insert_event_if_absent``).
    """
    path = paths.cursor_path(repo_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(data, dict):
        # A valid-but-non-object cursor (null / list / number) — treat as absent.
        return 0
    try:
        offset = int(data.get("offset", 0))
    except (TypeError, ValueError):
        return 0
    if offset < 0:
        return 0

    # Reset if the outbox was deleted and recreated (generation mismatch): a byte
    # offset into the old file is meaningless in the new one.
    stored_gen = data.get("generation")
    current_gen = _outbox_generation(repo_root)
    if stored_gen is not None and current_gen is not None and stored_gen != current_gen:
        return 0

    # Reset if the offset is past EOF (outbox shrank/was truncated) — otherwise a
    # stale offset would seek past EOF and yield [] forever, silently never
    # draining any future event.
    try:
        size = os.path.getsize(paths.outbox_path(repo_root))
    except OSError:
        size = 0
    if offset > size:
        return 0
    return offset


def write_cursor(repo_root: _PathLike, offset: int) -> None:
    """Atomically persist the drain cursor (byte offset + outbox generation).

    Written via the shared atomic writer (temp file + ``fsync`` + ``os.replace``)
    so ``cursor.json`` only ever appears fully-written. The outbox generation
    token is stored alongside the offset so :func:`read_cursor` can detect a
    recreated outbox. Also the drain worker's cursor-advance entry point (a byte
    offset from a :class:`PendingEvent`'s ``end_offset``).
    """
    if offset < 0:
        raise ValueError("cursor offset must be non-negative")
    paths.ensure_sync_dir(repo_root)
    payload = json.dumps(
        {"offset": int(offset), "generation": _outbox_generation(repo_root)}
    ).encode("utf-8")
    _fsutil.atomic_write_bytes(paths.cursor_path(repo_root), payload, fsync=True)


def read_pending(
    repo_root: _PathLike,
    cursor: Optional[int] = None,
    *,
    max_events: Optional[int] = 500,
    max_bytes: Optional[int] = None,
) -> List[PendingEvent]:
    """Read undrained lines from ``cursor`` (or the persisted cursor) to EOF.

    Only lines terminated by a newline are returned, so a line still being
    appended is never handed out half-written. The batch is bounded: at most
    ``max_bytes`` are read from disk (``None`` = to EOF) and at most
    ``max_events`` complete lines are returned (``None`` = unbounded), so a large
    backlog cannot be slurped into memory or pydantic-validated all at once — the
    worker advances the cursor to the last returned ``end_offset`` and calls
    again for the next batch. Malformed lines are returned with ``envelope=None``
    and an ``error`` string (never raise) so the caller can dead-letter them
    without stalling the drain. Does NOT move the cursor.
    """
    from contracts import EventEnvelope  # lazy: keeps store importable w/o pydantic

    path = paths.outbox_path(repo_root)
    if not path.exists():
        return []
    start = read_cursor(repo_root) if cursor is None else int(cursor)
    if start < 0:
        start = 0
    # Clamp an explicit cursor past EOF to the file size (read_cursor already
    # clamps the persisted one) so a stale explicit offset yields [] cleanly
    # rather than a seek past EOF.
    try:
        size = path.stat().st_size
    except OSError:
        size = start
    if start > size:
        start = size
    with open(path, "rb") as handle:
        handle.seek(start)
        data = handle.read() if max_bytes is None else handle.read(max_bytes)

    records: List[PendingEvent] = []
    pos = 0
    while True:
        if max_events is not None and len(records) >= max_events:
            break
        newline = data.find(b"\n", pos)
        if newline == -1:
            break  # trailing bytes without a newline: an in-progress append
        raw_bytes = data[pos:newline]
        end_offset = start + newline + 1
        raw = raw_bytes.decode("utf-8", errors="replace")
        pos = newline + 1
        if not raw.strip():
            # A blank line should never be written; skip but keep offsets exact.
            continue
        envelope = None
        error: Optional[str] = None
        try:
            envelope = EventEnvelope.model_validate_json(raw)
        except Exception as exc:  # pydantic ValidationError or JSON error
            error = str(exc)
        records.append(PendingEvent(envelope=envelope, raw=raw, end_offset=end_offset, error=error))
    return records


def dead_letter(repo_root: _PathLike, raw: str, reason: str) -> None:
    """Append a permanently-undeliverable / malformed line to ``dead-letter.jsonl``.

    ``raw`` is the original outbox line (or a serialized envelope); ``reason``
    records why it was dead-lettered. The record itself is a compact JSON object,
    appended with the same shared ``O_APPEND`` write-all discipline as the outbox.
    """
    paths.ensure_sync_dir(repo_root)
    record = json.dumps(
        {
            "reason": reason,
            "raw": raw,
            "dead_lettered_at": datetime.now(timezone.utc).isoformat(),
        },
        ensure_ascii=False,
    )
    data = (record + "\n").encode("utf-8")
    _fsutil.append_bytes(paths.dead_letter_path(repo_root), data, fsync=_fsync_enabled())


__all__ = [
    "MAX_LINE_BYTES",
    "PendingEvent",
    "append_line",
    "append_envelope",
    "read_cursor",
    "write_cursor",
    "read_pending",
    "dead_letter",
]
