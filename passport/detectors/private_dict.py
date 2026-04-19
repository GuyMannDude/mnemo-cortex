"""Private-dictionary detectors driven by denylist.local.yaml.

Each denylist bucket becomes one detector:
    clients                    → private_client_term
    projects                   → private_project_term
    employer_internal_domains  → private_internal_domain
    repos                      → private_repo_name
    workspaces                 → private_workspace_name
    family_names               → private_family_name

Matches are case-insensitive, word-boundary aware. A detector returns zero
findings if its bucket is empty — this is the common case for a fresh install.
"""
from __future__ import annotations

import re

from passport import config
from passport.detectors import Detector


_BUCKETS: list[tuple[str, str, str]] = [
    ("clients", "private_client_term", "private client name"),
    ("projects", "private_project_term", "private project name"),
    ("employer_internal_domains", "private_internal_domain", "private internal domain"),
    ("repos", "private_repo_name", "private repo name"),
    ("workspaces", "private_workspace_name", "private workspace name"),
    ("family_names", "private_family_name", "private family name"),
]


def _compile(terms: list[str]) -> re.Pattern[str] | None:
    terms = [t for t in terms if isinstance(t, str) and t.strip()]
    if not terms:
        return None
    alts = "|".join(re.escape(t.strip()) for t in terms)
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.IGNORECASE)


def _make_scanner(bucket_key: str, detector_id: str, label: str):
    def scan(text: str) -> list[dict]:
        denylist = config.load_denylist() or {}
        terms = denylist.get(bucket_key) or []
        pattern = _compile(terms)
        if pattern is None:
            return []
        out = []
        for m in pattern.finditer(text):
            out.append({
                "detector_id": detector_id,
                "category": "private_dict",
                "severity": "review_required",
                "start": m.start(),
                "end": m.end(),
                "label": label,
                "match": m.group(),
                "term": m.group(),
                "bucket": bucket_key,
            })
        return out
    return scan


DETECTORS: list[Detector] = [
    Detector(
        detector_id=_det_id,
        category="private_dict",
        default_severity="review_required",
        scan_fn=_make_scanner(_bucket, _det_id, _label),
        description=_label,
    )
    for _bucket, _det_id, _label in _BUCKETS
]


# ─── Redaction helpers (used by validation.py) ──────────────────────────────

def try_redact(text: str) -> tuple[str, bool]:
    """Substitute denylist terms with their category from redaction_map.

    Returns (redacted_text, changed). If redaction_map has no mapping for a
    term, it stays verbatim; the caller decides if that still counts as safe.
    """
    denylist = config.load_denylist() or {}
    mapping = (config.load_redaction_map() or {}).get("mappings") or {}
    if not mapping:
        return text, False
    changed = False
    out = text
    for bucket, _det_id, _label in _BUCKETS:
        for term in denylist.get(bucket) or []:
            if not isinstance(term, str) or not term.strip():
                continue
            replacement = mapping.get(term)
            if not replacement:
                continue
            pat = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
            new_out, n = pat.subn(replacement, out)
            if n > 0:
                out = new_out
                changed = True
    return out, changed
