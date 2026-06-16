---
name: review-and-close
description: Use after implementation to coordinate testing, independent review, graph refresh, card updates, and final completion.
---

# Review and Close

1. Load the card with `get_card` and inspect the implementation diff.
2. Invoke `test-engineer` when behavior, APIs, database logic, or regression risk changed.
3. Invoke `code-reviewer` for every significant implementation.
4. Use `code_impact_analysis`, `code_find_callers`, and `code_get_dependencies` to verify affected code.
5. For DB work, use `db_get_table`, `db_get_column`, `db_get_table_relationships`, and `db_get_routine_dependencies`.
6. On failure, record findings with `add_comment`, keep the card `In Progress`, and return it for fixes.
7. Repeat verification after fixes.
8. Refresh structural graphs with `graph_refresh` when required and verify with `graph_status`.
9. Record final tests and review outcome with `add_comment`.
10. Call `complete_card` only when every acceptance criterion is satisfied.
