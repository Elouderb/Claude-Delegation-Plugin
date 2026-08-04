from __future__ import annotations

import argparse
import json

from hook_common import (
    bootstrap_identity,
    dirty_path,
    emit_lifecycle_event,
    git_root,
    git_state_changed,
    is_lifecycle_reason,
    mark_dirty,
    read_hook_input,
    refresh_graphify,
    resolve_graphify,
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

    # Phase 1b: on the REAL lifecycle hook points only (session-start ->
    # agent.started; subagent-stop / session-end -> agent.finished), ensure
    # identity exists and emit the event. The bootstrap is GATED on the lifecycle
    # reason (finding 2): the hot reasons (prompt-submit/bash-tool/tool-batch/
    # turn-stop, which fire every turn and emit nothing) must not pay the
    # identity-bootstrap pydantic-import cost. emit_lifecycle_event self-gates too
    # and returns before importing outbox for non-lifecycle reasons. Both are
    # best-effort and independent of the graph refresh below.
    if is_lifecycle_reason(args.reason):
        bootstrap_identity(root)
    emit_lifecycle_event(root, args.reason, payload)

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

    if args.health:
        emit_health(root, graph_report)
    return 0

def emit_health(root, graph_report) -> None:
    cards = root / ".agent-os" / "cards.sqlite"
    db_graph = root / ".agent-os" / "db" / "db_graph.json"
    # resolve_graphify() already did the venv-safe work: it probed the dirs beside
    # sys.executable (raw and resolved) and PATH, and only returns the bare name
    # "graphify" when nothing was found (WINDOWS.md §2). Derive availability from
    # that single result rather than re-running which()/is_file() here — this hook
    # fires on every SessionStart, so the extra filesystem/PATH probes were pure
    # duplicated work.
    resolved_graphify = resolve_graphify()
    graphify_available = resolved_graphify != "graphify"
    context = (
        f"Agent OS status: repo={root}; "
        f"graphify_available={graphify_available}; "
        f"repo_graph_exists={graph_report.exists()}; "
        f"cards_db_exists={cards.exists()}; "
        f"db_graph_exists={db_graph.exists()}. "
        "Use cards for significant work and graph tools before broad search."
    )
    print(json.dumps({"additionalContext": context}))

if __name__ == "__main__":
    raise SystemExit(main())
