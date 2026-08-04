"""Plain-text board rendering for the M3 stand-in client (card 93baf05b).

"Keep it modest — this is a stand-in for a human eyeball, not a TUI project":
a handful of hand-aligned columns via ``str.ljust`` is enough, so this module
pulls in no third-party formatting library (consistent with the whole package
being stdlib-only, see :mod:`services.m3_client`).

All ages are rendered relative ("2m ago") and UTC-aware: every timestamp the
``/v1`` API returns is an ISO-8601 string with an explicit UTC offset (see
``services/api/store.py``'s ``_iso``), so :func:`parse_iso` requires one and
:func:`format_age` refuses a naive ``datetime`` rather than silently guessing
its zone.

Every DB-sourced string rendered onto the board is passed through
:func:`_sanitize` first (code review, card 93baf05b, Major finding #4): fields
such as a machine's ``name`` or a repository's ``canonical_remote`` are plain
unconstrained ``str`` at the ``contracts`` layer, populated via ``POST
/v1/machines|repositories/register`` — anyone holding the shared
``AGENT_OS_API_KEY`` can already register an arbitrary identity (see
8f84948b's accepted-risk register), and this module is the first thing that
``print()``s those fields to a real terminal. Without sanitizing, a crafted
value containing ANSI escape sequences could clear the screen or inject fake
colored text; a crafted control character (e.g. an embedded newline) could
forge a spurious extra board row. This only affects the plain-text board/event
lines; ``--json`` output is untouched, since it is never printed raw to a
terminal by this client.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

# Matches, in order of the alternation: a CSI sequence (``ESC [ ... final-byte``,
# e.g. ``\x1b[2J`` clear-screen or ``\x1b[31m`` color); an OSC sequence (``ESC ]
# ... BEL`` or ``ESC ] ... ST``, e.g. a terminal title-bar trick); or any other
# two-byte ``ESC``-prefixed escape (e.g. ``\x1b7`` save-cursor). stdlib ``re``
# only, consistent with the whole package being stdlib-only.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b[@-Z\\^_]"
)
# Any remaining C0 control character (incl. a bare/dangling ESC, newline, tab)
# or DEL/C1 control character not already covered above.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _sanitize(value: Any) -> str:
    """Strip ANSI escape sequences and control characters from ``value``.

    Applied at the render boundary (every :func:`_table` cell and every field
    in :func:`render_event_line`) rather than at ingestion, so the raw value is
    preserved everywhere else that isn't a terminal (API responses, storage,
    ``--json`` output).
    """
    text = _ANSI_ESCAPE_RE.sub("", str(value))
    return _CONTROL_CHAR_RE.sub("", text)


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string; ``None``/unparseable -> ``None``.

    A stray naive stamp (no offset) is defensively treated as UTC rather than
    raising, since a malformed upstream value shouldn't crash the board.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_age(instant: datetime, *, now: Optional[datetime] = None) -> str:
    """Render a compact relative age: 'just now', '5s ago', '3m ago', ...

    Raises ``ValueError`` for a naive ``instant`` — every real caller in this
    package goes through :func:`parse_iso`, which always returns an
    aware datetime (or ``None``, handled by :func:`format_age_iso`); a naive
    value reaching here would indicate a caller bypassing that normalization.
    """
    if instant.tzinfo is None:
        raise ValueError("format_age requires a timezone-aware instant")
    reference = now if now is not None else datetime.now(timezone.utc)
    seconds = (reference - instant).total_seconds()
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60.0
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24.0
    if days < 30:
        return f"{int(days)}d ago"
    months = days / 30.0
    if months < 12:
        return f"{int(months)}mo ago"
    years = days / 365.0
    return f"{int(years)}y ago"


def format_age_iso(value: Optional[str], *, now: Optional[datetime] = None) -> str:
    """:func:`format_age` over a raw ISO-8601 string; ``"unknown"`` if absent/bad."""
    dt = parse_iso(value)
    if dt is None:
        return "unknown"
    return format_age(dt, now=now)


def _table(headers: Sequence[str], rows: List[Sequence[Any]]) -> List[str]:
    if not rows:
        return ["  (none)"]
    widths = [len(h) for h in headers]
    str_rows = [[_sanitize(cell) for cell in row] for row in rows]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    return ["  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in str_rows]


def render_board(data: Dict[str, Any], *, now: Optional[datetime] = None) -> str:
    """Render the plain-text status board from a combined snapshot.

    ``data`` is ``{"machines": [...], "repositories": [...], "sessions": [...],
    "events": [...], "api_url": str}`` — the exact shapes ``GET /v1/machines``
    / ``/v1/repositories`` / ``/v1/sessions`` / ``/v1/events/recent`` return.
    """
    reference = now if now is not None else datetime.now(timezone.utc)
    lines: List[str] = [
        f"Agent OS -- M3 status board  (as of {reference.strftime('%Y-%m-%dT%H:%M:%SZ')})",
        f"API: {data.get('api_url', '')}",
        "",
    ]

    machines = data.get("machines") or []
    lines.append(f"MACHINES ({len(machines)})")
    lines.extend(_table(
        ["id", "name", "os", "last_seen"],
        [
            (m.get("machine_id", ""), m.get("name", ""), m.get("os", ""),
             "last_seen " + format_age_iso(m.get("last_seen_at"), now=reference))
            for m in machines
        ],
    ))
    lines.append("")

    repos = data.get("repositories") or []
    lines.append(f"REPOSITORIES ({len(repos)})")
    lines.extend(_table(
        ["id", "machine", "remote", "root"],
        [
            (r.get("repository_id", ""), r.get("machine_id", ""),
             r.get("canonical_remote") or "-", r.get("repo_root", ""))
            for r in repos
        ],
    ))
    lines.append("")

    sessions = data.get("sessions") or []
    lines.append(f"ACTIVE SESSIONS ({len(sessions)})")
    lines.extend(_table(
        ["id", "agent_type", "status", "heartbeat"],
        [
            (s.get("session_id", ""), s.get("agent_type", ""), s.get("status", ""),
             "heartbeat " + format_age_iso(s.get("last_heartbeat_at"), now=reference))
            for s in sessions
        ],
    ))
    lines.append("")

    events = data.get("events") or []
    lines.append(f"RECENT EVENTS ({len(events)})")
    lines.extend(_table(
        ["type", "repo", "age"],
        [
            (e.get("event_type", ""), e.get("repository_id") or "-",
             format_age_iso(e.get("occurred_at"), now=reference))
            for e in events
        ],
    ))

    return "\n".join(lines)


def render_event_line(event: Dict[str, Any], *, now: Optional[datetime] = None) -> str:
    """One compact line for a newly-arrived event during ``--follow``."""
    age = format_age_iso(event.get("occurred_at"), now=now)
    event_type = _sanitize(event.get("event_type", ""))
    repo = _sanitize(event.get("repository_id") or "-")
    seq = event.get("ingest_seq")
    return f"  [{age}] {event_type} repo={repo} seq={seq}"


__all__ = ["format_age", "format_age_iso", "parse_iso", "render_board", "render_event_line"]
