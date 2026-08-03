from __future__ import annotations

import sys
from hook_common import git_root, is_generated_in_repo, read_hook_input, relative_to_root

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
    if rel is not None and is_generated_in_repo(rel, root):
        if rel.as_posix().startswith("contracts/schemas/"):
            hint = (
                "Edit the pydantic models in contracts/*.py and regenerate with "
                "`python -m contracts.generate_schemas` instead."
            )
        else:
            hint = "Modify the source or generator instead."
        print(
            f"Blocked direct edit to generated artifact: {rel.as_posix()}. {hint}",
            file=sys.stderr,
        )
        return 2
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
