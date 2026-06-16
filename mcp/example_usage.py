#!/usr/bin/env python3
"""
Example usage of the task-cards MCP server.
This demonstrates how agents might use the card system in practice.
"""

# This file demonstrates the intended workflow for using task cards.
# In practice, agents will call these tools via MCP.

# Example 1: Agent creates a new task
# ==================================
# create_card(
#     title="Refactor authentication module",
#     description="Extract auth logic into separate service with dependency injection",
#     priority="high"
# )
# Returns: {
#     'card_id': 'abc12345',
#     'title': 'Refactor authentication module',
#     'status': 'Created',
#     'priority': 'high',
#     'created_at': '2024-06-14T10:30:00...'
# }

# Example 2: Agent moves to "In Progress" and starts work
# =======================================================
# update_card('abc12345', status='In Progress')
# add_comment('abc12345', author='claude', comment='Started refactoring auth module')
# # ... does work ...
# add_comment('abc12345', author='claude', comment='Created AuthService interface')

# Example 3: Agent queries open tasks
# ===================================
# result = list_cards(status='Created')
# for card in result['cards']:
#     print(f"{card['card_id']}: {card['title']} (priority: {card['priority']})")

# Example 4: Agent reviews progress on a specific card
# ====================================================
# card = get_card('abc12345')
# print(f"Card: {card['title']}")
# print(f"Status: {card['status']}")
# print(f"Work log:")
# for comment in card['comments']:
#     print(f"  [{comment['author']}] {comment['comment']}")

# Example 5: Agent completes task with summary
# ============================================
# complete_card(
#     'abc12345',
#     completion_summary='Auth refactoring complete. New service fully tested with 95% coverage.'
# )

# Multi-Agent Workflow Example
# =============================
# User: "Build out the payment system"
#
# Orchestrator Agent:
#   1. create_card("Implement Stripe integration", priority="high")
#   2. create_card("Add payment UI components", priority="medium")
#   3. create_card("Write payment tests", priority="medium")
#
# Worker Agent 1:
#   1. get_card('stripe-id')
#   2. update_card('stripe-id', status='In Progress')
#   3. add_comment('stripe-id', 'Installed stripe library')
#   4. # ... implements ...
#   5. add_comment('stripe-id', 'Webhook handling complete')
#   6. update_card('stripe-id', status='In Progress')  # still testing
#
# Worker Agent 2:
#   1. get_card('ui-id')
#   2. update_card('ui-id', status='In Progress')
#   3. # ... creates components ...
#   4. complete_card('ui-id', completion_summary='Components ready, integrated with stripe module')
#
# Orchestrator Agent (final):
#   1. result = list_cards(status='Complete')
#   2. Reviews all completed work
#   3. Updates memory systems
#   4. Summarizes for user

print("""
Task Cards MCP Server - Example Usage

This file demonstrates intended workflows. In practice, MCP tools are called by agents.

See CLAUDE.md for detailed tool documentation.
""")
