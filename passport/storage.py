"""Filesystem storage for Passport Lane.

Data layout at $MNEMO_PASSPORT_DIR (default ~/.mnemo/passport):
    passport/passport_shared_behavior.yaml   # stable claims (Phase 1: one file)
    pending/observations.yaml                # pending queue
    audit/mutations.yaml                     # append-only log
    .git/                                    # auto-init'd on first use
"""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml


STABLE_FILENAME = "passport_shared_behavior.yaml"
PENDING_FILENAME = "observations.yaml"
AUDIT_FILENAME = "mutations.yaml"

STABLE_SUBDIR = "passport"
PENDING_SUBDIR = "pending"
AUDIT_SUBDIR = "audit"


def get_passport_dir() -> Path:
    raw = os.environ.get("MNEMO_PASSPORT_DIR")
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".mnemo" / "passport").resolve()


def stable_path() -> Path:
    return get_passport_dir() / STABLE_SUBDIR / STABLE_FILENAME


def pending_path() -> Path:
    return get_passport_dir() / PENDING_SUBDIR / PENDING_FILENAME


def audit_path() -> Path:
    return get_passport_dir() / AUDIT_SUBDIR / AUDIT_FILENAME


# ─── Layout bootstrap ───────────────────────────────────────────────────────

def ensure_layout() -> Path:
    """Create the dir tree + initial YAML stubs. Idempotent. Returns the root."""
    root = get_passport_dir()
    (root / STABLE_SUBDIR / "snapshots").mkdir(parents=True, exist_ok=True)
    (root / PENDING_SUBDIR).mkdir(parents=True, exist_ok=True)
    (root / AUDIT_SUBDIR).mkdir(parents=True, exist_ok=True)

    sp = stable_path()
    if not sp.exists():
        _atomic_yaml_write(sp, _initial_stable_doc())
    pp = pending_path()
    if not pp.exists():
        _atomic_yaml_write(pp, _initial_pending_doc())
    ap = audit_path()
    if not ap.exists():
        _atomic_yaml_write(ap, _initial_audit_doc())

    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(
            "# Passport Lane (Phase 1)\n\n"
            "This directory holds your portable working-style claims.\n"
            "- `passport/` — stable claims (auto-committed)\n"
            "- `pending/`  — candidate observations awaiting promotion\n"
            "- `audit/`    — append-only mutation log\n\n"
            "Never commits secrets. Never auto-pushes. Hand-edits are allowed\n"
            "between sessions (but avoid YAML comments — they won't survive\n"
            "round-trip in Phase 1).\n"
        )

    return root


def _initial_stable_doc() -> dict:
    return {
        "passport_version": "0.1",
        "passport_id": "guy-shared",
        "owner_id": "guy",
        "source_of_truth": "mnemo_local",
        "meta": {
            "description": "Portable working-style profile shared across environments.",
            "default_language": "en",
            "trust_mode": "evidence_required",
            "minimum_evidence_count": 2,
        },
        "stable_core": {
            "communication": [],
            "workflow": [],
        },
        "negative_constraints": [],
        "situational_overlays": [],
        "platform_adapters": {},
    }


def _initial_pending_doc() -> dict:
    return {
        "queue_version": "0.1",
        "owner_id": "guy",
        "next_counter": 1,
        "pending_observations": [],
    }


def _initial_audit_doc() -> dict:
    return {
        "audit_version": "0.1",
        "owner_id": "guy",
        "entries": [],
    }


# ─── Read / write helpers ───────────────────────────────────────────────────

def load_yaml(path: Path) -> dict:
    with path.open("r") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            data = yaml.safe_load(f)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return data or {}


def _atomic_yaml_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yaml.safe_dump(data, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    os.replace(tmp, path)


@contextmanager
def exclusive_lock(path: Path):
    """Exclusive advisory lock around a whole read-modify-write block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lockfile = path.with_suffix(path.suffix + ".lock")
    with lockfile.open("a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ─── Typed accessors ────────────────────────────────────────────────────────

def load_stable() -> dict:
    sp = stable_path()
    if not sp.exists():
        ensure_layout()
    return load_yaml(sp)


def save_stable(data: dict) -> None:
    _atomic_yaml_write(stable_path(), data)


def load_pending() -> dict:
    pp = pending_path()
    if not pp.exists():
        ensure_layout()
    return load_yaml(pp)


def save_pending(data: dict) -> None:
    _atomic_yaml_write(pending_path(), data)


def load_audit() -> dict:
    ap = audit_path()
    if not ap.exists():
        ensure_layout()
    return load_yaml(ap)


def save_audit(data: dict) -> None:
    _atomic_yaml_write(audit_path(), data)
