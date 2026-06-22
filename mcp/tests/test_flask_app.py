"""
Tests for mcp/db_tools/app.py via Flask's test client.

Covers:
  - GET /                          (index — empty and populated registry)
  - GET /<slug>/                   (repo index)
  - GET /<slug>/task_cards         (task-cards table)
  - GET /health                    (health endpoint)
  - Unknown slug → 404

XSS-escaping invariant: a slug or card value containing '<script>' must
appear as '&lt;script&gt;' in every HTML response — never as raw HTML.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Make sure mcp/db_tools is importable regardless of working directory.
_DB_TOOLS_DIR = Path(__file__).resolve().parent.parent / "db_tools"
sys.path.insert(0, str(_DB_TOOLS_DIR))

import app as flask_app_module  # noqa: E402

# The Flask application object
_app = flask_app_module.app
_app.config["TESTING"] = True


class _AppTestCase(unittest.TestCase):
    """Base class providing a Flask test client and registry helpers."""

    def setUp(self):
        self.client = _app.test_client()

    # ------------------------------------------------------------------ helpers

    def _patch_registry(self, registry: dict):
        """Patch _load_registry and _repo_root_for_slug for the duration of a test."""
        return patch.object(flask_app_module, "_load_registry", return_value=registry)

    def _patch_slug(self, slug: str, root: Path | None):
        """Patch _repo_root_for_slug to return `root` for `slug`."""
        def _resolve(s):
            return root if s == slug else None
        return patch.object(flask_app_module, "_repo_root_for_slug", side_effect=_resolve)


class TestIndexRoute(_AppTestCase):

    def test_empty_registry_returns_200(self):
        with self._patch_registry({}):
            resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)

    def test_empty_registry_no_repo_links(self):
        with self._patch_registry({}):
            resp = self.client.get("/")
        body = resp.data.decode()
        self.assertIn("No repositories registered", body)

    def test_populated_registry_lists_repos(self):
        registry = {"my-repo": "/home/user/my-repo", "other-repo": "/home/user/other-repo"}
        with self._patch_registry(registry):
            resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        self.assertIn("my-repo", body)
        self.assertIn("other-repo", body)

    def test_health_link_present(self):
        with self._patch_registry({}):
            resp = self.client.get("/")
        body = resp.data.decode()
        self.assertIn("/health", body)

    def test_xss_slug_escaped_in_index(self):
        """A slug containing '<script>' must appear escaped, not raw."""
        xss_slug = "<script>alert(1)</script>"
        registry = {xss_slug: "/tmp/xss-repo"}
        with self._patch_registry(registry):
            resp = self.client.get("/")
        body = resp.data.decode()
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;", body)


class TestHealthRoute(_AppTestCase):

    def test_health_returns_200(self):
        with self._patch_registry({"repo-a": "/path/a"}):
            resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)

    def test_health_json_status_ok(self):
        with self._patch_registry({"repo-a": "/path/a"}):
            resp = self.client.get("/health")
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "ok")

    def test_health_lists_repos(self):
        with self._patch_registry({"repo-a": "/path/a", "repo-b": "/path/b"}):
            resp = self.client.get("/health")
        data = json.loads(resp.data)
        self.assertIn("repo-a", data["repos"])
        self.assertIn("repo-b", data["repos"])


class TestRepoIndexRoute(_AppTestCase):

    def test_unknown_slug_returns_404(self):
        with self._patch_slug("my-repo", None):
            resp = self.client.get("/nonexistent-slug/")
        self.assertEqual(resp.status_code, 404)

    def test_unknown_slug_body_mentions_slug(self):
        with self._patch_slug("my-repo", None):
            resp = self.client.get("/some-missing-slug/")
        body = resp.data.decode()
        self.assertIn("some-missing-slug", body)

    def test_known_slug_returns_200(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/")
        self.assertEqual(resp.status_code, 200)

    def test_known_slug_shows_panels(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/")
        body = resp.data.decode()
        self.assertIn("Code Graph", body)
        self.assertIn("Database Graph", body)
        self.assertIn("Task Cards", body)

    def test_xss_slug_escaped_in_repo_index(self):
        """A slug containing '<script>' read from the registry is escaped in the HTML.

        Flask's routing layer rejects literal '<' / '>' in URL path segments, so
        the attack surface is the *registry data* (active_repos.json) being
        reflected back, not URL injection.  We exercise that by patching
        _repo_root_for_slug to return a real dir for a known-safe slug, then
        patching escape/the body construction.

        We test this at the index route where registry slugs are directly
        written into the response HTML via escape().
        """
        xss_slug = "<script>xss</script>"
        registry = {xss_slug: "/tmp/safe"}
        with self._patch_registry(registry):
            resp = self.client.get("/")
        body = resp.data.decode()
        # The raw tag must never appear
        self.assertNotIn("<script>xss</script>", body)
        # markupsafe escape() must have encoded it
        self.assertIn("&lt;script&gt;", body)


class TestTaskCardsRoute(_AppTestCase):

    def _setup_db(self, tmpdir: str) -> Path:
        root = Path(tmpdir)
        agent_os = root / ".agent-os"
        agent_os.mkdir(parents=True)
        db_path = agent_os / "cards.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE cards (
                card_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL,
                priority TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()
        return root

    def test_missing_db_returns_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards")
        self.assertEqual(resp.status_code, 404)

    def test_empty_db_returns_200(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards")
        self.assertEqual(resp.status_code, 200)

    def test_empty_db_shows_no_cards_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards")
        body = resp.data.decode()
        self.assertIn("No cards yet", body)

    def test_card_title_appears_in_table(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            db_path = root / ".agent-os" / "cards.sqlite"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "INSERT INTO cards VALUES (?,?,?,?,?,?,?)",
                ("abc12345", "My Test Card", None, "Created", "high",
                 "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
            )
            conn.commit()
            conn.close()
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards")
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        self.assertIn("My Test Card", body)

    def test_card_count_displayed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            db_path = root / ".agent-os" / "cards.sqlite"
            conn = sqlite3.connect(str(db_path))
            for i in range(3):
                conn.execute(
                    "INSERT INTO cards VALUES (?,?,?,?,?,?,?)",
                    (f"id{i:05}", f"Card {i}", None, "Created", "medium",
                     "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
                )
            conn.commit()
            conn.close()
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards")
        body = resp.data.decode()
        self.assertIn("3 card(s)", body)

    def test_xss_card_title_escaped(self):
        """A card title containing '<script>' must be escaped in the HTML output."""
        xss_title = "<script>alert('xss')</script>"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            db_path = root / ".agent-os" / "cards.sqlite"
            conn = sqlite3.connect(str(db_path))
            conn.execute(
                "INSERT INTO cards VALUES (?,?,?,?,?,?,?)",
                ("xss00001", xss_title, None, "Created", "high",
                 "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
            )
            conn.commit()
            conn.close()
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards")
        body = resp.data.decode()
        # The raw script tag must NOT appear in the response
        self.assertNotIn("<script>alert('xss')</script>", body)
        # The escaped version must be present
        self.assertIn("&lt;script&gt;", body)

    def test_unknown_slug_returns_404(self):
        with self._patch_slug("my-repo", None):
            resp = self.client.get("/nonexistent/task_cards")
        self.assertEqual(resp.status_code, 404)


class TestXssSlugInNotFoundPage(_AppTestCase):
    """XSS escaping when a malicious slug from the registry hits the 404 helper.

    Flask's string path converter rejects literal '<' / '>' in URL segments, so
    '<script>' slugs cannot reach the view via the URL.  The real attack vector
    is a corrupted active_repos.json whose slug contains '<script>' — the index
    page reflects that directly.  We also directly unit-test _slug_not_found().
    """

    def test_xss_slug_escaped_in_index(self):
        """Registry slug with '<script>' must be escaped on the index page."""
        xss_slug = "<script>bad()</script>"
        registry = {xss_slug: "/tmp/bad"}
        with self._patch_registry(registry):
            resp = self.client.get("/")
        body = resp.data.decode()
        self.assertNotIn("<script>bad()</script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_slug_not_found_helper_escapes_slug(self):
        """_slug_not_found() must HTML-escape its slug argument."""
        xss_slug = "<script>bad()</script>"
        response = flask_app_module._slug_not_found(xss_slug)
        body = response.get_data(as_text=True)
        self.assertNotIn("<script>bad()</script>", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertEqual(response.status_code, 404)


class TestRefreshSecurity(_AppTestCase):
    """H1 (CSRF) + H2/M1 (no subprocess-output leak) on the refresh POST endpoints."""

    def test_post_without_xrw_header_is_forbidden(self):
        # A state-changing POST lacking X-Requested-With is rejected by the CSRF
        # guard before reaching the view (so no subprocess can run).
        self.assertEqual(self.client.post("/my-repo/refresh/repo").status_code, 403)
        self.assertEqual(self.client.post("/my-repo/refresh/db").status_code, 403)

    def test_post_with_xrw_header_passes_csrf_guard(self):
        # With the header the guard lets the request through to the view, which
        # 404s for an unknown slug — proving the 403 above came from the guard,
        # not from routing.
        with self._patch_slug("my-repo", None):
            resp = self.client.post(
                "/nonexistent/refresh/repo",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        self.assertEqual(resp.status_code, 404)

    def test_refresh_repo_failure_does_not_leak_subprocess_output(self):
        # H2/M1: a failed refresh must not return subprocess stdout/stderr to the
        # caller (for the DB build that output can contain DB_CONNECTION_STRING).
        secret = "Server=db;Uid=admin;Pwd=SUPERSECRET"
        err = subprocess.CalledProcessError(
            returncode=1, cmd=["graphify", "update", ".", "--force"],
            output="stdout noise", stderr=secret,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with self._patch_slug("my-repo", Path(tmpdir)), \
                 patch.object(flask_app_module.subprocess, "run", side_effect=err):
                resp = self.client.post(
                    "/my-repo/refresh/repo",
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
        self.assertEqual(resp.status_code, 500)
        body = resp.data.decode()
        self.assertNotIn(secret, body)
        self.assertNotIn("stderr", body)
        self.assertNotIn("stdout", body)

    def test_refresh_db_failure_does_not_leak_credentials(self):
        # H2/M1 for the DB endpoint: neither the connection string nor the build
        # stderr may reach the caller.
        secret = "Server=db;Uid=admin;Pwd=SUPERSECRET"
        err = subprocess.CalledProcessError(
            returncode=1, cmd=["python", "build_db_graph.py"],
            output="stdout noise", stderr=secret,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with self._patch_slug("my-repo", Path(tmpdir)), \
                 patch.dict(os.environ, {"DB_CONNECTION_STRING": secret}), \
                 patch.object(flask_app_module.subprocess, "run", side_effect=err):
                resp = self.client.post(
                    "/my-repo/refresh/db",
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
        self.assertEqual(resp.status_code, 500)
        body = resp.data.decode()
        self.assertNotIn(secret, body)
        self.assertNotIn("stderr", body)

    def test_refresh_db_success_does_not_leak_credentials(self):
        # H2/M1 success path: a successful build whose stdout echoes the
        # connection string must not surface it in the response body.
        secret = "Server=db;Uid=admin;Pwd=SUPERSECRET"
        ok = subprocess.CompletedProcess(
            args=["python", "build_db_graph.py"], returncode=0,
            stdout=f"connected to {secret}", stderr="",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with self._patch_slug("my-repo", Path(tmpdir)), \
                 patch.dict(os.environ, {"DB_CONNECTION_STRING": secret}), \
                 patch.object(flask_app_module.subprocess, "run", return_value=ok):
                resp = self.client.post(
                    "/my-repo/refresh/db",
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        self.assertEqual(json.loads(body)["status"], "refreshed")
        self.assertNotIn(secret, body)
        self.assertNotIn("build_output", body)


class TestTaskCardDetailRoute(_AppTestCase):
    """Tests for GET /<slug>/task_cards/<card_id> — the card detail page."""

    def _setup_db(self, tmpdir: str) -> Path:
        """Create a minimal cards.sqlite with both tables."""
        root = Path(tmpdir)
        agent_os = root / ".agent-os"
        agent_os.mkdir(parents=True)
        db_path = agent_os / "cards.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE cards (
                card_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL,
                priority TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE card_comments (
                comment_id INTEGER PRIMARY KEY AUTOINCREMENT,
                card_id TEXT NOT NULL,
                author TEXT,
                comment TEXT,
                created_at TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()
        return root

    def _insert_card(self, db_path: Path, card_id="abc12345", title="Test Card",
                     description="A description", status="In Progress",
                     priority="high"):
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO cards VALUES (?,?,?,?,?,?,?)",
            (card_id, title, description, status, priority,
             "2026-01-01T00:00:00", "2026-01-02T00:00:00"),
        )
        conn.commit()
        conn.close()

    def _insert_comment(self, db_path: Path, card_id="abc12345",
                        author="agent", comment="Did a thing",
                        created_at="2026-01-01T01:00:00"):
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO card_comments(card_id, author, comment, created_at) VALUES (?,?,?,?)",
            (card_id, author, comment, created_at),
        )
        conn.commit()
        conn.close()

    # --- 200 cases ---

    def test_known_card_returns_200(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            self._insert_card(root / ".agent-os" / "cards.sqlite")
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards/abc12345")
        self.assertEqual(resp.status_code, 200)

    def test_detail_page_shows_title(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            self._insert_card(root / ".agent-os" / "cards.sqlite")
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards/abc12345")
        self.assertIn("Test Card", resp.data.decode())

    def test_detail_page_shows_description(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            self._insert_card(root / ".agent-os" / "cards.sqlite", description="Detailed description here")
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards/abc12345")
        self.assertIn("Detailed description here", resp.data.decode())

    def test_empty_description_shows_placeholder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            self._insert_card(root / ".agent-os" / "cards.sqlite", description=None)
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards/abc12345")
        self.assertIn("No description", resp.data.decode())

    def test_detail_page_shows_status_badge(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            self._insert_card(root / ".agent-os" / "cards.sqlite", status="Complete")
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards/abc12345")
        self.assertIn("Complete", resp.data.decode())

    def test_detail_page_shows_comment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            db_path = root / ".agent-os" / "cards.sqlite"
            self._insert_card(db_path)
            self._insert_comment(db_path, comment="Started work")
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards/abc12345")
        self.assertIn("Started work", resp.data.decode())

    def test_no_comments_shows_placeholder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            self._insert_card(root / ".agent-os" / "cards.sqlite")
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards/abc12345")
        self.assertIn("No work log yet", resp.data.decode())

    def test_breadcrumb_links_to_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            self._insert_card(root / ".agent-os" / "cards.sqlite")
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards/abc12345")
        body = resp.data.decode()
        self.assertIn("/my-repo/task_cards", body)

    # --- 404 cases ---

    def test_unknown_card_id_returns_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards/does-not-exist")
        self.assertEqual(resp.status_code, 404)

    def test_missing_db_returns_404(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards/abc12345")
        self.assertEqual(resp.status_code, 404)

    def test_unknown_slug_returns_404(self):
        with self._patch_slug("my-repo", None):
            resp = self.client.get("/nonexistent/task_cards/abc12345")
        self.assertEqual(resp.status_code, 404)

    # --- XSS cases ---

    def test_xss_in_title_is_escaped(self):
        xss = "<script>alert('xss')</script>"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            self._insert_card(root / ".agent-os" / "cards.sqlite", title=xss)
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards/abc12345")
        body = resp.data.decode()
        self.assertNotIn("<script>alert('xss')</script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_xss_in_description_is_escaped(self):
        xss = "<b>not bold</b>"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            self._insert_card(root / ".agent-os" / "cards.sqlite", description=xss)
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards/abc12345")
        body = resp.data.decode()
        self.assertNotIn("<b>not bold</b>", body)
        self.assertIn("&lt;b&gt;", body)

    def test_xss_in_comment_is_escaped(self):
        xss = '<img src=x onerror="alert(1)">'
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            db_path = root / ".agent-os" / "cards.sqlite"
            self._insert_card(db_path)
            self._insert_comment(db_path, comment=xss)
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards/abc12345")
        body = resp.data.decode()
        self.assertNotIn('<img src=x onerror="alert(1)">', body)
        self.assertIn("&lt;img", body)

    def test_xss_in_author_is_escaped(self):
        xss_author = "<script>bad</script>"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._setup_db(tmpdir)
            db_path = root / ".agent-os" / "cards.sqlite"
            self._insert_card(db_path)
            self._insert_comment(db_path, author=xss_author)
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards/abc12345")
        body = resp.data.decode()
        self.assertNotIn("<script>bad</script>", body)
        self.assertIn("&lt;script&gt;", body)


class TestAllCardsRoutes(_AppTestCase):
    """Tests for GET /all_cards and GET /all_cards.json."""

    # ------------------------------------------------------------------ helpers

    def _make_repo(self, tmpdir: str) -> Path:
        """Create a cards.sqlite in tmpdir/.agent-os/ with the standard schema."""
        root = Path(tmpdir)
        agent_os = root / ".agent-os"
        agent_os.mkdir(parents=True, exist_ok=True)
        db_path = agent_os / "cards.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE cards (
                card_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL,
                priority TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()
        return root

    def _insert_card(self, root: Path, card_id: str, title: str,
                     status: str = "Created", priority: str = "medium",
                     updated_at: str = "2026-01-02T00:00:00"):
        db_path = root / ".agent-os" / "cards.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO cards VALUES (?,?,?,?,?,?,?)",
            (card_id, title, None, status, priority,
             "2026-01-01T00:00:00", updated_at),
        )
        conn.commit()
        conn.close()

    def _registry_from_roots(self, slugs_roots: dict) -> dict:
        """Build a registry dict mapping slug -> str(root)."""
        return {slug: str(root) for slug, root in slugs_roots.items()}

    # ── /all_cards HTML endpoint ───────────────────────────────────────────────

    def test_all_cards_empty_registry_returns_200(self):
        with self._patch_registry({}):
            resp = self.client.get("/all_cards")
        self.assertEqual(resp.status_code, 200)

    def test_all_cards_empty_registry_shows_no_repos_message(self):
        with self._patch_registry({}):
            resp = self.client.get("/all_cards")
        self.assertIn("No repositories registered", resp.data.decode())

    def test_all_cards_shows_project_slug(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_repo(tmpdir)
            registry = self._registry_from_roots({"my-repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("my-repo", resp.data.decode())

    def test_all_cards_shows_three_status_columns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_repo(tmpdir)
            registry = self._registry_from_roots({"my-repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards")
        body = resp.data.decode()
        self.assertIn("Created", body)
        self.assertIn("In Progress", body)
        self.assertIn("Complete", body)

    def test_all_cards_card_appears_in_correct_column(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_repo(tmpdir)
            self._insert_card(root, "card0001", "My Created Card", status="Created")
            self._insert_card(root, "card0002", "My InProgress Card", status="In Progress")
            self._insert_card(root, "card0003", "My Complete Card", status="Complete")
            registry = self._registry_from_roots({"repo-a": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards")
        body = resp.data.decode()
        self.assertIn("My Created Card", body)
        self.assertIn("My InProgress Card", body)
        self.assertIn("My Complete Card", body)

    def test_all_cards_missing_db_shows_empty_not_500(self):
        """A repo without cards.sqlite should show empty columns, not 500."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)  # no .agent-os/cards.sqlite
            registry = self._registry_from_roots({"no-db-repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("no-db-repo", resp.data.decode())

    def test_all_cards_multi_repo_aggregation(self):
        with tempfile.TemporaryDirectory() as td1, \
             tempfile.TemporaryDirectory() as td2:
            root_a = self._make_repo(td1)
            root_b = self._make_repo(td2)
            self._insert_card(root_a, "aaa00001", "Alpha Card")
            self._insert_card(root_b, "bbb00001", "Beta Card")
            registry = self._registry_from_roots({"alpha": root_a, "beta": root_b})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards")
        body = resp.data.decode()
        self.assertIn("Alpha Card", body)
        self.assertIn("Beta Card", body)

    def test_all_cards_xss_title_escaped_in_html(self):
        """A card title with <script> must appear escaped, not injected."""
        xss = "<script>alert('xss')</script>"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_repo(tmpdir)
            self._insert_card(root, "xss00001", xss)
            registry = self._registry_from_roots({"xss-repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards")
        body = resp.data.decode()
        self.assertNotIn("<script>alert('xss')</script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_all_cards_link_from_index(self):
        """Index page must contain a link to /all_cards."""
        with self._patch_registry({}):
            resp = self.client.get("/")
        self.assertIn("/all_cards", resp.data.decode())

    # ── /all_cards.json endpoint ───────────────────────────────────────────────

    def test_all_cards_json_returns_200(self):
        with self._patch_registry({}):
            resp = self.client.get("/all_cards.json")
        self.assertEqual(resp.status_code, 200)

    def test_all_cards_json_empty_registry(self):
        with self._patch_registry({}):
            resp = self.client.get("/all_cards.json")
        data = json.loads(resp.data)
        self.assertEqual(data["projects"], [])

    def test_all_cards_json_shape(self):
        """JSON must have projects[] with slug, counts, columns keys."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_repo(tmpdir)
            self._insert_card(root, "c0000001", "A Card", status="Created")
            registry = self._registry_from_roots({"my-repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards.json")
        data = json.loads(resp.data)
        self.assertIn("projects", data)
        proj = data["projects"][0]
        self.assertEqual(proj["slug"], "my-repo")
        self.assertIn("counts", proj)
        self.assertIn("columns", proj)
        self.assertIn("Created", proj["columns"])
        self.assertIn("In Progress", proj["columns"])
        self.assertIn("Complete", proj["columns"])

    def test_all_cards_json_status_grouping(self):
        """Cards must appear in the correct status column in the JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_repo(tmpdir)
            self._insert_card(root, "c0000001", "Created Card", status="Created")
            self._insert_card(root, "c0000002", "InProg Card", status="In Progress")
            self._insert_card(root, "c0000003", "Done Card", status="Complete")
            registry = self._registry_from_roots({"repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards.json")
        data = json.loads(resp.data)
        proj = data["projects"][0]
        self.assertEqual(len(proj["columns"]["Created"]), 1)
        self.assertEqual(proj["columns"]["Created"][0]["title"], "Created Card")
        self.assertEqual(len(proj["columns"]["In Progress"]), 1)
        self.assertEqual(len(proj["columns"]["Complete"]), 1)
        self.assertEqual(proj["counts"]["Created"], 1)
        self.assertEqual(proj["counts"]["In Progress"], 1)
        self.assertEqual(proj["counts"]["Complete"], 1)

    def test_all_cards_json_noncanonical_status_in_other(self):
        """Cards with non-canonical status must appear in the Other bucket."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_repo(tmpdir)
            self._insert_card(root, "c0000001", "Weird Card", status="Weird")
            registry = self._registry_from_roots({"repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards.json")
        data = json.loads(resp.data)
        proj = data["projects"][0]
        self.assertEqual(proj["counts"]["Other"], 1)
        self.assertEqual(proj["columns"]["Other"][0]["title"], "Weird Card")

    def test_all_cards_json_missing_db_empty_columns(self):
        """A repo without cards.sqlite must return empty columns, no error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            registry = self._registry_from_roots({"no-db": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards.json")
        data = json.loads(resp.data)
        proj = data["projects"][0]
        self.assertIsNone(proj["error"])
        self.assertEqual(proj["columns"]["Created"], [])

    def test_all_cards_json_multi_repo_sorted_by_slug(self):
        with tempfile.TemporaryDirectory() as td1, \
             tempfile.TemporaryDirectory() as td2:
            root_a = self._make_repo(td1)
            root_b = self._make_repo(td2)
            registry = self._registry_from_roots({"zzz": root_a, "aaa": root_b})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards.json")
        data = json.loads(resp.data)
        slugs = [p["slug"] for p in data["projects"]]
        self.assertEqual(slugs, sorted(slugs))

    def test_all_cards_json_card_fields(self):
        """Each card entry must have card_id, title, priority."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_repo(tmpdir)
            self._insert_card(root, "mycard01", "My Card", priority="high")
            registry = self._registry_from_roots({"repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards.json")
        data = json.loads(resp.data)
        card = data["projects"][0]["columns"]["Created"][0]
        self.assertEqual(card["card_id"], "mycard01")
        self.assertEqual(card["title"], "My Card")
        self.assertEqual(card["priority"], "high")

    def test_all_cards_json_xss_title_raw_in_json(self):
        """JSON carries the raw title; client JS must use textContent (not innerHTML)."""
        xss = "<script>alert('xss')</script>"
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_repo(tmpdir)
            self._insert_card(root, "xss00001", xss)
            registry = self._registry_from_roots({"repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards.json")
        data = json.loads(resp.data)
        title = data["projects"][0]["columns"]["Created"][0]["title"]
        self.assertEqual(title, xss)

    # ── corrupt-DB resilience + Other-bucket count ─────────────────────────────

    def _make_corrupt_repo(self, tmpdir: str) -> Path:
        """Create a cards.sqlite that exists but is not a valid SQLite database."""
        root = Path(tmpdir)
        agent_os = root / ".agent-os"
        agent_os.mkdir(parents=True, exist_ok=True)
        (agent_os / "cards.sqlite").write_bytes(b"this is not a sqlite database")
        return root

    def test_all_cards_corrupt_db_does_not_500(self):
        """A corrupt cards.sqlite must not 500 the whole board."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_corrupt_repo(tmpdir)
            registry = self._registry_from_roots({"corrupt-repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("corrupt-repo", resp.data.decode())

    def test_all_cards_json_corrupt_db_sets_error_and_empty_columns(self):
        """A corrupt cards.sqlite exercises the except branch: error set, columns empty."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_corrupt_repo(tmpdir)
            registry = self._registry_from_roots({"corrupt-repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards.json")
        self.assertEqual(resp.status_code, 200)
        proj = json.loads(resp.data)["projects"][0]
        self.assertIsNotNone(proj["error"])
        self.assertEqual(proj["columns"]["Created"], [])
        self.assertEqual(proj["columns"]["In Progress"], [])
        self.assertEqual(proj["columns"]["Complete"], [])

    def test_all_cards_count_excludes_other_and_shows_badge(self):
        """Non-canonical cards must not inflate the count; they get an 'other' badge."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_repo(tmpdir)
            self._insert_card(root, "c0000001", "Canon", status="Created")
            self._insert_card(root, "c0000002", "Weird", status="Archived")
            registry = self._registry_from_roots({"repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards")
        body = resp.data.decode()
        self.assertIn("1 card</div>", body)   # count excludes the Other-status card
        self.assertIn("+1 other", body)       # but the off-status card is surfaced


if __name__ == "__main__":
    unittest.main()
