---
name: database-graph-usage
description: Use for live SQL Server schema discovery, table and column inspection, foreign-key paths, routine dependencies, and database impact analysis.
---

# Database Graph Usage

Every `db_*` call rebuilds the live DB graph by running `build_db_graph.py` and `build_graph_html.py`. If refresh fails, stop; never use stale output.

- Unknown object: `db_search_schema`.
- Known table: `db_get_table`.
- Known column: `db_get_column`.
- Adjacent tables and key pairs: `db_get_table_relationships`.
- Path between tables: `db_find_relationship_path`.
- Routine dependencies: `db_get_routine_dependencies`.
- Generic traversal: `graph_get_node`, `graph_get_neighbors`, `graph_find_path`, `graph_get_subgraph`.
- Availability/freshness: `graph_status`.

Preserve direction: `Stores` Table→Column; `Links` FK Column→referenced key Column; `Creates` Procedure/Function→referenced Table/Column. Dynamic SQL may be incomplete.
