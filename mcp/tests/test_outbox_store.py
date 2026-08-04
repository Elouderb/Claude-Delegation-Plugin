"""Tests for the durable outbox store (outbox.store) — Phase 1b.

Covers the append/read/cursor round-trip, concurrent multi-process appends
(real subprocesses, exercising O_APPEND atomicity), cursor read/write
atomicity, malformed-line dead-lettering, and the drain-side offset contract.

The store itself needs no identity — it is the storage substrate under emission —
so these tests use a bare temp directory as the repo root.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from contracts import EVENT, EventEnvelope, new_id, utc_now  # noqa: E402
from outbox import store  # noqa: E402
from outbox import paths as outbox_paths  # noqa: E402


def _env(rec) -> EventEnvelope:
    """Narrow a PendingEvent to its (asserted-present) envelope."""
    assert rec.envelope is not None, rec.error
    return rec.envelope


def _make_envelope(seq: int) -> EventEnvelope:
    return EventEnvelope(
        event_id=new_id(EVENT),
        event_type="task.created",
        occurred_at=utc_now(),
        machine_id="mach_00000000000000000000000000",
        repository_id="repo_00000000000000000000000000",
        payload={"seq": seq},
    )


class _TempRepo(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="agent_os_outbox_")
        self.repo = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TestAppendReadCursor(_TempRepo):
    def test_append_then_read_pending_round_trip(self):
        for i in range(3):
            store.append_envelope(self.repo, _make_envelope(i))
        pending = store.read_pending(self.repo)
        self.assertEqual(len(pending), 3)
        self.assertEqual([_env(p).payload["seq"] for p in pending], [0, 1, 2])
        self.assertTrue(all(p.error is None for p in pending))
        # Offsets are strictly increasing and the last equals the file size.
        offsets = [p.end_offset for p in pending]
        self.assertEqual(offsets, sorted(offsets))
        self.assertEqual(offsets[-1], outbox_paths.outbox_path(self.repo).stat().st_size)

    def test_default_cursor_is_zero(self):
        self.assertEqual(store.read_cursor(self.repo), 0)

    def test_write_cursor_consumes_read_events(self):
        for i in range(3):
            store.append_envelope(self.repo, _make_envelope(i))
        pending = store.read_pending(self.repo)
        # Advance past the first two; only the third remains pending.
        store.write_cursor(self.repo, pending[1].end_offset)
        self.assertEqual(store.read_cursor(self.repo), pending[1].end_offset)
        remaining = store.read_pending(self.repo)
        self.assertEqual([_env(p).payload["seq"] for p in remaining], [2])
        # Advancing to EOF drains everything.
        store.write_cursor(self.repo, pending[2].end_offset)
        self.assertEqual(store.read_pending(self.repo), [])

    def test_read_pending_accepts_explicit_cursor(self):
        for i in range(2):
            store.append_envelope(self.repo, _make_envelope(i))
        first_end = store.read_pending(self.repo)[0].end_offset
        # An explicit cursor overrides the persisted one (which is still 0 here).
        from_explicit = store.read_pending(self.repo, cursor=first_end)
        self.assertEqual([_env(p).payload["seq"] for p in from_explicit], [1])

    def test_missing_outbox_reads_empty(self):
        self.assertEqual(store.read_pending(self.repo), [])


class TestCursorAtomicity(_TempRepo):
    def _fill_outbox(self, n: int) -> None:
        """Append n envelopes so the outbox exists and is larger than the small
        offsets these tests write (read_cursor resets an offset past EOF)."""
        for i in range(n):
            store.append_envelope(self.repo, _make_envelope(i))

    def test_write_cursor_records_offset_and_generation_no_leftovers(self):
        self._fill_outbox(2)
        store.write_cursor(self.repo, 8)
        data = json.loads(outbox_paths.cursor_path(self.repo).read_text(encoding="utf-8"))
        self.assertEqual(data["offset"], 8)
        # A generation token is recorded alongside the offset (the outbox exists
        # here, so it is a concrete device:inode string, not None).
        self.assertIn("generation", data)
        self.assertIsNotNone(data["generation"])
        leftovers = [
            p.name
            for p in outbox_paths.sync_dir(self.repo).iterdir()
            if p.name.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])

    def test_write_cursor_overwrites_atomically(self):
        self._fill_outbox(3)
        store.write_cursor(self.repo, 1)
        store.write_cursor(self.repo, 2)
        self.assertEqual(store.read_cursor(self.repo), 2)

    def test_negative_cursor_rejected(self):
        with self.assertRaises(ValueError):
            store.write_cursor(self.repo, -1)

    def test_corrupt_cursor_reads_as_zero(self):
        outbox_paths.ensure_sync_dir(self.repo)
        outbox_paths.cursor_path(self.repo).write_text("not json", encoding="utf-8")
        self.assertEqual(store.read_cursor(self.repo), 0)


class TestCursorRobustness(_TempRepo):
    """read_cursor must never raise and must reset on rotation/corruption."""

    def _write_cursor_json(self, payload) -> None:
        outbox_paths.ensure_sync_dir(self.repo)
        outbox_paths.cursor_path(self.repo).write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_non_dict_cursor_json_reads_as_zero(self):
        # Valid JSON that is not an object (null / list / number) must reset to 0,
        # not raise AttributeError on data.get(...).
        for payload in (None, [1, 2, 3], 42, "a string"):
            with self.subTest(payload=payload):
                self._write_cursor_json(payload)
                self.assertEqual(store.read_cursor(self.repo), 0)

    def test_non_integer_offset_reads_as_zero(self):
        self._write_cursor_json({"offset": "not-an-int"})
        self.assertEqual(store.read_cursor(self.repo), 0)

    def test_offset_past_filesize_resets_to_zero(self):
        # A stale offset that survives a shrunk/truncated outbox must reset to 0,
        # not seek past EOF and yield [] forever (Finder A #5).
        store.append_envelope(self.repo, _make_envelope(0))
        size = outbox_paths.outbox_path(self.repo).stat().st_size
        store.write_cursor(self.repo, size)
        self.assertEqual(store.read_cursor(self.repo), size)
        # Truncate the outbox to empty; the offset now points past EOF.
        outbox_paths.outbox_path(self.repo).write_text("", encoding="utf-8")
        self.assertEqual(store.read_cursor(self.repo), 0)

    def test_deleted_and_recreated_outbox_resets_via_generation(self):
        # Fully drain, then delete+recreate the outbox (new inode = new
        # generation). Even if the new file grows PAST the old offset, the stored
        # cursor must reset so the drain does not resume mid-stream in a different
        # file's bytes.
        for i in range(3):
            store.append_envelope(self.repo, _make_envelope(i))
        pending = store.read_pending(self.repo)
        store.write_cursor(self.repo, pending[-1].end_offset)
        old_offset = store.read_cursor(self.repo)
        self.assertGreater(old_offset, 0)
        # Recreate the outbox with a fresh, larger set of lines (new inode).
        os.unlink(outbox_paths.outbox_path(self.repo))
        for i in range(6):
            store.append_envelope(self.repo, _make_envelope(100 + i))
        new_size = outbox_paths.outbox_path(self.repo).stat().st_size
        self.assertGreater(new_size, old_offset)  # offset alone wouldn't catch it
        self.assertEqual(
            store.read_cursor(self.repo), 0,
            "recreated outbox (new generation) must reset the cursor to 0",
        )
        # And a full re-read yields exactly the new file's events.
        seqs = [_env(p).payload["seq"] for p in store.read_pending(self.repo)]
        self.assertEqual(seqs, [100, 101, 102, 103, 104, 105])


class TestDeadLetter(_TempRepo):
    def test_malformed_line_is_flagged_not_fatal(self):
        store.append_line(self.repo, "this is not json")
        store.append_envelope(self.repo, _make_envelope(1))
        pending = store.read_pending(self.repo)
        self.assertEqual(len(pending), 2)
        self.assertIsNone(pending[0].envelope)
        self.assertIsNotNone(pending[0].error)
        # The valid line after the bad one is still parsed (a bad line never
        # stalls the drain).
        self.assertIsNotNone(pending[1].envelope)

    def test_dead_letter_appends_record(self):
        store.dead_letter(self.repo, "bad raw line", "unparseable")
        lines = outbox_paths.dead_letter_path(self.repo).read_text(
            encoding="utf-8"
        ).splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["reason"], "unparseable")
        self.assertEqual(record["raw"], "bad raw line")
        self.assertIn("dead_lettered_at", record)


class TestAppendDiscipline(_TempRepo):
    def test_append_line_rejects_embedded_newline(self):
        with self.assertRaises(ValueError):
            store.append_line(self.repo, "a\nb")

    def test_read_pending_ignores_incomplete_trailing_line(self):
        store.append_envelope(self.repo, _make_envelope(0))
        # Simulate a line still mid-append: bytes with no trailing newline.
        path = outbox_paths.outbox_path(self.repo)
        with open(path, "ab") as handle:
            handle.write(b'{"partial": true}')  # no newline
        pending = store.read_pending(self.repo)
        self.assertEqual(len(pending), 1)  # only the complete first line
        self.assertEqual(_env(pending[0]).payload["seq"], 0)

    def test_oversized_line_is_dead_lettered_not_appended(self):
        # A line over MAX_LINE_BYTES must NOT be appended to the outbox (torn-write
        # risk); it is preserved in the dead-letter log instead, and never raises.
        huge = "x" * (store.MAX_LINE_BYTES + 10)
        store.append_line(self.repo, huge)
        self.assertFalse(
            outbox_paths.outbox_path(self.repo).exists(),
            "oversized line must not be written to the outbox",
        )
        dl = outbox_paths.dead_letter_path(self.repo)
        self.assertTrue(dl.exists())
        record = json.loads(dl.read_text(encoding="utf-8").splitlines()[0])
        self.assertIn("MAX_LINE_BYTES", record["reason"])
        self.assertEqual(record["raw"], huge)
        # A normal-sized line still appends fine afterwards.
        store.append_envelope(self.repo, _make_envelope(0))
        self.assertEqual(len(store.read_pending(self.repo)), 1)

    def test_read_pending_bounds_batch_by_max_events(self):
        for i in range(10):
            store.append_envelope(self.repo, _make_envelope(i))
        first = store.read_pending(self.repo, max_events=4)
        self.assertEqual([_env(p).payload["seq"] for p in first], [0, 1, 2, 3])
        # The worker advances to the last returned offset and reads the next batch.
        store.write_cursor(self.repo, first[-1].end_offset)
        second = store.read_pending(self.repo, max_events=4)
        self.assertEqual([_env(p).payload["seq"] for p in second], [4, 5, 6, 7])


class TestConcurrentAppends(_TempRepo):
    @unittest.skipIf(
        sys.platform == "win32",
        "concurrent cross-process append non-interleaving is a POSIX O_APPEND "
        "guarantee; Windows lacks it (single-writer-per-process only) — the "
        "Windows CI leg must not flake on this (card ce79a987 finding 7).",
    )
    def test_multi_process_appends_are_not_interleaved(self):
        procs = 6
        per_proc = 40
        # Each subprocess appends `per_proc` distinct, tagged JSON lines to the
        # SAME outbox via outbox.store.append_line. If O_APPEND single-writes were
        # not atomic across processes, lines would interleave/corrupt and the
        # count or parse would fail.
        program = (
            "import sys\n"
            f"sys.path.insert(0, {str(_REPO_ROOT)!r})\n"
            "from outbox import store\n"
            "import json\n"
            "tag = sys.argv[1]\n"
            "repo = sys.argv[2]\n"
            f"for i in range({per_proc}):\n"
            "    store.append_line(repo, json.dumps({'tag': tag, 'i': i}))\n"
        )
        running = []
        for p in range(procs):
            running.append(
                subprocess.Popen(
                    [sys.executable, "-c", program, f"proc{p}", str(self.repo)]
                )
            )
        for proc in running:
            self.assertEqual(proc.wait(timeout=60), 0)

        raw = outbox_paths.outbox_path(self.repo).read_text(encoding="utf-8")
        lines = [ln for ln in raw.splitlines() if ln]
        self.assertEqual(len(lines), procs * per_proc)
        seen = set()
        for ln in lines:
            obj = json.loads(ln)  # every line must be intact, parseable JSON
            seen.add((obj["tag"], obj["i"]))
        # Every (proc, i) pair present exactly once — nothing lost or corrupted.
        expected = {(f"proc{p}", i) for p in range(procs) for i in range(per_proc)}
        self.assertEqual(seen, expected)


if __name__ == "__main__":
    unittest.main()
