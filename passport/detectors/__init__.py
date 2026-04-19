"""Named-detector registry for Passport Lane validation.

Design: each detector declares its ID, category (mapped to a policy disposition),
and default severity. Patterns live in Python modules (secrets.py, pii.py,
private_dict.py, injection.py) — NOT in YAML. detectors.yaml only toggles which
detectors run and overrides severity.

A detector's `scan(text)` returns a list of Finding dicts:
    {
        "detector_id": "secret_openai_key",
        "category": "secret",
        "severity": "hard_block",
        "start": 12,
        "end": 54,
        "label": "openai-style api key",
        "match": "sk-...redacted..."  # short redacted preview, never raw
    }
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from passport import config


@dataclass
class Detector:
    detector_id: str
    category: str
    default_severity: str
    scan_fn: Callable[[str], list[dict]] = field(repr=False)
    description: str = ""

    def scan(self, text: str) -> list[dict]:
        if not text:
            return []
        return self.scan_fn(text)


def redact_preview(text: str, start: int, end: int, keep: int = 4) -> str:
    """Return a short preview that never leaks the full secret."""
    span = text[start:end]
    if len(span) <= keep * 2:
        return "***"
    return f"{span[:keep]}***{span[-keep:]}"


# ─── Registry assembly ──────────────────────────────────────────────────────
# Import detectors AFTER helpers are defined, because each detector module
# calls back into this module for Detector/redact_preview.

from passport.detectors import secrets as _secrets  # noqa: E402
from passport.detectors import pii as _pii          # noqa: E402
from passport.detectors import private_dict as _pd  # noqa: E402
from passport.detectors import injection as _inj    # noqa: E402


_ALL_DETECTORS: list[Detector] = (
    _secrets.DETECTORS
    + _pii.DETECTORS
    + _pd.DETECTORS
    + _inj.DETECTORS
)


def active_detectors() -> list[Detector]:
    """Resolve the active detector set from config.

    - If `detectors.yaml.enabled` is empty → all built-ins active.
    - If listed → only those IDs, in that order.
    - Severity overrides from `detectors.yaml.overrides` are applied here.
    """
    cfg = config.load_detectors_config()
    enabled: list[str] = cfg.get("enabled") or []
    overrides: dict[str, str] = cfg.get("overrides") or {}

    if enabled:
        picked = [d for d in _ALL_DETECTORS if d.detector_id in enabled]
    else:
        picked = list(_ALL_DETECTORS)

    if overrides:
        out = []
        for d in picked:
            sev = overrides.get(d.detector_id, d.default_severity)
            out.append(
                Detector(
                    detector_id=d.detector_id,
                    category=d.category,
                    default_severity=sev,
                    scan_fn=d.scan_fn,
                    description=d.description,
                )
            )
        return out
    return picked


def scan_text(text: str) -> list[dict]:
    """Run every active detector over `text`. Returns a flat list of findings."""
    findings: list[dict] = []
    for det in active_detectors():
        for f in det.scan(text):
            findings.append(f)
    return findings
