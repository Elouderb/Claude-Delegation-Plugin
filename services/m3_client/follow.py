"""``--follow`` keyset walk: fetch every event newer than a cursor, across as
many backward pages as needed, without dropping or duplicating any (card
93baf05b acceptance: "no drops/dups across pages").

``GET /v1/events/recent`` returns events newest-first (``ingest_seq DESC``) and
pages *backward* (older) via ``?after_seq=`` / ``next_after`` — the shape a
"load more history" feed needs. A ``--follow`` tail needs the opposite
direction (only events newer than what it already showed), so this module
walks that same backward-paging API starting from the newest page and keeps
turning pages only for as long as an entire page is still newer than the
caller's cursor (i.e., more new events arrived last tick than fit in one page)
— stopping the moment it finds an event at or before the cursor, or the feed
runs out. That handles an arbitrarily large burst between polls with no gap,
while the common case (fewer than one page of new events) costs exactly one
request.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

# fetch_page(after_seq) -> {"events": [...ingest_seq DESC...], "next_after": int|None}
# — the exact shape of a GET /v1/events/recent response body.
PageFetcher = Callable[[Optional[int]], Dict[str, Any]]


def walk_new_events(
    fetch_page: PageFetcher,
    cursor: Optional[int],
    *,
    max_pages: int = 1000,
) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """Return ``(new_events_ascending, new_cursor)``.

    ``cursor`` is the largest ``ingest_seq`` already reported as seen, or
    ``None`` on the very first call for a client that has never seen anything.
    On a ``None`` cursor this establishes a BASELINE from the single newest
    page only (no events are reported as "new" — they were already shown by
    the initial full-board render) rather than walking the server's entire
    history, which would be unbounded for a long-lived event log.

    ``max_pages`` bounds the walk so a misbehaving/adversarial server response
    (e.g. ``next_after`` that never stops advancing) cannot spin this forever.
    When that bound is what stops the walk (rather than reaching an
    already-seen event), ``new_cursor`` advances only to the OLDEST event
    actually collected this call — never all the way to ``top_seq`` — so the
    still-unwalked older span is left eligible to be picked up on a later poll
    instead of being silently marked "seen" (code review, card 93baf05b, Minor
    finding: a burst larger than ``max_pages`` pages between polls used to make
    the truncated walk jump the cursor straight to the newest event's seq,
    permanently skipping the un-collected middle of the burst).

    The returned ``new_cursor`` never regresses below the input ``cursor``
    even if the server ever reports an event log with a lower top ``ingest_seq``
    than before (e.g. a wiped dev database) — regressing would make the next
    poll re-report an already-shown burst as "new" (a duplicate).
    """
    collected: List[Dict[str, Any]] = []
    after_seq: Optional[int] = None
    top_seq: Optional[int] = None
    pages = 0
    truncated = False  # True only when max_pages stopped the walk short of the boundary

    while True:
        page = fetch_page(after_seq)
        events = page.get("events") or []
        if not events:
            break
        if top_seq is None:
            top_seq = events[0].get("ingest_seq")
        if cursor is None:
            # First-ever call: this page is the baseline, not "new" events.
            break

        boundary_hit = False
        for event in events:
            seq = event.get("ingest_seq")
            if seq is None or seq <= cursor:
                boundary_hit = True
                break
            collected.append(event)

        pages += 1
        if boundary_hit:
            break
        if pages >= max_pages:
            truncated = True
            break
        next_after = page.get("next_after")
        if next_after is None:
            # Feed genuinely ran out of history - nothing left to walk, so
            # this is NOT a truncation (distinct from the max_pages case
            # below): every event down to the end of the log was collected.
            break
        after_seq = next_after

    collected.reverse()  # oldest-new-event first (chronological print order)

    if top_seq is None:
        new_cursor = cursor
    elif cursor is None:
        new_cursor = top_seq
    elif truncated and collected:
        # max_pages stopped the walk before finding the boundary: only the
        # events actually collected this call are proven new. Advancing to
        # top_seq here would make the NEXT poll's boundary check treat the
        # still-unwalked older span as already-seen too - silently and
        # permanently skipping it. Advancing only to the oldest event actually
        # collected (``collected[0]`` after the reverse() above) keeps that
        # span reachable on a later poll, at the cost of a harmless duplicate
        # re-display of the newer events near the top next tick.
        new_cursor = max(collected[0]["ingest_seq"], cursor)
    else:
        # The boundary was actually found, or the feed ran out of history, or
        # nothing new exists - every event down to the new cursor's target was
        # genuinely examined, so it's safe to advance all the way to the
        # newest event seen.
        new_cursor = max(top_seq, cursor)
    return collected, new_cursor


__all__ = ["PageFetcher", "walk_new_events"]
