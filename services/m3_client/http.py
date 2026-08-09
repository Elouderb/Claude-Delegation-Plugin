"""stdlib-only HTTP client for the Agent OS ``/v1`` read (+ sync-cursor) API.

Mirrors ``outbox/drain.py``'s ``post_json`` transport pattern (urllib only,
Windows-safe) but is deliberately self-contained here rather than imported
from ``outbox`` — this package must not depend on anything outside the
standard library, and ``outbox`` pulls in the bundled ``contracts`` (pydantic)
package for identity handling. See the package docstring
(:mod:`services.m3_client`) for the full "why".
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional


class ApiError(Exception):
    """A transport failure (connection/timeout/DNS) or a non-2xx API response.

    ``status``/``code`` are populated from the API's uniform error envelope
    (``{"error": {"code", "message"}}``) when the response parses as one;
    ``status`` is ``None`` for a pure transport failure (no response at all).
    """

    def __init__(self, message: str, *, status: Optional[int] = None, code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.code = code


def _request(
    method: str, url: str, api_key: Optional[str], *,
    payload: Optional[Dict[str, Any]] = None, timeout: float = 10.0,
) -> Dict[str, Any]:
    """Issue one HTTP request and return the parsed JSON body.

    Raises :class:`ApiError` on a connection-level failure OR a non-2xx HTTP
    status (unlike ``outbox.drain.post_json``, which returns non-200 to its
    caller for cursor-discipline reasons this read-only client doesn't share —
    here a failure is always exceptional).
    """
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or response.getcode())
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:  # a real response with an error status
        status = int(exc.code)
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ApiError(f"{method} {url} failed: {exc}") from exc

    try:
        decoded: Any = json.loads(raw) if raw else {}
    except ValueError:
        decoded = {}
    parsed: Dict[str, Any] = decoded if isinstance(decoded, dict) else {}

    if status >= 300:
        error_candidate = parsed.get("error")
        error_obj: Dict[str, Any] = error_candidate if isinstance(error_candidate, dict) else {}
        raise ApiError(
            error_obj.get("message") or f"HTTP {status}",
            status=status, code=error_obj.get("code"),
        )
    return parsed


class Client:
    """Thin client for the subset of the Agent OS ``/v1`` API this stand-in
    reads: machines, repositories, sessions, recent events, and sync cursors."""

    def __init__(self, base_url: str, api_key: Optional[str] = None, *, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        url = f"{self.base_url}{path}"
        if params:
            query = {k: v for k, v in params.items() if v is not None}
            if query:
                url = f"{url}?{urllib.parse.urlencode(query)}"
        return url

    def get_machines(self) -> List[Dict[str, Any]]:
        body = _request("GET", self._url("/v1/machines"), self.api_key, timeout=self.timeout)
        return body.get("machines") or []

    def get_repositories(self, machine_id: Optional[str] = None) -> List[Dict[str, Any]]:
        url = self._url("/v1/repositories", {"machine_id": machine_id})
        body = _request("GET", url, self.api_key, timeout=self.timeout)
        return body.get("repositories") or []

    def get_sessions(self, active: bool = False) -> List[Dict[str, Any]]:
        params = {"active": "1"} if active else None
        url = self._url("/v1/sessions", params)
        body = _request("GET", url, self.api_key, timeout=self.timeout)
        return body.get("sessions") or []

    def get_recent_events(
        self, *, limit: int = 50, repository_id: Optional[str] = None,
        event_type: Optional[str] = None, after_seq: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Returns the raw ``{"events": [...], "next_after": int|None}`` body
        (not just the list) — callers that keyset-walk need ``next_after``."""
        params = {
            "limit": limit, "repository_id": repository_id,
            "event_type": event_type, "after_seq": after_seq,
        }
        url = self._url("/v1/events/recent", params)
        return _request("GET", url, self.api_key, timeout=self.timeout)

    def get_tasks(
        self, *, repository_id: Optional[str] = None, since: Optional[int] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Returns the raw ``{"tasks": [...], "next_since": int|None}`` body — the
        DOWN-projection poller keyset-walks by ``next_since``."""
        params = {"repository_id": repository_id, "since": since, "limit": limit}
        url = self._url("/v1/tasks", params)
        return _request("GET", url, self.api_key, timeout=self.timeout)

    def get_sync_cursor(self, client_id: str) -> Dict[str, Any]:
        url = self._url(f"/v1/sync/{urllib.parse.quote(client_id, safe='')}")
        return _request("GET", url, self.api_key, timeout=self.timeout)

    def put_sync_cursor(
        self, client_id: str, *, last_seq: Optional[int], last_event_id: Optional[str],
    ) -> Dict[str, Any]:
        url = self._url(f"/v1/sync/{urllib.parse.quote(client_id, safe='')}")
        payload = {"last_seq": last_seq, "last_event_id": last_event_id}
        return _request("PUT", url, self.api_key, payload=payload, timeout=self.timeout)

    # -- per-machine credential management (admin key required) ------------- #
    def issue_credential(
        self, machine_id: str, *, label: Optional[str] = None
    ) -> Dict[str, Any]:
        """Issue a per-machine key (admin-only). Returns ``{key_id, secret, key}`` —
        the ``secret``/``key`` are shown ONCE and never retrievable again."""
        url = self._url(
            f"/v1/machines/{urllib.parse.quote(machine_id, safe='')}/credentials"
        )
        payload = {"label": label} if label is not None else {}
        return _request("POST", url, self.api_key, payload=payload, timeout=self.timeout)

    def revoke_credential(self, machine_id: str, key_id: str) -> Dict[str, Any]:
        """Revoke a per-machine key by ``key_id`` (admin-only; idempotent)."""
        url = self._url(
            f"/v1/machines/{urllib.parse.quote(machine_id, safe='')}"
            f"/credentials/{urllib.parse.quote(key_id, safe='')}/revoke"
        )
        return _request("POST", url, self.api_key, payload={}, timeout=self.timeout)

    def list_credentials(self, machine_id: str) -> List[Dict[str, Any]]:
        """List a machine's credentials (admin-only) — ids + status, never secrets."""
        url = self._url(
            f"/v1/machines/{urllib.parse.quote(machine_id, safe='')}/credentials"
        )
        body = _request("GET", url, self.api_key, timeout=self.timeout)
        return body.get("credentials") or []


__all__ = ["ApiError", "Client"]
