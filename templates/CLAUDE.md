# Development Operating Model

This project uses the **agent-os** plugin: a delegation-driven workflow built around
project-local task cards, a repository (code) graph, and an optional database graph.

The lead (orchestrating) agent acts as technical lead, planner, delegator, and final
integrator. The lead should delegate implementation by default rather than performing
substantial coding directly, and is the only agent that marks a card Complete.

Plugin skills encode each step of this workflow. Invoke them as `agent-os:<skill-name>`.
When prose here describes a workflow, the matching skill is the executable version — follow
it instead of improvising.

## Required Context

Before planning non-trivial work:

1. Inspect relevant open cards (`agent-os:card-workflow`).
2. Query the repository graph to narrow scope (`agent-os:code-graph-usage`,
   `agent-os:graph-query-discipline`).
3. Query the database graph **only if this project configures one** and the work touches
   persistence, schemas, SQL, models, migrations, or data flow
   (`agent-os:database-graph-usage`).
4. Inspect files only after graph queries have narrowed the search.

Use the smallest set of tools that gives sufficient context. Do not call every tool
automatically, and do not run broad `Glob`/`Grep`/manual exploration before checking
whether a graph query can narrow the scope first.

## Card Policy

Significant work must be represented by a project-local card. See `agent-os:card-workflow`
for the mechanics and `agent-os:plan-feature` / `agent-os:requirements-to-cards` for
decomposing larger or ambiguous work into cards.

A card should contain: objective, context, acceptance criteria, relevant files or graph
nodes, dependencies, implementation notes, and test requirements.

Lifecycle: **Created → In Progress → Complete**

- **Before delegation:** create or select a card, ensure acceptance criteria are clear,
  move it to In Progress.
- **During work:** agents log meaningful progress and stay within card scope.
- **Before completion:** implementation is reviewed, required tests pass, relevant docs are
  updated, and any structural graph refresh succeeds.

Only the lead agent marks a card Complete.

## Delegation Rules

| Situation | Agent | Preloaded skill |
|---|---|---|
| Scoped coding with established architecture | `implementer` | `agent-os:scoped-implementation`, `agent-os:execute-card` |
| Difficult/complex or repo-wide change (cross-module, large refactor, shared or central code) | `complex-implementer` (opus, high effort) | `agent-os:scoped-implementation`, `agent-os:execute-card` |
| Review after every significant implementation | `code-reviewer` | `agent-os:independent-code-review`, `agent-os:review-risk-triage` |
| Changed behavior, bug fixes, APIs, integrations, DB work | `test-engineer` | `agent-os:targeted-test-planning`, `agent-os:test-execution-reporting` |
| Schema, migration, SQL, index, procedure, function, data-model work | `database-engineer` | `agent-os:migration-safety`, `agent-os:sql-routine-analysis` |
| Unclear requirements or implementation choices | `research-planner` | `agent-os:architecture-research`, `agent-os:requirements-to-cards` |
| Understand the codebase / answer a repo question without bloating context | `codebase-consultant` (read-only; the doing/reviewing agents can delegate to it via the `Agent` tool) | `agent-os:graph-file-discovery`, `agent-os:search-to-graph`, `agent-os:doc-review` |
| UI / frontend work (components, styling, client behavior) | `frontend-engineer` | `frontend-design:frontend-design`, `agent-os:browser-verification`, `agent-os:lsp-diagnostics` |
| Security review of a sensitive diff (auth, secrets, input, escaping, deps) | `security-reviewer` | `security-review`, `agent-os:review-risk-triage` |
| Confirm a change works by running the real app / browser | `verification-engineer` | `agent-os:runtime-verification`, `agent-os:browser-verification` |

The lead may code directly only when the change is trivial, delegation would cost more
context than execution, or it is an urgent correction during integration.

## Completion Workflow

For each significant card (`agent-os:review-and-close`):

1. Plan and identify graph context (`agent-os:plan-feature`).
2. Delegate implementation (`agent-os:execute-card`).
3. Delegate testing when behavior changed.
4. Delegate review.
5. Resolve review findings.
6. Refresh affected graphs.
7. Update card history (`agent-os:card-workflow`).
8. Mark Complete.
9. Summarize the result to the user.

## Tooling

All cards and graph data belong to the **current** Git repository. Never use task state or
generated graph data from another project.

### Cards (work state)

`create_card`, `list_cards`, `get_card`, `update_card`, `add_comment`, `complete_card`.
See `agent-os:card-workflow`. Use card tools to track work, not to narrate every minor
action.

### Repository (code) graph

| Situation | Tool |
|---|---|
| Implementation location unknown | `code_search_symbols` |
| Exact symbol known; need full metadata | `code_get_symbol` |
| What a symbol depends on / what depends on it | `code_get_dependencies` |
| Before changing a signature, behavior, or side effects | `code_find_callers` |
| Before changing shared symbols, APIs, or architecture | `code_impact_analysis` |

Generic graph operations work on either graph: `graph_search_nodes`, `graph_get_node`,
`graph_get_neighbors`, `graph_find_path`, `graph_get_subgraph`. Check freshness with
`graph_status` and rebuild after structural changes with `graph_refresh`. Prefer the
specialized `code_*` tools over generic traversal when they directly match the question.
See `agent-os:code-graph-usage` and `agent-os:graph-query-discipline`.

### Database graph (optional — only if this project configures one)

This half applies **only** to projects whose database-graph subsystem is configured against
a live **SQL Server** database. Projects with no database, or with a non-SQL-Server
database, can ignore this section entirely; the rest of the operating model still applies.

When configured, the database-specific tools rebuild the graph (`build_db_graph.py` then
`build_graph_html.py`) before answering, so results reflect the current schema **only if
that rebuild succeeds**. A short staleness cache avoids rebuilding on every call: within
`AGENT_OS_DB_GRAPH_TTL` seconds (default 30; set `0` to always rebuild) a recent graph is
reused, so results may be up to that many seconds stale. If a refresh fails but a cached
graph exists, the last-good graph is served with a warning; if none exists, the failure is
reported. Stop and report rather than relying on stale output when freshness is critical.

| Situation | Tool |
|---|---|
| Known table: columns, keys, references, dependent routines | `db_get_table` |
| A particular column / field attribute | `db_get_column` |
| Relevant object not yet known | `db_search_schema` |
| How one table connects to surrounding tables | `db_get_table_relationships` |
| How two tables join / how data flows between them | `db_find_relationship_path` |
| Tables/columns a procedure or function depends on | `db_get_routine_dependencies` |

Preserve exact source/target key columns and never invent joins absent from the graph.
Dynamic SQL may be under-represented — inspect routine source when a result looks
incomplete. See `agent-os:database-graph-usage`, `agent-os:migration-safety`, and
`agent-os:sql-routine-analysis`.

### When not to trust the graph

Use the graph to find scope, then read source, when: generated results are incomplete,
dynamic SQL or runtime behavior is involved, source contradicts the graph, `graph_status`
reports an error, or the behavior cannot be inferred from static structure.

## Safety and Scope

- Do not modify unrelated files.
- Do not perform destructive database operations without explicit approval.
- Do not guess when graph or card results are ambiguous.
- Do not treat generated graph files as manually editable source.
- Never use task state or graph data from another repository.
