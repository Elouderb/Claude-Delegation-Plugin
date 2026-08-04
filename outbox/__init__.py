"""Repository-local sync outbox — Phase 1b (proposal §5.5, §17 first slice).

The offline half of the local-first sync plane: identity bootstrap, a durable
append-only event outbox under ``.agent-os/sync/``, and validated
``EventEnvelope`` emission at the plugin's existing lifecycle points. **No
network** — draining the outbox to the central Agent OS API is Phase 1c's job;
this package only records events locally so a central-server outage can never
corrupt local workflows.

Layering: depends only on the standard library and the top-level ``contracts``
package (the pure Phase 0 models); it never imports from ``mcp/``. Both ``mcp/``
and ``scripts/`` import it by bare package name after putting the repo root on
``sys.path`` (the same convention the test-suite uses), and a future Phase 1c
drain worker imports the :mod:`~outbox.store` reader API the same way.

Public surface (deliberately the PRODUCER surface only — the drain/store/paths
API is reached as ``outbox.store.*`` / ``outbox.paths.*`` by a Phase 1c worker,
not re-exported at the top level, so the package's public contract stays small
and matches what the living plugin actually calls):

* identity   — :func:`bootstrap_identity` (startup + hook entry).
* emission   — :func:`emit_event` plus the typed per-site emitters
  (``emit_task_created`` / ``emit_task_updated`` / ``emit_task_completed`` /
  ``emit_repo_graph_updated`` / ``emit_db_graph_updated`` / ``emit_agent_started``
  / ``emit_agent_finished``); ``AGENT_OS_EVENTS_DISABLED=1`` disables all of it.
* submodules — :mod:`~outbox.store` (drain/reader API, Phase 1c),
  :mod:`~outbox.paths`, :mod:`~outbox.emit`, :mod:`~outbox.identity`.
"""

from __future__ import annotations

from . import emit, identity, paths, store
from .emit import (
    emit_agent_finished,
    emit_agent_started,
    emit_db_graph_updated,
    emit_event,
    emit_repo_graph_updated,
    emit_task_completed,
    emit_task_created,
    emit_task_updated,
    events_disabled,
)
from .identity import bootstrap_identity, git_remote_origin

__all__ = [
    # submodules (drain/store/paths API reached through these)
    "emit",
    "identity",
    "paths",
    "store",
    # identity
    "bootstrap_identity",
    "git_remote_origin",
    # emission (producer surface)
    "emit_event",
    "events_disabled",
    "emit_task_created",
    "emit_task_updated",
    "emit_task_completed",
    "emit_repo_graph_updated",
    "emit_db_graph_updated",
    "emit_agent_started",
    "emit_agent_finished",
]
