"""
Card management MCP tool implementations.

Functions here are imported by server.py, registered with @server.tool(), and
re-exported so that ``import server; server.create_card(...)`` continues to
work (as the test suite relies on that form).

Storage is a repo-local SQLite file whose *path* is injected by server.py via
``set_db_path()``.  Each card operation opens its own short-lived connection
through the ``_connect()`` context manager rather than sharing one long-lived
handle.  This is deliberate: a persistent connection keeps pointing at the
original inode, so when ``cards.sqlite`` is replaced underneath the process
(a git operation touching the directory, an external rewrite, delete+recreate)
SQLite flips the stale handle to read-only (``SQLITE_READONLY_DBMOVED``) and
every write fails.  Re-opening by path on each call always targets the current
file, and ``_ensure_schema`` re-creates the tables so even a freshly swapped or
empty file self-heals.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union
import sqlite3
import uuid

import graph_io
from graph_io import log

# Allowed card lifecycle states
VALID_STATUSES = ("Created", "In Progress", "Complete")

# Path to the repo-local card database, injected by server.py after it has
# resolved the repo root.  ``None`` until the server initialises.
_db_path: Optional[Path] = None


# --- Phase 1b event emission (best-effort, never breaks a card op) -----------
# The top-level ``outbox`` package is reached through the SHARED, tri-state
# ``graph_io.get_outbox()`` probe (one copy for card + graph tools), and the
# emission repo root through ``graph_io.emission_repo_root()`` — the SINGLE
# source of truth so card events and graph events for one repo always land in the
# same outbox. Emission itself swallows all errors and no-ops when identity is
# not bootstrapped, so a card create/update/complete can never fail because of
# the outbox; ``_emit_task_event`` adds a belt-and-braces guard so even an
# unexpected error in the emission plumbing can't turn a committed card into a
# reported failure.


def _repo_root() -> Optional[Path]:
    """Repo root inferred from the card DB path (``<repo>/.agent-os/cards.sqlite``).

    Used as the fallback source for :func:`graph_io.emission_repo_root` — in the
    running server this equals the server-set emission root (both derive from the
    same resolved repo_root), so card and graph emission agree.
    """
    if _db_path is None:
        return None
    return _db_path.parent.parent


def _emit_task_event(
    emit_method: str, *, card_id, title, status, priority,
    global_id=None, version=None, description=None,
) -> None:
    """Emit one task lifecycle event (best-effort; never raises to the caller).

    Single helper for the three previously copy-pasted emission blocks
    (create/update/complete). Resolves the outbox + the shared emission repo root,
    no-ops if either is unavailable, and swallows any error so a card write is
    never reported as a failure because of the outbox. The Phase 2a sync fields
    (``global_id``/``version``/``description``) make the emitted payload
    TaskRecord-shaped so the central materializer can down-project it; they are
    passed through only when present.
    """
    try:
        ob = graph_io.get_outbox()
        root = graph_io.emission_repo_root(fallback=_repo_root())
        if ob is None or root is None:
            return
        getattr(ob, emit_method)(
            root, card_id=card_id, title=title, status=status, priority=priority,
            global_id=global_id, version=version, description=description,
        )
    except Exception as exc:  # defense-in-depth: emit must never fail a card op
        log(f"event emission skipped ({emit_method}): {exc}")


def _mint_global_id() -> Optional[str]:
    """Mint a fresh central task id (``task_`` ULID), or ``None`` if unavailable.

    Guarded and lazy: imports only ``contracts.identifiers`` (stdlib-backed, no
    pydantic) and never raises. A checkout genuinely missing the bundled
    ``contracts`` package simply gets ``None`` — the card is still created, just
    without a synchronizable global identity (the emitted task event is then inert
    centrally, exactly as before Phase 2a).
    """
    try:
        from contracts.identifiers import TASK, new_id

        return new_id(TASK)
    except Exception:
        return None


def _sync_status_to_local(sync_status) -> str:
    """Map a central TaskSyncStatus (enum/value) to a local card status string.

    Guarded so the down-projection works even without ``contracts`` importable:
    falls back to an inline table (and, ultimately, ``"Created"``) so it always
    returns a value in :data:`VALID_STATUSES`.
    """
    try:
        from contracts.tasks import sync_status_to_local

        return sync_status_to_local(sync_status)
    except Exception:
        fallback = {
            "created": "Created", "in_progress": "In Progress",
            "blocked": "In Progress", "complete": "Complete",
        }
        mapped = fallback.get(str(sync_status), "Created")
        return mapped if mapped in VALID_STATUSES else "Created"


def set_db_path(path: Optional[Union[str, Path]]) -> None:
    """Point this module at the card database file (opened fresh per operation).

    Pass ``None`` to clear it (used by tests; also restores the not-initialized
    guard).
    """
    global _db_path
    _db_path = Path(path) if path is not None else None


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the cards / card_comments tables if they do not already exist."""
    conn.execute("""
    CREATE TABLE IF NOT EXISTS cards (
        card_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT,
        status TEXT NOT NULL,
        priority TEXT,
        created_at TIMESTAMP,
        updated_at TIMESTAMP,
        global_id TEXT,
        sync_version INTEGER
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS card_comments (
        comment_id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_id TEXT NOT NULL,
        author TEXT,
        comment TEXT,
        created_at TIMESTAMP
    )
    """)
    _ensure_card_sync_columns(conn)


def _ensure_card_sync_columns(conn: sqlite3.Connection) -> None:
    """Add the Phase 2a sync columns to a pre-existing ``cards`` table.

    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so a card DB
    created before Phase 2a lacks ``global_id`` / ``sync_version``. This adds them
    idempotently (checked against ``PRAGMA table_info``) so an existing repo's
    card DB self-heals on the next connection — the same self-healing posture the
    module docstring describes for a swapped-out ``cards.sqlite``.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(cards)").fetchall()}
    if "global_id" not in existing:
        conn.execute("ALTER TABLE cards ADD COLUMN global_id TEXT")
    if "sync_version" not in existing:
        conn.execute("ALTER TABLE cards ADD COLUMN sync_version INTEGER")


@contextmanager
def _connect():
    """Yield a fresh SQLite connection to the current card database.

    Opens by path (so a replaced inode is always resolved to the live file),
    ensures the schema exists, commits on clean exit, rolls back on error, and
    always closes.  Raises if ``set_db_path`` has not been called yet.
    """
    if _db_path is None:
        raise RuntimeError("Database not initialized. Server may not have started properly.")
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    try:
        _ensure_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Eagerly create the database file and schema (called once at startup)."""
    with _connect():
        pass


def create_card(title: str, description: Optional[str] = None, priority: str = "medium") -> dict:
    """Create a new task card."""
    try:
        card_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()
        # Mint the central canonical task id at creation (proposal §14.2). Best-
        # effort: None if contracts is unavailable — the card is still created.
        global_id = _mint_global_id()
        sync_version = 1

        with _connect() as conn:
            conn.execute("""
            INSERT INTO cards (card_id, title, description, status, priority, created_at, updated_at, global_id, sync_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (card_id, title, description, "Created", priority, now, now, global_id, sync_version))

        log(f"Created card {card_id} (global_id={global_id}): {title}")

        _emit_task_event(
            "emit_task_created", card_id=card_id, title=title,
            status="Created", priority=priority,
            global_id=global_id, version=sync_version, description=description,
        )

        return {
            "card_id": card_id,
            "title": title,
            "description": description,
            "status": "Created",
            "priority": priority,
            "created_at": now,
            "global_id": global_id,
            "sync_version": sync_version,
        }
    except Exception as e:
        log(f"ERROR creating card: {e}")
        return {"error": f"Failed to create card: {str(e)}"}


def list_cards(status: Optional[str] = None, priority: Optional[str] = None) -> dict:
    """List cards with optional filters."""
    try:
        query = "SELECT * FROM cards WHERE 1=1"
        params = []

        if status:
            query += " AND status = ?"
            params.append(status)

        if priority:
            query += " AND priority = ?"
            params.append(priority)

        query += " ORDER BY created_at DESC"

        with _connect() as conn:
            cards = [dict(row) for row in conn.execute(query, params).fetchall()]

        log(f"Listed {len(cards)} cards (status={status}, priority={priority})")
        return {
            "cards": cards,
            "total": len(cards)
        }
    except Exception as e:
        log(f"ERROR listing cards: {e}")
        return {"error": f"Failed to list cards: {str(e)}", "cards": [], "total": 0}


def get_card(card_id: str) -> dict:
    """Retrieve a card by card_id."""
    try:
        with _connect() as conn:
            card_row = conn.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,)).fetchone()

            if not card_row:
                log(f"Card not found: {card_id}")
                return {"error": f"Card {card_id} not found"}

            card = dict(card_row)

            comments = [dict(row) for row in conn.execute("""
            SELECT comment_id, author, comment, created_at FROM card_comments
            WHERE card_id = ? ORDER BY created_at ASC
            """, (card_id,)).fetchall()]

        card["comments"] = comments

        log(f"Retrieved card {card_id} with {len(comments)} comments")
        return card
    except Exception as e:
        log(f"ERROR retrieving card {card_id}: {e}")
        return {"error": f"Failed to retrieve card: {str(e)}"}


def update_card(card_id: str, title: Optional[str] = None,
                description: Optional[str] = None, priority: Optional[str] = None,
                status: Optional[str] = None) -> dict:
    """Update a card's fields."""
    try:
        if status is not None and status not in VALID_STATUSES:
            return {"error": f"Invalid status '{status}'. Must be one of: {', '.join(VALID_STATUSES)}"}

        # Build dynamic update query
        updates = []
        params = []

        if title is not None:
            updates.append("title = ?")
            params.append(title)

        if description is not None:
            updates.append("description = ?")
            params.append(description)

        if priority is not None:
            updates.append("priority = ?")
            params.append(priority)

        if status is not None:
            updates.append("status = ?")
            params.append(status)

        if not updates:
            return {"error": "No fields to update"}

        updates.append("updated_at = ?")
        params.append(datetime.now(timezone.utc).isoformat())
        # Every mutation is a new synchronizable version (monotonic; reconciliation-
        # ready for the next slice). COALESCE handles a pre-Phase-2a row (NULL).
        updates.append("sync_version = COALESCE(sync_version, 0) + 1")
        # Backfill a global id for a card created before Phase 2a (NULL stays NULL
        # only if minting is unavailable) so an updated legacy card becomes
        # synchronizable without a separate migration pass.
        updates.append("global_id = COALESCE(global_id, ?)")
        params.append(_mint_global_id())
        params.append(card_id)

        query = f"UPDATE cards SET {', '.join(updates)} WHERE card_id = ?"

        with _connect() as conn:
            conn.execute(query, params)

        log(f"Updated card {card_id}")

        # Return updated card (opens its own short-lived connection)
        result = get_card(card_id)

        # Emit task.updated from the fresh card state (covers status transitions).
        if isinstance(result, dict) and "error" not in result:
            _emit_task_event(
                "emit_task_updated",
                card_id=card_id,
                title=result.get("title"),
                status=result.get("status"),
                priority=result.get("priority"),
                global_id=result.get("global_id"),
                version=result.get("sync_version"),
                description=result.get("description"),
            )

        return result
    except Exception as e:
        log(f"ERROR updating card {card_id}: {e}")
        return {"error": f"Failed to update card: {str(e)}"}


def add_comment(card_id: str, author: str, comment: str) -> dict:
    """Add a work log entry/comment to a card."""
    try:
        now = datetime.now(timezone.utc).isoformat()

        with _connect() as conn:
            # Verify card exists
            if not conn.execute("SELECT card_id FROM cards WHERE card_id = ?", (card_id,)).fetchone():
                log(f"Card not found for comment: {card_id}")
                return {"error": f"Card {card_id} not found"}

            conn.execute("""
            INSERT INTO card_comments (card_id, author, comment, created_at)
            VALUES (?, ?, ?, ?)
            """, (card_id, author, comment, now))

            # Visible within this connection before commit.
            result = conn.execute(
                "SELECT comment_id FROM card_comments WHERE card_id = ? ORDER BY comment_id DESC LIMIT 1",
                (card_id,)
            ).fetchone()

            if not result:
                raise RuntimeError("Failed to retrieve inserted comment")

            comment_id = result[0]

        log(f"Added comment {comment_id} to card {card_id}")

        return {
            "comment_id": comment_id,
            "card_id": card_id,
            "author": author,
            "comment": comment,
            "created_at": now
        }
    except Exception as e:
        log(f"ERROR adding comment to card {card_id}: {e}")
        return {"error": f"Failed to add comment: {str(e)}"}


def complete_card(card_id: str, completion_summary: str) -> dict:
    """Mark a card as Complete with a summary."""
    try:
        log(f"Completing card {card_id}")

        # Add completion summary as final comment
        add_comment(card_id, "system", f"Completion: {completion_summary}")

        # Update status. Note: update_card emits task.updated(status=Complete) for
        # the transition; we additionally emit task.completed here so a consumer
        # sees both the status transition and the completion marker (both truthful).
        result = update_card(card_id, status="Complete")

        if isinstance(result, dict) and "error" not in result:
            _emit_task_event(
                "emit_task_completed",
                card_id=card_id,
                title=result.get("title"),
                status=result.get("status"),
                priority=result.get("priority"),
                global_id=result.get("global_id"),
                version=result.get("sync_version"),
                description=result.get("description"),
            )

        log(f"Card {card_id} marked as Complete")
        return result
    except Exception as e:
        log(f"ERROR completing card {card_id}: {e}")
        return {"error": f"Failed to complete card: {str(e)}"}


def apply_task_projection(task: Union[dict, object]) -> dict:
    """Apply a central TaskRecord DOWN into the local card DB (Phase 2a).

    The DOWN half of task sync (mirror of the UP-path event emission above): given
    a central task projection (a TaskRecord-shaped ``dict`` or object with the same
    attributes), idempotently create-or-update the local ``cards.sqlite`` row that
    carries this ``global_id``.

    Correctness invariants (see card ffcaf548 scope cuts):

    * **Idempotent, keyed by ``global_id``.** A repeated apply of the same or an
      older projection finds the existing local card by ``global_id`` and updates
      it in place (last-write-wins from central — M1 is the sole writer this
      slice); it never creates a duplicate and never changes the local ``card_id``.
    * **Never re-emits a task event.** This path writes the card row directly and
      does NOT call :func:`_emit_task_event` — re-emitting would ship the applied
      state straight back up and create a sync loop.
    * **Always writes a valid local status.** The central TaskSyncStatus is mapped
      back to a :data:`VALID_STATUSES` value (``blocked`` -> "In Progress").

    Best-effort return: the applied local card dict (via :func:`get_card`), or an
    ``{"error": ...}`` dict — it never raises, so a poller can advance its cursor
    only on a clean result.
    """
    try:
        def _field(name):
            return task.get(name) if isinstance(task, dict) else getattr(task, name, None)

        global_id = _field("global_id")
        if not global_id:
            return {"error": "apply_task_projection requires a global_id"}

        title = _field("title")
        description = _field("description")
        priority = _field("priority")
        local_status = _sync_status_to_local(_field("status"))
        version = _field("version")
        now = datetime.now(timezone.utc).isoformat()

        with _connect() as conn:
            existing = conn.execute(
                "SELECT card_id FROM cards WHERE global_id = ?", (global_id,)
            ).fetchone()
            if existing is not None:
                card_id = existing["card_id"]
                # Last-write-wins update; keep the local card_id, never emit. Content
                # fields COALESCE so a status-only projection does not blank title.
                conn.execute(
                    "UPDATE cards SET title = COALESCE(?, title), "
                    "description = COALESCE(?, description), "
                    "priority = COALESCE(?, priority), status = ?, "
                    "sync_version = ?, updated_at = ? WHERE global_id = ?",
                    (title, description, priority, local_status, version, now, global_id),
                )
                action = "updated"
            else:
                # A newly-projected card gets its OWN fresh local display id; the
                # global_id is the cross-machine identity that keeps it idempotent.
                card_id = str(uuid.uuid4())[:8]
                conn.execute(
                    "INSERT INTO cards (card_id, title, description, status, priority, "
                    "created_at, updated_at, global_id, sync_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (card_id, title if title is not None else "", description,
                     local_status, priority, now, now, global_id, version),
                )
                action = "created"

        log(f"Applied task projection {global_id} -> local card {card_id} ({action}, {local_status})")
        return get_card(card_id)
    except Exception as e:
        log(f"ERROR applying task projection: {e}")
        return {"error": f"Failed to apply task projection: {str(e)}"}
