---
name: graph-query-discipline
description: Use whenever querying either graph to keep results bounded, current, unambiguous, and suitable for downstream agents.
---

# Graph Query Discipline

1. Start with `graph_search_nodes` or the appropriate domain search tool.
2. Resolve to stable node IDs.
3. Use `graph_get_node` for complete metadata.
4. Use `graph_get_neighbors` for local relationships.
5. Use `graph_find_path` only between known nodes.
6. Use `graph_get_subgraph` with conservative depth and limits.
7. Use `graph_status` when freshness matters.
8. Use `graph_refresh` after structural changes when domain tools do not refresh automatically.

Preserve direction and relationship type. Include timestamps in decisions. Report truncation and ambiguous matches.
