"""Git commit helper for Passport Lane.

Auto-stages all changes in the passport data dir and creates a commit.
Never pushes. Never force-resets. If the dir is not yet a Git repo, initializes it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from passport import storage


FALLBACK_NAME = "Mnemo Passport"
FALLBACK_EMAIL = "passport@mnemo.local"


def _run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def ensure_repo() -> Path:
    """Idempotent. Initialize the passport data dir as a Git repo if needed.

    Does NOT auto-commit the bootstrap state — we leave it for the first real
    `commit()` call so the first commit message reflects the caller's action
    rather than a generic "init layout" message that would consume the diff.
    """
    root = storage.ensure_layout()
    if not (root / ".git").exists():
        _run(["init", "-q", "-b", "main"], root)
    # Always ensure local identity is set (idempotent overwrites are fine).
    name = _run(["config", "--local", "--get", "user.name"], root, check=False)
    if name.returncode != 0:
        _run(["config", "--local", "user.name", FALLBACK_NAME], root)
    email = _run(["config", "--local", "--get", "user.email"], root, check=False)
    if email.returncode != 0:
        _run(["config", "--local", "user.email", FALLBACK_EMAIL], root)
    return root


def commit(action: str, claim_or_obs_id: str, description: str) -> str | None:
    """Stage all changes and commit. Returns the new commit SHA, or None if nothing changed."""
    root = ensure_repo()
    _run(["add", "-A"], root)
    status = _run(["status", "--porcelain"], root)
    if not status.stdout.strip():
        return None
    short_desc = description[:60].replace("\n", " ").strip()
    msg = f"passport: {action} {claim_or_obs_id} — {short_desc}"
    _run(["commit", "-q", "-m", msg], root)
    sha = _run(["rev-parse", "HEAD"], root).stdout.strip()
    return sha
