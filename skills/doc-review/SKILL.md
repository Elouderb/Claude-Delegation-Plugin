---
name: doc-review
description: Preload for the codebase-consultant agent; audit the repository's documentation against the actual code and structure, and report what is stale.
---

# Doc Review

Audit Markdown documentation for accuracy against the real codebase. Read-only: report findings; do not edit (the lead dispatches fixes).

1. Enumerate the docs: `Glob` for `**/*.md` (README, CHANGELOG, CLAUDE.md, templates, and the docs under `mcp/`, `hooks/`, `skills/`, `agents/`). Note which are canonical — overview, install, operating model — versus incidental.
2. Read each doc and extract its concrete claims: commands, file paths, tool / agent / skill names and counts, version strings, URLs, config keys, API shapes, and described behavior.
3. Cross-check each claim against reality:
   - Code and structure claims — verify with the code graph (`code_search_symbols`, `code_get_symbol`, `code_get_dependencies`) and by reading the referenced files; confirm paths exist with `Glob`.
   - Counts and inventories (e.g. a stated number of tools, agents, or skills) — recount from the source of truth.
   - Cross-references — confirm linked files and sections still exist.
4. Classify each finding: **stale** (no longer true), **contradictory** (docs disagree with each other), **missing** (real behavior undocumented), or **redundant** (the same thing documented in several places that have since drifted).

Return a per-file report: each finding with the doc `file:line`, what it claims, what is actually true (with the source `file:line` or node ID), and a concrete suggested fix. Prioritize canonical and user-facing docs. Do not modify files.
