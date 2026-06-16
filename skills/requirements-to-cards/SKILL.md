---
name: requirements-to-cards
description: Preload for the research-planner agent; use to convert ambiguous or broad requirements into executable, dependency-aware cards.
---

# Requirements to Cards

1. Clarify outcome, non-goals, constraints, and success criteria.
2. Inspect existing work with `list_cards`.
3. Query code and DB graphs to identify affected components.
4. Break work into independently testable units.
5. Define objective, context, acceptance criteria, dependencies, node IDs, tests, and recommended agent for each unit.
6. Use `create_card` only when instructed to materialize the plan.
7. Use `add_comment` for sequencing or shared assumptions.

Avoid unsupported implementation detail.
