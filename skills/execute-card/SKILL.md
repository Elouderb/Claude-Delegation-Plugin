---
name: execute-card
description: Use proactively when a project card has clear acceptance criteria and is ready for implementation by the appropriate specialist subagent.
---

# Execute Card

1. Read the card with `get_card`.
2. If scope or acceptance criteria are unclear, stop and recommend `plan-feature`.
3. Move the card to `In Progress` with `update_card`.
4. Select the agent: `implementer` for scoped application work, `database-engineer` for DB work, or `research-planner` when choices remain unresolved.
5. Gather compact context with `code_search_symbols`, `code_get_dependencies`, `graph_get_subgraph`, and relevant `db_*` tools.
6. Invoke the selected subagent with card ID, acceptance criteria, graph node IDs, constraints, and expected tests.
7. Record progress, changed files, tests, blockers, and risks with `add_comment`.

Do not call `complete_card`; independent review and verification are required.
