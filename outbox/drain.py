"""Outbox drain worker: ship local events to the Agent OS API (Phase 1c, §17).

``python -m outbox.drain [--once]``. The network half of the local-first sync
plane: it reads this repo's durable outbox (written offline by :mod:`outbox.emit`)
and POSTs the events to the central Agent OS API, advancing a byte-offset cursor
only for events the server has durably accepted — so a network outage costs
nothing but a retry (proposal §17's network-failure criterion), never a lost or
double-recorded event.

Dependencies
------------
The *transport* is stdlib-only: HTTP uses :mod:`urllib` (no third-party
``requests``), so shipping events adds no dependency and is Windows-safe (no
POSIX-only calls). Loading machine/repo identity DOES use the bundled top-level
``contracts`` package (pydantic) — the same models the rest of the plugin uses.
That import is required, not optional: if ``contracts`` cannot be imported the
worker fails fast with a clear message rather than silently retrying forever (a
bare-Python checkout without the plugin's own packages is a misconfiguration, not
a transient outage).

Cursor discipline (the correctness core)
----------------------------------------
Per batch:

* A malformed outbox line (``PendingEvent.envelope is None``) can never be sent —
  it is dead-lettered locally and counts as *resolved*.
* Valid events are POSTed to ``/v1/events/batch``; the server returns a per-event
  ``accepted`` | ``duplicate`` | ``rejected`` verdict. ``accepted`` and
  ``duplicate`` are resolved; ``rejected`` is dead-lettered locally (preserved for
  diagnosis) and then resolved.
* Only once **every** event in the batch is resolved AND the request returned HTTP
  200 does the cursor advance to the batch's last ``end_offset``.
* A transport failure — connection refused, timeout, any non-200 (5xx or 4xx),
  or a malformed/underlength response — advances the cursor by **nothing** and
  triggers exponential backoff with jitter. At-least-once redelivery is safe
  because central ingest is idempotent on ``event_id``
  (``insert_event_if_absent`` / the ``ops.events`` PK).

Config (all from the environment):

* ``AGENT_OS_API_URL`` (default ``http://127.0.0.1:8765``)
* ``AGENT_OS_API_KEY`` (bearer key; sent as ``Authorization: Bearer ...``)
* ``AGENT_OS_API_CA_BUNDLE`` path to a PEM of the hub's internal root CA — when
  ``AGENT_OS_API_URL`` is ``https``, the server cert is verified against it;
  unset falls back to the system trust store. Ignored for ``http`` (no TLS).
* ``AGENT_OS_SYNC_INTERVAL`` seconds between idle polls (default 30)
* ``AGENT_OS_SYNC_BATCH`` max events per batch (default 500)
* ``AGENT_OS_SYNC_TIMEOUT`` per-request timeout seconds (default 10)
* ``AGENT_OS_SYNC_DISABLED=1`` kill-switch — the worker no-ops.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# outbox.drain is a standalone worker, NOT the producer hot path, so importing
# contracts (pydantic) here is fine — the hot-path import discipline (card
# ce79a987 finding 2) applies to emit/store/paths, which this module does not
# regress: ``import outbox`` still never imports drain (see outbox/__init__.py).
from . import _fsutil, paths, store

_DISABLED_ENV = "AGENT_OS_SYNC_DISABLED"
_URL_ENV = "AGENT_OS_API_URL"
_KEY_ENV = "AGENT_OS_API_KEY"
_CA_BUNDLE_ENV = "AGENT_OS_API_CA_BUNDLE"
_INTERVAL_ENV = "AGENT_OS_SYNC_INTERVAL"
_BATCH_ENV = "AGENT_OS_SYNC_BATCH"
_TIMEOUT_ENV = "AGENT_OS_SYNC_TIMEOUT"

_DEFAULT_URL = "http://127.0.0.1:8765"
_DEFAULT_INTERVAL = 30.0
_DEFAULT_BATCH = 500
_DEFAULT_TIMEOUT = 10.0


class TransportError(Exception):
    """A retryable failure (connection/timeout/non-200/protocol) — never advance
    the cursor when this is raised; back off and retry the whole batch."""


class ConfigurationError(Exception):
    """A non-recoverable startup failure (e.g. the ``contracts`` package cannot be
    imported). Retrying cannot fix it, so the worker fails fast instead of looping
    forever."""


@dataclass
class DrainConfig:
    """Resolved drain configuration (see module docstring for env sources)."""

    api_url: str
    api_key: Optional[str]
    repo_root: Path
    ca_bundle: Optional[str] = None
    interval: float = _DEFAULT_INTERVAL
    batch_size: int = _DEFAULT_BATCH
    timeout: float = _DEFAULT_TIMEOUT
    backoff_initial: float = 1.0
    backoff_max: float = 60.0
    disabled: bool = False


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def config_from_env(repo_root: Optional[Path] = None) -> DrainConfig:
    """Build a :class:`DrainConfig` from the process environment."""
    return DrainConfig(
        api_url=os.environ.get(_URL_ENV) or _DEFAULT_URL,
        api_key=os.environ.get(_KEY_ENV),
        ca_bundle=os.environ.get(_CA_BUNDLE_ENV),
        repo_root=Path(repo_root) if repo_root is not None else paths.find_repo_root(),
        interval=_env_float(_INTERVAL_ENV, _DEFAULT_INTERVAL),
        batch_size=_env_int(_BATCH_ENV, _DEFAULT_BATCH),
        timeout=_env_float(_TIMEOUT_ENV, _DEFAULT_TIMEOUT),
        disabled=_fsutil.env_flag(_DISABLED_ENV),
    )


def _log(message: str) -> None:
    """Timestamped stderr line (redirected to a log file when spawned; see
    ``mcp/sync_server.py``). Best-effort — never raises."""
    try:
        stamp = datetime.now(timezone.utc).isoformat()
        print(f"{stamp} [drain] {message}", file=sys.stderr, flush=True)
    except Exception:  # pragma: no cover - stderr closed / encoding edge
        pass


def _build_ssl_context(
    api_url: str, ca_bundle: Optional[str]
) -> Optional[ssl.SSLContext]:
    """Build a certificate-verifying TLS context for an ``https`` API URL.

    Returns ``None`` for a plain ``http`` URL, so the HTTP path is byte-for-byte
    unchanged (no ``context`` is ever passed to ``urlopen`` — the current
    trusted-LAN default until the TLS cutover). For ``https``:

    * ``ca_bundle`` set   → verify the hub's cert against that PEM CA bundle
      (``ssl.create_default_context(cafile=ca_bundle)``) — the hub behind an
      internal-CA reverse proxy (Caddy ``tls internal``);
    * ``ca_bundle`` unset → the system trust store
      (``ssl.create_default_context()``), for a publicly-trusted cert.

    There is deliberately NO skip-verify / insecure mode. Built ONCE at
    :class:`Drainer` construction and reused for every request (a context per
    request is expensive).
    """
    if urllib.parse.urlsplit(api_url).scheme != "https":
        return None
    if ca_bundle:
        return ssl.create_default_context(cafile=ca_bundle)
    return ssl.create_default_context()


def post_json(
    url: str, api_key: Optional[str], payload: Dict[str, Any], timeout: float,
    *, ssl_context: Optional[ssl.SSLContext] = None,
) -> Tuple[int, Dict[str, Any]]:
    """POST ``payload`` as JSON; return ``(status, parsed_body)``.

    Raises :class:`TransportError` on a connection-level failure (refused, DNS,
    timeout). An HTTP error *response* (4xx/5xx) is returned as ``(status, body)``
    — NOT raised — so the caller applies uniform "non-200 ⇒ do not advance" logic.
    urllib only; Windows-safe.

    ``ssl_context`` (built once by :class:`Drainer`) is threaded to ``urlopen``
    for an ``https`` URL; for ``http`` it is ``None`` and no ``context`` kwarg is
    passed at all, keeping the plain-HTTP path unchanged.
    """
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    open_kwargs: Dict[str, Any] = {"timeout": timeout}
    if ssl_context is not None:
        open_kwargs["context"] = ssl_context
    try:
        with urllib.request.urlopen(request, **open_kwargs) as response:
            status = int(getattr(response, "status", 0) or response.getcode())
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:  # 4xx/5xx: a real response with a body
        status = int(exc.code)
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransportError(f"POST {url} failed: {exc}") from exc
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return status, parsed


class Drainer:
    """Registers identity, then drains the outbox to the API with safe cursoring."""

    def __init__(self, config: DrainConfig):
        self.config = config
        self.repo_root = Path(config.repo_root)
        # Resolve the TLS trust anchor ONCE (an https api_url verifies the hub's
        # cert against config.ca_bundle when set, else the system trust store; an
        # http url builds no context). Reused for every post_json below.
        self._ssl_context = _build_ssl_context(config.api_url, config.ca_bundle)
        self._registered = False
        self._local_repo_id: Optional[str] = None
        self._canonical_repo_id: Optional[str] = None
        # A session identity for THIS drain process (one session per run). Minted
        # lazily in :meth:`_load_identity` so the ids embed the run's timestamp.
        self._machine: Optional[Dict[str, Any]] = None
        self._repo: Optional[Dict[str, Any]] = None
        self._session_id: Optional[str] = None
        self._agent_id: Optional[str] = None
        self._started_at: Optional[str] = None

    # -- identity ---------------------------------------------------------- #
    def _load_identity(self) -> bool:
        """Ensure + load machine/repo identity; mint this run's session id.

        Returns ``False`` (and logs) on a *transient* failure (e.g. an identity
        file mid-write) — the worker retries later. Raises :class:`ConfigurationError`
        on a *fatal* one (the ``contracts`` package cannot be imported at all), so
        the worker fails fast with a clear message instead of retrying forever.
        """
        if self._machine is not None and self._repo is not None:
            return True
        try:
            from . import identity as outbox_identity
            from contracts import AGENT, SESSION, new_id, utc_now
            from contracts.identity import (
                MachineIdentity,
                RepositoryIdentity,
                machine_identity_path,
                repository_identity_path,
            )
        except ImportError as exc:
            raise ConfigurationError(
                "the bundled 'contracts' package is required by outbox.drain but "
                f"could not be imported ({exc}); ensure the plugin root is on "
                "PYTHONPATH (the server companion sets this — see mcp/sync_server.py)"
            ) from exc
        try:
            # Ensure identity files exist (idempotent, self-healing), then load them.
            outbox_identity.bootstrap_identity(self.repo_root)
            machine = MachineIdentity.model_validate_json(
                machine_identity_path().read_text(encoding="utf-8")
            )
            repo = RepositoryIdentity.model_validate_json(
                repository_identity_path(self.repo_root).read_text(encoding="utf-8")
            )
        except Exception as exc:
            _log(f"identity unavailable, will retry: {exc}")
            return False

        self._machine = machine.model_dump(mode="json")
        self._repo = repo.model_dump(mode="json")
        self._session_id = new_id(SESSION)
        self._agent_id = new_id(AGENT)
        self._started_at = utc_now().isoformat()
        return True

    def _url(self, path: str) -> str:
        return f"{self.config.api_url.rstrip('/')}{path}"

    # -- registration ------------------------------------------------------ #
    def register(self, attempts: int = 1) -> bool:
        """Register machine → repository → session. Returns True iff all succeeded.

        Retries up to ``attempts`` times on a transport failure with a short
        backoff (used by ``--once`` so a just-started server is not a spurious
        failure); the long-running loop drives its own backoff instead.
        """
        for attempt in range(max(1, attempts)):
            try:
                if self._register_once():
                    return True
            except TransportError as exc:
                _log(f"registration transport error: {exc}")
            if attempt < attempts - 1:
                time.sleep(min(self.config.backoff_initial * (attempt + 1), self.config.backoff_max))
        return False

    def _register_once(self) -> bool:
        if not self._load_identity():
            return False
        assert self._machine is not None and self._repo is not None

        status, _ = post_json(
            self._url("/v1/machines/register"), self.config.api_key, self._machine,
            self.config.timeout, ssl_context=self._ssl_context,
        )
        if status != 200:
            _log(f"machine register returned HTTP {status}")
            return False

        repo_payload = dict(self._repo)
        repo_payload["repo_root"] = str(self.repo_root)
        # Register the repo under the CURRENT machine identity, not the one stamped
        # into the repo identity file at mint time. If ~/.agent-os was wiped and the
        # machine id was re-minted, the stored machine_id is stale — sending it
        # would fail the machine FK forever (permanent register loop). The machine
        # we just registered above is the current one.
        repo_payload["machine_id"] = self._machine["machine_id"]
        status, body = post_json(
            self._url("/v1/repositories/register"), self.config.api_key, repo_payload,
            self.config.timeout, ssl_context=self._ssl_context,
        )
        if status != 200:
            _log(f"repository register returned HTTP {status}")
            return False
        self._local_repo_id = self._repo.get("repository_id")
        self._canonical_repo_id = body.get("repository_id") or self._local_repo_id

        status, _ = post_json(
            self._url("/v1/sessions/register"), self.config.api_key,
            self._session_payload(), self.config.timeout, ssl_context=self._ssl_context,
        )
        if status != 200:
            _log(f"session register returned HTTP {status}")
            return False
        self._registered = True
        return True

    def _session_payload(self) -> Dict[str, Any]:
        assert self._machine is not None
        return {
            "agent_id": self._agent_id,
            "session_id": self._session_id,
            "agent_type": "sync-drain",
            "machine_id": self._machine["machine_id"],
            "repository_id": self._canonical_repo_id,
            "capabilities": [],
            "status": "running",
            "runtime": "outbox.drain",
            "started_at": self._started_at,
        }

    def heartbeat(self) -> None:
        """Best-effort session heartbeat; a failure never blocks draining."""
        if not self._session_id:
            return
        try:
            post_json(
                self._url(f"/v1/sessions/{self._session_id}/heartbeat"),
                self.config.api_key, {"status": "running"}, self.config.timeout,
                ssl_context=self._ssl_context,
            )
        except TransportError as exc:
            _log(f"heartbeat failed (ignored): {exc}")

    # -- draining ---------------------------------------------------------- #
    def drain_batch(self) -> str:
        """Process one batch. Returns ``"empty"`` | ``"progress"``.

        Raises :class:`TransportError` on any transport/protocol failure WITHOUT
        moving the cursor OR writing any dead-letter, so a retried batch after
        backoff produces exactly one dead-letter record per bad line (never one per
        retry) — dead-lettering and the cursor advance are deferred until the whole
        batch is durably resolved (HTTP 200 with an index-aligned result set).
        """
        # Bound the disk read as well as the event count: a huge backlog (first sync
        # after a long offline stretch) must not slurp the whole tail into memory.
        max_bytes = self.config.batch_size * store.MAX_LINE_BYTES
        pending = store.read_pending(
            self.repo_root, max_events=self.config.batch_size, max_bytes=max_bytes
        )
        if not pending:
            return "empty"

        # Collect what WOULD be dead-lettered, but do not write any of it until the
        # batch is resolved below.
        to_dead_letter: List[Tuple[str, str]] = []  # (raw, reason)
        to_send = []
        for event in pending:
            if event.envelope is None:
                to_dead_letter.append(
                    (event.raw, event.error or "unparseable outbox line")
                )
            else:
                to_send.append(event)

        if to_send:
            payload = {"events": [self._outgoing_event(event.raw) for event in to_send]}
            status, body = post_json(
                self._url("/v1/events/batch"), self.config.api_key, payload,
                self.config.timeout, ssl_context=self._ssl_context,
            )
            if status != 200:
                raise TransportError(f"events/batch returned HTTP {status}")
            results = body.get("results")
            if not isinstance(results, list) or len(results) != len(to_send):
                raise TransportError("events/batch response missing/misaligned results")
            for event, result in zip(to_send, results):
                if not isinstance(result, dict):
                    raise TransportError("events/batch result item malformed")
                outcome = result.get("outcome")
                if outcome == "rejected":
                    to_dead_letter.append((event.raw, result.get("reason") or "rejected"))
                elif outcome not in ("accepted", "duplicate"):
                    raise TransportError(f"unknown per-event outcome: {outcome!r}")

        # The batch is durably resolved (no transport error was raised): NOW write
        # the dead-letters exactly once, then advance the cursor past the batch.
        for raw, reason in to_dead_letter:
            store.dead_letter(self.repo_root, raw, reason)
        store.write_cursor(self.repo_root, pending[-1].end_offset)
        return "progress"

    def _outgoing_event(self, raw: str) -> Dict[str, Any]:
        """Parse an outbox line, rewriting a remapped repository_id for transport.

        When registration reported ``remapped=true`` (the central store keeps the
        first-seen ``repository_id`` for this checkout, e.g. after a local re-mint),
        events still carry this run's LOCAL repository_id. Rewrite it to the
        canonical id so the events are queryable under the same repository the
        registry knows — otherwise they would be orphaned under an id the read
        surface never returns. The envelope is re-serialized (it still validates);
        ``event_id`` is untouched, so idempotent redelivery is unaffected.
        """
        obj = json.loads(raw)
        if (
            self._canonical_repo_id is not None
            and self._local_repo_id is not None
            and self._canonical_repo_id != self._local_repo_id
            and isinstance(obj, dict)
            and obj.get("repository_id") == self._local_repo_id
        ):
            obj["repository_id"] = self._canonical_repo_id
        return obj

    def drain_all(self) -> int:
        """Drain until the outbox is empty. Returns the number of batches applied.

        Propagates :class:`TransportError` (the caller decides retry vs. exit).
        """
        batches = 0
        while True:
            if self.drain_batch() == "empty":
                return batches
            batches += 1

    # -- entry points ------------------------------------------------------ #
    def run_once(self) -> int:
        """One full drain pass for tests / manual runs. Exit code: 0 ok, 1 could
        not register, 2 transport failure mid-drain, 3 fatal configuration error."""
        if self.config.disabled:
            _log("sync disabled (AGENT_OS_SYNC_DISABLED) — nothing to do")
            return 0
        try:
            registered = self.register(attempts=3)
        except ConfigurationError as exc:
            _log(f"fatal configuration error (not retrying): {exc}")
            return 3
        if not registered:
            _log("could not register with the API; aborting --once run")
            return 1
        try:
            batches = self.drain_all()
        except TransportError as exc:
            _log(f"transport failure during drain (cursor NOT advanced): {exc}")
            return 2
        self.heartbeat()
        _log(f"--once complete: {batches} batch(es) drained")
        return 0

    def run_forever(self) -> None:
        """Long-running loop: register, drain, heartbeat, sleep — with backoff."""
        if self.config.disabled:
            _log("sync disabled (AGENT_OS_SYNC_DISABLED) — worker idle")
            return
        backoff = self.config.backoff_initial
        while True:
            try:
                if not self._registered and not self.register(attempts=1):
                    self._sleep_backoff(backoff)
                    backoff = min(backoff * 2, self.config.backoff_max)
                    continue
                self.drain_all()
                self.heartbeat()
                backoff = self.config.backoff_initial  # reset after a clean cycle
                self._sleep_idle()
            except ConfigurationError as exc:
                # Retrying cannot fix a missing package — stop instead of spinning
                # forever. The companion surfaces the child's log tail on exit.
                _log(f"fatal configuration error; worker exiting: {exc}")
                return
            except TransportError as exc:
                _log(f"transport failure (cursor held): {exc}")
                self._registered = False  # re-register after an outage
                self._sleep_backoff(backoff)
                backoff = min(backoff * 2, self.config.backoff_max)
            except KeyboardInterrupt:  # pragma: no cover - manual stop
                _log("interrupted; exiting")
                return

    def _sleep_backoff(self, backoff: float) -> None:
        # Jittered 50–100% of the current backoff, so a fleet of workers does not
        # retry in lockstep after a shared outage.
        time.sleep(backoff * (0.5 + random.random() * 0.5))

    def _sleep_idle(self) -> None:
        interval = self.config.interval
        time.sleep(interval + random.uniform(0, min(interval * 0.1, 5.0)))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drain the repo-local outbox to the Agent OS API (proposal §17)."
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Drain everything currently pending, then exit (for tests / manual runs).",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=None,
        help="Repository root whose outbox to drain (default: walk up from cwd to .git).",
    )
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover - dotenv is a core dep
        pass

    config = config_from_env(repo_root=args.repo_root)
    drainer = Drainer(config)
    if args.once:
        return drainer.run_once()
    drainer.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
