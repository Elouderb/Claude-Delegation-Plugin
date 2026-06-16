from __future__ import annotations

from hook_common import (
    git_root,
    is_relevant_source,
    mark_dirty,
    read_hook_input,
    relative_to_root,
)

def main() -> int:
    payload = read_hook_input()
    root = git_root(payload.get("cwd"))
    if root is None:
        return 0

    tool_input = payload.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("path")
    if not path:
        return 0

    rel = relative_to_root(path, root)
    if rel is not None and is_relevant_source(rel):
        mark_dirty(root, f"Claude modified {rel.as_posix()}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
