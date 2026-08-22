"""Repo checkout with a clone-once cache.

SWE-bench instances each specify a ``repo`` (owner/name) and a ``base_commit``.
To run the agent we need that repo checked out at ``base_commit`` in a clean state.
Cloning is expensive, so we clone each repo once into ``repo_cache/`` and then just
``checkout -f`` the required commit per instance.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Absolute so paths stay valid when subprocesses run with cwd=<repo> (the agent
# is launched inside the checked-out repo, where a relative path would break).
CACHE_DIR = Path("repo_cache").resolve()


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def ensure_repo(repo: str) -> Path:
    """Clone ``owner/name`` into the cache if absent; return its path."""
    CACHE_DIR.mkdir(exist_ok=True)
    dest = CACHE_DIR / repo.replace("/", "__")
    if not dest.exists():
        url = f"https://github.com/{repo}.git"
        _run(["git", "clone", "--quiet", url, str(dest)])
    return dest


def checkout(repo: str, base_commit: str) -> Path:
    """Return a repo path checked out clean at ``base_commit``."""
    path = ensure_repo(repo)
    # Discard any prior agent edits and untracked files, then pin the commit.
    _run(["git", "reset", "--hard", "--quiet"], cwd=path)
    _run(["git", "clean", "-fdxq"], cwd=path)
    try:
        _run(["git", "checkout", "-f", "--quiet", base_commit], cwd=path)
    except subprocess.CalledProcessError:
        # Commit may be newer than the shallow default; fetch it and retry.
        _run(["git", "fetch", "--quiet", "origin", base_commit], cwd=path)
        _run(["git", "checkout", "-f", "--quiet", base_commit], cwd=path)
    return path


def diff(repo_path: Path) -> str:
    """Unified diff of the agent's working-tree changes (the model_patch).

    Excludes untracked files by default (SWE-bench grades tracked-file edits).
    """
    out = subprocess.run(
        ["git", "diff"], cwd=repo_path, check=True, capture_output=True, text=True
    )
    return out.stdout
