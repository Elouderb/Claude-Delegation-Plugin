---
name: library-docs
description: Preload for code-editing and research agents; before writing or debugging code against an unfamiliar library, framework, SDK, CLI, or cloud API, fetch current docs via context7.
---

# Library Docs (context7)

Your training data may lag the library. Before committing to an API you are not certain of — even a well-known one — confirm it.

1. `resolve-library-id` to map the library / framework / SDK name to its context7 ID.
2. `query-docs` with that ID and a specific question — the API, config key, migration step, or CLI flag you need.
3. Use the returned docs to write the call correctly the first time, rather than guessing and iterating against errors.

Use this for library / framework / SDK / CLI / cloud-service specifics — not for general programming concepts or business-logic debugging. For anything touching Claude / Anthropic models, prefer the `claude-api` skill. For open-ended, multi-source investigation, prefer `deep-research`.
