"""
Mnemo Cortex — secret redaction at ingest (v4.1)
================================================
A memory system that remembers everything will also remember the API key you
accidentally printed. Two real key leaks in one week arrived through the
auto-capture pipeline (terminal output → JSONL sync → /writeback). This module
is the single choke point: every byte that enters the store via /writeback or
/ingest passes through redact_text() first.

Design rules learned the hard way:
  - Patterns must match REAL key shapes, not idealized ones. The Session-73
    rotation leak happened because a grep mask used `sk-or-[A-Za-z0-9]{20}`
    which does not match `sk-or-v1-…` (hyphen in the body). Every pattern here
    is tested against the actual shape of the credential it claims to catch.
  - Fail toward redaction: a redacted non-secret costs a few characters of
    context; an unredacted secret costs a key rotation across 9 config files.
  - Redaction is loud, never silent: callers receive a count and the server
    logs a warning naming the kinds found (never the values).

Replacement token: [REDACTED:<kind>] — greppable, and tells a future reader
what category of secret was removed without leaking entropy.
"""
from __future__ import annotations

import re

# Each entry: (kind, compiled pattern). Order matters only for overlapping
# matches (first pattern wins via the combined scan below); more specific
# prefixes go before generic ones.
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    # ── Vendor-prefixed API keys (high confidence — distinctive prefixes) ──
    # OpenRouter: sk-or-v1-<64 hex>, but tolerate future variants: sk-or- then
    # any run of key-ish chars INCLUDING hyphens (the Session-73 lesson).
    ("openrouter", re.compile(r"\bsk-or-[A-Za-z0-9-]{20,}")),
    # Anthropic: sk-ant-api03-… / sk-ant-… (body may contain - and _)
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    # OpenAI project/service keys, then classic sk- keys. The generic sk- form
    # requires 30+ chars to avoid eating prose like "sk-learn".
    ("openai", re.compile(r"\bsk-(?:proj|svcacct|None)-[A-Za-z0-9_-]{20,}")),
    ("openai", re.compile(r"\bsk-[A-Za-z0-9]{30,}")),
    # GitHub tokens: classic + fine-grained.
    ("github", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("github", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}")),
    # AWS access key id (distinctive 4-letter prefixes, 16 uppercase body).
    ("aws", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    # Google API key.
    ("google", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    # Slack tokens (bot/user/app/refresh) + webhook URLs.
    ("slack", re.compile(r"\bxox[abeprs]-[A-Za-z0-9-]{10,}")),
    ("slack-webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+")),
    # Discord bot tokens (base64-ish triplet) + webhook URLs.
    ("discord-webhook", re.compile(r"https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_-]{30,}")),
    # Stripe live/restricted keys (test keys too — they still authenticate).
    ("stripe", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{20,}")),
    # Tailscale auth keys.
    ("tailscale", re.compile(r"\btskey-[A-Za-z0-9-]{15,}")),
    # Hugging Face.
    ("huggingface", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    # npm automation tokens.
    ("npm", re.compile(r"\bnpm_[A-Za-z0-9]{30,}")),
    # Shopify tokens (admin/custom-app/storefront).
    ("shopify", re.compile(r"\bshp(?:at|ca|pa|ss)_[a-fA-F0-9]{20,}")),
    # ── Structural secrets ──
    # PEM private key blocks (multiline, the whole block goes).
    ("private-key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL)),
    # JWTs: three dot-separated base64url segments, first decodes to {"alg"….
    # eyJ is base64url for '{"' — distinctive enough combined with structure.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    # ── Generic assignment forms (lower confidence — require a long opaque
    #    value right after a credential-ish name to keep false positives low).
    #    The name may be prefixed (MNEMO_AUTH_TOKEN, SHOPIFY_API_KEY, …). ──
    ("generic-assignment", re.compile(
        r"""(?ix)\b
        [a-z0-9_-]*
        (api[_-]?key|api[_-]?secret|auth[_-]?token|access[_-]?token|
         secret[_-]?key|client[_-]?secret|webhook[_-]?secret|password|passwd)
        \s*[=:]\s*["']?
        (?P<val>[A-Za-z0-9_\-./+]{16,})["']?
        """)),
    # Bare credential names the alternation above misses (DISCORD_TOKEN=…,
    # SECRET: …, PRIVATE_KEY=…, MY_PASSPHRASE=…). Case-SENSITIVE uppercase on
    # purpose: lowercase `token = self.access_token` is ordinary code —
    # captured sessions are full of it — and redacting the right-hand side
    # would corrupt legitimate code memories (review catch). Uppercase names
    # are the env-var convention where real secrets actually live.
    ("env-credential", re.compile(
        r"""(?x)\b
        [A-Z0-9_]*
        (TOKEN|SECRET|PRIVATE[_-]?KEY|PASSPHRASE)
        \s*[=:]\s*["']?
        (?P<val>[A-Za-z0-9_\-./+]{16,})["']?
        """)),
    # Credentials embedded in connection URLs (postgres://user:pass@host,
    # amqp/redis/mongodb/… — any scheme). Only the password is redacted.
    ("url-credential", re.compile(
        r"(?i)\b[a-z][a-z0-9+.-]{1,30}://[^\s/:@]{1,64}:(?P<val>[^\s/@]{4,})@")),
    # Authorization headers pasted from curl/log output.
    ("bearer-token", re.compile(
        r"(?i)\bBearer\s+(?P<val>[A-Za-z0-9_\-./+=]{20,})")),
]

REPLACEMENT_FMT = "[REDACTED:{kind}]"

# Values that look secret-shaped to the generic-assignment pattern but are
# clearly not credentials (paths, placeholders, env-var references).
_GENERIC_VALUE_ALLOWLIST = re.compile(
    r"""(?ix)^(
        \$\{?[A-Z_]+\}? |          # ${ENV_VAR} / $ENV_VAR
        <[^>]+> |                  # <placeholder>
        x{8,} | \*{4,} |           # xxxxxxxx / ****
        (?:/[\w.-]+){2,} |         # /file/system/path
        \[REDACTED:[\w-]+\]        # already redacted
    )$""")


def redact_text(text: str) -> tuple[str, dict[str, int]]:
    """Redact secrets in `text`. Returns (clean_text, {kind: count}).

    Idempotent: running it over already-redacted text finds nothing new.
    """
    if not text:
        return text, {}
    found: dict[str, int] = {}
    for kind, pattern in SECRET_PATTERNS:
        if "val" in pattern.groupindex:
            # Value-capturing patterns: redact only the value, and skip values
            # that are clearly placeholders/paths (see allowlist).
            def _sub(m: re.Match, _kind=kind) -> str:
                val = m.group("val")
                if _GENERIC_VALUE_ALLOWLIST.match(val):
                    return m.group(0)
                found[_kind] = found.get(_kind, 0) + 1
                return m.group(0).replace(val, REPLACEMENT_FMT.format(kind=_kind))
            text = pattern.sub(_sub, text)
        else:
            text, n = pattern.subn(REPLACEMENT_FMT.format(kind=kind), text)
            if n:
                found[kind] = found.get(kind, 0) + n
    return text, found


def redact_obj(obj):
    """Recursively redact every string inside dicts/lists/strings.

    Returns (clean_obj, {kind: count}). Non-string leaves pass through
    untouched. Used for /ingest metadata, key_facts lists, etc.
    """
    totals: dict[str, int] = {}

    def _merge(counts: dict[str, int]) -> None:
        for k, v in counts.items():
            totals[k] = totals.get(k, 0) + v

    def _walk(node):
        if isinstance(node, str):
            clean, counts = redact_text(node)
            _merge(counts)
            return clean
        if isinstance(node, list):
            return [_walk(item) for item in node]
        if isinstance(node, dict):
            return {key: _walk(value) for key, value in node.items()}
        return node

    return _walk(obj), totals
