from __future__ import annotations

import hashlib
import json
import os
import shlex
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

def is_relevant_source(rel: Path) -> bool:
    if is_generated(rel):
        return False
    if any(part in IGNORED_PARTS for part in rel.parts):
        return False
    return rel.suffix.lower() in SOURCE_EXTENSIONS

def state_dir(root: Path) -> Path:
    path = root / ".agent-os" / "hooks"
    path.mkdir(parents=True, exist_ok=True)
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

def graphify_command(root: Path) -> list[str]:
    executable = os.getenv("AGENT_OS_GRAPHIFY_EXECUTABLE", "graphify")
    # Baseline incremental update only. No semantic/deep/force flags.
    command = [executable, ".", "--update"]
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
        result = subprocess.run(
            graphify_command(root),
            cwd=root,
            capture_output=True,
            text=True,
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
