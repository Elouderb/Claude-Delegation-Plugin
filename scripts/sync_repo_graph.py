from __future__ import annotations

import argparse
import json
import shutil

from hook_common import (
    dirty_path,
    git_root,
    git_state_changed,
    mark_dirty,
    read_hook_input,
    refresh_graphify,
)

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reason", default="hook")
    parser.add_argument("--check-git", action="store_true")
    parser.add_argument("--if-dirty", action="store_true")
    parser.add_argument("--health", action="store_true")
    return parser.parse_args()

def main() -> int:
    args = parse_args()
    payload = read_hook_input()
    root = git_root(payload.get("cwd"))

    if root is None:
        if args.health:
            print(json.dumps({
                "additionalContext": "Agent OS: current directory is not inside a Git repository."
            }))
        return 0

    if args.check_git and git_state_changed(root):
        mark_dirty(root, f"Git/worktree state changed before {args.reason}")

    graph_report = root / "graphify-out" / "GRAPH_REPORT.md"
    needs_refresh = dirty_path(root).exists() or not graph_report.exists()

    if args.if_dirty and not needs_refresh:
        if args.health:
            emit_health(root, graph_report)
        return 0

    if needs_refresh:
        ok, message = refresh_graphify(root, args.reason)
        if not ok:
            print(json.dumps({"systemMessage": message}))
    elif args.health:
        emit_health(root, graph_report)

    if args.health:
        emit_health(root, graph_report)
    return 0

def emit_health(root, graph_report) -> None:
    cards = root / ".agent-os" / "cards.sqlite"
    db_graph = root / ".agent-os" / "db" / "db_graph.json"
    context = (
        f"Agent OS status: repo={root}; "
        f"graphify_available={shutil.which('graphify') is not None}; "
        f"repo_graph_exists={graph_report.exists()}; "
        f"cards_db_exists={cards.exists()}; "
        f"db_graph_exists={db_graph.exists()}. "
        "Use cards for significant work and graph tools before broad search."
    )
    print(json.dumps({"additionalContext": context}))

if __name__ == "__main__":
    raise SystemExit(main())
