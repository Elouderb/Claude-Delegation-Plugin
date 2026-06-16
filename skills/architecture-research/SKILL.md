---
name: architecture-research
description: Preload for the research-planner agent; use to investigate unfamiliar architecture, libraries, alternatives, or unresolved requirements before coding.
---

# Architecture Research

Research and plan only.

1. Read the card or request.
2. Use `code_search_symbols`, `code_get_symbol`, `code_get_dependencies`, and `graph_get_subgraph`.
3. Use DB graph tools when data design is relevant.
4. Identify constraints, alternatives, tradeoffs, repo precedent, risks, and open questions.
5. Recommend one approach with explicit reasoning.
6. Propose card boundaries and acceptance criteria.
7. Record material findings with `add_comment` when a card exists.

Separate graph-confirmed facts, source-confirmed facts, and inference.
