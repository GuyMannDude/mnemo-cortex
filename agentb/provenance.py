"""
Mnemo Cortex v3 — Provenance and decay.

Runtime helpers for tagging memories with source and category at write time
and surfacing stale_warning on aged records at read time. Lives next to the
storage layer; called from server.py on /writeback and /context.

Originally shipped 2026-05-13 in /home/guy/agentb-bridge/agentb_bridge.py.
Merged into the public open-source core 2026-05-16. See CHANGELOG v2.10.0.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Optional


VALID_SOURCES = {"user", "tool", "inferred", "brain", "migrated"}

VALID_CATEGORIES = {
    "topology",
    "current_state",
    "doctrine",
    "incident",
    "identity",
    "relationship",
    "decision",
    "session_log",
    "unknown",
}

# Decay thresholds (days). Override per-deployment via env. Perpetual
# categories (doctrine, incident, identity, decision) are absent on purpose
# and never return a stale_warning.
DECAY_THRESHOLDS = {
    "topology": {
        "warn": int(os.getenv("MNEMO_DECAY_TOPOLOGY_WARN_DAYS", "30")),
        "stale": int(os.getenv("MNEMO_DECAY_TOPOLOGY_STALE_DAYS", "90")),
    },
    "current_state": {
        "warn": int(os.getenv("MNEMO_DECAY_CURRENT_STATE_WARN_DAYS", "90")),
    },
    "relationship": {
        "warn": int(os.getenv("MNEMO_DECAY_RELATIONSHIP_WARN_DAYS", "180")),
    },
    "session_log": {
        "warn": int(os.getenv("MNEMO_DECAY_SESSION_LOG_WARN_DAYS", "90")),
    },
    "unknown": {
        "warn": int(os.getenv("MNEMO_DECAY_CURRENT_STATE_WARN_DAYS", "90")),
    },
}

# Categories hidden from default recall. Caller can opt back in via explicit
# `category=session_log` on a ContextRequest or by passing an empty list to
# `exclude_categories` to disable hiding entirely.
DEFAULT_HIDDEN_CATEGORIES = {"session_log"}

# Regex for write-time category auto-suggester. Single source of truth — used
# by both /writeback at runtime and by migration scripts that bulk-tag legacy
# records. Order matters: topology first (most operationally critical),
# decision before incident (decision verbs are more diagnostic than failure
# nouns), relationship narrowed to avoid bare-first-name false positives.
PROVENANCE_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "topology",
        re.compile(
            r"\b(port|host|running on|hostname|systemd|service|process|"
            r"gateway|listening on|\d+\.\d+\.\d+\.\d+|:[0-9]{4,5}\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "doctrine",
        re.compile(
            r"\b(guy said|guy wants|guy prefers|doctrine|principle|"
            r"convention|rule|policy)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "decision",
        re.compile(
            r"\b(decided|chose|picked|ruled out|because we|selected)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "incident",
        re.compile(
            r"\b(incident|crash|postmortem|regression|outage|"
            r"failure|broke|broken|bug)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "relationship",
        re.compile(
            r"\b(hoffman|customer|client|collaborator|merchant)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "identity",
        re.compile(
            r"\b(rocky|opie|cc|bw|bullwinkle|cliff|sparky|dave|"
            r"is the (ai )?assistant)\b",
            re.IGNORECASE,
        ),
    ),
]


def suggest_category(text: str) -> tuple[str, list[str]]:
    """Regex-based category suggester. Returns (category, matched_keywords).
    Falls back to ("unknown", []) when nothing matches."""
    if not text:
        return "unknown", []
    for category, pattern in PROVENANCE_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            keywords: list[str] = []
            seen: set[str] = set()
            for m in matches:
                kw = (m if isinstance(m, str) else m[0]).strip().lower()
                if kw and kw not in seen:
                    seen.add(kw)
                    keywords.append(kw)
            return category, keywords[:5]
    return "unknown", []


def compute_stale_warning(
    category: Optional[str], created_at: Optional[float]
) -> Optional[dict]:
    """Return a structured stale_warning dict if the record is past its
    category's warn threshold, else None. Perpetual categories (doctrine,
    incident, identity, decision) never return a warning."""
    if not category or category not in DECAY_THRESHOLDS:
        return None
    if not created_at:
        return None
    thresholds = DECAY_THRESHOLDS[category]
    warn_days = thresholds.get("warn")
    if not warn_days:
        return None

    age_seconds = time.time() - float(created_at)
    if age_seconds < 0:
        return None
    age_days = age_seconds / 86400.0
    if age_days < warn_days:
        return None

    stale_days = thresholds.get("stale", warn_days * 1.5)
    severity = "stale" if age_days >= stale_days else "warn"
    created_iso = datetime.fromtimestamp(
        float(created_at), tz=timezone.utc
    ).strftime("%Y-%m-%d")

    return {
        "category": category,
        "age_days": round(age_days, 1),
        "threshold_days": warn_days,
        "severity": severity,
        "message": (
            f"{category.upper()} fact from {created_iso} "
            f"({int(age_days)} days old). Verify with a tool call before acting."
        ),
    }
