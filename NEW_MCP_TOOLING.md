# MCP_GRAPH_TOOLS_SPEC.md

## Objective

Extend the project-local FastMCP server with 18 read-oriented tools for querying the database graph and Graphify code graph. Tools should return compact JSON, preserve stable node IDs and relationship direction, report ambiguous matches instead of guessing, and include graph generation timestamps where available.

## Database Refresh Requirement

Every database-specific tool call **must rebuild the database graph before answering**. The implementation should run these scripts, in order, from the project root:

```bash
python build_db_graph.py
python build_graph_html.py
```

If either script fails, return a structured MCP error containing the command, exit code, stdout, and stderr. Do not return stale database results after a refresh failure. Use `subprocess.run(..., check=True, capture_output=True, text=True)` with a reasonable timeout. After successful execution, load the newly generated `.agent-os/db/db_graph.json`.

Create a shared helper such as `refresh_database_graph()` and call it at the start of every DB tool.

## Shared Graph Tools

These tools should support `graph="database"` or `graph="code"` where applicable.

1. **graph_search_nodes** — Search nodes by name, qualified name, type, and metadata. Support exact/fuzzy matching, type filters, and limits.

2. **graph_get_node** — Return complete metadata for one stable node ID.

3. **graph_get_neighbors** — Return incoming, outgoing, or bidirectional neighbors with relationship and depth filters.

4. **graph_find_path** — Find directed or undirected paths between nodes with maximum depth, relationship filters, and result limits.

5. **graph_get_subgraph** — Return a bounded node-and-edge subgraph around one or more seed nodes.

6. **graph_status** — Return file existence, generation timestamp, source path, node/edge counts, and staleness information.

7. **graph_refresh** — Explicitly rebuild the selected graph. Database refresh runs both scripts above; Graphify refresh invokes the configured Graphify update command.

## Database Graph Tools

All six tools below must call `refresh_database_graph()` before reading graph data.

8. **db_get_table** — Return table metadata, columns, primary keys, foreign keys, incoming/outgoing references, and dependent routines.

9. **db_get_column** — Return type, nullability, default, identity/computed flags, key status, owning table, linked columns, and routine dependencies.

10. **db_search_schema** — Search Table, Column, Function, and Procedure nodes by name or qualified name.

11. **db_get_table_relationships** — Return table-level incoming and outgoing relationships while preserving exact key-column pairs.

12. **db_find_relationship_path** — Find foreign-key paths between two tables and return the table sequence plus join columns.

13. **db_get_routine_dependencies** — Return each Table or Column connected to a Function or Procedure through `Creates` relationships.

## Graphify Code-Graph Tools

14. **code_get_symbol** — Return one code symbol, source file, line range when available, containing module/class, callers, callees, imports, and connected symbols.

15. **code_search_symbols** — Search classes, functions, methods, modules, files, interfaces, and services.

16. **code_get_dependencies** — Return incoming and outgoing dependencies with depth and relationship filters.

17. **code_find_callers** — Return direct and transitive callers of a symbol with bounded depth.

18. **code_impact_analysis** — Return likely affected callers, modules, tests, interfaces, and public entry points for a proposed symbol change.

## Response Standard

Every tool should return:

```json
{
  "graph": "database",
  "generated_at": "ISO-8601 timestamp",
  "query": {},
  "results": {},
  "warnings": [],
  "truncated": false
}
```

Use project-local graph files only. Keep tools read-only except for explicit refresh operations. Never silently choose between ambiguous node matches.
