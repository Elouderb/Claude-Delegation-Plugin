---
name: graph-file-discovery
description: Preload for the codebase-consultant agent; use the code graph to find the files relevant to a question, then read only those files.
---

# Graph File Discovery

Use when you have names to start from — a symbol, function, class, concept, or feature.

1. `code_search_symbols` to locate candidate symbols by name or keyword; resolve to stable node IDs.
2. `code_get_symbol` for the definition and metadata of an exact match.
3. `code_get_dependencies` (what it uses) and `code_find_callers` (what uses it) to expand outward only as far as the question needs.
4. `code_impact_analysis` before reasoning about the effect of a change to shared or central code.
5. `graph_get_subgraph` with conservative depth for a bounded structural view; `graph_status` for freshness.
6. Read the files the graph points to — and only those. Confirm behavior in the source; the graph finds scope, the file proves it.

Prefer the specialized `code_*` tools over generic traversal. Cite a node ID and `file:line` for every file you relied on. Keep reads targeted — do not load a whole file when a region answers the question.
