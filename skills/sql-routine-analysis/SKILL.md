---
name: sql-routine-analysis
description: Preload for the database-engineer agent; use for stored procedures, functions, queries, indexes, and routine-to-table or column dependencies.
---

# SQL and Routine Analysis

Use `db_get_routine_dependencies` for known routines and `db_search_schema` when unknown. Inspect tables with `db_get_table`, columns with `db_get_column`, and relationships with `db_get_table_relationships`.

Check contracts, referenced objects, transactions, null handling, cardinality, join paths, indexes, dynamic SQL gaps, code callers, and schema compatibility. Because `Creates` is dependency metadata, verify routine source when write semantics matter. Record findings with `add_comment`.
