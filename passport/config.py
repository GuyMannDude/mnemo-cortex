"""Passport Lane policy + detector configuration.

Loads four YAML files from $MNEMO_PASSPORT_DIR (default ~/.mnemo/passport):

    policy.yaml              — rules, dispositions, thresholds (repo-tracked friendly)
    detectors.yaml           — enabled detector IDs + severity overrides
    denylist.local.yaml      — Guy-owned private nouns (NEVER synced; gitignored)
    redaction_map.local.yaml — noun→category mappings (Guy-owned; gitignored)

If a file is missing, embedded defaults are used and a skeleton is written on
first access so Guy can edit by hand.

Authority: Opie's Phase 1.5 kickstart, AL's 3-pass design review.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from passport.storage import get_passport_dir


POLICY_FILENAME = "policy.yaml"
DETECTORS_FILENAME = "detectors.yaml"
DENYLIST_FILENAME = "denylist.local.yaml"
REDACTION_MAP_FILENAME = "redaction_map.local.yaml"


# ─── Embedded defaults ──────────────────────────────────────────────────────

DEFAULT_POLICY: dict[str, Any] = {
    "policy_version": "0.1",
    "rules": {
        "min_evidence_for_promote_shared": 2,
        "min_provenance_buckets_for_promote_shared": 2,
        "untrusted_alone_can_promote_shared": False,
        "untrusted_alone_can_promote_local": True,
    },
    # source_platform → provenance_bucket. Unknown values coerce to
    # "unknown_remote" (untrusted_web) with metadata_untrusted taint.
    "provenance_buckets": {
        "trusted_local": ["opie", "cc", "rocky", "cli"],
        "trusted_curated_import": ["al_chatgpt", "manual_paste", "curated_ai_import"],
        "semi_trusted_remote": ["gemini", "codex", "copilot"],
        "untrusted_web": ["chrome_extension", "web_page_dom", "unknown_remote"],
    },
    # Default disposition per bucket before detector overrides.
    "bucket_defaults": {
        "trusted_local": "allow",
        "trusted_curated_import": "allow",
        "semi_trusted_remote": "allow",
        "untrusted_web": "review_required",
    },
    # Category → disposition. Detectors carry a `category` which maps here.
    "dispositions": {
        "secret": "hard_block",
        "pii_hard": "hard_block",
        "pii_soft": "local_only",
        "pii_adjacent": "local_only",
        "private_dict": "review_required",
        "injection_in_claim": "hard_block",
        "injection_in_evidence": "review_required",
        "generic_fluff": "hard_block",
        "duplicate": "hard_block",
        "insufficient_evidence": "review_required",
    },
}


DEFAULT_DETECTORS: dict[str, Any] = {
    "detectors_version": "0.1",
    # If this list is empty, all built-in detectors are enabled. Listed IDs
    # narrow the active set. Use `overrides` to change severity per-detector.
    "enabled": [],
    "overrides": {},
}


DEFAULT_DENYLIST_SKELETON: dict[str, Any] = {
    # ---
    # denylist.local.yaml — your private nouns. NEVER synced. NEVER shared.
    # Add terms that should never leave your machine, even in aggregate.
    # Examples:
    #   clients: ["AcmeBank", "Hoffman Bedding"]
    #   projects: ["ProjectRed"]
    #   employer_internal_domains: ["corp.example.com"]
    #   repos: ["acme-private-monorepo"]
    #   workspaces: ["acme-slack-workspace"]
    #   family_names: []
    # ---
    "version": "0.1",
    "clients": [],
    "projects": [],
    "employer_internal_domains": [],
    "repos": [],
    "workspaces": [],
    "family_names": [],
}


DEFAULT_REDACTION_MAP_SKELETON: dict[str, Any] = {
    # ---
    # redaction_map.local.yaml — noun→category mappings for safe redaction.
    # When a denylist term appears in a claim that has a reusable behavioral
    # core, the validator substitutes the category label.
    # Example:
    #   AcmeBank: regulated audiences
    #   Hoffman Bedding: a retail client
    # Nothing here is auto-written — all entries are human-approved.
    # ---
    "version": "0.1",
    "mappings": {},
}


# ─── File I/O ───────────────────────────────────────────────────────────────

def _config_dir() -> Path:
    return get_passport_dir()


def _path(name: str) -> Path:
    return _config_dir() / name


def _load_or_seed(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """Load a YAML config. If missing, write the skeleton and return defaults."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            yaml.safe_dump(default, f, sort_keys=False, default_flow_style=False)
        return dict(default)
    with path.open("r") as f:
        data = yaml.safe_load(f)
    return data or dict(default)


# ─── Public API ─────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def load_policy() -> dict[str, Any]:
    return _load_or_seed(_path(POLICY_FILENAME), DEFAULT_POLICY)


@lru_cache(maxsize=1)
def load_detectors_config() -> dict[str, Any]:
    return _load_or_seed(_path(DETECTORS_FILENAME), DEFAULT_DETECTORS)


@lru_cache(maxsize=1)
def load_denylist() -> dict[str, Any]:
    return _load_or_seed(_path(DENYLIST_FILENAME), DEFAULT_DENYLIST_SKELETON)


@lru_cache(maxsize=1)
def load_redaction_map() -> dict[str, Any]:
    return _load_or_seed(_path(REDACTION_MAP_FILENAME), DEFAULT_REDACTION_MAP_SKELETON)


def reload() -> None:
    """Clear cached config. Call after editing YAMLs without restarting."""
    load_policy.cache_clear()
    load_detectors_config.cache_clear()
    load_denylist.cache_clear()
    load_redaction_map.cache_clear()


# ─── Bucket resolution ──────────────────────────────────────────────────────

def resolve_bucket(source_platform: str) -> tuple[str, bool]:
    """Map a source_platform string to its provenance bucket.

    Returns (bucket, metadata_untrusted). If the platform is unknown, the bucket
    is "untrusted_web" and metadata_untrusted=True so callers can flag the
    observation appropriately.
    """
    policy = load_policy()
    buckets: dict[str, list[str]] = policy.get("provenance_buckets", {})
    for bucket, platforms in buckets.items():
        if source_platform in platforms:
            return bucket, False
    return "untrusted_web", True


# Strength order for picking the "weakest" (most distrusted) bucket.
BUCKET_RANK = {
    "trusted_local": 4,
    "trusted_curated_import": 3,
    "semi_trusted_remote": 2,
    "untrusted_web": 1,
    "metadata_untrusted": 0,
}


def weakest_bucket(buckets: list[str]) -> str:
    if not buckets:
        return "untrusted_web"
    return min(buckets, key=lambda b: BUCKET_RANK.get(b, 0))
