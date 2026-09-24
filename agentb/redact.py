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

# Field names that hold a credential, matched against the WHOLE name
# (case-insensitive). Used for dict keys in redact_obj and for the JSON
# `"name": "value"` shape in redact_text (v4.25.3, inspection #1): the
# name-then-[=:] patterns below never see a JSON key next to its value --
# redact_obj walks them as separate strings, and in raw JSON the closing
# quote sits between the name and the colon. A bare `*_key` is too common in
# ordinary code (sort_key, primary_key, public_key) to redact case-blind, so
# the catch-all is UPPERCASE only -- the env-var convention, as in
# env-credential below.
_CREDENTIAL_NAME = (
    r"(?:(?i:[a-z0-9_.-]*(?:password|passwd|passphrase|secret|token|credentials?|"
    r"authorization|cookie|(?:api|access|secret|private|client|signing|encryption|master)"
    r"[_-]?key))|[A-Z0-9_.-]*_KEY)"
)
_CREDENTIAL_KEY_RE = re.compile(_CREDENTIAL_NAME)
# The same names at the start of a name-character run (v4.26.1): the
# lookbehind keeps a scan from restarting inside the run of name characters.
# Values stay linear too: each pattern below stops its value at a character
# that also ends the next restart's reach (review of the 4.26.1 draft: a
# value class that allowed `=` rescanned `password=password=…` to the end
# from every name, 12 s at 100k chars).
_ASSIGN_NAME = rf"(?<![A-Za-z0-9_.-]){_CREDENTIAL_NAME}"

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
    # PEM private key blocks (multiline, the whole block goes). v4.25.3
    # (inspection #2): a key with no END line (`head` of a key file, a
    # clipped tool_result) still goes -- the BEGIN line plus the body runs
    # after it: base64 runs that are long (16+), carry a digit/+/=, or have
    # a capital past the first letter (a short final line), and
    # `Proc-Type:`-style header lines, across whitespace or JSON-escaped
    # newlines. Prose after the key (plain words) and the closing quote of a
    # JSON string that carried it survive.
    ("private-key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
        r"(?:.*?-----END [A-Z ]*PRIVATE KEY-----"
        r"|(?:(?:\s|\\[nr])*(?:[A-Za-z0-9+/=]{16,}|[A-Za-z]*[0-9+/=][A-Za-z0-9+/=]*"
        r"|[A-Za-z][a-z]*[A-Z][A-Za-z]*|[A-Z][A-Za-z-]+:[^\n\\\"]*))*)",
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
    # Cookie / Set-Cookie headers (v4.26.1): the whole header value goes --
    # every Cookie pair is a credential, and a Set-Cookie's attributes carry
    # nothing worth the parsing. Needs name=value after the colon, so prose
    # about cookies survives. A quoted cookie value (RFC 6265) goes whole: a
    # quote right after `=` opens one. The cookie name has no `:`, so
    # `cookie:cookie:…` cannot rescan.
    ("cookie", re.compile(
        r"(?i)(?<![\w-])(?:set-)?cookie\s*:[ \t]*"
        r"(?P<val>[^\s=;,:\"'\\]+=(?:[^\r\n\"'\\]|(?<==)\"[^\"\r\n\\]*\")*)")),
    # Short secrets under a credential NAME (v4.26.1, snag-redactor-short-kv):
    # generic-assignment and env-credential need 16+ chars; a credential-
    # named key gets a floor of 6. Generic names keep 16. Two shapes only:
    #  - quoted literal, any spacing: password = "abc123" (escaped quotes too,
    #    as in raw JSONL text);
    #  - unquoted, no spaces, ending at whitespace / & ; / a quote / a
    #    backslash / end: env files, CLI flags, query strings. Most code
    #    keeps its right-hand sides: a kwarg ends at , or ), a call at (, an
    #    index at [, and `x = y` has spaces. NOT kept: a spaceless
    #    `token=self.access_token` (it is shaped exactly like a secret).
    #    `=` is not a value character (linear time), except as base64
    #    padding at the end.
    ("credential-assignment", re.compile(
        rf"{_ASSIGN_NAME}\s*=\s*\\?(?P<q>[\"'])"
        r"(?P<val>(?:(?!(?P=q))[^\\\n]){6,})\\?(?P=q)")),
    ("credential-assignment", re.compile(
        rf"{_ASSIGN_NAME}=(?![=\"'])"
        r"(?P<val>[^\s&;,=\"'\\()\[\]{}<>]{6,}+=*+)(?=[\s&;\"'\\<>]|$)")),
    # JSON credential field: "password": "…", "api_key": "…" (v4.25.3,
    # inspection #1). Any non-empty value -- the field name is the evidence.
    ("credential-field", re.compile(
        rf'"{_CREDENTIAL_NAME}"\s*:\s*"(?P<val>(?:[^"\\]|\\.)+)"')),
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
                # Splice by span: str.replace would hit the first copy of the
                # value, which can sit inside the NAME ("password": "pass").
                s, e = m.start("val") - m.start(), m.end("val") - m.start()
                return m.group(0)[:s] + REPLACEMENT_FMT.format(kind=_kind) + m.group(0)[e:]
            text = pattern.sub(_sub, text)
        else:
            text, n = pattern.subn(REPLACEMENT_FMT.format(kind=kind), text)
            if n:
                found[kind] = found.get(kind, 0) + n
    return text, found


def _is_credential_value(value) -> bool:
    """A leaf worth redacting under a credential-named key: a non-empty
    string that is not a placeholder/path/already-redacted, or a number."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    return (isinstance(value, str) and bool(value.strip())
            and not _GENERIC_VALUE_ALLOWLIST.match(value))


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
            # v4.25.1: KEYS too. A secret used as a dict key (a tool_use
            # input keyed by a token, an attachment map) used to pass
            # through untouched with counts == {} (review of 3138ba7).
            # v4.25.2: two secrets of one kind both redact to the same
            # key, and the later one overwrote the earlier value. A key
            # that lands on an occupied slot gets '#2', '#3', ... so every
            # value survives (review of 1717aeb).
            out: dict = {}
            for key, value in node.items():
                new_key = _walk(key) if isinstance(key, str) else key
                if new_key in out:
                    n = 2
                    while f"{new_key}#{n}" in out:
                        n += 1
                    new_key = f"{new_key}#{n}"
                if (isinstance(key, str) and _CREDENTIAL_KEY_RE.fullmatch(key)
                        and isinstance(value, list)):
                    # v4.26.1: a list under a credential name (Node gives
                    # set-cookie as an array) -- each scalar element goes.
                    clean = []
                    for item in value:
                        if _is_credential_value(item):
                            totals["credential-field"] = totals.get("credential-field", 0) + 1
                            clean.append(REPLACEMENT_FMT.format(kind="credential-field"))
                        else:
                            clean.append(_walk(item))
                    out[new_key] = clean
                    continue
                if (isinstance(key, str) and _CREDENTIAL_KEY_RE.fullmatch(key)
                        and _is_credential_value(value)):
                    # v4.25.3 (inspection #1): the key names a credential,
                    # so the value goes whatever its shape.
                    totals["credential-field"] = totals.get("credential-field", 0) + 1
                    out[new_key] = REPLACEMENT_FMT.format(kind="credential-field")
                    continue
                out[new_key] = _walk(value)
            return out
        return node

    return _walk(obj), totals
