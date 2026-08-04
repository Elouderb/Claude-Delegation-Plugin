"""Agent OS long-running services (control-plane side, proposal §17 / §18.1).

Top-level home for standalone services that front the central store. Unlike the
plugin's ``mcp/`` layer (which runs inside each Claude Code process), a service
here is a separate long-lived process — currently just :mod:`services.api`, the
versioned HTTP API in front of ``agent_os_memory`` (Phase 1c, epic 78a6ac11).

Depends only on the standard library, Flask, pyodbc, the ``database`` package,
and ``contracts`` — never on ``mcp/`` (ruling 1: nothing central depends on the
plugin's MCP layer).
"""

from __future__ import annotations

__all__: list[str] = []
