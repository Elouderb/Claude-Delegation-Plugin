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
                     updated_at: str = "2026-01-02T00:00:00",
                     created_at: str = "2026-01-01T00:00:00"):
        db_path = root / ".agent-os" / "cards.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO cards VALUES (?,?,?,?,?,?,?)",
            (card_id, title, None, status, priority, created_at, updated_at),
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

    # ── top-3 chip limit + Expand button (AC: Part A) ─────────────────────────

    def test_all_cards_top3_shown_when_column_exceeds_3(self):
        """Server HTML must show at most 3 chips per column when >3 cards exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_repo(tmpdir)
            for i in range(5):
                self._insert_card(root, f"c000000{i}", f"Card {i}", status="Created",
                                  created_at=f"2026-01-0{i+1}T00:00:00")
            registry = self._registry_from_roots({"repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards")
        body = resp.data.decode()
        # Only 3 chips can appear — count chip anchor elements in body
        chip_count = body.count('class="chip"')
        self.assertEqual(chip_count, 3)

    def test_all_cards_top3_are_newest_by_created_at(self):
        """The 3 chips shown must be the 3 most-recently-created cards."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_repo(tmpdir)
            # Insert 5 cards with distinct created_at timestamps; newest = card4, card3, card2
            for i in range(5):
                self._insert_card(root, f"c000000{i}", f"Title{i}", status="Created",
                                  created_at=f"2026-01-0{i+1}T00:00:00")
            registry = self._registry_from_roots({"repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards")
        body = resp.data.decode()
        # Newest 3 (indexes 4, 3, 2 → most-recent creation) must appear
        self.assertIn("Title4", body)
        self.assertIn("Title3", body)
        self.assertIn("Title2", body)
        # Oldest 2 must NOT appear
        self.assertNotIn("Title0", body)
        self.assertNotIn("Title1", body)

    def test_all_cards_expand_button_present_when_column_gt3(self):
        """Expand button must appear in the slug cell when any column has >3 cards."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_repo(tmpdir)
            for i in range(4):
                self._insert_card(root, f"c000000{i}", f"Card {i}", status="Created",
                                  created_at=f"2026-01-0{i+1}T00:00:00")
            registry = self._registry_from_roots({"repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards")
        body = resp.data.decode()
        # The actual button element must appear in the HTML (not just the JS class string)
        self.assertIn('<button class="board-expand"', body)

    def test_all_cards_expand_button_absent_when_all_columns_le3(self):
        """Expand button must NOT appear when all columns have ≤3 cards."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_repo(tmpdir)
            # 3 Created, 1 In Progress, 1 Complete — none exceed 3
            for i in range(3):
                self._insert_card(root, f"c00000{i}a", f"Created {i}", status="Created")
            self._insert_card(root, "c000010b", "InProg 1", status="In Progress")
            self._insert_card(root, "c000010c", "Done 1", status="Complete")
            registry = self._registry_from_roots({"repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards")
        body = resp.data.decode()
        # No actual button element — the JS class string will still be present
        self.assertNotIn('<button class="board-expand"', body)

    def test_all_cards_json_returns_all_cards_not_top3(self):
        """/all_cards.json must return ALL cards, not just the 3 shown in HTML."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_repo(tmpdir)
            for i in range(5):
                self._insert_card(root, f"c000000{i}", f"Card {i}", status="Created",
                                  created_at=f"2026-01-0{i+1}T00:00:00")
            registry = self._registry_from_roots({"repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards.json")
        data = json.loads(resp.data)
        col = data["projects"][0]["columns"]["Created"]
        self.assertEqual(len(col), 5)

    # ── prominent board link (AC: Part B) ─────────────────────────────────────

    def test_all_cards_prominent_board_link_near_top(self):
        """The board link button must appear BEFORE the repo list, not only in the footer."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_repo(tmpdir)
            registry = self._registry_from_roots({"repo": root})
            with self._patch_registry(registry):
                resp = self.client.get("/")
        body = resp.data.decode()
        # The prominent link must appear before <ul class="repos">
        board_pos = body.find("board-link-btn")
        repo_list_pos = body.find('class="repos"')
        self.assertGreater(board_pos, -1, "board-link-btn class not found in index HTML")
        self.assertGreater(repo_list_pos, -1, "'repos' list not found in index HTML")
        self.assertLess(board_pos, repo_list_pos,
                        "board-link-btn must appear before the repo list")

    def test_all_cards_prominent_board_link_empty_registry(self):
        """The prominent board link must also appear when the registry is empty."""
        with self._patch_registry({}):
            resp = self.client.get("/")
        body = resp.data.decode()
        self.assertIn("board-link-btn", body)

    def test_all_cards_link_not_in_footer(self):
        """The /all_cards link must no longer appear in the footer paragraph."""
        with self._patch_registry({}):
            resp = self.client.get("/")
        body = resp.data.decode()
        # Footer paragraph contains Health; All Cards link must NOT be in the foot <p>
        foot_start = body.find('class="foot"')
        self.assertGreater(foot_start, -1, "Footer paragraph not found")
        foot_text = body[foot_start:]
        self.assertNotIn("/all_cards", foot_text,
                         "board link must not be in the footer paragraph")


class TestPerProjectTaskCardsJson(_AppTestCase):
    """Tests for GET /<slug>/task_cards.json — per-project card data JSON."""

    def _make_root(self, tmpdir: str) -> Path:
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
        self.addCleanup(lambda: None)  # tmpdir is managed by the caller
        return root

    def _insert_card(self, root: Path, card_id: str, title: str,
                     status: str = "Created", priority: str = "medium",
                     updated_at: str = "2026-01-02T00:00:00"):
        db_path = root / ".agent-os" / "cards.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO cards VALUES (?,?,?,?,?,?,?)",
            (card_id, title, None, status, priority, "2026-01-01T00:00:00", updated_at),
        )
        conn.commit()
        conn.close()

    def test_known_slug_returns_200(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_root(tmpdir)
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards.json")
        self.assertEqual(resp.status_code, 200)

    def test_response_shape(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_root(tmpdir)
            self._insert_card(root, "card0001", "A Card", status="Created")
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/task_cards.json")
        data = json.loads(resp.data)
        self.assertEqual(data["slug"], "my-repo")
        self.assertIn("counts", data)
        self.assertIn("columns", data)
        self.assertIn("Created", data["columns"])
        self.assertIn("In Progress", data["columns"])
        self.assertIn("Complete", data["columns"])
        self.assertIn("error", data)

    def test_unknown_slug_returns_404(self):
        with self._patch_slug("my-repo", None):
            resp = self.client.get("/nonexistent/task_cards.json")
        self.assertEqual(resp.status_code, 404)

    def test_missing_db_returns_empty_columns_not_500(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)  # no .agent-os/cards.sqlite
            with self._patch_slug("no-db", root):
                resp = self.client.get("/no-db/task_cards.json")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIsNone(data["error"])
        self.assertEqual(data["columns"]["Created"], [])

    def test_cards_appear_in_correct_column(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_root(tmpdir)
            self._insert_card(root, "c0000001", "Created Card", status="Created")
            self._insert_card(root, "c0000002", "InProg Card", status="In Progress")
            self._insert_card(root, "c0000003", "Done Card", status="Complete")
            with self._patch_slug("repo", root):
                resp = self.client.get("/repo/task_cards.json")
        data = json.loads(resp.data)
        self.assertEqual(len(data["columns"]["Created"]), 1)
        self.assertEqual(data["columns"]["Created"][0]["title"], "Created Card")
        self.assertEqual(len(data["columns"]["In Progress"]), 1)
        self.assertEqual(len(data["columns"]["Complete"]), 1)

    def test_corrupt_db_returns_empty_columns_with_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            agent_os = root / ".agent-os"
            agent_os.mkdir(parents=True)
            (agent_os / "cards.sqlite").write_bytes(b"not a sqlite database")
            with self._patch_slug("corrupt", root):
                resp = self.client.get("/corrupt/task_cards.json")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIsNotNone(data["error"])
        self.assertEqual(data["columns"]["Created"], [])


class TestRepoIndexBoard(_AppTestCase):
    """Tests for the per-project board rendered on GET /<slug>/."""

    def _make_root(self, tmpdir: str) -> Path:
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
                     status: str = "Created",
                     created_at: str = "2026-01-01T00:00:00"):
        db_path = root / ".agent-os" / "cards.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO cards VALUES (?,?,?,?,?,?,?)",
            (card_id, title, None, status, "medium", created_at, "2026-01-02T00:00:00"),
        )
        conn.commit()
        conn.close()

    def test_board_has_three_status_column_headers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_root(tmpdir)
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/")
        body = resp.data.decode()
        self.assertIn("Created", body)
        self.assertIn("In Progress", body)
        self.assertIn("Complete", body)

    def test_board_chips_link_to_card_detail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_root(tmpdir)
            self._insert_card(root, "abc12345", "My Card", status="Created")
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/")
        body = resp.data.decode()
        self.assertIn("/my-repo/task_cards/abc12345", body)

    def test_board_top3_cap_server_rendered(self):
        """Server HTML must render at most 3 chips per column."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_root(tmpdir)
            for i in range(5):
                self._insert_card(root, f"c000000{i}", f"Card {i}", status="Created",
                                  created_at=f"2026-01-0{i+1}T00:00:00")
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/")
        body = resp.data.decode()
        chip_count = body.count('class="chip"')
        self.assertEqual(chip_count, 3)

    def test_board_expand_button_present_when_column_gt3(self):
        """Expand button must appear in the board when any column exceeds 3."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_root(tmpdir)
            for i in range(4):
                self._insert_card(root, f"c000000{i}", f"Card {i}", status="Created",
                                  created_at=f"2026-01-0{i+1}T00:00:00")
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/")
        body = resp.data.decode()
        self.assertIn('<button class="board-expand"', body)

    def test_board_expand_button_absent_when_columns_le3(self):
        """Expand button must NOT appear when all columns have 3 or fewer cards."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_root(tmpdir)
            for i in range(3):
                self._insert_card(root, f"c000000{i}", f"Card {i}", status="Created")
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/")
        body = resp.data.decode()
        self.assertNotIn('<button class="board-expand"', body)

    def test_view_cards_link_still_present(self):
        """The existing 'View cards' table link must remain on the page."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_root(tmpdir)
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/")
        body = resp.data.decode()
        self.assertIn("/my-repo/task_cards", body)

    def test_poll_script_present(self):
        """Page must include the per-project poll script targeting task_cards.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = self._make_root(tmpdir)
            with self._patch_slug("my-repo", root):
                resp = self.client.get("/my-repo/")
        body = resp.data.decode()
        self.assertIn("task_cards.json", body)
        self.assertIn("setInterval", body)


class TestAllCardsActivitySort(_AppTestCase):
    """Tests for /all_cards and /all_cards.json project ordering by last_activity."""

    def _make_root_with_cards(self, tmpdir: str,
                               cards: list[tuple]) -> Path:
        """Create repo with given cards. cards: [(card_id, title, status, updated_at)]."""
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
        for card_id, title, status, updated_at in cards:
            conn.execute(
                "INSERT INTO cards VALUES (?,?,?,?,?,?,?)",
                (card_id, title, None, status, "medium",
                 "2026-01-01T00:00:00", updated_at),
            )
        conn.commit()
        conn.close()
        return root

    def _make_empty_root(self, tmpdir: str) -> Path:
        """Create repo with DB but no cards (no activity)."""
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

    def test_all_cards_json_sorted_by_last_activity_desc(self):
        """Projects with more-recent activity appear first in /all_cards.json."""
        with tempfile.TemporaryDirectory() as td1, \
             tempfile.TemporaryDirectory() as td2:
            root_old = self._make_root_with_cards(td1, [
                ("c0000001", "Old Card", "Created", "2026-01-01T00:00:00"),
            ])
            root_new = self._make_root_with_cards(td2, [
                ("c0000002", "New Card", "Created", "2026-06-01T00:00:00"),
            ])
            registry = {"alpha": str(root_old), "beta": str(root_new)}
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards.json")
        data = json.loads(resp.data)
        slugs = [p["slug"] for p in data["projects"]]
        # beta has newer activity → must appear first
        self.assertEqual(slugs[0], "beta")
        self.assertEqual(slugs[1], "alpha")

    def test_all_cards_json_no_activity_projects_last(self):
        """Projects with no cards/activity must sort after active projects."""
        with tempfile.TemporaryDirectory() as td1, \
             tempfile.TemporaryDirectory() as td2, \
             tempfile.TemporaryDirectory() as td3:
            root_active = self._make_root_with_cards(td1, [
                ("c0000001", "Active Card", "Created", "2026-01-15T00:00:00"),
            ])
            root_empty1 = self._make_empty_root(td2)
            root_empty2 = self._make_empty_root(td3)
            registry = {
                "zzz-active": str(root_active),
                "aaa-empty": str(root_empty1),
                "bbb-empty": str(root_empty2),
            }
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards.json")
        data = json.loads(resp.data)
        slugs = [p["slug"] for p in data["projects"]]
        # Active project first, then empty projects in slug order
        self.assertEqual(slugs[0], "zzz-active")
        # Empty projects sort after active, alphabetically
        self.assertIn("aaa-empty", slugs[1:])
        self.assertIn("bbb-empty", slugs[1:])

    def test_all_cards_json_empty_repos_tiebreak_by_slug(self):
        """When activity is equal (all empty), fall back to slug alphabetical order."""
        with tempfile.TemporaryDirectory() as td1, \
             tempfile.TemporaryDirectory() as td2:
            root_z = self._make_empty_root(td1)
            root_a = self._make_empty_root(td2)
            registry = {"zzz": str(root_z), "aaa": str(root_a)}
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards.json")
        data = json.loads(resp.data)
        slugs = [p["slug"] for p in data["projects"]]
        self.assertEqual(slugs, sorted(slugs))

    def test_all_cards_html_sorted_by_last_activity_desc(self):
        """/all_cards HTML must present projects most-recently-active first."""
        with tempfile.TemporaryDirectory() as td1, \
             tempfile.TemporaryDirectory() as td2:
            root_old = self._make_root_with_cards(td1, [
                ("c0000001", "Old Card", "Created", "2026-01-01T00:00:00"),
            ])
            root_new = self._make_root_with_cards(td2, [
                ("c0000002", "New Card", "Created", "2026-06-01T00:00:00"),
            ])
            registry = {"alpha": str(root_old), "beta": str(root_new)}
            with self._patch_registry(registry):
                resp = self.client.get("/all_cards")
        body = resp.data.decode()
        pos_beta = body.find("beta")
        pos_alpha = body.find("alpha")
        self.assertGreater(pos_beta, -1)
        self.assertGreater(pos_alpha, -1)
        self.assertLess(pos_beta, pos_alpha, "beta (newer) must appear before alpha (older)")


class TestRepoIndexInlineScriptXSS(_AppTestCase):
    """The slug is embedded into the per-project board's inline <script> as a JS
    string literal; a slug containing </script> must be escaped so it cannot break
    out of the <script> block. Slugs come from the registry and Flask routing
    rejects </> in URL path segments, so we invoke the view directly (defense-in-
    depth against a tampered active_repos.json / malicious repo directory name)."""

    def _empty_repo(self) -> Path:
        import shutil
        tmp = tempfile.mkdtemp(prefix="agent_os_xsstest_")
        self.addCleanup(shutil.rmtree, tmp, True)
        agent_os = Path(tmp) / ".agent-os"
        agent_os.mkdir(parents=True)
        conn = sqlite3.connect(str(agent_os / "cards.sqlite"))
        conn.execute(
            "CREATE TABLE cards (card_id TEXT PRIMARY KEY, title TEXT NOT NULL,"
            " description TEXT, status TEXT NOT NULL, priority TEXT,"
            " created_at TIMESTAMP, updated_at TIMESTAMP)"
        )
        conn.commit()
        conn.close()
        return Path(tmp)

    def test_script_breakout_slug_is_escaped(self):
        xss_slug = "pre</script><svg onload=alert(1)>post"
        root = self._empty_repo()
        with patch.object(flask_app_module, "_repo_root_for_slug", return_value=root):
            with _app.test_request_context():
                resp = flask_app_module.repo_index(xss_slug)
        body = resp.get_data(as_text=True)
        # The raw breakout sequence must NOT survive anywhere in the page.
        self.assertNotIn("</script><svg onload", body)
        # The slug must appear in the inline script in its escaped (</ -> <\/) form.
        self.assertIn(r"<\/script>", body)


if __name__ == "__main__":
    unittest.main()
