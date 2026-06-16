---
name: targeted-test-planning
description: Preload for the test-engineer agent; use to select the smallest adequate test suite from the card, diff, callers, and dependency graph.
---

# Targeted Test Planning

1. Read the card with `get_card`.
2. Inspect the diff.
3. Use `code_find_callers`, `code_get_dependencies`, and `code_impact_analysis`.
4. For DB changes, use `db_get_table_relationships`, `db_get_column`, and `db_get_routine_dependencies`.
5. Select unit, integration, regression, migration, or end-to-end tests based on risk.
6. Avoid the full suite when bounded tests provide adequate evidence unless project policy requires it.
7. Record the plan and rationale with `add_comment`.
