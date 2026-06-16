# JIRA_CARD_MCP_SPEC.md

## Objective

Create a lightweight Jira-style task management MCP server using FastMCP and SQLite. This system will act as the primary task management layer for Claude Code and AI agents working within a repository.

The system should be simple, reliable, and repository-local.

---

# Core Requirements

## Technology Stack

- Python
- FastMCP
- SQLite
- Repository-local storage

---

# Project Isolation

Cards MUST exist at the repository/project level.

Each repository should contain:

```txt
.agent-os/
  cards.sqlite
  config.toml
```

The MCP server code may be globally installed, but card data must remain local to the active repository.

---

# Card States

Cards may only exist in:

- Created
- In Progress
- Complete

---

# SQLite Database

Minimum schema:

```sql
CREATE TABLE cards (
    card_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL,
    priority TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);
```

```sql
CREATE TABLE card_comments (
    comment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT NOT NULL,
    author TEXT,
    comment TEXT,
    created_at TIMESTAMP
);
```

---

# MCP Tools

## create_card
Create a new card.

Inputs:
- title
- description
- priority

Returns:
- card_id

## list_cards
List cards with optional filters:
- status
- priority

## get_card
Retrieve a card by card_id.

## update_card
Update:
- title
- description
- priority
- status

## add_comment
Add a work log entry.

Inputs:
- card_id
- author
- comment

## complete_card
Mark a card as Complete.

Inputs:
- card_id
- completion_summary

---

# Agent Workflow

User
→ Opus (Architect / Delegator)
→ Subagents
→ Card Updates
→ Review

Workflow:

1. Opus creates cards.
2. Subagents work against a specific card.
3. Progress is logged via comments.
4. Status moves:
   - Created
   - In Progress
   - Complete
5. Completed work is reviewed.
6. Memory systems may be updated.

---

# Claude Code Integration

The MCP server should always be available inside Claude Code.

Requirements:

- Automatically connected after setup.
- Available to all agents by default.
- Treated as part of the standard development environment.

---

# CLAUDE.md Integration

Recommended rules:

- Work begins with a card.
- Significant implementation tasks require a card.
- Progress should be logged to cards.
- Completed work should update card status.
- Cards are the source of truth for active work.

---

# Future Expansion (Not Required Initially)

Potential future features:

- Agent ownership
- Dependencies
- Card-to-memory links
- Card-to-file links
- ADR integration
- Graphify integration
- Multi-agent coordination

Focus on a simple, reliable repository-local implementation first.
