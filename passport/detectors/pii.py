"""PII + PII-adjacent detectors.

Two categories feed the policy dispositions:
    pii_hard     → hard_block     (email, phone, ssn, credit card, dob)
    pii_soft     → local_only     (ipv4, ipv6, gps, street address)
    pii_adjacent → local_only     (employee id, customer id, mrn, badge, case)

Keyword-gated detectors (employee_id, mrn, etc.) only fire when the anchoring
keyword appears, to avoid flagging every number in prose.
"""
from __future__ import annotations

import re

from passport.detectors import Detector, redact_preview


# ─── Luhn helper for credit cards ───────────────────────────────────────────

def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ─── Core patterns ──────────────────────────────────────────────────────────

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(
    r"\b(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b"
)
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CC_CANDIDATE = re.compile(r"\b(?:\d[\s\-]?){13,19}\b")
_DOB_KEYWORD = re.compile(
    r"\b(?:DOB|dob|date\s+of\s+birth|born\s+on|birthday)\b[^\n]{0,40}?"
    r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)

_IPV4 = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"
)
_IPV6 = re.compile(
    r"\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}\b"
)
_GPS = re.compile(
    r"\b-?\d{1,3}\.\d{3,}\s*,\s*-?\d{1,3}\.\d{3,}\b"
)
_STREET_ADDRESS = re.compile(
    r"\b\d{1,5}\s+[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*\s+"
    r"(?:St(?:reet)?|Ave(?:nue)?|Rd|Road|Blvd|Boulevard|Ln|Lane|Dr|Drive|Ct|Court|Way)\b"
)

# Keyword-gated PII-adjacent. Group 1 is the value span.
_KEYWORD_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "pii_employee_id",
        "employee id",
        re.compile(r"\b(?:employee\s+id|emp\s+id|empid)\s*[:#]?\s*([A-Z0-9\-]{3,})", re.IGNORECASE),
    ),
    (
        "pii_customer_id",
        "customer id",
        re.compile(r"\b(?:customer\s+id|cust(?:omer)?\s*#)\s*[:#]?\s*([A-Z0-9\-]{3,})", re.IGNORECASE),
    ),
    (
        "pii_case_number",
        "case number",
        re.compile(r"\b(?:case\s+(?:no|number|#)|ticket\s+#)\s*[:#]?\s*([A-Z0-9\-]{3,})", re.IGNORECASE),
    ),
    (
        "pii_mrn",
        "medical record number",
        re.compile(r"\b(?:mrn|medical\s+record\s+(?:no|number|#))\s*[:#]?\s*([A-Z0-9\-]{3,})", re.IGNORECASE),
    ),
    (
        "pii_badge_number",
        "badge number",
        re.compile(r"\b(?:badge\s+(?:no|number|#))\s*[:#]?\s*([A-Z0-9\-]{3,})", re.IGNORECASE),
    ),
]


# ─── Scanners ───────────────────────────────────────────────────────────────

def _scan_email(text: str) -> list[dict]:
    out = []
    for m in _EMAIL.finditer(text):
        out.append({
            "detector_id": "pii_email", "category": "pii_hard",
            "severity": "hard_block", "start": m.start(), "end": m.end(),
            "label": "email address",
            "match": redact_preview(text, m.start(), m.end()),
        })
    return out


def _scan_phone(text: str) -> list[dict]:
    out = []
    for m in _PHONE.finditer(text):
        out.append({
            "detector_id": "pii_phone", "category": "pii_hard",
            "severity": "hard_block", "start": m.start(), "end": m.end(),
            "label": "phone number",
            "match": redact_preview(text, m.start(), m.end()),
        })
    return out


def _scan_ssn(text: str) -> list[dict]:
    out = []
    for m in _SSN.finditer(text):
        out.append({
            "detector_id": "pii_ssn_tin", "category": "pii_hard",
            "severity": "hard_block", "start": m.start(), "end": m.end(),
            "label": "ssn/tin",
            "match": redact_preview(text, m.start(), m.end()),
        })
    return out


def _scan_credit_card(text: str) -> list[dict]:
    out = []
    for m in _CC_CANDIDATE.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            out.append({
                "detector_id": "pii_credit_card_luhn", "category": "pii_hard",
                "severity": "hard_block", "start": m.start(), "end": m.end(),
                "label": "credit card (luhn valid)",
                "match": redact_preview(text, m.start(), m.end()),
            })
    return out


def _scan_dob(text: str) -> list[dict]:
    out = []
    for m in _DOB_KEYWORD.finditer(text):
        out.append({
            "detector_id": "pii_dob", "category": "pii_hard",
            "severity": "hard_block", "start": m.start(), "end": m.end(),
            "label": "date of birth",
            "match": redact_preview(text, m.start(), m.end()),
        })
    return out


def _scan_ipv4(text: str) -> list[dict]:
    out = []
    for m in _IPV4.finditer(text):
        out.append({
            "detector_id": "pii_ipv4", "category": "pii_soft",
            "severity": "local_only", "start": m.start(), "end": m.end(),
            "label": "ipv4 address",
            "match": redact_preview(text, m.start(), m.end()),
        })
    return out


def _scan_ipv6(text: str) -> list[dict]:
    out = []
    for m in _IPV6.finditer(text):
        out.append({
            "detector_id": "pii_ipv6", "category": "pii_soft",
            "severity": "local_only", "start": m.start(), "end": m.end(),
            "label": "ipv6 address",
            "match": redact_preview(text, m.start(), m.end()),
        })
    return out


def _scan_gps(text: str) -> list[dict]:
    out = []
    for m in _GPS.finditer(text):
        out.append({
            "detector_id": "pii_gps_coords", "category": "pii_soft",
            "severity": "local_only", "start": m.start(), "end": m.end(),
            "label": "gps coordinates",
            "match": redact_preview(text, m.start(), m.end()),
        })
    return out


def _scan_street(text: str) -> list[dict]:
    out = []
    for m in _STREET_ADDRESS.finditer(text):
        out.append({
            "detector_id": "pii_street_address", "category": "pii_soft",
            "severity": "local_only", "start": m.start(), "end": m.end(),
            "label": "street address",
            "match": redact_preview(text, m.start(), m.end()),
        })
    return out


def _make_keyword_scanner(detector_id: str, label: str, pattern: re.Pattern[str]):
    def scan(text: str) -> list[dict]:
        out = []
        for m in pattern.finditer(text):
            out.append({
                "detector_id": detector_id, "category": "pii_adjacent",
                "severity": "local_only", "start": m.start(), "end": m.end(),
                "label": label,
                "match": redact_preview(text, m.start(), m.end()),
            })
        return out
    return scan


# ─── Registry ───────────────────────────────────────────────────────────────

DETECTORS: list[Detector] = [
    Detector("pii_email", "pii_hard", "hard_block", _scan_email, "email address"),
    Detector("pii_phone", "pii_hard", "hard_block", _scan_phone, "phone number"),
    Detector("pii_ssn_tin", "pii_hard", "hard_block", _scan_ssn, "ssn/tin"),
    Detector("pii_credit_card_luhn", "pii_hard", "hard_block", _scan_credit_card, "credit card (luhn)"),
    Detector("pii_dob", "pii_hard", "hard_block", _scan_dob, "date of birth"),
    Detector("pii_ipv4", "pii_soft", "local_only", _scan_ipv4, "ipv4 address"),
    Detector("pii_ipv6", "pii_soft", "local_only", _scan_ipv6, "ipv6 address"),
    Detector("pii_gps_coords", "pii_soft", "local_only", _scan_gps, "gps coordinates"),
    Detector("pii_street_address", "pii_soft", "local_only", _scan_street, "street address"),
] + [
    Detector(did, "pii_adjacent", "local_only", _make_keyword_scanner(did, label, pat), label)
    for did, label, pat in _KEYWORD_PATTERNS
]
