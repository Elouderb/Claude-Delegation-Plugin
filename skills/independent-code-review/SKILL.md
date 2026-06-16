---
name: independent-code-review
description: Preload for the code-reviewer agent; use to review a diff independently against its card, graph context, and project standards.
---

# Independent Code Review

Do not modify code unless explicitly asked.

1. Read the card with `get_card`.
2. Inspect the diff and relevant source.
3. Use `code_get_dependencies`, `code_find_callers`, and `code_impact_analysis`.
4. Use DB graph tools for persistence changes.
5. Check correctness, acceptance criteria, scope, regressions, security, error handling, compatibility, and tests.
6. Distinguish blocking findings from suggestions.
7. Record the result with `add_comment`.

Return exactly `PASS`, `PASS_WITH_NOTES`, or `FAIL`, with evidence and exact fixes.
