---
name: code-graph-usage
description: Use for repository discovery, symbol lookup, dependency analysis, caller analysis, bounded architecture context, and code-change impact analysis.
---

# Code Graph Usage

Prefer graph tools before broad `Glob` or `Grep`.

- Unknown symbol or location: `code_search_symbols`.
- Known symbol details: `code_get_symbol`.
- Dependencies and dependents: `code_get_dependencies`.
- Signature or behavior changes: `code_find_callers`.
- Shared or architectural changes: `code_impact_analysis`.
- Generic neighborhood/path questions: `graph_get_neighbors`, `graph_find_path`.
- Bounded planning context: `graph_get_subgraph`.
- Freshness and rebuild: `graph_status`, `graph_refresh`.

Use exact node IDs after resolution. Report ambiguity rather than guessing. Query small subgraphs and verify source files before editing.
