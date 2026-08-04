"""Agent OS central-store database package (proposal §17 first slice, Phase 1a).

This package owns the migration runner and versioned SQL migrations that stand
up the ``registry.*`` and ``ops.*`` schema groups in the central SQL Server
database (``agent_os_memory``) — the write-capable store shared across
machines once the control plane exists (see epic 78a6ac11 / card 6f83ab1f).

Submodules:

  * :mod:`database.migrate` — the migration runner: discovery, checksumming,
    transactional apply, status/dry-run reporting, and the CLI
    (``python -m database.migrate``).
  * :mod:`database.events` — minimal helpers to round-trip a
    ``contracts.events.EventEnvelope`` through ``ops.events`` (insert / fetch /
    delete), used by the live verification test.
  * ``database/migrations/*.sql`` — numbered, checksummed migration files.

Out of scope for this phase (see the card): the ``work``/``memory``/
``content``/``vector`` schema groups, the Agent OS API service, and the
outbox — those are later phases in the epic.

This package depends only on the standard library, ``pyodbc``, ``python-dotenv``,
and ``contracts`` (for :mod:`database.events`) — never on ``mcp/`` — mirroring
``contracts``'s own dependency direction (ruling 1: nothing central depends on
the plugin's MCP layer).
"""

from __future__ import annotations

__all__: list[str] = []
