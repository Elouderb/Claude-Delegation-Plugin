"""
Card management MCP tool implementations.

Functions here are imported by server.py, registered with @server.tool(), and
re-exported so that ``import server; server.create_card(...)`` continues to
work (as the test suite relies on that form).

``_db_conn`` is set by ``server.py`` after database initialisation via
``set_db_conn()``.  Card functions reference it at call time, not import time,
so the late binding is safe.
"""

from datetime import datetime, timezone
from typing import Optional
import uuid

from graph_io import log

# Allowed card lifecycle states
VALID_STATUSES = ("Created", "In Progress", "Complete")

# Injected by server.py after database initialisation.
_db_conn = None


def set_db_conn(conn):
    """Point this module at the active SQLite connection."""
    global _db_conn
    _db_conn = conn


def create_card(title: str, description: Optional[str] = None, priority: str = "medium") -> dict:
    """Create a new task card."""
    try:
        if not _db_conn:
            raise RuntimeError("Database not initialized. Server may not have started properly.")

        card_id = str(uuid.uuid4())[:8]
        now = datetime.now(timezone.utc).isoformat()

        cursor = _db_conn.cursor()
        cursor.execute("""
        INSERT INTO cards (card_id, title, description, status, priority, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (card_id, title, description, "Created", priority, now, now))

        _db_conn.commit()
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
        if not _db_conn:
            raise RuntimeError("Database not initialized. Server may not have started properly.")

        cursor = _db_conn.cursor()

        query = "SELECT * FROM cards WHERE 1=1"
        params = []

        if status:
            query += " AND status = ?"
            params.append(status)

        if priority:
            query += " AND priority = ?"
            params.append(priority)

        query += " ORDER BY created_at DESC"
        cursor.execute(query, params)

        cards = []
        for row in cursor.fetchall():
            cards.append(dict(row))

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
        if not _db_conn:
            raise RuntimeError("Database not initialized. Server may not have started properly.")

        cursor = _db_conn.cursor()

        # Get card
        cursor.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,))
        card_row = cursor.fetchone()

        if not card_row:
            log(f"Card not found: {card_id}")
            return {"error": f"Card {card_id} not found"}

        card = dict(card_row)

        # Get comments
        cursor.execute("""
        SELECT comment_id, author, comment, created_at FROM card_comments
        WHERE card_id = ? ORDER BY created_at ASC
        """, (card_id,))

        comments = [dict(row) for row in cursor.fetchall()]
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
        if not _db_conn:
            raise RuntimeError("Database not initialized. Server may not have started properly.")

        if status is not None and status not in VALID_STATUSES:
            return {"error": f"Invalid status '{status}'. Must be one of: {', '.join(VALID_STATUSES)}"}

        cursor = _db_conn.cursor()

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
        cursor.execute(query, params)
        _db_conn.commit()

        log(f"Updated card {card_id}")

        # Return updated card
        return get_card(card_id)
    except Exception as e:
        log(f"ERROR updating card {card_id}: {e}")
        return {"error": f"Failed to update card: {str(e)}"}


def add_comment(card_id: str, author: str, comment: str) -> dict:
    """Add a work log entry/comment to a card."""
    try:
        if not _db_conn:
            raise RuntimeError("Database not initialized. Server may not have started properly.")

        cursor = _db_conn.cursor()

        # Verify card exists
        cursor.execute("SELECT card_id FROM cards WHERE card_id = ?", (card_id,))
        if not cursor.fetchone():
            log(f"Card not found for comment: {card_id}")
            return {"error": f"Card {card_id} not found"}

        now = datetime.now(timezone.utc).isoformat()

        cursor.execute("""
        INSERT INTO card_comments (card_id, author, comment, created_at)
        VALUES (?, ?, ?, ?)
        """, (card_id, author, comment, now))

        _db_conn.commit()

        cursor.execute("SELECT comment_id FROM card_comments WHERE card_id = ? ORDER BY comment_id DESC LIMIT 1", (card_id,))
        result = cursor.fetchone()

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
        if not _db_conn:
            raise RuntimeError("Database not initialized. Server may not have started properly.")

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
