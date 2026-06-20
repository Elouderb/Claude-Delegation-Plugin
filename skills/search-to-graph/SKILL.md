---
name: search-to-graph
description: Preload for the codebase-consultant agent; start from grep / tree / glob when you lack good symbol names, then pivot into the code graph for structure and reach.
---

# Search to Graph

Use when you do NOT have reliable names to query the graph with — an unfamiliar area, a raw string or error message, config / text / markdown, or a "where does this literal come from" question.

1. Get a foothold with traditional search:
   - `Grep` for a string, identifier, error text, or pattern.
   - `Glob` and `tree` (via Bash) to see directory structure and file layout.
   - Read one or two hits to learn the real names in play — functions, classes, modules.
2. Pivot into the graph as soon as you have a name: feed those identifiers to `code_search_symbols`, then `code_get_dependencies` / `code_find_callers` / `graph_get_subgraph` to understand structure and reach instead of grepping the whole tree.
3. Alternate as needed: grep to find a concrete anchor, the graph to understand how it connects.

The grep/tree step is a launching point, not the destination — once you know what to ask the graph, let the graph do the structural work (it is bounded and relationship-aware where raw search is not). Keep `agent-os:graph-query-discipline` in mind for limits and freshness. Cite `file:line` and node IDs.
