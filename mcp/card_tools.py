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

from graph_io import log

# Allowed card lifecycle states
VALID_STATUSES = ("Created", "In Progress", "Complete")

# Path to the repo-local card database, injected by server.py after it has
# resolved the repo root.  ``None`` until the server initialises.
_db_path: Optional[Path] = None


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
        updated_at TIMESTAMP
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

        with _connect() as conn:
            conn.execute("""
            INSERT INTO cards (card_id, title, description, status, priority, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (card_id, title, description, "Created", priority, now, now))

        log(f"Created card {card_id}: {title}")

        return {
            "card_id": card_id,
            "title": title,
            "description": description,
            "status": "Created",
            "priority": priority,
            "created_at": now
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
        params.append(card_id)

        query = f"UPDATE cards SET {', '.join(updates)} WHERE card_id = ?"

        with _connect() as conn:
            conn.execute(query, params)

        log(f"Updated card {card_id}")

        # Return updated card (opens its own short-lived connection)
        return get_card(card_id)
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

        # Update status
        result = update_card(card_id, status="Complete")
        log(f"Card {card_id} marked as Complete")
        return result
    except Exception as e:
        log(f"ERROR completing card {card_id}: {e}")
        return {"error": f"Failed to complete card: {str(e)}"}
