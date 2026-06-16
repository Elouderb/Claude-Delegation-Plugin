#!/usr/bin/env python3
"""
Test script for the task-cards MCP server.
Verifies database operations without starting the server.
"""

import sys
import os
from pathlib import Path
import tempfile
import shutil

# Add server module to path
sys.path.insert(0, str(Path(__file__).parent))

import server


def test_task_cards():
    """Run basic tests against the server."""
    print("Testing task-cards MCP server...\n")

    # Use a temporary directory for testing
    test_dir = tempfile.mkdtemp(prefix="task_cards_test_")
    original_cwd = os.getcwd()

    try:
        os.chdir(test_dir)
        print(f"Test directory: {test_dir}\n")

        # Initialize server
        server.ensure_agent_os()
        print("✓ Database initialized\n")

        # Test: Create cards
        print("Test 1: Create cards")
        card1 = server.create_card(
            title="Implement user auth",
            description="Add JWT-based authentication",
            priority="high"
        )
        print(f"  Created: {card1['card_id']} - {card1['title']}")

        card2 = server.create_card(
            title="Fix database queries",
            priority="medium"
        )
        print(f"  Created: {card2['card_id']} - {card2['title']}")

        card3 = server.create_card(
            title="Update documentation",
            priority="low"
        )
        print(f"  Created: {card3['card_id']} - {card3['title']}\n")

        # Test: List all cards
        print("Test 2: List all cards")
        result = server.list_cards()
        print(f"  Total cards: {result['total']}")
        for card in result['cards']:
            print(f"    - {card['card_id']}: {card['title']} ({card['status']})")
        print()

        # Test: Filter by priority
        print("Test 3: Filter by priority (high)")
        result = server.list_cards(priority="high")
        print(f"  Found: {result['total']}")
        for card in result['cards']:
            print(f"    - {card['title']}")
        print()

        # Test: Update card status
        print("Test 4: Update card status")
        card_id = card1['card_id']
        print(f"  Moving {card_id} to 'In Progress'")
        updated = server.update_card(card_id, status="In Progress")
        print(f"  Status: {updated['status']}")
        print()

        # Test: Add comments
        print("Test 5: Add work log comments")
        server.add_comment(card_id, "claude", "Started implementation")
        print(f"  Added comment to {card_id}")
        server.add_comment(card_id, "claude", "Database schema finalized")
        print(f"  Added another comment")
        print()

        # Test: Get card with comments
        print("Test 6: Retrieve card with comments")
        full_card = server.get_card(card_id)
        print(f"  Card: {full_card['title']}")
        print(f"  Status: {full_card['status']}")
        print(f"  Comments: {len(full_card['comments'])}")
        for comment in full_card['comments']:
            print(f"    - [{comment['author']}] {comment['comment']}")
        print()

        # Test: Complete card
        print("Test 7: Complete card")
        card2_id = card2['card_id']
        print(f"  Completing {card2_id}")
        completed = server.complete_card(card2_id, "All queries optimized and tested")
        print(f"  Status: {completed['status']}")
        print(f"  Comments: {len(completed['comments'])}")
        print()

        # Test: List by status
        print("Test 8: Filter by status")
        for status in ["Created", "In Progress", "Complete"]:
            result = server.list_cards(status=status)
            print(f"  {status}: {result['total']} cards")

        print("\n✓ All tests passed!")
        return True

    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        os.chdir(original_cwd)
        shutil.rmtree(test_dir, ignore_errors=True)


if __name__ == "__main__":
    success = test_task_cards()
    sys.exit(0 if success else 1)
