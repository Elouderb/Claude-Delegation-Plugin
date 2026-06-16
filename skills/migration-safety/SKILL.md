---
name: migration-safety
description: Preload for the database-engineer agent; use for schema changes and migrations requiring live dependency inspection and rollback-aware planning.
---

# Migration Safety

1. Read the card with `get_card`.
2. Use `db_search_schema`, `db_get_table`, `db_get_column`, `db_get_table_relationships`, `db_find_relationship_path`, and `db_get_routine_dependencies`.
3. Inspect connected code with `code_search_symbols` and `code_impact_analysis`.
4. Identify destructive changes, locking risk, backfills, key constraints, index effects, compatibility windows, and rollback strategy.
5. Prefer additive, backwards-compatible migrations.
6. Never execute destructive production operations without explicit approval.
7. Record the plan and risks with `add_comment`.
