from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SOURCE_EXTENSIONS = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".kts", ".go", ".rs", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".rb", ".php", ".swift", ".scala", ".sh", ".bash", ".zsh",
    ".sql", ".md", ".mdx", ".rst", ".html", ".css", ".scss", ".vue",
    ".svelte", ".toml", ".yaml", ".yml", ".json",
}

IGNORED_PARTS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist",
    "build", ".next", "coverage", "graphify-out",
}

GENERATED_PREFIXES = (
    "graphify-out/",
    ".agent-os/db/",
)

def read_hook_input() -> dict[str, Any]:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}

def git_root(start: str | Path | None = None) -> Path | None:
    cwd = Path(start or os.getcwd()).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
        )
        return Path(result.stdout.strip()).resolve()
    except Exception:
        return None

def relative_to_root(path: str | Path, root: Path) -> Path | None:
    try:
        return Path(path).resolve().relative_to(root)
    except Exception:
        return None

def is_generated(rel: Path) -> bool:
    value = rel.as_posix()
    return any(value == p.rstrip("/") or value.startswith(p) for p in GENERATED_PREFIXES)

# The launcher config the installer GENERATES from templates/ (see
# scripts/generate_config.py). These are only generated artifacts inside the
# plugin's OWN repo; in a consuming project a hand-written .mcp.json is
# legitimate, so they must NOT be protected unconditionally.
PLUGIN_GENERATED_FILES = (
    ".mcp.json",
    "hooks/hooks.json",
)

def _is_plugin_repo(root: Path) -> bool:
    """True when ``root`` is the agent-os plugin's own repo — detected by the
    presence of the config template the installer generates .mcp.json from."""
    return (root / "templates" / "mcp.json.tmpl").is_file()

def is_generated_in_repo(rel: Path, root: Path) -> bool:
    """``is_generated`` plus the plugin-only generated launcher config.

    Inside the plugin's own repo, ``.mcp.json`` and ``hooks/hooks.json`` are
    generated from ``templates/`` by the installer, so hand-edits should be
    blocked (edit the template / generator instead). In a consuming repo those
    files may be authored by hand, so they are NOT protected there.
    """
    if is_generated(rel):
        return True
    if rel.as_posix() in PLUGIN_GENERATED_FILES and _is_plugin_repo(root):
        return True
    return False

def is_relevant_source(rel: Path) -> bool:
    if is_generated(rel):
        return False
    if any(part in IGNORED_PARTS for part in rel.parts):
        return False
    return rel.suffix.lower() in SOURCE_EXTENSIONS

def _ensure_agent_os_gitignore(agent_os_dir: Path) -> None:
    """Create .agent-os/.gitignore with a wildcard if it does not already exist.

    The wildcard makes git ignore everything under .agent-os/ (cards.sqlite,
    db/, hooks/) regardless of the project's root .gitignore.  Write is
    idempotent: an existing file is never overwritten.  Any OS error is
    swallowed so protection is best-effort and never breaks hook operations.
    """
    try:
        gitignore_path = agent_os_dir / ".gitignore"
        if not gitignore_path.exists():
            gitignore_path.write_text("*\n", encoding="utf-8")
    except OSError:
        pass


def state_dir(root: Path) -> Path:
    path = root / ".agent-os" / "hooks"
    path.mkdir(parents=True, exist_ok=True)
    _ensure_agent_os_gitignore(root / ".agent-os")
    return path

def dirty_path(root: Path) -> Path:
    return state_dir(root) / "graphify.dirty"

def fingerprint_path(root: Path) -> Path:
    return state_dir(root) / "git-state.sha256"

def lock_path(root: Path) -> Path:
    return state_dir(root) / "graphify.lock"

def log_path(root: Path) -> Path:
    return state_dir(root) / "hooks.log"

def log(root: Path, message: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with log_path(root).open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")

def mark_dirty(root: Path, reason: str) -> None:
    dirty_path(root).write_text(
        json.dumps({"marked_at": time.time(), "reason": reason}, indent=2),
        encoding="utf-8",
    )
    log(root, f"marked dirty: {reason}")

def git_state_fingerprint(root: Path) -> str:
    """
    Detect branch switches, commits, merges, rebases, worktree merges, staged
    changes, unstaged changes, additions, deletions, and renames.

    The status output is metadata only; file contents are not stored.
    """
    commands = [
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        ["git", "-C", str(root), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
    ]
    digest = hashlib.sha256()
    for command in commands:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=20,
            check=False,
        )
        digest.update(result.stdout)
        digest.update(b"\0")
    return digest.hexdigest()

def git_state_changed(root: Path) -> bool:
    current = git_state_fingerprint(root)
    path = fingerprint_path(root)
    previous = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    if current != previous:
        path.write_text(current, encoding="utf-8")
        return bool(previous)
    return False

def resolve_graphify() -> str:
    """Resolve the ``graphify`` console script in a venv-safe, cross-platform way.

    The plugin runs from an interpreter invoked by absolute path, so the venv's
    ``Scripts/`` (Windows) / ``bin/`` (POSIX) directory is NOT on ``PATH`` — a
    bare ``graphify`` fails to resolve there even when it is correctly installed
    (WINDOWS.md §2). Resolution order:

      1. ``AGENT_OS_GRAPHIFY_EXECUTABLE`` env override — used verbatim if set.
      2. A console script beside ``sys.executable`` — ``graphify.exe`` then
         ``graphify`` — so ``Scripts/graphify.exe`` and ``bin/graphify`` both
         resolve without touching ``PATH``.
      3. ``shutil.which('graphify')`` on ``PATH``.
      4. Fall back to the bare name ``"graphify"`` so the caller's existing
         ``FileNotFoundError`` handling surfaces a clear "not found" message.

    Returns a command token (absolute path or bare name); never raises.
    """
    override = os.getenv("AGENT_OS_GRAPHIFY_EXECUTABLE")
    if override:
        return override

    # Probe the interpreter's OWN directory first (raw, un-resolved). In a venv,
    # sys.executable is .venv/bin/python — itself a symlink — and pip installs the
    # ``graphify`` console script beside that symlink in .venv/bin. Calling
    # ``.resolve()`` first follows the symlink OUT of the venv (e.g. to the
    # pyenv/system bin the venv was built from), where the venv's graphify does
    # NOT exist, so the beside-interpreter probe misses and falls through to a
    # PATH lookup that also fails (WINDOWS.md §2; verified live on POSIX where
    # .venv/bin/python -> pyenv). We still probe the resolved parent second, which
    # covers a graphify installed beside the base interpreter (pyenv-installed, no
    # venv shim). Raw wins on ties so the venv's own console script is preferred.
    raw_dir = Path(sys.executable).parent
    resolved_dir = Path(sys.executable).resolve().parent
    probe_dirs = [raw_dir]
    if resolved_dir != raw_dir:
        probe_dirs.append(resolved_dir)
    for exe_dir in probe_dirs:
        for name in ("graphify.exe", "graphify"):
            candidate = exe_dir / name
            if candidate.exists():
                return str(candidate)

    found = shutil.which("graphify")
    if found:
        return found

    return "graphify"


def graphify_command() -> list[str]:
    executable = resolve_graphify()
    # Baseline incremental, code-only update. The `update <path>` subcommand
    # rebuilds graph.json without an LLM key; the older `. --update` form fell
    # into the semantic-extraction path, which errors out with no API key and
    # never rebuilds the graph. `--force` overwrites graph.json even when the
    # rebuild has fewer nodes — without it, refactors that delete code make
    # graphify refuse the write and the hook reports an error.
    command = [executable, "update", ".", "--force"]
    # Allow extra args without code changes, e.g.
    # AGENT_OS_GRAPHIFY_ARGS="--backend ollama" to enable semantic extraction.
    extra = os.getenv("AGENT_OS_GRAPHIFY_ARGS")
    if extra:
        command.extend(shlex.split(extra))
    return command

def refresh_graphify(root: Path, reason: str, timeout: int = 160) -> tuple[bool, str]:
    lock = lock_path(root)
    try:
        descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(descriptor)
    except FileExistsError:
        try:
            if time.time() - lock.stat().st_mtime > timeout + 30:
                lock.unlink(missing_ok=True)
            else:
                return True, "Graphify refresh already running"
        except OSError:
            return True, "Graphify refresh already running"

    try:
        # Force UTF-8 in the child (PYTHONIOENCODING) AND decode its pipes with
        # errors="replace": on a cp1252 Windows console graphify would otherwise
        # emit bytes the parent's strict UTF-8 decode rejects, turning a SUCCESSFUL
        # run into a UnicodeDecodeError that is misreported as "Graphify update
        # error" — leaving the dirty flag set so every subsequent hook re-fails
        # (WINDOWS.md §2/§3). Degraded-but-non-fatal output is always preferable.
        child_env = dict(os.environ)
        child_env.setdefault("PYTHONIOENCODING", "utf-8")
        result = subprocess.run(
            graphify_command(),
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            lowered = stderr.lower()
            # Expected, non-fatal: the corpus contains docs/images that need
            # semantic extraction but no LLM API key is configured. Treat this
            # as a clean no-op instead of emitting a failure on every hook
            # event. Configure a key (or AGENT_OS_GRAPHIFY_ARGS="--backend ...")
            # to enable semantic extraction.
            if "no llm api key" in lowered or "code-only corpus needs no key" in lowered:
                dirty_path(root).unlink(missing_ok=True)
                fingerprint_path(root).write_text(git_state_fingerprint(root), encoding="utf-8")
                message = (
                    f"Graphify semantic extraction skipped ({reason}): no LLM API key "
                    "configured. Set an API key or AGENT_OS_GRAPHIFY_ARGS to enable."
                )
                log(root, message)
                return True, message
            message = (
                f"Graphify baseline update failed ({reason}); "
                f"exit={result.returncode}; stderr={stderr}"
            )
            log(root, message)
            return False, message

        dirty_path(root).unlink(missing_ok=True)
        fingerprint_path(root).write_text(git_state_fingerprint(root), encoding="utf-8")
        message = f"Graphify baseline graph updated ({reason})"
        log(root, message)
        return True, message
    except FileNotFoundError:
        message = "Graphify executable not found"
        log(root, message)
        return False, message
    except subprocess.TimeoutExpired:
        message = f"Graphify update timed out after {timeout}s ({reason})"
        log(root, message)
        return False, message
    except Exception as exc:
        message = f"Graphify update error ({reason}): {exc}"
        log(root, message)
        return False, message
    finally:
        lock.unlink(missing_ok=True)
