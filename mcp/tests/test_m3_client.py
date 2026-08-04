"""Tests for services.m3_client (card 93baf05b, Phase 1d stand-in M3 client).

Three layers:

* :class:`TestWalkNewEvents` — pure logic, no I/O: the ``--follow`` keyset walk
  (:mod:`services.m3_client.follow`) against a scripted in-memory
  ``fetch_page`` callable. Proves "no drops/dups across pages" directly.
* :class:`TestClientAndFollowStubServer` / :class:`TestSyncCursorClient` —
  the real :class:`services.api.app` Flask app (over ``FakeStore``, no
  database) served on a real loopback socket via werkzeug (the same pattern
  ``mcp/tests/test_drain.py`` uses for the drain), so
  :class:`services.m3_client.http.Client` is exercised against the actual
  ``/v1`` contract end to end — not a hand-rolled mirror of it that could
  encode the same bug the mirror and the client happen to share.
* :class:`TestRender` — plain-text board formatting and relative-age math.
* :class:`TestCli` — argparse defaults and one full ``status``/``--follow``
  run against the served app, capturing stdout.
"""

from __future__ import annotations

import io
import json
import logging
import socket
import sys
import threading
import unittest
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from werkzeug.serving import make_server  # noqa: E402

from services.api.app import create_app  # noqa: E402
from services.api.store import FakeStore  # noqa: E402
from services.m3_client import __main__ as m3_main  # noqa: E402
from services.m3_client.follow import walk_new_events  # noqa: E402
from services.m3_client.http import ApiError, Client  # noqa: E402
from services.m3_client import render as m3_render  # noqa: E402

logging.getLogger("werkzeug").setLevel(logging.ERROR)

_KEY = "m3-test-key"


# --------------------------------------------------------------------------- #
# TestWalkNewEvents: pure logic, scripted in-memory pages
# --------------------------------------------------------------------------- #
def _page(events, next_after):
    return {"events": events, "next_after": next_after}


def _ev(seq):
    return {"event_id": f"evt_{seq}", "event_type": "task.created", "ingest_seq": seq}


class _ScriptedPages:
    """``fetch_page`` stand-in that returns a pre-scripted sequence of pages,
    asserting each call's ``after_seq`` matches what the walk should request
    next (catches an off-by-one before it ever reaches a real server)."""

    def __init__(self, pages_by_after_seq):
        self._pages = pages_by_after_seq  # {after_seq_or_None: page_dict}
        self.calls = []

    def __call__(self, after_seq):
        self.calls.append(after_seq)
        return self._pages[after_seq]


class TestWalkNewEvents(unittest.TestCase):
    def test_first_call_establishes_baseline_no_new_events(self):
        # cursor=None: the very first page is the baseline, not "new" — and
        # only ONE page is fetched even though a real feed could be huge.
        fetch = _ScriptedPages({None: _page([_ev(5), _ev(4), _ev(3)], 3)})
        new_events, cursor = walk_new_events(fetch, None)
        self.assertEqual(new_events, [])
        self.assertEqual(cursor, 5)
        self.assertEqual(fetch.calls, [None])

    def test_first_call_empty_feed(self):
        fetch = _ScriptedPages({None: _page([], None)})
        new_events, cursor = walk_new_events(fetch, None)
        self.assertEqual(new_events, [])
        self.assertIsNone(cursor)

    def test_single_page_catch_up_no_drops_or_dups(self):
        # 3 new events since cursor=5, all fit on one page.
        fetch = _ScriptedPages({None: _page([_ev(8), _ev(7), _ev(6), _ev(5)], 5)})
        new_events, cursor = walk_new_events(fetch, 5)
        self.assertEqual([e["ingest_seq"] for e in new_events], [6, 7, 8])  # ascending
        self.assertEqual(cursor, 8)
        self.assertEqual(fetch.calls, [None])  # exactly one request

    def test_quiet_tick_reports_nothing_and_holds_cursor(self):
        fetch = _ScriptedPages({None: _page([_ev(8), _ev(7), _ev(6), _ev(5)], 5)})
        new_events, cursor = walk_new_events(fetch, 8)
        self.assertEqual(new_events, [])
        self.assertEqual(cursor, 8)

    def test_multi_page_burst_walks_backward_with_no_drop_or_dup(self):
        # A burst of 7 new events (seq 6..12) since cursor=5, but the page size
        # is 3 -> must walk 3 pages (12-10, 9-7, then 6 hits the boundary).
        fetch = _ScriptedPages({
            None: _page([_ev(12), _ev(11), _ev(10)], 10),
            10: _page([_ev(9), _ev(8), _ev(7)], 7),
            7: _page([_ev(6), _ev(5), _ev(4)], 4),  # seq 5 hits/crosses the cursor boundary
        })
        new_events, cursor = walk_new_events(fetch, 5)
        self.assertEqual([e["ingest_seq"] for e in new_events], [6, 7, 8, 9, 10, 11, 12])
        self.assertEqual(cursor, 12)
        self.assertEqual(fetch.calls, [None, 10, 7])  # exactly 3 pages, no re-fetch

    def test_burst_exactly_drains_history_without_hitting_cursor(self):
        # The feed runs out (next_after=None) before any event <= cursor is
        # seen (e.g. cursor refers to a since-purged position) -> must not loop.
        fetch = _ScriptedPages({
            None: _page([_ev(3), _ev(2), _ev(1)], 1),
            1: _page([], None),
        })
        new_events, cursor = walk_new_events(fetch, 0)
        self.assertEqual([e["ingest_seq"] for e in new_events], [1, 2, 3])
        self.assertEqual(cursor, 3)

    def test_cursor_never_regresses(self):
        # Defensive: if the server ever reports a lower top ingest_seq than the
        # cursor (e.g. a wiped dev database), the returned cursor must not go
        # backward - regressing would re-report an already-shown burst as new.
        fetch = _ScriptedPages({None: _page([_ev(2), _ev(1)], 1)})
        new_events, cursor = walk_new_events(fetch, 100)
        self.assertEqual(new_events, [])
        self.assertEqual(cursor, 100)

    def test_max_pages_bounds_a_pathological_walk(self):
        # next_after keeps advancing forever (all events newer than cursor);
        # max_pages must stop the walk rather than spin.
        def fetch(after_seq):
            n = (after_seq or 1000) - 1
            return _page([_ev(n)], n - 1)

        new_events, cursor = walk_new_events(fetch, 0, max_pages=5)
        self.assertEqual(len(new_events), 5)
        # Code review, card 93baf05b, Minor finding: the walk stopped due to
        # max_pages WITHOUT finding the boundary (cursor=0 is never reached),
        # so the cursor must advance only to the oldest event actually
        # collected (991) - NOT to the newest/top_seq (999) - or the unwalked
        # span below 991 (down to the true cursor=0) would be silently and
        # permanently marked as already-seen.
        self.assertEqual(cursor, 991)

    def test_max_pages_truncation_leaves_unwalked_span_for_next_poll(self):
        # A burst bigger than max_pages*page_size arrives between polls: the
        # first --follow tick can only walk part of it before max_pages fires.
        # The returned cursor must reflect only what was actually collected
        # this tick, not silently claim the whole burst (up to the newest
        # event) as seen - reproduces the code-review Major/Minor exploit
        # scenario directly (card 93baf05b comment 151).
        fetch = _ScriptedPages({
            None: _page([_ev(10), _ev(9)], 9),
            9: _page([_ev(8), _ev(7)], 7),
            # page(7) -> [_ev(6), _ev(5)] is intentionally NOT scripted: with
            # max_pages=2 the walk must stop after exactly 2 pages and never
            # request after_seq=7, proving the truncation actually happened.
        })
        new_events, cursor = walk_new_events(fetch, 0, max_pages=2)
        self.assertEqual([e["ingest_seq"] for e in new_events], [7, 8, 9, 10])
        self.assertEqual(fetch.calls, [None, 9])  # exactly 2 pages, bounded
        # Cursor lands on the oldest event actually collected (7), not the
        # newest (10) - events 1-6 remain > cursor and so stay eligible to be
        # reported on a future poll instead of being silently marked seen.
        self.assertEqual(cursor, 7)


# --------------------------------------------------------------------------- #
# Real Flask app (FakeStore) served on a loopback socket
# --------------------------------------------------------------------------- #
@contextmanager
def serve_api(*, store=None, api_key=_KEY):
    app = create_app(api_key=api_key, store=store if store is not None else FakeStore())
    app.testing = True
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _post_event(base_url, api_key, *, event_id, event_type="task.created", repository_id=None):
    import urllib.request

    body = json.dumps({"events": [{
        "event_id": event_id, "event_type": event_type,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "machine_id": "mach_test", "repository_id": repository_id,
        "payload": {},
    }]}).encode("utf-8")
    req = urllib.request.Request(f"{base_url}/v1/events/batch", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


class TestHttpClient(unittest.TestCase):
    def test_reads_against_real_app(self):
        with serve_api() as base_url:
            client = Client(base_url, _KEY)
            self.assertEqual(client.get_machines(), [])
            self.assertEqual(client.get_repositories(), [])
            self.assertEqual(client.get_sessions(active=True), [])
            body = client.get_recent_events(limit=10)
            self.assertEqual(body, {"events": [], "next_after": None})

    def test_wrong_key_raises_api_error_401(self):
        with serve_api() as base_url:
            client = Client(base_url, "wrong-key")
            with self.assertRaises(ApiError) as ctx:
                client.get_machines()
            self.assertEqual(ctx.exception.status, 401)
            self.assertEqual(ctx.exception.code, "unauthorized")

    def test_connection_refused_raises_api_error_no_status(self):
        port = _free_port()
        client = Client(f"http://127.0.0.1:{port}", _KEY, timeout=1)
        with self.assertRaises(ApiError) as ctx:
            client.get_machines()
        self.assertIsNone(ctx.exception.status)

    def test_sync_cursor_round_trip_against_real_app(self):
        with serve_api() as base_url:
            client = Client(base_url, _KEY)
            fresh = client.get_sync_cursor("m3-standin")
            self.assertIsNone(fresh["last_seq"])
            put = client.put_sync_cursor("m3-standin", last_seq=7, last_event_id="evt_7")
            self.assertEqual(put["last_seq"], 7)
            self.assertEqual(client.get_sync_cursor("m3-standin")["last_seq"], 7)


class TestFollowAgainstRealServer(unittest.TestCase):
    """The end-to-end proof: the real Client + the real /v1 pagination
    contract (not a hand-rolled mirror) + walk_new_events, across a burst
    bigger than one page."""

    def test_burst_larger_than_page_size_no_drops_or_dups(self):
        with serve_api() as base_url:
            client = Client(base_url, _KEY)
            for i in range(3):
                _post_event(base_url, _KEY, event_id=f"evt_base_{i}")
            baseline = client.get_recent_events(limit=50)
            cursor = baseline["events"][0]["ingest_seq"]

            added_ids = [f"evt_burst_{i}" for i in range(25)]
            for event_id in added_ids:
                _post_event(base_url, _KEY, event_id=event_id)

            def fetch_page(after_seq):
                return client.get_recent_events(limit=10, after_seq=after_seq)

            new_events, cursor = walk_new_events(fetch_page, cursor)
            self.assertEqual(len(new_events), 25)
            self.assertEqual([e["event_id"] for e in new_events], added_ids)  # ascending, exact

            quiet_events, cursor2 = walk_new_events(fetch_page, cursor)
            self.assertEqual(quiet_events, [])
            self.assertEqual(cursor2, cursor)
            assert cursor2 is not None  # narrows for pyright; asserted non-None above

            _post_event(base_url, _KEY, event_id="evt_tail")
            tail_events, cursor3 = walk_new_events(fetch_page, cursor2)
            self.assertEqual([e["event_id"] for e in tail_events], ["evt_tail"])
            self.assertEqual(cursor3, cursor2 + 1)


# --------------------------------------------------------------------------- #
# render.py
# --------------------------------------------------------------------------- #
class TestRender(unittest.TestCase):
    def test_format_age_buckets(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        cases = [
            (timedelta(seconds=2), "just now"),
            (timedelta(seconds=45), "45s ago"),
            (timedelta(minutes=5), "5m ago"),
            (timedelta(hours=3), "3h ago"),
            (timedelta(days=2), "2d ago"),
            (timedelta(days=60), "2mo ago"),
            (timedelta(days=800), "2y ago"),
        ]
        for delta, expected in cases:
            self.assertEqual(m3_render.format_age(now - delta, now=now), expected)

    def test_format_age_rejects_naive_datetime(self):
        with self.assertRaises(ValueError):
            m3_render.format_age(datetime(2026, 1, 1))

    def test_format_age_iso_unknown_for_bad_value(self):
        self.assertEqual(m3_render.format_age_iso(None), "unknown")
        self.assertEqual(m3_render.format_age_iso("not-a-timestamp"), "unknown")

    def test_parse_iso_defensively_treats_naive_as_utc(self):
        dt = m3_render.parse_iso("2026-01-01T00:00:00")
        self.assertIsNotNone(dt)
        assert dt is not None  # narrows for pyright; asserted non-None above
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_render_board_smoke(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        data = {
            "api_url": "http://127.0.0.1:8765",
            "machines": [{"machine_id": "mach_1", "name": "pop-os", "os": "Linux",
                          "last_seen_at": (now - timedelta(seconds=5)).isoformat()}],
            "repositories": [{"repository_id": "repo_1", "machine_id": "mach_1",
                              "canonical_remote": "github.com/x/y", "repo_root": "/home/x/y"}],
            "sessions": [{"session_id": "sess_1", "agent_type": "implementer", "status": "running",
                         "last_heartbeat_at": (now - timedelta(seconds=8)).isoformat()}],
            "events": [{"event_type": "agent.finished", "repository_id": "repo_1",
                       "occurred_at": (now - timedelta(minutes=2)).isoformat()}],
        }
        board = m3_render.render_board(data, now=now)
        self.assertIn("MACHINES (1)", board)
        self.assertIn("mach_1", board)
        self.assertIn("REPOSITORIES (1)", board)
        self.assertIn("github.com/x/y", board)
        self.assertIn("ACTIVE SESSIONS (1)", board)
        self.assertIn("heartbeat 8s ago", board)
        self.assertIn("RECENT EVENTS (1)", board)
        self.assertIn("2m ago", board)

    def test_render_board_empty_sections_say_none(self):
        board = m3_render.render_board({"api_url": "x"}, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertIn("(none)", board)

    def test_render_event_line(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        line = m3_render.render_event_line(
            {"event_type": "agent.started", "repository_id": "repo_1", "ingest_seq": 42,
             "occurred_at": (now - timedelta(seconds=10)).isoformat()},
            now=now,
        )
        self.assertIn("agent.started", line)
        self.assertIn("repo=repo_1", line)
        self.assertIn("seq=42", line)
        self.assertIn("10s ago", line)

    def test_sanitize_strips_ansi_and_control_bytes(self):
        # Code review, card 93baf05b, Major finding #4: a crafted machine
        # name containing a screen-clear + fake alert must never reach the
        # rendered output with its escape sequence intact.
        hostile = "\x1b[2J\x1b[H\x1b[31mFAKE ALERT\x1b[0m"
        cleaned = m3_render._sanitize(hostile)
        self.assertNotIn("\x1b", cleaned)
        self.assertIn("FAKE ALERT", cleaned)
        # Bare control characters (newline, tab, other C0/C1) are stripped too.
        self.assertEqual(m3_render._sanitize("a\nb\tc\x00d\x7f"), "abcd")

    def test_render_board_sanitizes_machine_name(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        hostile_name = "\x1b[2J\x1b[H\x1b[31mFAKE ALERT\x1b[0m"
        data = {
            "api_url": "http://127.0.0.1:8765",
            "machines": [{"machine_id": "mach_1", "name": hostile_name, "os": "Linux",
                          "last_seen_at": now.isoformat()}],
        }
        board = m3_render.render_board(data, now=now)
        self.assertNotIn("\x1b", board)
        self.assertIn("FAKE ALERT", board)
        # No control character survives WITHIN the single rendered machine
        # line (the board's own "\n" line separators between sections/rows
        # are legitimate and intentionally excluded from this check).
        machine_line = next(line for line in board.splitlines() if "mach_1" in line)
        for byte in list(range(0x00, 0x20)) + list(range(0x7f, 0xa0)):
            self.assertNotIn(chr(byte), machine_line)

    def test_render_event_line_sanitizes_event_type_and_repo(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        line = m3_render.render_event_line(
            {"event_type": "agent.started\x1b[31m", "repository_id": "repo_1\x1b[0m",
             "ingest_seq": 42, "occurred_at": now.isoformat()},
            now=now,
        )
        self.assertNotIn("\x1b", line)
        self.assertIn("agent.started", line)
        self.assertIn("repo=repo_1", line)


# --------------------------------------------------------------------------- #
# __main__.py CLI
# --------------------------------------------------------------------------- #
class TestCli(unittest.TestCase):
    def test_parser_defaults_from_env(self):
        with mock.patch.dict(
            "os.environ",
            {"AGENT_OS_API_URL": "http://example:9", "AGENT_OS_API_KEY": "envkey"},
            clear=False,
        ):
            args = m3_main.build_parser().parse_args(["status"])
            self.assertEqual(args.api_url, "http://example:9")
            self.assertEqual(args.api_key, "envkey")
            self.assertEqual(args.client_id, "m3-standin")
            self.assertFalse(args.json)
            self.assertFalse(args.follow)

    def test_parser_requires_a_subcommand(self):
        with self.assertRaises(SystemExit):
            m3_main.build_parser().parse_args([])

    def test_status_json_against_real_server(self):
        with serve_api() as base_url:
            out = io.StringIO()
            with redirect_stdout(out):
                code = m3_main.main(["--api-url", base_url, "--api-key", _KEY, "status", "--json"])
            self.assertEqual(code, 0)
            body = json.loads(out.getvalue())
            self.assertEqual(body["machines"], [])
            self.assertEqual(body["api_url"], base_url)

    def test_status_plain_board_against_real_server(self):
        with serve_api() as base_url:
            _post_event(base_url, _KEY, event_id="evt_cli_1")
            out = io.StringIO()
            with redirect_stdout(out):
                code = m3_main.main(["--api-url", base_url, "--api-key", _KEY, "status"])
            self.assertEqual(code, 0)
            self.assertIn("M3 status board", out.getvalue())
            self.assertIn("RECENT EVENTS (1)", out.getvalue())

    def test_status_reports_failure_on_unreachable_server(self):
        port = _free_port()
        out = io.StringIO()
        with redirect_stdout(out):
            code = m3_main.main(["--api-url", f"http://127.0.0.1:{port}", "--api-key", _KEY, "status"])
        self.assertEqual(code, 1)

    def test_follow_prints_new_event_and_persists_cursor(self):
        # Deterministic termination: the second time.sleep() call (the poll
        # cadence, called once per loop iteration before fetching) raises
        # KeyboardInterrupt, which _run_follow treats as a clean exit.
        with serve_api() as base_url:
            _post_event(base_url, _KEY, event_id="evt_before_follow")

            call_count = {"n": 0}

            def fake_sleep(_seconds):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    _post_event(base_url, _KEY, event_id="evt_during_follow")
                    return
                raise KeyboardInterrupt

            out = io.StringIO()
            with mock.patch("services.m3_client.__main__.time.sleep", side_effect=fake_sleep):
                with redirect_stdout(out):
                    code = m3_main.main([
                        "--api-url", base_url, "--api-key", _KEY, "status",
                        "--follow", "--client-id", "test-follow-client", "--json",
                    ])
            self.assertEqual(code, 0)
            # The initial snapshot is pretty-printed (indent=2) across many
            # lines, none of which are valid standalone JSON; the follow-mode
            # event is a single compact JSON line (json.dumps(..., sort_keys=True)
            # with no indent) - so scanning for lines that parse AND carry the
            # expected event_id unambiguously isolates it from the snapshot.
            new_event_objs = []
            for line in out.getvalue().splitlines():
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("event_id") == "evt_during_follow":
                    new_event_objs.append(obj)
            self.assertEqual(len(new_event_objs), 1)

            # The cursor was persisted server-side under this run's client id.
            client = Client(base_url, _KEY)
            cursor = client.get_sync_cursor("test-follow-client")
            self.assertIsNotNone(cursor["last_seq"])
            self.assertEqual(cursor["last_event_id"], "evt_during_follow")


if __name__ == "__main__":
    unittest.main()
