---
name: scoped-implementation
description: Preload for the implementer and complex-implementer agents; use to implement a clear card with minimal, complete, in-scope changes.
---

# Scoped Implementation

1. Read the card with `get_card`.
2. Use `code_search_symbols`, `code_get_symbol`, `code_get_dependencies`, and `graph_get_subgraph` before editing.
3. Use `db_*` tools when persistence may be affected.
4. Implement the smallest complete change satisfying every acceptance criterion.
5. Avoid unrelated cleanup, architecture invention, and unnecessary public API changes.
6. Run relevant tests and static checks.
7. Add a card comment with changed files, tests, acceptance-criteria status, and remaining risks.

Do not mark the card Complete or broaden scope without approval.
