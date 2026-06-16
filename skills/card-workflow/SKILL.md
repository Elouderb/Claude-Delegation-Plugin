---
name: card-workflow
description: Use whenever creating, selecting, updating, logging, reviewing, or completing project-local Jira-style cards.
---

# Card Workflow

Cards are project-local and must never cross repository boundaries.

- Use `list_cards` before creating work.
- Use `create_card` for non-trivial work with explicit acceptance criteria.
- Use `get_card` before implementation, testing, review, or completion.
- Allowed states are exactly `Created`, `In Progress`, and `Complete`.
- Use `update_card` to move to `In Progress` before implementation.
- Use `add_comment` for decisions, graph findings, changed files, tests, blockers, and review outcomes.
- Use `complete_card` only after acceptance criteria, testing, and review succeed.
- Keep work within card scope; avoid unrelated refactors.
