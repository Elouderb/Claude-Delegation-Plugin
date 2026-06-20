---
name: database-graph-usage
description: Use for live SQL Server schema discovery, table and column inspection, foreign-key paths, routine dependencies, and database impact analysis.
---

# Database Graph Usage

The `db_*` tools use two build strategies. Four of them build a **targeted, bounded neighborhood** in memory; two build (or reuse) the **full, TTL-cached** graph.

## Targeted (bounded-neighborhood) tools

`db_get_table`, `db_get_column`, `db_get_table_relationships`, and `db_get_routine_dependencies` build only the subgraph around the object you queried, expanding outward a bounded number of relationship hops over FK edges (`sys.foreign_keys`) **and** routine/object dependency edges (`sys.sql_expression_dependencies`). The scoped graph is built **in memory and returned directly** — it does **not** write `db_graph.json`, does **not** read or update the TTL cache, and does **not** touch the shared full-graph file. The node/edge schema is identical to the full build; the result is simply a subset.

Depth semantics (the entry object is always included):

- `depth = 0` → the entry object only (plus its own columns, for a table).
- `depth = N` → objects within `N` relationship hops; objects beyond `N` hops are excluded (and so are the edges that would cross the boundary).

Default depths: `db_get_table`, `db_get_column`, and `db_get_table_relationships` use depth **1** (the object plus its immediate neighbors); `db_get_routine_dependencies` uses depth **2** (far enough to reach the columns of the tables a routine touches). Override every targeted default with `AGENT_OS_DB_GRAPH_DEPTH=<n>` (a non-negative integer; blank/invalid falls back to the per-tool default).

A targeted result reflects only its bounded neighborhood. If you need global reach, use a full tool below or widen the depth.

## Full (TTL-cached) tools

`db_search_schema` (a global search needs every object) and `db_find_relationship_path` (two arbitrary endpoints) build/reuse the whole-schema graph. Each calls `refresh_database_graph()` before reading. By default that graph is rebuilt at most once every 30 seconds (TTL guard): consecutive calls within the same window reuse the cached `.agent-os/db/db_graph.json` without spawning any subprocess. Results are therefore **fresh within the TTL window** (at most 30 s stale in a burst). Set `AGENT_OS_DB_GRAPH_TTL=0` to disable the cache and restore unconditional rebuilds on every call.

If a full rebuild fails but a cached graph file exists on disk, the last-good graph is served and a warning is included in the response. If no cache is available and the rebuild fails, the call returns an error — stop and report rather than use stale output. (Targeted tools have no shared cache to fall back on: a build failure surfaces as "Database graph not found".)

## Tool selection

- Unknown object: `db_search_schema` (full).
- Known table: `db_get_table` (targeted, depth 1).
- Known column: `db_get_column` (targeted, depth 1).
- Adjacent tables and key pairs: `db_get_table_relationships` (targeted, depth 1).
- Path between tables: `db_find_relationship_path` (full).
- Routine dependencies: `db_get_routine_dependencies` (targeted, depth 2).
- Generic traversal: `graph_get_node`, `graph_get_neighbors`, `graph_find_path`, `graph_get_subgraph`.
- Availability/freshness: `graph_status`.

Preserve direction: `Stores` Table→Column; `Links` FK Column→referenced key Column; `Creates` Procedure/Function→referenced Table/Column. Dynamic SQL may be incomplete.
