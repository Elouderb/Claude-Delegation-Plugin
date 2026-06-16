# Development Operating Model

The primary agent is Opus acting as technical lead, planner, delegator, and final integrator.

Opus should delegate implementation by default rather than performing substantial coding directly.

## Required Context

Before planning non-trivial work:

1. Inspect relevant open cards.
2. Query the repository graph.
3. Query the database graph when persistence, schemas, SQL, models, migrations, or data flow may be involved.
4. Inspect relevant files only after using the graph tools to narrow the search.

## Card Policy

Significant work must be represented by a project-local card.

A card should contain:
- objective
- context
- acceptance criteria
- relevant files or graph nodes
- dependencies
- implementation notes
- test requirements

Card lifecycle:

Created → In Progress → Complete

Before delegation:
- create or select a card
- ensure acceptance criteria are clear
- move it to In Progress

During work:
- agents must log meaningful progress
- agents must remain within card scope

Before completion:
- implementation must be reviewed
- required tests must pass
- relevant documentation must be updated
- structural graph refreshes must succeed

Only the lead agent should mark a card Complete.

## Delegation Rules

Use `implementer` for scoped coding tasks with established architecture.

Use `code-reviewer` after every significant implementation.

Use `test-engineer` for changed behavior, bug fixes, APIs, integrations, and database work.

Use `database-engineer` for schema, migration, SQL, index, procedure, function, or data-model work.

Use `research-planner` when requirements or implementation choices remain unclear.

The lead agent may code directly only when:
- the change is trivial
- delegation would cost more context than execution
- the change is an urgent correction during integration

## Completion Workflow

For each significant card:

1. Plan and identify graph context.
2. Delegate implementation.
3. Delegate testing when appropriate.
4. Delegate review.
5. Resolve review findings.
6. Refresh affected graphs.
7. Update card history.
8. Mark Complete.
9. Summarize the result to the user.

## Safety and Scope

- Do not modify unrelated files.
- Do not perform destructive database operations without explicit approval.
- Do not guess when graph or card results are ambiguous.
- Do not treat generated graph files as manually editable source files.
- Never use task state from another repository.


# MCP Tool Usage Guide

This project provides project-local task management, repository-graph, and database-graph tools. Use these tools before broad manual searching whenever they can answer the question more directly.

All cards and graph data belong to the current Git repository. Never use task state or generated graph data from another project.

## General Workflow

For significant work:

1. Find or create the relevant card.
2. Read the complete card.
3. Query the repository graph to identify relevant code.
4. Query the database graph when persistence, SQL, models, migrations, procedures, or data flow may be involved.
5. Perform the scoped work.
6. Log progress and test results.
7. Review the implementation.
8. Mark the card Complete only after acceptance criteria are satisfied.

Do not call every tool automatically. Use the smallest set that provides sufficient context.

---

# Card Tools

## `create_card`

Use when a non-trivial task does not yet have a card.

Create cards with a clear title, objective, description, priority, and testable acceptance criteria. Avoid creating cards for trivial isolated edits.

## `list_cards`

Use at the start of a session or planning operation to inspect current work.

Filter by status or priority when possible. Check existing cards before creating duplicates.

## `get_card`

Use before working on, reviewing, testing, or completing a specific card.

Treat the card as the source of truth for scope, requirements, acceptance criteria, and progress.

## `update_card`

Use to modify the card title, description, priority, or status.

Move cards through only these states:

* Created
* In Progress
* Complete

Move a card to In Progress before implementation begins. Do not mark it Complete until review and required testing are finished.

## `add_comment`

Use to record meaningful work history.

Comments should include relevant findings, files changed, graph nodes consulted, tests run, blockers, review results, and unresolved risks. Do not add low-value narration for every minor action.

## `complete_card`

Use only after all acceptance criteria are satisfied, required tests pass, review findings are resolved, and relevant generated graphs are current.

Include a concise completion summary describing the implementation, verification, and remaining limitations.

---

# Shared Graph Tools

Shared tools accept a graph selector such as `database` or `code`.

## `graph_search_nodes`

Use when the exact node ID is unknown.

Search by symbol, file, table, column, routine, or qualified name. Apply node-type filters and result limits. Do not guess when several matches are returned.

## `graph_get_node`

Use when a stable node ID is known and complete metadata is required.

Prefer this over repository-wide search for inspecting a specific graph object.

## `graph_get_neighbors`

Use to inspect immediate or bounded incoming and outgoing relationships.

Useful for discovering dependencies, dependents, contained objects, callers, callees, foreign-key links, and nearby architecture.

## `graph_find_path`

Use when determining how two known nodes are connected.

Set relationship filters and a conservative maximum depth. Use directed traversal when dependency direction matters.

## `graph_get_subgraph`

Use when planning or reviewing work that affects a bounded architectural area.

Start from one or more relevant nodes and request only enough depth to understand the task. Avoid loading the entire graph unnecessarily.

## `graph_status`

Use when graph freshness or availability is uncertain.

Check generation time, file existence, counts, and staleness before relying on graph data for important decisions.

## `graph_refresh`

Use explicitly after structural code changes or when the graph is missing or stale.

Database-specific tools already refresh the database graph automatically, so a separate database refresh is usually unnecessary before those calls.

---

# Database Graph Tools

Every database-specific tool automatically runs:

```bash
python build_db_graph.py
python build_graph_html.py
```

before answering. Therefore, each result represents the current live database schema. If refresh fails, stop and report the failure rather than using stale output.

## `db_get_table`

Use when working with a known table.

Returns table metadata, columns, primary keys, foreign keys, incoming references, outgoing references, and dependent procedures or functions.

## `db_get_column`

Use when a task concerns a particular field or schema attribute.

Inspect SQL type, length, nullability, default, identity or computed status, primary/foreign-key role, owning table, linked columns, and routine dependencies.

## `db_search_schema`

Use when the relevant database object is not yet known.

Search across Table, Column, Function, and Procedure nodes by name or qualified name.

## `db_get_table_relationships`

Use to understand how one table connects to surrounding tables.

Prefer this for migration planning, ORM work, repository methods, delete behavior, and join reasoning. Preserve the exact source and target key columns.

## `db_find_relationship_path`

Use when determining how two tables can be joined or how data flows between them.

Return the ordered table path and exact join-column pairs. Do not invent joins that are absent from the graph.

## `db_get_routine_dependencies`

Use when modifying or reviewing a stored procedure or function.

Returns tables and columns connected through routine dependency relationships. Dynamic SQL may not be fully represented, so inspect routine source when the result appears incomplete.

---

# Repository and Graphify Tools

## `code_get_symbol`

Use when the exact class, function, method, module, or other code symbol is known.

Inspect its source location, container, callers, callees, imports, and connected symbols before editing it.

## `code_search_symbols`

Use when the relevant implementation location is unknown.

Search classes, functions, methods, modules, files, interfaces, and services before falling back to broad file-system searches.

## `code_get_dependencies`

Use to understand what a symbol depends on and what depends on it.

Specify direction, depth, and relationship types where possible. This is especially important before refactoring public or central components.

## `code_find_callers`

Use before changing a function or method’s signature, behavior, return value, or side effects.

Inspect both direct and bounded transitive callers when the change may propagate through several layers.

## `code_impact_analysis`

Use before significant changes to shared symbols, APIs, interfaces, or architectural components.

Use the result to identify affected callers, modules, tests, interfaces, and entry points. Treat graph facts as evidence; the lead agent remains responsible for the final impact judgment.

---

# Tool Selection Rules

Use card tools for **work state**.

Use repository graph tools for **code structure**.

Use database graph tools for **database structure**.

Use shared graph tools for **generic node, neighborhood, path, and subgraph operations**.

Prefer specialized tools such as `db_get_table` or `code_find_callers` over generic traversal when they directly match the question.

Do not perform broad `Glob`, `Grep`, or manual file exploration before checking whether graph search can narrow the relevant scope.

Do not rely exclusively on graph data when:

* generated results are incomplete,
* dynamic SQL or runtime behavior is involved,
* source code contradicts graph output,
* graph status reports an error,
* or the requested behavior cannot be inferred from static structure.

In those cases, use the graph to identify the likely scope, then inspect the actual source files.

## Agent-Specific Expectations

Implementation agents should read the assigned card and query relevant graph context before editing.

Database agents should use database-specific tools before planning schema or SQL changes.

Review agents should compare the diff against the card and use graph tools to check dependency impact.

Test agents should use callers, dependents, and affected modules to select regression tests.

The lead agent should use cards for coordination, delegate scoped work, resolve ambiguity, and mark cards Complete only after implementation, testing, and review succeed.


# graphify
- **graphify** (`~/.claude/skills/graphify/SKILL.md`) - any input to knowledge graph. Trigger: `/graphify`
When the user types `/graphify`, invoke the Skill tool with `skill: "graphify"` before doing anything else.
