"""Tests for the memory_* MCP tool surface + graceful degradation.

The key contract: importing memory_tools and calling the tools NEVER requires
the optional heavy deps to be installed, and the server registers the tools
regardless.  These tests run in every environment (they force the
"unavailable" path via patching so they do not depend on which extras happen to
be installed) and never touch the real ~/.agent-os.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

_MCP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MCP_DIR))

import memory_tools  # noqa: E402
from memory.availability import MemoryUnavailable, check_availability  # noqa: E402

# Subprocess script: block the optional heavy deps entirely (via a find_spec
# meta-path finder), then confirm the server still imports, the card tools still
# work, and the memory tools degrade — the real "deps absent" import-guard case.
_IMPORT_GUARD_SCRIPT = textwrap.dedent(
    """
    import sys, os, importlib.abc
    BLOCKED = {"sqlite_vec", "pysqlite3", "sentence_transformers", "torch"}
    class Blocker(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ModuleNotFoundError("blocked: " + name)
            return None
    sys.meta_path.insert(0, Blocker())
    for m in list(sys.modules):
        if m.split(".")[0] in BLOCKED:
            del sys.modules[m]
    sys.path.insert(0, __MCP_DIR__)

    import server, memory_tools

    st = memory_tools.memory_status()
    assert st["available"] is False, st
    # sqlite-vec is blocked, so storage is unavailable regardless of which
    # stdlib SQLite this build ships; assert the degradation CONTRACT rather than
    # a build-specific backend value (stdlib sqlite3 may or may not load exts).
    assert st["storage_available"] is False, st
    assert st["sqlite_backend"] in (None, "sqlite3"), st
    assert "sqlite-vec" in st["missing"], st
    assert "sentence-transformers" in st["missing"], st

    q = memory_tools.memory_query("hello")
    assert q["available"] is False and "memory_requirements" in q["error"], q
    ig = memory_tools.memory_ingest("/tmp/does-not-exist")
    assert ig["available"] is False, ig

    import tempfile, shutil, card_tools
    d = tempfile.mkdtemp(prefix="agent_os_memtest_")
    try:
        card_tools.set_db_path(os.path.join(d, "cards.sqlite")); card_tools.init_db()
        c = card_tools.create_card("still works")
        assert "error" not in c, c
        assert card_tools.get_card(c["card_id"])["title"] == "still works"
    finally:
        shutil.rmtree(d, ignore_errors=True)

    for n in ("memory_ingest", "memory_query", "memory_status"):
        assert callable(getattr(server, n)), n
    print("DEGRADATION_OK")
    """
)

_UNAVAILABLE = {
    "available": False,
    "storage_available": False,
    "embedder_available": False,
    "sqlite_backend": None,
    "missing": ["sqlite-vec", "loadable-extension-capable SQLite (pysqlite3-binary)", "sentence-transformers"],
    "hint": "memory subsystem unavailable — install its optional dependencies with "
            "`pip install -r mcp/memory_requirements.txt`",
}


class TestGracefulDegradation(unittest.TestCase):
    """Tools must degrade to a clear message when deps are absent, not raise."""

    def test_query_unavailable_returns_message(self):
        with patch.object(memory_tools, "check_availability", return_value=_UNAVAILABLE):
            result = memory_tools.memory_query("anything")
        self.assertFalse(result["available"])
        self.assertIn("mcp/memory_requirements.txt", result["error"])
        self.assertIn("sqlite-vec", result["missing"])

    def test_ingest_unavailable_returns_message(self):
        with patch.object(memory_tools, "check_availability", return_value=_UNAVAILABLE):
            result = memory_tools.memory_ingest("/nonexistent/path")
        self.assertFalse(result["available"])
        self.assertIn("mcp/memory_requirements.txt", result["error"])

    def test_unavailable_path_does_not_import_heavy_deps(self):
        # Simulate the heavy modules being entirely absent; the tool must still
        # return cleanly because it checks availability before importing them.
        with patch.object(memory_tools, "check_availability", return_value=_UNAVAILABLE):
            with patch.dict(sys.modules, {"sentence_transformers": None, "sqlite_vec": None}):
                result = memory_tools.memory_query("q")
        self.assertFalse(result["available"])

    def test_real_environment_query_matches_availability(self):
        # Non-mocked: if the real env lacks any dep, the tool reports unavailable
        # rather than raising. (In CI the embedder is absent -> unavailable.)
        if not check_availability()["available"]:
            result = memory_tools.memory_query("q")
            self.assertFalse(result["available"])
            self.assertIn("missing", result)


class TestMemoryStatus(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="agent_os_memtools_")
        self._prev = os.environ.get("AGENT_OS_MEMORY_HOME")
        os.environ["AGENT_OS_MEMORY_HOME"] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("AGENT_OS_MEMORY_HOME", None)
        else:
            os.environ["AGENT_OS_MEMORY_HOME"] = self._prev
        self._tmp.cleanup()

    def test_status_reports_expected_fields(self):
        status = memory_tools.memory_status()
        for key in (
            "available", "storage_available", "embedder_available",
            "embedding_model", "embedding_dim", "db_path", "db_exists",
            "missing", "hint",
        ):
            self.assertIn(key, status)
        self.assertEqual(status["embedding_model"], "BAAI/bge-small-en-v1.5")
        self.assertEqual(status["embedding_dim"], 384)
        # Points at the overridden (temp) home, never the real ~/.agent-os.
        self.assertTrue(status["db_path"].startswith(self._tmp.name))

    def test_status_never_raises_when_unavailable(self):
        with patch.object(memory_tools, "check_availability", return_value=_UNAVAILABLE):
            status = memory_tools.memory_status()
        self.assertFalse(status["available"])
        self.assertEqual(status["missing"], _UNAVAILABLE["missing"])


_AVAILABLE = {
    "available": True,
    "storage_available": True,
    "embedder_available": True,
    "reranker_available": True,
    "sqlite_backend": "pysqlite3",
    "missing": [],
    "hint": "",
}

_STORAGE_PRESENT = check_availability()["storage_available"]


class _StubEmbedder384:
    """Deterministic 384-dim stub so the tool happy-path never loads torch."""

    model_name = "stub-384"

    @property
    def dim(self):
        return 384

    def _v(self):
        vec = [0.0] * 384
        vec[0] = 1.0
        return vec

    def embed_documents(self, texts):
        return [self._v() for _ in texts]

    def embed_query(self, text):
        return self._v()


@unittest.skipUnless(_STORAGE_PRESENT, "memory storage extras not installed")
class TestMemoryToolsHappyPath(unittest.TestCase):
    """Tool plumbing exercised end-to-end with a stubbed embedder via the seam.

    Availability is forced True and the default embedder is patched to a stub, so
    this runs wherever the storage extras exist without downloading a model.
    """

    def setUp(self):
        import memory.embeddings as embeddings

        self._embeddings = embeddings
        self._tmp = tempfile.TemporaryDirectory(prefix="agent_os_memtools_")
        self._prev = os.environ.get("AGENT_OS_MEMORY_HOME")
        os.environ["AGENT_OS_MEMORY_HOME"] = self._tmp.name

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("AGENT_OS_MEMORY_HOME", None)
        else:
            os.environ["AGENT_OS_MEMORY_HOME"] = self._prev
        self._tmp.cleanup()

    def test_ingest_then_query_round_trip(self):
        note = Path(self._tmp.name) / "note.txt"
        note.write_text("The quick brown fox jumps.", encoding="utf-8")
        with patch.object(memory_tools, "check_availability", return_value=_AVAILABLE):
            with patch.object(self._embeddings, "get_default_embedder", return_value=_StubEmbedder384()):
                ingest = memory_tools.memory_ingest(str(note))
                self.assertTrue(ingest["available"])
                self.assertEqual(ingest["documents_new"], 1)
                self.assertEqual(ingest["operation"], "memory_ingest")

                result = memory_tools.memory_query("quick fox", top_k=3)
        self.assertTrue(result["available"])
        self.assertEqual(result["operation"], "memory_query")
        self.assertEqual(result["count"], 1)
        self.assertIn("quick brown fox", result["results"][0]["text"])

    def test_rerank_happy_path_reorders_and_annotates(self):
        # rerank=True routes through a (stubbed) reranker; results carry the
        # reranked flag and each row gets a rerank_score. No model is downloaded.
        for name in ("cats.txt", "dogs.txt", "widget.txt"):
            (Path(self._tmp.name) / name).write_text(
                {
                    "cats.txt": "A small domesticated feline that purrs.",
                    "dogs.txt": "A loyal canine companion that barks.",
                    "widget.txt": "Replacement part serial ZX-9000 in warehouse stock.",
                }[name],
                encoding="utf-8",
            )

        class _StubReranker:
            model_name = "stub-reranker"

            def score(self, query, texts):
                # Prefer the warehouse/widget text so ordering visibly changes.
                return [10.0 if "warehouse" in t else 0.0 for t in texts]

        import memory.reranker as reranker

        with patch.object(memory_tools, "check_availability", return_value=_AVAILABLE):
            with patch.object(self._embeddings, "get_default_embedder", return_value=_StubEmbedder384()):
                memory_tools.memory_ingest(str(Path(self._tmp.name)))
                with patch.object(reranker, "get_default_reranker", return_value=_StubReranker()):
                    result = memory_tools.memory_query("anything", top_k=3, rerank=True)
        self.assertTrue(result["available"])
        self.assertTrue(result["reranked"])
        self.assertGreaterEqual(result["count"], 1)
        self.assertTrue(all("rerank_score" in r for r in result["results"]))
        self.assertIn("warehouse", result["results"][0]["text"])

    def test_rerank_unavailable_model_returns_clean_unavailable(self):
        # Deps present (gate passes) but the reranker MODEL cannot load ->
        # MemoryUnavailable -> clean unavailable result, never a crash.
        import memory.reranker as reranker

        def _boom():
            raise MemoryUnavailable("reranker model 'x' could not be loaded")

        with patch.object(memory_tools, "check_availability", return_value=_AVAILABLE):
            with patch.object(self._embeddings, "get_default_embedder", return_value=_StubEmbedder384()):
                with patch.object(reranker, "get_default_reranker", side_effect=_boom):
                    result = memory_tools.memory_query("q", rerank=True)
        # Routes through the established "unavailable" contract, never a crash.
        self.assertFalse(result["available"])
        self.assertEqual(result["operation"], "memory_query")
        self.assertIn("reranker model", result["error"])
        self.assertIn("hint", result)

    def test_query_bad_top_k_returns_stable_error_shape(self):
        with patch.object(memory_tools, "check_availability", return_value=_AVAILABLE):
            with patch.object(self._embeddings, "get_default_embedder", return_value=_StubEmbedder384()):
                result = memory_tools.memory_query("anything", top_k=0)
        # Error results keep the branch-safe keys callers read.
        self.assertTrue(result["available"])
        self.assertIn("error", result)
        self.assertEqual(result["operation"], "memory_query")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], [])
        self.assertIn("top_k", result)
        self.assertIn("max_per_doc", result)

    def test_query_threads_and_echoes_max_per_doc(self):
        note = Path(self._tmp.name) / "note.txt"
        note.write_text("The quick brown fox jumps.", encoding="utf-8")
        with patch.object(memory_tools, "check_availability", return_value=_AVAILABLE):
            with patch.object(self._embeddings, "get_default_embedder", return_value=_StubEmbedder384()):
                memory_tools.memory_ingest(str(note))
                result = memory_tools.memory_query("quick fox", top_k=3, max_per_doc=1)
        self.assertTrue(result["available"])
        self.assertEqual(result["operation"], "memory_query")
        self.assertEqual(result["max_per_doc"], 1)
        # The default (1, dedup on) is echoed; an explicit None (opt-out) is too.
        with patch.object(memory_tools, "check_availability", return_value=_AVAILABLE):
            with patch.object(self._embeddings, "get_default_embedder", return_value=_StubEmbedder384()):
                default = memory_tools.memory_query("quick fox", top_k=3)
                opted_out = memory_tools.memory_query("quick fox", top_k=3, max_per_doc=None)
        self.assertEqual(default["max_per_doc"], 1)
        self.assertIsNone(opted_out["max_per_doc"])

    def test_query_bad_max_per_doc_returns_stable_error_shape(self):
        with patch.object(memory_tools, "check_availability", return_value=_AVAILABLE):
            with patch.object(self._embeddings, "get_default_embedder", return_value=_StubEmbedder384()):
                result = memory_tools.memory_query("anything", max_per_doc=0)
        # The store's ValueError surfaces verbatim through the stable error shape.
        self.assertTrue(result["available"])
        self.assertIn("max_per_doc", result["error"])
        self.assertEqual(result["operation"], "memory_query")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["results"], [])

    def test_unexpected_exception_is_sanitized(self):
        def boom():
            raise RuntimeError("secret internal detail /etc/passwd")

        with patch.object(memory_tools, "check_availability", return_value=_AVAILABLE):
            with patch.object(self._embeddings, "get_default_embedder", side_effect=boom):
                result = memory_tools.memory_query("q")
        # Raw exception text must not leak into the response (graph_io precedent).
        self.assertTrue(result["available"])
        self.assertNotIn("secret", result["error"])
        self.assertNotIn("/etc/passwd", result["error"])
        self.assertIn("see server logs", result["error"])
        self.assertEqual(result["results"], [])


class TestUnavailableShapeConsistency(unittest.TestCase):
    def test_ingest_and_query_unavailable_share_keys(self):
        with patch.object(memory_tools, "check_availability", return_value=_UNAVAILABLE):
            ing = memory_tools.memory_ingest("/nope")
            qry = memory_tools.memory_query("q")
        for res, op in ((ing, "memory_ingest"), (qry, "memory_query")):
            self.assertEqual(
                set(res) >= {"available", "operation", "error", "missing", "hint"},
                True,
                res,
            )
            self.assertFalse(res["available"])
            self.assertEqual(res["operation"], op)


class TestServerRegistration(unittest.TestCase):
    def test_server_exposes_memory_tools(self):
        import server

        for name in ("memory_ingest", "memory_query", "memory_status"):
            self.assertTrue(callable(getattr(server, name)), f"{name} not registered")


class TestImportGuardSubprocess(unittest.TestCase):
    """With the heavy deps genuinely unimportable, the server must still start,
    the existing card tools must still work, and the memory tools must degrade."""

    def test_server_and_card_tools_survive_missing_memory_deps(self):
        with tempfile.TemporaryDirectory(prefix="agent_os_memguard_") as home:
            script = _IMPORT_GUARD_SCRIPT.replace("__MCP_DIR__", repr(str(_MCP_DIR)))
            env = dict(os.environ, AGENT_OS_MEMORY_HOME=home)
            proc = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
            )
            self.assertEqual(
                proc.returncode, 0, msg=f"stdout={proc.stdout}\nstderr={proc.stderr}"
            )
            self.assertIn("DEGRADATION_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
