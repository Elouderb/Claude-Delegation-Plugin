"""Event emission: build a validated EventEnvelope and append it to the outbox.

This is the producer entry the living plugin calls at each existing lifecycle
point (card create/update/complete, graph refresh, session/subagent hooks). The
whole layer is built around three non-negotiables from the card:

* **Cheap** — one small buffered append (``outbox.store.append_envelope``); no
  network, ever. Context ids are read from the identity files once and cached
  per-process (:func:`_resolve_context`) so repeated emits do not re-read them.
* **Never breaks the caller** — :func:`emit_event` swallows *every* exception and
  logs it best-effort to ``.agent-os/sync/outbox.log`` (the ``graph_io.log`` /
  hooks.log precedent). A failing outbox must never fail a card or graph
  operation. A *missing* identity (bootstrap not run / failed) is an expected,
  silent no-op — not even logged, and it creates no files.
* **Kill-switchable** — ``AGENT_OS_EVENTS_DISABLED=1`` disables emission entirely
  (belt-and-braces for perf/debugging), checked before any work.

Every envelope is constructed through :class:`contracts.EventEnvelope` (so it is
validated on the way out), with ``event_id = new_id(EVENT)``,
``occurred_at = contracts.utc_now()``, and an ``event_type`` drawn from the
``contracts.EventType`` registry. Payloads are intentionally tiny — ids, titles,
statuses — never full card bodies or comment threads.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from . import _fsutil, paths, store

_PathLike = Union[str, Path]

_DISABLED_ENV = "AGENT_OS_EVENTS_DISABLED"

# Per-process cache of (machine_id, repository_id, project_id) keyed by repo root.
# Identity is stable, so this never needs invalidation; it just spares repeated
# JSON reads on the hot path. Guarded by a lock for defense-in-depth if the
# server is ever run multi-threaded.
_ids_cache: Dict[str, Tuple[str, str, Optional[str]]] = {}
_ids_lock = threading.Lock()


def events_disabled() -> bool:
    """True iff the ``AGENT_OS_EVENTS_DISABLED`` kill-switch is engaged."""
    return _fsutil.env_flag(_DISABLED_ENV)


def seed_context(repo_root: _PathLike, ids: Tuple[str, str, Optional[str]]) -> None:
    """Seed the per-process identity cache from already-resolved ids.

    Called by :func:`outbox.identity.bootstrap_identity` so a subsequent
    :func:`emit_event` skips its own identity file read + pydantic validation
    (relevant on the one-shot hook path, where bootstrap and emit run
    back-to-back in a fresh interpreter). ``ids`` is
    ``(machine_id, repository_id, project_id)``.
    """
    with _ids_lock:
        _ids_cache[str(Path(repo_root))] = ids


def _resolve_context(repo_root: _PathLike) -> Optional[Tuple[str, str, Optional[str]]]:
    """Return ``(machine_id, repository_id, project_id)`` for ``repo_root``.

    Reads the machine-global and repo-local identity files (cached per-process).
    Returns ``None`` — the "identity not bootstrapped" signal — if either file is
    absent or unparseable; the caller treats that as a silent no-op.
    """
    key = str(Path(repo_root))
    cached = _ids_cache.get(key)
    if cached is not None:
        return cached
    from contracts.identity import (
        MachineIdentity,
        RepositoryIdentity,
        machine_identity_path,
        repository_identity_path,
    )

    machine_file = machine_identity_path()
    repo_file = repository_identity_path(Path(repo_root))
    if not machine_file.exists() or not repo_file.exists():
        return None
    machine = MachineIdentity.model_validate_json(machine_file.read_text(encoding="utf-8"))
    repo = RepositoryIdentity.model_validate_json(repo_file.read_text(encoding="utf-8"))
    ids = (machine.machine_id, repo.repository_id, repo.project_id)
    with _ids_lock:
        _ids_cache[key] = ids
    return ids


def _log_best_effort(repo_root: _PathLike, message: str) -> None:
    """Append a diagnostic line to ``.agent-os/sync/outbox.log`` — never raises."""
    try:
        paths.ensure_sync_dir(repo_root)
        stamp = datetime.now(timezone.utc).isoformat()
        with paths.log_path(repo_root).open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except Exception:
        pass


def _type_str(event_type: Any) -> str:
    """Coerce an EventType enum member (or a bare string) to its dotted value."""
    value = getattr(event_type, "value", event_type)
    return str(value)


def emit_event(
    repo_root: _PathLike,
    event_type: Any,
    payload: Optional[Dict[str, Any]] = None,
    *,
    task_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Optional[str]:
    """Build, validate, and append one EventEnvelope; return its id (or ``None``).

    Returns ``None`` (a no-op) when events are disabled, when identity is not
    bootstrapped, or when anything at all goes wrong (the error is logged
    best-effort). It never raises — card/graph/hook operations are the caller and
    must never fail because of the outbox.
    """
    if events_disabled():
        return None
    try:
        context = _resolve_context(repo_root)
    except Exception:
        # Identity present-but-unreadable: treat as not-bootstrapped, silent.
        return None
    if context is None:
        return None  # identity not bootstrapped — expected silent no-op
    machine_id, repository_id, project_id = context
    try:
        from contracts import EVENT, EventEnvelope, new_id, utc_now

        envelope = EventEnvelope(
            event_id=new_id(EVENT),
            event_type=_type_str(event_type),
            occurred_at=utc_now(),
            machine_id=machine_id,
            repository_id=repository_id,
            project_id=project_id,
            task_id=task_id,
            agent_id=agent_id,
            session_id=session_id,
            payload=dict(payload) if payload else {},
        )
        store.append_envelope(repo_root, envelope)
        return envelope.event_id
    except Exception as exc:
        _log_best_effort(repo_root, f"emit failed for {_type_str(event_type)}: {exc}")
        return None


# --------------------------------------------------------------------------- #
# Typed convenience emitters — one per emission site in the plan. Each keeps the
# payload tiny.
#
# CRITICAL (card ce79a987 finding 1): these MUST NOT import ``contracts`` (e.g.
# ``from contracts import EventType``) in their own bodies. Such an import runs
# *outside* :func:`emit_event`'s swallow AND before its kill-switch check, so a
# broken/absent ``contracts`` (or an engaged kill-switch) would let the exception
# escape and break the calling card/graph/hook op — the exact invariant this
# layer must never violate. Instead they pass the dotted event-type *string*
# straight to :func:`emit_event`, which checks ``events_disabled()`` first and
# builds/validates the envelope inside its own try/except. The strings are kept
# in lockstep with ``contracts.EventType`` by test_event_emission's conformance
# test (each constant == the matching registry member's value); the envelope's
# ``event_type`` field is an open ``Dotted`` string, so passing the value is
# semantically identical to passing the enum member.
# --------------------------------------------------------------------------- #
# Dotted event-type constants (mirror contracts.EventType values; test-locked).
TASK_CREATED = "task.created"
TASK_UPDATED = "task.updated"
TASK_COMPLETED = "task.completed"
REPO_GRAPH_UPDATED = "repo.graph_updated"
DB_GRAPH_UPDATED = "db.graph_updated"
AGENT_STARTED = "agent.started"
AGENT_FINISHED = "agent.finished"


def _task_payload(card_id, title, status, priority) -> Dict[str, Any]:
    return {"card_id": card_id, "title": title, "status": status, "priority": priority}


def _emit_task(event_type: str, repo_root, *, card_id, title, status, priority) -> Optional[str]:
    """Shared body for the three task emitters (identical but for the type)."""
    return emit_event(
        repo_root,
        event_type,
        _task_payload(card_id, title, status, priority),
        task_id=card_id,
    )


def emit_task_created(repo_root, *, card_id, title, status, priority) -> Optional[str]:
    return _emit_task(TASK_CREATED, repo_root, card_id=card_id, title=title,
                      status=status, priority=priority)


def emit_task_updated(repo_root, *, card_id, title, status, priority) -> Optional[str]:
    return _emit_task(TASK_UPDATED, repo_root, card_id=card_id, title=title,
                      status=status, priority=priority)


def emit_task_completed(repo_root, *, card_id, title, status, priority) -> Optional[str]:
    return _emit_task(TASK_COMPLETED, repo_root, card_id=card_id, title=title,
                      status=status, priority=priority)


def emit_repo_graph_updated(repo_root) -> Optional[str]:
    return emit_event(repo_root, REPO_GRAPH_UPDATED, {"graph": "code", "status": "refreshed"})


def emit_db_graph_updated(repo_root) -> Optional[str]:
    return emit_event(repo_root, DB_GRAPH_UPDATED, {"graph": "database", "status": "refreshed"})


def emit_agent_started(
    repo_root, *, session_id=None, source=None, reason=None
) -> Optional[str]:
    payload = {k: v for k, v in (("source", source), ("reason", reason)) if v is not None}
    return emit_event(repo_root, AGENT_STARTED, payload, session_id=session_id)


def emit_agent_finished(
    repo_root, *, session_id=None, agent_id=None, reason=None, scope=None
) -> Optional[str]:
    payload = {k: v for k, v in (("reason", reason), ("scope", scope)) if v is not None}
    return emit_event(
        repo_root,
        AGENT_FINISHED,
        payload,
        session_id=session_id,
        agent_id=agent_id,
    )


def _reset_ids_cache_for_tests() -> None:
    """Clear the per-process identity cache (test-only hook)."""
    with _ids_lock:
        _ids_cache.clear()


__all__ = [
    "events_disabled",
    "seed_context",
    "emit_event",
    "emit_task_created",
    "emit_task_updated",
    "emit_task_completed",
    "emit_repo_graph_updated",
    "emit_db_graph_updated",
    "emit_agent_started",
    "emit_agent_finished",
]
