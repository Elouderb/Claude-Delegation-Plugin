---
name: review-risk-triage
description: Preload for the code-reviewer agent; use to prioritize review findings by severity, confidence, and affected graph surface.
---

# Review Risk Triage

Classify findings as:

- **Blocker:** data loss, security flaw, broken acceptance criterion, invalid migration, or major regression.
- **Major:** likely defect, compatibility break, missing required test, or broad unhandled impact.
- **Minor:** maintainability or edge-case concern that does not block acceptance.
- **Note:** optional improvement.

Use `code_impact_analysis`, `code_find_callers`, `graph_get_neighbors`, and DB relationship tools as evidence. Cite exact files, symbols, tables, columns, or graph node IDs.
