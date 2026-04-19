"""Shaped-secret detectors. Category: `secret` → default disposition hard_block.

Only SHAPED secrets live here — patterns that look unambiguously like a
credential (prefixed API keys, key blocks, JWTs, etc.). Loose keyword rules
like `password = ...` are handled in pii.py under pii_adjacent since they can
fire on benign content.
"""
from __future__ import annotations

import re

from passport.detectors import Detector, redact_preview


# ─── Pattern table: (detector_id, label, compiled regex) ─────────────────────

_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "secret_openai_key",
        "openai-style api key",
        re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"),
    ),
    (
        "secret_aws_access_key",
        "aws access key id",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    (
        "secret_github_pat_old",
        "github personal access token (classic)",
        re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    ),
    (
        "secret_github_pat_new",
        "github fine-grained pat",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{80,}\b"),
    ),
    (
        "secret_slack_token",
        "slack token",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ),
    (
        "secret_private_key_pem",
        "private key pem block",
        re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "secret_jwt",
        "json web token",
        re.compile(r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),
    ),
    (
        "secret_bearer_token",
        "bearer token header",
        re.compile(r"\bBearer\s+[A-Za-z0-9_\-.=]{20,}\b"),
    ),
    (
        "secret_db_url_with_creds",
        "database url with inline credentials",
        re.compile(r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s:@/]+:[^\s@/]+@[^\s/]+"),
    ),
    (
        "secret_gcp_service_account",
        "gcp service account json marker",
        re.compile(r'"type"\s*:\s*"service_account"'),
    ),
    (
        "secret_dotenv_style",
        "dotenv-style secret assignment",
        re.compile(
            r"\b(?:API_KEY|SECRET|TOKEN|PASSWORD|PRIVATE_KEY|CLIENT_SECRET)"
            r"\s*=\s*[\"']?[A-Za-z0-9_\-./+=]{12,}[\"']?"
        ),
    ),
]


def _make_scanner(detector_id: str, label: str, pattern: re.Pattern[str]):
    def scan(text: str) -> list[dict]:
        out: list[dict] = []
        for m in pattern.finditer(text):
            out.append({
                "detector_id": detector_id,
                "category": "secret",
                "severity": "hard_block",
                "start": m.start(),
                "end": m.end(),
                "label": label,
                "match": redact_preview(text, m.start(), m.end()),
            })
        return out
    return scan


DETECTORS: list[Detector] = [
    Detector(
        detector_id=did,
        category="secret",
        default_severity="hard_block",
        scan_fn=_make_scanner(did, label, pat),
        description=label,
    )
    for did, label, pat in _PATTERNS
]
