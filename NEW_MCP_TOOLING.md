# MCP Graph Tools

> These 18 tools are **IMPLEMENTED** in `mcp/server.py`; this document was the
> original spec and is retained as a reference for their intended behavior.

## Objective

The project-local MCP server (`mcp/server.py`) provides 18 read-oriented tools for querying the database graph and Graphify code graph. Tools return compact JSON, preserve stable node IDs and relationship direction, report ambiguous matches instead of guessing, and include graph generation timestamps where available.

## Database Refresh Requirement

Every database-specific tool call **rebuilds the database graph before answering**. The implementation runs these scripts, in order, from `mcp/db_tools/` (located at runtime by `find_db_tools_dir()`, which resolves the scripts relative to `server.py` with fallbacks):

```bash
python build_db_graph.py
python build_graph_html.py
```

If either script fails, the tool returns a structured MCP error containing the command, exit code, stdout, and stderr. Stale database results are not returned after a refresh failure. Execution uses `subprocess.run(..., check=True, capture_output=True, text=True)` with a reasonable timeout. After successful execution, the newly generated `.agent-os/db/db_graph.json` is loaded.

A shared helper, `refresh_database_graph()`, is called at the start of every DB tool.

## Shared Graph Tools

These tools support `graph="database"` or `graph="code"` where applicable.

1. **graph_search_nodes** — Search nodes by name, qualified name, type, and metadata. Supports exact/fuzzy matching, type filters, and limits.

2. **graph_get_node** — Returns complete metadata for one stable node ID.

3. **graph_get_neighbors** — Returns incoming, outgoing, or bidirectional neighbors with relationship and depth filters.

4. **graph_find_path** — Finds directed or undirected paths between nodes with maximum depth, relationship filters, and result limits.

5. **graph_get_subgraph** — Returns a bounded node-and-edge subgraph around one or more seed nodes.

6. **graph_status** — Returns file existence, generation timestamp, source path, node/edge counts, and staleness information.

7. **graph_refresh** — Explicitly rebuilds the selected graph. Database refresh runs both scripts above; Graphify refresh invokes the configured Graphify update command.

## Database Graph Tools

All six tools below call `refresh_database_graph()` before reading graph data.

8. **db_get_table** — Returns table metadata, columns, primary keys, foreign keys, incoming/outgoing references, and dependent routines.

9. **db_get_column** — Returns type, nullability, default, identity/computed flags, key status, owning table, linked columns, and routine dependencies.

10. **db_search_schema** — Searches Table, Column, Function, and Procedure nodes by name or qualified name.

11. **db_get_table_relationships** — Returns table-level incoming and outgoing relationships while preserving exact key-column pairs.

12. **db_find_relationship_path** — Finds foreign-key paths between two tables and returns the table sequence plus join columns.

13. **db_get_routine_dependencies** — Returns each Table or Column connected to a Function or Procedure through `Creates` relationships.

## Graphify Code-Graph Tools

14. **code_get_symbol** — Returns one code symbol, source file, line range when available, containing module/class, callers, callees, imports, and connected symbols.

15. **code_search_symbols** — Searches classes, functions, methods, modules, files, interfaces, and services.

16. **code_get_dependencies** — Returns incoming and outgoing dependencies with depth and relationship filters.

17. **code_find_callers** — Returns direct and transitive callers of a symbol with bounded depth.

18. **code_impact_analysis** — Returns likely affected callers, modules, tests, interfaces, and public entry points for a proposed symbol change.

## Response Standard

Every tool returns:

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

Tools use project-local graph files only. They are read-only except for explicit refresh operations, and never silently choose between ambiguous node matches.
