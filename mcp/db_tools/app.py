from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import urllib.parse
from pathlib import Path

from flask import Flask, Response, jsonify, send_from_directory, url_for
from markupsafe import escape
from dotenv import dotenv_values

SCRIPT_DIR = Path(__file__).resolve().parent  # mcp/db_tools — location of the build scripts
BUILD_SCRIPT = SCRIPT_DIR / "build_db_graph.py"
VISUALIZE_SCRIPT = SCRIPT_DIR / "build_graph_html.py"

_REGISTRY_FILE = Path.home() / ".agent-os" / "active_repos.json"

app = Flask(__name__)


def _load_registry() -> dict[str, str]:
    """Return {slug: repo_root_str} from the shared registry file."""
    if not _REGISTRY_FILE.exists():
        return {}
    try:
        data = json.loads(_REGISTRY_FILE.read_text())
        return {str(k): str(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except Exception:
        return {}


def _repo_root_for_slug(slug: str) -> Path | None:
    """Resolve a repo root path from the registry. Returns None if not found."""
    path_str = _load_registry().get(slug)
    if not path_str:
        return None
    p = Path(path_str)
    return p if p.exists() else None


def _paths(slug: str) -> tuple[Path | None, Path, Path]:
    """Return (repo_root, db_graph_dir, code_graph_dir) for a slug.

    repo_root is None when the slug is not in the registry.
    """
    repo_root = _repo_root_for_slug(slug)
    if repo_root is None:
        return None, Path(), Path()
    return repo_root, repo_root / ".agent-os" / "db", repo_root / "graphify-out"


def _slug_not_found(slug: str) -> Response:
    slug_esc = escape(slug)
    return Response(
        f"<h1>Repository '{slug_esc}' not found</h1>"
        "<p>This slug is not in the registry. Open a Claude Code session in the repository to register it.</p>"
        "<p><a href='/'>← Back to all repositories</a></p>",
        status=404, mimetype="text/html",
    )


def _missing_file(slug: str, msg: str) -> Response:
    slug_esc = escape(slug)
    return Response(
        f"<h1>{escape(msg)}</h1><p><a href='/{slug_esc}/'>← Back to {slug_esc}</a></p>",
        status=404, mimetype="text/html",
    )


@app.get("/")
def index():
    registry = _load_registry()
    if not registry:
        body = (
            "<p>No repositories registered yet.</p>"
            "<p>Open a Claude Code session in a repository — "
            "the Agent OS plugin registers it automatically.</p>"
        )
    else:
        items = "".join(
            f'<li><a href="/{escape(s)}/">{escape(s)}</a>'
            f' <small>— {escape(p)}</small></li>'
            for s, p in sorted(registry.items())
        )
        body = f"<ul>{items}</ul>"

    html = f"""<!DOCTYPE html>
<html>
<head><title>Agent OS Graph Server</title><style>
  body {{ font-family: sans-serif; margin: 40px; color: #222; }}
  h1 {{ color: #333; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ margin: 10px 0; font-size: 1.1em; }}
  a {{ color: #0066cc; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  small {{ color: #888; font-size: 0.75em; }}
  hr {{ margin-top: 40px; border: none; border-top: 1px solid #eee; }}
</style></head>
<body>
  <h1>Agent OS Graph Server</h1>
  <p>Active repositories:</p>
  {body}
  <hr>
  <p><a href="/health">Health</a></p>
</body>
</html>"""
    return Response(html, mimetype="text/html")


@app.get("/<slug>/")
def repo_index(slug: str):
    repo_root = _repo_root_for_slug(slug)
    if repo_root is None:
        return _slug_not_found(slug)

    slug_h = escape(slug)
    slug_u = urllib.parse.quote(slug, safe="")
    root_h = escape(str(repo_root))

    html = f"""<!DOCTYPE html>
<html>
<head><title>{slug_h} — Agent OS</title><style>
  body {{ font-family: sans-serif; margin: 40px; color: #222; }}
  h1 {{ color: #333; }}
  .panels {{ display: flex; gap: 24px; margin-top: 28px; flex-wrap: wrap; }}
  .panel {{ border: 1px solid #ddd; border-radius: 8px; padding: 20px 24px; min-width: 180px; }}
  .panel h2 {{ color: #555; font-size: 1em; margin: 0 0 14px; border-bottom: 1px solid #eee; padding-bottom: 8px; }}
  .panel a {{ display: block; color: #0066cc; text-decoration: none; margin: 6px 0; font-size: 0.95em; }}
  .panel a:hover {{ text-decoration: underline; }}
  .crumb {{ color: #888; font-size: 0.85em; margin-bottom: 8px; }}
  small {{ color: #888; font-size: 0.8em; }}
  hr {{ margin-top: 40px; border: none; border-top: 1px solid #eee; }}
</style></head>
<body>
  <p class="crumb"><a href="/">← All repositories</a></p>
  <h1>{slug_h}</h1>
  <small>{root_h}</small>
  <div class="panels">
    <div class="panel">
      <h2>Code Graph</h2>
      <a href="/{slug_u}/code_graph">View graph</a>
      <a href="/{slug_u}/api/code_graph">JSON API</a>
      <a href="/{slug_u}/download/repo/markdown">Download report</a>
      <a href="#" onclick="fetch('/{slug_u}/refresh/repo',{{method:'POST'}}).then(r=>r.json()).then(d=>alert(d.status||d.error));return false;">Refresh</a>
    </div>
    <div class="panel">
      <h2>Database Graph</h2>
      <a href="/{slug_u}/db_graph">View graph</a>
      <a href="/{slug_u}/api/db_graph">JSON API</a>
      <a href="/{slug_u}/download/db/graphml">Download GraphML</a>
      <a href="/{slug_u}/download/db/markdown">Download Markdown</a>
      <a href="#" onclick="fetch('/{slug_u}/refresh/db',{{method:'POST'}}).then(r=>r.json()).then(d=>alert(d.status||d.error));return false;">Refresh</a>
    </div>
    <div class="panel">
      <h2>Task Cards</h2>
      <a href="/{slug_u}/task_cards">View cards</a>
    </div>
  </div>
  <hr>
  <p><a href="/{slug_u}/health">Health check</a></p>
</body>
</html>"""
    return Response(html, mimetype="text/html")


@app.get("/<slug>/code_graph")
def code_graph(slug: str):
    repo_root, _, code_graph_dir = _paths(slug)
    if repo_root is None:
        return _slug_not_found(slug)
    html_file = code_graph_dir / "graph.html"
    if not html_file.exists():
        return _missing_file(slug, "Code graph not found — refresh via the repo page")
    return send_from_directory(str(code_graph_dir), html_file.name)


@app.get("/<slug>/db_graph")
def db_graph(slug: str):
    repo_root, db_graph_dir, _ = _paths(slug)
    if repo_root is None:
        return _slug_not_found(slug)
    html_file = db_graph_dir / "db_graph.html"
    if not html_file.exists():
        return _missing_file(slug, "Database graph not found — refresh via the repo page")
    return send_from_directory(str(db_graph_dir), html_file.name)


@app.get("/<slug>/task_cards")
def task_cards(slug: str):
    repo_root, _, _ = _paths(slug)
    if repo_root is None:
        return _slug_not_found(slug)

    db_path = repo_root / ".agent-os" / "cards.sqlite"
    if not db_path.exists():
        return _missing_file(slug, "No task-cards database found in this repository")

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cards = conn.execute(
            "SELECT card_id, title, status, priority, created_at, updated_at"
            " FROM cards ORDER BY created_at DESC"
        ).fetchall()
        conn.close()
    except Exception:
        print("task_cards: error reading cards.sqlite", file=sys.stderr)
        return Response("<h1>Error reading cards</h1><p>Could not read the task-cards database.</p>", status=500, mimetype="text/html")

    slug_h = escape(slug)
    slug_u = urllib.parse.quote(slug, safe="")
    _status_color = {"Created": "#6c757d", "In Progress": "#0066cc", "Complete": "#28a745"}
    if not cards:
        rows = "<tr><td colspan='5' style='text-align:center;padding:20px;color:#888'>No cards yet</td></tr>"
    else:
        rows = ""
        for c in cards:
            color = _status_color.get(c["status"], "#333")
            rows += (
                f"<tr>"
                f"<td><code>{escape(c['card_id'])[:8]}</code></td>"
                f"<td>{escape(c['title'])}</td>"
                f"<td><span style='color:{color};font-weight:600'>{escape(c['status'])}</span></td>"
                f"<td>{escape(c['priority'] or '')}</td>"
                f"<td style='color:#888;font-size:0.85em'>{escape(c['updated_at'] or '')[:16]}</td>"
                f"</tr>"
            )

    html = f"""<!DOCTYPE html>
<html>
<head><title>{slug_h} — Task Cards</title><style>
  body {{ font-family: sans-serif; margin: 40px; color: #222; }}
  h1 {{ color: #333; }}
  .crumb {{ color: #888; font-size: 0.85em; margin-bottom: 8px; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
  th, td {{ border: 1px solid #ddd; padding: 10px 14px; text-align: left; vertical-align: top; }}
  th {{ background: #f5f5f5; color: #555; }}
  tr:hover td {{ background: #fafafa; }}
  code {{ font-size: 0.85em; color: #666; }}
  .count {{ color: #888; font-size: 0.85em; margin-top: 12px; }}
</style></head>
<body>
  <p class="crumb"><a href="/{slug_u}/">← Back to {slug_h}</a></p>
  <h1>Task Cards — {slug_h}</h1>
  <table>
    <thead>
      <tr><th>ID</th><th>Title</th><th>Status</th><th>Priority</th><th>Updated</th></tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  <p class="count">{len(cards)} card(s)</p>
</body>
</html>"""
    return Response(html, mimetype="text/html")


@app.get("/<slug>/api/code_graph")
def code_graph_json(slug: str):
    repo_root, _, code_graph_dir = _paths(slug)
    if repo_root is None:
        return jsonify({"error": f"Repo '{slug}' not found"}), 404
    f = code_graph_dir / "graph.json"
    if not f.exists():
        return jsonify({"error": "graph.json not found"}), 404
    return send_from_directory(str(code_graph_dir), f.name, mimetype="application/json")


@app.get("/<slug>/api/db_graph")
def db_graph_json(slug: str):
    repo_root, db_graph_dir, _ = _paths(slug)
    if repo_root is None:
        return jsonify({"error": f"Repo '{slug}' not found"}), 404
    f = db_graph_dir / "db_graph.json"
    if not f.exists():
        return jsonify({"error": "db_graph.json not found"}), 404
    return send_from_directory(str(db_graph_dir), f.name, mimetype="application/json")


@app.get("/<slug>/download/repo/markdown")
def download_repo_markdown(slug: str):
    repo_root, _, code_graph_dir = _paths(slug)
    if repo_root is None:
        return jsonify({"error": f"Repo '{slug}' not found"}), 404
    f = code_graph_dir / "GRAPH_REPORT.md"
    if not f.exists():
        return jsonify({"error": "GRAPH_REPORT.md not found"}), 404
    return send_from_directory(str(code_graph_dir), f.name, as_attachment=True, download_name="GRAPH_REPORT.md")


@app.get("/<slug>/download/db/graphml")
def download_db_graphml(slug: str):
    repo_root, db_graph_dir, _ = _paths(slug)
    if repo_root is None:
        return jsonify({"error": f"Repo '{slug}' not found"}), 404
    f = db_graph_dir / "db_graph.graphml"
    if not f.exists():
        return jsonify({"error": "db_graph.graphml not found"}), 404
    return send_from_directory(str(db_graph_dir), f.name, as_attachment=True, download_name="db_graph.graphml")


@app.get("/<slug>/download/db/markdown")
def download_db_markdown(slug: str):
    repo_root, db_graph_dir, _ = _paths(slug)
    if repo_root is None:
        return jsonify({"error": f"Repo '{slug}' not found"}), 404
    f = db_graph_dir / "db_graph.md"
    if not f.exists():
        return jsonify({"error": "db_graph.md not found"}), 404
    return send_from_directory(str(db_graph_dir), f.name, as_attachment=True, download_name="db_graph.md")


@app.get("/health")
def health():
    registry = _load_registry()
    return jsonify({"status": "ok", "repos": sorted(registry.keys())})


@app.get("/<slug>/health")
def repo_health(slug: str):
    repo_root, db_graph_dir, code_graph_dir = _paths(slug)
    if repo_root is None:
        return jsonify({"error": f"Repo '{slug}' not found"}), 404
    return jsonify({
        "status": "ok",
        "slug": slug,
        "repo_root": str(repo_root),
        "db_graph": {
            "html_exists": (db_graph_dir / "db_graph.html").exists(),
            "json_exists": (db_graph_dir / "db_graph.json").exists(),
            "graphml_exists": (db_graph_dir / "db_graph.graphml").exists(),
            "markdown_exists": (db_graph_dir / "db_graph.md").exists(),
        },
        "code_graph": {
            "html_exists": (code_graph_dir / "graph.html").exists(),
            "json_exists": (code_graph_dir / "graph.json").exists(),
            "report_exists": (code_graph_dir / "GRAPH_REPORT.md").exists(),
        },
    })


@app.post("/<slug>/refresh/repo")
def refresh_repo(slug: str):
    repo_root, _, _ = _paths(slug)
    if repo_root is None:
        return jsonify({"error": f"Repo '{slug}' not found"}), 404
    try:
        result = subprocess.run(
            ["graphify", "update", "."],
            cwd=repo_root, capture_output=True, text=True,
            timeout=300, check=True,
        )
    except FileNotFoundError:
        return jsonify({"error": "graphify not found — install with: pip install graphifyy"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Code graph refresh timed out"}), 504
    except subprocess.CalledProcessError as exc:
        return jsonify({"error": "Code graph refresh failed", "stdout": exc.stdout, "stderr": exc.stderr}), 500
    return jsonify({
        "status": "refreshed",
        "graph_type": "code",
        "output": result.stdout.strip(),
        "graph_url": url_for("code_graph", slug=slug),
    })


@app.post("/<slug>/refresh/db")
def refresh_db(slug: str):
    repo_root, _, _ = _paths(slug)
    if repo_root is None:
        return jsonify({"error": f"Repo '{slug}' not found"}), 404

    env_vars = dotenv_values(repo_root / ".env")
    db_conn_str = env_vars.get("DB_CONNECTION_STRING") or os.getenv("DB_CONNECTION_STRING")
    if not db_conn_str:
        return jsonify({"error": "DB_CONNECTION_STRING is not set"}), 500
    if not BUILD_SCRIPT.exists() or not VISUALIZE_SCRIPT.exists():
        return jsonify({"error": "build_db_graph.py or build_graph_html.py is missing"}), 500

    env = {**os.environ, "DB_CONNECTION_STRING": db_conn_str}
    try:
        build = subprocess.run(
            [sys.executable, str(BUILD_SCRIPT)],
            cwd=repo_root, capture_output=True, text=True,
            timeout=120, check=True, env=env,
        )
        visualize = subprocess.run(
            [sys.executable, str(VISUALIZE_SCRIPT)],
            cwd=repo_root, capture_output=True, text=True,
            timeout=120, check=True, env=env,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Database graph refresh timed out"}), 504
    except subprocess.CalledProcessError as exc:
        return jsonify({"error": "Database graph refresh failed", "stdout": exc.stdout, "stderr": exc.stderr}), 500

    return jsonify({
        "status": "refreshed",
        "graph_type": "database",
        "build_output": build.stdout.strip(),
        "visualization_output": visualize.stdout.strip(),
        "graph_url": url_for("db_graph", slug=slug),
    })


if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes"}
    app.run(host=host, port=port, debug=debug)
