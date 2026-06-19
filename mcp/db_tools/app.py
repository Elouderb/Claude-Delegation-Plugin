from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, send_from_directory, url_for
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent  # mcp/db_tools — location of the build scripts


def _repo_root() -> Path:
    """Resolve the repository this graph server serves.

    Walk up from the current working directory to the nearest .git. app.py is
    launched with cwd set to the repo root, so .env and graph data are read from
    the local repository being worked in, never from the plugin install dir.
    """
    current = Path.cwd().resolve()
    search = current
    while search != search.parent:
        if (search / ".git").exists():
            return search
        search = search.parent
    return current


REPO_ROOT = _repo_root()
DB_GRAPH_DIR = REPO_ROOT / ".agent-os" / "db"
CODE_GRAPH_DIR = REPO_ROOT / "graphify-out"

# Database graph files
DB_GRAPH_HTML = DB_GRAPH_DIR / "db_graph.html"
DB_GRAPH_JSON = DB_GRAPH_DIR / "db_graph.json"
DB_GRAPH_GRAPHML = DB_GRAPH_DIR / "db_graph.graphml"
DB_GRAPH_MARKDOWN = DB_GRAPH_DIR / "db_graph.md"

# Code graph files (from graphify)
CODE_GRAPH_HTML = CODE_GRAPH_DIR / "graph.html"
CODE_GRAPH_JSON = CODE_GRAPH_DIR / "graph.json"
CODE_GRAPH_REPORT = CODE_GRAPH_DIR / "GRAPH_REPORT.md"

BUILD_SCRIPT = SCRIPT_DIR / "build_db_graph.py"
VISUALIZE_SCRIPT = SCRIPT_DIR / "build_graph_html.py"

load_dotenv(REPO_ROOT / ".env")
app = Flask(__name__)

@app.get("/")
def index():
    html = """
    <html>
    <head><title>Graph Server</title><style>
        body { font-family: sans-serif; margin: 40px; }
        h1 { color: #333; }
        .graphs { display: flex; gap: 40px; margin-top: 30px; }
        .graph-section { flex: 1; }
        .graph-section h2 { color: #666; border-bottom: 2px solid #ddd; padding-bottom: 10px; }
        a { color: #0066cc; text-decoration: none; }
        a:hover { text-decoration: underline; }
        ul { list-style: none; padding: 0; }
        li { margin: 8px 0; }
    </style></head>
    <body>
        <h1>📊 Graph Server</h1>
        <p>Database schema and code repository graphs</p>
        <div class="graphs">
            <div class="graph-section">
                <h2>Database Graph</h2>
                <ul>
                    <li><a href="/db_graph">📈 View Graph</a></li>
                    <li><a href="/api/db_graph">📋 JSON API</a></li>
                    <li><a href="/download/db/graphml">⬇️ GraphML</a></li>
                    <li><a href="/download/db/markdown">📄 Markdown</a></li>
                    <li><a href="/refresh/db" onclick="if(confirm('Rebuild database graph?')) { fetch(this.href, {method:'POST'}).then(r=>r.json()).then(d=>alert(d.status)); return false; }">🔄 Refresh</a></li>
                </ul>
            </div>
            <div class="graph-section">
                <h2>Repository Graph</h2>
                <ul>
                    <li><a href="/repo_graph">📈 View Graph</a></li>
                    <li><a href="/api/repo_graph">📋 JSON API</a></li>
                    <li><a href="/download/repo/markdown">📄 Report</a></li>
                    <li><a href="/refresh/repo" onclick="if(confirm('Rebuild code graph?')) { fetch(this.href, {method:'POST'}).then(r=>r.json()).then(d=>alert(d.status)); return false; }">🔄 Refresh</a></li>
                </ul>
            </div>
        </div>
        <hr style="margin-top: 40px;">
        <p><a href="/health">Health Check</a></p>
    </body>
    </html>
    """
    return Response(html, mimetype="text/html")

@app.get("/db_graph")
def db_graph():
    if not DB_GRAPH_HTML.exists():
        return Response(
            "<h1>Database Graph not found</h1><p>Run build_db_graph.py and build_graph_html.py, or POST to /refresh/db.</p>",
            status=404,
            mimetype="text/html",
        )
    return send_from_directory(DB_GRAPH_DIR, DB_GRAPH_HTML.name)

@app.get("/repo_graph")
def repo_graph():
    if not CODE_GRAPH_HTML.exists():
        return Response(
            "<h1>Repository Graph not found</h1><p>Run graphify or POST to /refresh/repo.</p>",
            status=404,
            mimetype="text/html",
        )
    return send_from_directory(CODE_GRAPH_DIR, CODE_GRAPH_HTML.name)

@app.get("/api/db_graph")
def db_graph_json():
    if not DB_GRAPH_JSON.exists():
        return jsonify({"error": "db_graph.json not found"}), 404
    return send_from_directory(DB_GRAPH_DIR, DB_GRAPH_JSON.name, mimetype="application/json")

@app.get("/api/repo_graph")
def repo_graph_json():
    if not CODE_GRAPH_JSON.exists():
        return jsonify({"error": "graph.json not found"}), 404
    return send_from_directory(CODE_GRAPH_DIR, CODE_GRAPH_JSON.name, mimetype="application/json")

@app.get("/download/db/graphml")
def download_db_graphml():
    if not DB_GRAPH_GRAPHML.exists():
        return jsonify({"error": "db_graph.graphml not found"}), 404
    return send_from_directory(
        DB_GRAPH_DIR, DB_GRAPH_GRAPHML.name, as_attachment=True,
        download_name="db_graph.graphml"
    )

@app.get("/download/db/markdown")
def download_db_markdown():
    if not DB_GRAPH_MARKDOWN.exists():
        return jsonify({"error": "db_graph.md not found"}), 404
    return send_from_directory(
        DB_GRAPH_DIR, DB_GRAPH_MARKDOWN.name, as_attachment=True,
        download_name="db_graph.md"
    )

@app.get("/download/repo/markdown")
def download_repo_markdown():
    if not CODE_GRAPH_REPORT.exists():
        return jsonify({"error": "GRAPH_REPORT.md not found"}), 404
    return send_from_directory(
        CODE_GRAPH_DIR, CODE_GRAPH_REPORT.name, as_attachment=True,
        download_name="GRAPH_REPORT.md"
    )

@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "db_graph": {
            "html_exists": DB_GRAPH_HTML.exists(),
            "json_exists": DB_GRAPH_JSON.exists(),
            "graphml_exists": DB_GRAPH_GRAPHML.exists(),
            "markdown_exists": DB_GRAPH_MARKDOWN.exists(),
        },
        "code_graph": {
            "html_exists": CODE_GRAPH_HTML.exists(),
            "json_exists": CODE_GRAPH_JSON.exists(),
            "report_exists": CODE_GRAPH_REPORT.exists(),
        },
    })

@app.post("/refresh/db")
def refresh_db():
    if not BUILD_SCRIPT.exists() or not VISUALIZE_SCRIPT.exists():
        return jsonify({"error": "build_db_graph.py or build_graph_html.py is missing"}), 500
    if not os.getenv("DB_CONNECTION_STRING"):
        return jsonify({"error": "DB_CONNECTION_STRING is not set"}), 500

    try:
        build = subprocess.run(
            [sys.executable, str(BUILD_SCRIPT)],
            cwd=REPO_ROOT, capture_output=True, text=True,
            timeout=120, check=True
        )
        visualize = subprocess.run(
            [sys.executable, str(VISUALIZE_SCRIPT)],
            cwd=REPO_ROOT, capture_output=True, text=True,
            timeout=120, check=True
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Database graph refresh timed out"}), 504
    except subprocess.CalledProcessError as exc:
        return jsonify({
            "error": "Database graph refresh failed",
            "stdout": exc.stdout,
            "stderr": exc.stderr,
        }), 500

    return jsonify({
        "status": "refreshed",
        "graph_type": "database",
        "build_output": build.stdout.strip(),
        "visualization_output": visualize.stdout.strip(),
        "graph_url": url_for("db_graph"),
    })

@app.post("/refresh/repo")
def refresh_repo():
    try:
        result = subprocess.run(
            ["graphify", ".", "--update"],
            cwd=REPO_ROOT, capture_output=True, text=True,
            timeout=300, check=True
        )
    except FileNotFoundError:
        return jsonify({"error": "graphify command not found. Install with: pip install graphifyy"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Code graph refresh timed out"}), 504
    except subprocess.CalledProcessError as exc:
        return jsonify({
            "error": "Code graph refresh failed",
            "stdout": exc.stdout,
            "stderr": exc.stderr,
        }), 500

    return jsonify({
        "status": "refreshed",
        "graph_type": "code",
        "output": result.stdout.strip(),
        "graph_url": url_for("repo_graph"),
    })

@app.post("/refresh")
def refresh():
    """Legacy endpoint - refresh database graph."""
    return refresh_db()

if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes"}
    app.run(host=host, port=port, debug=debug)
