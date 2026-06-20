---
name: implementation-handoff
description: Preload for the implementer and complex-implementer agents; use when returning completed implementation work to the lead and reviewer.
---

# Implementation Handoff

- Re-read the card with `get_card`.
- Compare the diff against each acceptance criterion.
- Use `code_find_callers` or `code_impact_analysis` for shared symbols.
- Use relevant `db_*` tools for DB behavior.
- Run the narrowest adequate tests.
- Use `add_comment` to record files changed, behavior implemented, tests, migrations/configuration, unresolved risks, and reviewer focus.

Return a concise handoff. Never claim tests passed when they were not run.
