"""v4.1 secret redaction — pattern coverage + server wiring.

The pattern tests use realistic FAKE key shapes (correct prefix + length,
random bodies). The sk-or-v1 case is the Session-73 regression: the old grep
mask `sk-or-[A-Za-z0-9]{20}` missed the hyphen in `v1-`, leaking two live keys
into a transcript. Every shape here must match the credential it claims to.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentb.redact import redact_text, redact_obj
from agentb.config import (
    AgentBConfig, ResilientProviderConfig, ProviderConfig,
    CacheConfig, ServerConfig, ClassificationConfig, DEFAULT_PERSONAS,
)

VEC = [0.0] * 768
VEC[0] = 1.0
_STATUS = {"primary": "fake", "active": "fake", "failed_over": False,
           "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}


class FakeEmbedding:
    active_label = "fake/embed"
    @property
    def status(self): return _STATUS
    async def embed(self, text, *, use_breaker=True, task_type="document"): return list(VEC)
    async def health_check(self): return True


class FakeReasoning:
    active_label = "fake/reason"
    @property
    def status(self): return _STATUS
    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True): return "decision"
    async def health_check(self): return True


@pytest.fixture
def client(tmp_path):
    cfg = AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50099),
        data_dir=str(tmp_path),
        classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS),
    )
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        with TestClient(create_app(cfg)) as c:
            yield c


# ── Pattern coverage (realistic fake shapes) ──
# Samples are assembled at RUNTIME from fragments. A full key-shaped literal
# in this file trips GitHub push protection and any other secret scanner —
# which is exactly the behavior these patterns exist to feed. (The first
# version of this file was blocked at push for precisely that reason.)

_B = "AbCdEfGh" + "IjKlMnOp" + "QrStUvWx" + "Yz012345"  # 32 opaque chars
_HEX = "9f86" + "d081" + "884c" + "7d65" + "9a2f" + "eaa0" + "c55a" + "d015"  # 32 hex

SECRET_SAMPLES = [
    # (kind, sample) — bodies are synthetic, prefixes/structure are real
    ("openrouter", "sk-or-" + "v1-" + _HEX + _HEX),
    ("openrouter", "sk-or-" + _B),
    ("anthropic", "sk-ant-" + "api03-" + _B + "_-" + _B[:8]),
    ("openai", "sk-proj-" + _B),
    ("openai", "sk-" + _B),
    ("github", "ghp_" + _B + "6789"),
    ("github", "github_pat_" + "11ABCDEFG0_" + _B + _B[:10]),
    ("aws", "AKIA" + "IOSFODNN" + "7EXAMPLE"),
    ("google", "AIza" + "SyA-" + _B + "9"),
    ("slack", "xoxb-" + "1234567890-" + "1234567890123-" + _B[:16]),
    ("stripe", "sk_live_" + _B),
    ("tailscale", "tskey-" + "auth-" + "kFGiAS5CNTRL-" + _B[:16]),
    ("huggingface", "hf_" + _B + "6789"),
    ("npm", "npm_" + _B + "6789"),
    ("shopify", "shpat_" + _HEX),
    ("jwt", "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + "." + _B + _B[:11]),
]


@pytest.mark.parametrize("kind,sample", SECRET_SAMPLES)
def test_redacts_known_key_shapes(kind, sample):
    text = f"oops, printed it: {sample} in the terminal"
    clean, counts = redact_text(text)
    assert sample not in clean, f"{kind} sample survived redaction"
    assert f"[REDACTED:{kind}]" in clean
    assert counts.get(kind, 0) >= 1


def test_redacts_pem_private_key_block():
    pem = ("-----BEGIN " + "OPENSSH PRIVATE KEY-----\n"
           + "b3Blbn" + "NzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMw\n"
           + "-----END " + "OPENSSH PRIVATE KEY-----")
    clean, counts = redact_text(f"key file contents:\n{pem}\ndone")
    assert "BEGIN OPENSSH" not in clean
    assert counts["private-key"] == 1


def test_redacts_generic_assignment():
    clean, counts = redact_text("set MNEMO_AUTH_TOKEN=Zx9kQp3vTn8wRy2mLb6cHd4f then restart")
    assert "Zx9kQp3vTn8wRy2mLb6cHd4f" not in clean
    assert counts["generic-assignment"] == 1


def test_generic_assignment_skips_placeholders_and_paths():
    for text in [
        "api_key=${OPENROUTER_API_KEY} from env",
        "password: <your-password-here>",
        "auth_token=/home/guy/.mnemo-auth-token",
        "api_key=xxxxxxxxxxxxxxxx",
    ]:
        clean, counts = redact_text(text)
        assert counts == {}, f"false positive on: {text}"
        assert clean == text


def test_clean_prose_untouched():
    text = ("Deployed mnemo-cortex v4.0.3 to artforge:50001. The sk-learn "
            "pipeline and the task-force notes are unaffected. Port 50060.")
    clean, counts = redact_text(text)
    assert clean == text
    assert counts == {}


def test_idempotent():
    sample = "sk-or-v1-" + "a1" * 32
    once, _ = redact_text(f"key {sample}")
    twice, counts = redact_text(once)
    assert twice == once
    assert counts == {}


def test_redact_obj_walks_nested_structures():
    obj = {
        "actions": [{"command": "export GH=ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "n": 3}],
        "note": "fine",
    }
    clean, counts = redact_obj(obj)
    assert "ghp_" not in json.dumps(clean)
    assert clean["actions"][0]["n"] == 3
    assert clean["note"] == "fine"
    assert counts["github"] == 1


# ── Server wiring ──

def test_writeback_redacts_before_storage(client, tmp_path):
    key = "sk-or-v1-" + "b2" * 32
    r = client.post("/writeback", json={
        "session_id": "leak-test",
        "summary": f"Rotated the OpenRouter key, new value {key} saved to USB.",
        "key_facts": [f"old key {key} revoked"],
        "category": "decision",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["redactions"] == 2
    mem_path = tmp_path / "agents" / "default" / "memory" / f"{body['memory_id']}.json"
    stored = mem_path.read_text()
    assert key not in stored
    assert "[REDACTED:openrouter]" in stored


def test_ingest_redacts_prompt_response_metadata(client, tmp_path):
    key = "sk-ant-api03-" + "c3" * 24
    r = client.post("/ingest", json={
        "prompt": f"here is the key: {key}",
        "response": "saved it",
        "metadata": {"actions": [f"echo {key}"]},
    })
    assert r.status_code == 200
    assert r.json()["redactions"] == 2
    hot = list((tmp_path / "agents" / "default" / "sessions" / "hot").glob("*.jsonl"))
    assert hot, "expected a hot session file"
    content = hot[0].read_text()
    assert key not in content
    assert "[REDACTED:anthropic]" in content


# ── v4.25.3: inspection #1 / #2 (code-inspection-2026-09-24-cc3.md) ──

def test_inspection_1_json_key_credential_redacted():
    """A credential NAME as a JSON key with its secret as the value: redact_obj
    walked key and value as separate strings, and the name-then-[=:] patterns
    never matched raw JSON (the closing quote sits before the colon)."""
    secret = "Zx9qR4tLm2Vb7Kp1Wd"
    obj = {"password": secret, "GITHUB_TOKEN": secret, "apiKey": "short",
           "SHOPIFY_KEY": secret, "secret_key": secret,
           "nested": [{"client_secret": secret, "Authorization": "Basic " + secret}]}
    clean, counts = redact_obj(obj)
    assert secret not in json.dumps(clean)
    assert "short" not in json.dumps(clean)
    assert counts == {"credential-field": 7}
    # Ordinary fields that merely contain the words stay untouched.
    keep = {"max_tokens": 1500, "tokenizer": "cl100k", "author": "guy",
            "password_hint": "the dog", "flag_token": True, "api_key": "",
            # ordinary code names: *_key is redacted only in UPPERCASE form
            "sort_key": "created_at", "primary_key": "id", "public_key": "ssh-ed25519 AAAA"}
    assert redact_obj(keep) == (keep, {})

    for raw in ('{"password": "%s"}' % secret, '{"password":"pass"}',
                '{"x-api-key": "%s", "n": 1}' % secret):
        out, found = redact_text(raw)
        assert found == {"credential-field": 1}, raw
        assert json.loads(out)  # still valid JSON — only the value went
        assert secret not in out and '"pass"' not in out
    # Control: the name=value form still redacts as before, and it is idempotent.
    assert redact_text("password=" + secret) == (
        "password=[REDACTED:generic-assignment]", {"generic-assignment": 1})
    once, _ = redact_text('{"password": "%s"}' % secret)
    assert redact_text(once) == (once, {})


def test_inspection_2_partial_pem_redacted():
    """A private key with no END line (head of a key file, a clipped
    tool_result) was stored raw: the pattern needed BEGIN…END."""
    body = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW"
    begin = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"   # split: push-guard
    out, found = redact_text(begin + "\n" + body + "\n" + body[:20])
    assert found == {"private-key": 1}
    assert body[:20] not in out and "BEGIN" not in out
    # Inside raw JSON (escaped newlines): the body goes, the JSON survives.
    line = json.dumps({"tool_result": begin + "\n" + body, "n": 1})
    out, found = redact_text(line)
    assert found == {"private-key": 1}
    assert json.loads(out) == {"tool_result": "[REDACTED:private-key]", "n": 1}
    # Prose after an unterminated key survives; only key-shaped runs go.
    out, _ = redact_text(begin + "\n" + body + "\nbmUAAAAE\n\nand then we asked about Tokyo.")
    assert out == "[REDACTED:private-key]\n\nand then we asked about Tokyo."
    # Control: a complete block still redacts to its END line only.
    full = begin + "\n" + body + "\n-----END " + "OPENSSH PRIVATE KEY-----\nafter."
    assert redact_text(full) == ("[REDACTED:private-key]\nafter.", {"private-key": 1})


# ── v4.26.1: short credential-named key=value + Cookie headers ──
# (snag-redactor-short-kv-secret-passes.md; batch 2 leftovers #4 + #3)

SHORT = "abc" + "123"   # six chars: under the 16-char generic floor


@pytest.mark.parametrize("text, secret", [
    ("password=" + SHORT, SHORT),
    ('password="' + SHORT + '"', SHORT),
    ("api_key=" + SHORT + "45", SHORT + "45"),
    ("PASSWORD=" + SHORT, SHORT),
    ("password = '" + SHORT + "'", SHORT),
    ("export DB_PASSWORD=hunt" + "er2 && run", "hunt" + "er2"),
    ("GITHUB_TOKEN=" + SHORT + "xyz\n", SHORT + "xyz"),
    ("https://x.test/cb?user=guy&token=" + SHORT + "&page=2", SHORT),
    ("mysql --password=" + SHORT + " -u root", SHORT),
    ("password=p@ss!" + "23", "p@ss!" + "23"),
    ("db.password=" + SHORT, SHORT),
    ("token=" + SHORT + "==", SHORT),            # base64 padding
])
def test_short_credential_named_assignment_redacted(text, secret):
    out, found = redact_text(text)
    assert secret not in out, text
    assert found == {"credential-assignment": 1}, (text, found)
    assert redact_text(out) == (out, {})   # idempotent


@pytest.mark.parametrize("text, kept", [
    ("Cookie: session=" + SHORT, "Cookie: "),
    ("Set-Cookie: sid=" + SHORT + "; Path=/; HttpOnly", "Set-Cookie: "),
    ('curl -H "Cookie: a=1; sid=' + SHORT + '" https://x.test', '" https://x.test'),
    ("> cookie: sid=" + SHORT + "\n< HTTP/1.1 200", "\n< HTTP/1.1 200"),
    ('Cookie: sid="' + SHORT + '"; b=2', "Cookie: "),         # RFC 6265 quoted
])
def test_cookie_header_redacted(text, kept):
    out, found = redact_text(text)
    assert SHORT not in out, text
    assert found == {"cookie": 1}, (text, found)
    assert kept in out
    assert redact_text(out) == (out, {})


def test_cookie_json_header_field_redacted():
    """A headers dict in a log line: the Cookie VALUE goes, JSON stays valid."""
    obj = {"headers": {"Cookie": "sid=" + SHORT, "Set-Cookie": "a=" + SHORT,
                       "Accept": "text/html"}}
    clean, counts = redact_obj(obj)
    assert SHORT not in json.dumps(clean)
    assert clean["headers"]["Accept"] == "text/html"
    out, found = redact_text(json.dumps(obj))
    assert SHORT not in out and json.loads(out)["headers"]["Accept"] == "text/html"
    # Node gives set-cookie as an array: every element goes.
    clean, counts = redact_obj({"set-cookie": ["sid=" + SHORT + "; Path=/", "b=" + SHORT],
                                "token": [None, True]})
    assert SHORT not in json.dumps(clean) and counts == {"credential-field": 2}
    assert clean["token"] == [None, True]


def test_short_secret_inside_raw_json_line():
    """Raw JSONL text: the quotes around the value arrive escaped."""
    line = json.dumps({"cmd": 'login password="' + SHORT + '" now', "n": 1})
    out, found = redact_text(line)
    assert SHORT not in out
    assert json.loads(out)["n"] == 1
    line = json.dumps({"cmd": "login password=" + SHORT + "\nnext"})
    out, _ = redact_text(line)
    assert SHORT not in out and json.loads(out)["cmd"].endswith("\nnext")


# The fails-closed pairing (doctrine-redaction-fails-closed): real-shaped lines
# that share the name=value shape and must come back byte-identical.
FALSE_POSITIVE_CORPUS = [
    "mode=ro", "sort_key=" + SHORT, "version=4.25.3", "branch=main",
    "immutable=1", "port=50001", "key=value", "timeout=5s", "tenant=cc",
    "model=claude-fable-5-1", "log_level=INFO", "GET /memories?page=2&sort=asc",
    "file:mnemo.db?mode=ro&immutable=1",
    # names that merely contain a credential word
    "max_tokens=4096", "num_tokens=123456", "password_hint=the_dog",
    "primary_key=user_id", "public_key=ssh-ed25519", "tokenizer=cl100k",
    "author=guy", "keyword=memory", "monkey=banana1",
    # code: the right-hand side is a variable, a call, or a comparison
    "if password==hashed_pw:", "token = self.access_token",
    "password = get_password()", "api_key=os.environ['API_KEY']",
    "token=tok.strip()", "password: str", "password: Optional[str] = None",
    "apiKey: string", "def f(password: str, token: str | None = None):",
    # placeholders, env references, too short to be a secret
    "password=${DB_PASSWORD}", "password=$DB_PASSWORD", "password=",
    'password=""', "password=abc", "api_key=<your-key>", "password=********",
    # prose about cookies (no name=value after the colon)
    "the cookie: chocolate chip", "Set-Cookie headers are not logged",
    "Cookie:", "cookies=enabled",
    # UPPERCASE *_KEY without a credential word (CC ruling I1, #3837)
    "SORT_KEY=created_at", "PRIMARY_KEY=user_id", "CACHE_KEY=memories",
    "PUBLIC_KEY=ssh-ed25519", "PARTITION_KEY=tenant", "FOREIGN_KEY=user_id",
    # a whole config block
    "[server]\nhost=0.0.0.0\nport=50001\nworkers=4\nlog_level=INFO\n",
]


@pytest.mark.parametrize("text", FALSE_POSITIVE_CORPUS)
def test_false_positive_corpus_byte_identical(text):
    assert redact_text(text) == (text, {})


def test_long_value_keeps_its_generic_kind():
    """The 16+ patterns still fire first: the kind a long secret reports is
    unchanged by 4.26.1."""
    long_secret = "Zx9qR4tLm2Vb7Kp1Wd"
    assert redact_text("password=" + long_secret) == (
        "password=[REDACTED:generic-assignment]", {"generic-assignment": 1})


def test_short_assignment_linear_on_long_name_runs():
    """No quadratic rescan: a 200k-char run of name characters is one pass."""
    import time
    for blob in ("a" * 200_000, "A_" * 100_000, "x.y-" * 50_000,
                 # rescans the review of the draft found (12 s / 8 s / 2 s)
                 "password=" * 20_000 + ",", "TOKEN=aaaaaaa|" * 14_000 + ",",
                 "cookie:" * 30_000, 'password="' * 20_000, "Cookie: a=" * 20_000):
        t0 = time.perf_counter()
        redact_text(blob)
        assert time.perf_counter() - t0 < 2.0, blob[:8]


# ── CC rulings #3837: *_KEY needs a credential word for the 6 floor (I1);
#    , and ) end a value instead of failing it open (D4) ──

@pytest.mark.parametrize("text, out", [
    ("API_KEY=" + SHORT, "API_KEY=[REDACTED:credential-assignment]"),
    ("SECRET_KEY=" + SHORT, "SECRET_KEY=[REDACTED:credential-assignment]"),
    ("PRIVATE_KEY=" + SHORT, "PRIVATE_KEY=[REDACTED:credential-assignment]"),
    ("session_key=" + SHORT, "session_key=[REDACTED:credential-assignment]"),
    ("MY_SERVICE_KEY=" + SHORT, "MY_SERVICE_KEY=[REDACTED:credential-assignment]"),
    ("Auth-Key=" + SHORT, "Auth-Key=[REDACTED:credential-assignment]"),
    # D4: a comma inside the value no longer fails the whole value open
    ("PASSWORD=abc," + "123", "PASSWORD=[REDACTED:credential-assignment]"),
    ("PASSWORD=abc," + "123,x9", "PASSWORD=[REDACTED:credential-assignment]"),
    ("PASSWORD=ab,," + "cd12", "PASSWORD=[REDACTED:credential-assignment]"),
    # D4: a kwarg / call argument ends at , or ) and is redacted (fail closed)
    ("f(password=" + SHORT + ", x=1)", "f(password=[REDACTED:credential-assignment], x=1)"),
    ("login(password=db_password)", "login(password=[REDACTED:credential-assignment])"),
    ("connect(host=h, password=pw_var1, port=5432)",
     "connect(host=h, password=[REDACTED:credential-assignment], port=5432)"),
    ("user=guy,token=" + SHORT + ",page=2", "user=guy,token=[REDACTED:credential-assignment],page=2"),
    # the next argument is a call / index / brace / comparison (light review)
    ("f(password=hunt" + "er22,g(x))", "f(password=[REDACTED:credential-assignment],g(x))"),
    ("f(token=" + SHORT + ",items[0])", "f(token=[REDACTED:credential-assignment],items[0])"),
    ("x(secret=" + SHORT + ",y{", "x(secret=[REDACTED:credential-assignment],y{"),
    ("token=" + SHORT + ",x==y", "token=[REDACTED:credential-assignment],x==y"),
])
def test_cc_rulings_3837_presence(text, out):
    assert redact_text(text) == (out, {"credential-assignment": 1})
    assert redact_text(out) == (out, {})


def test_bare_upper_key_keeps_the_16_floor():
    """I1: a bare *_KEY (no credential word) is not short-redacted, but a
    16+ value under it still goes (it did at f04e97d via the 6 floor)."""
    assert redact_text("SHOPIFY_KEY=abc123") == ("SHOPIFY_KEY=abc123", {})
    long_secret = "Zx9qR4tLm2Vb7Kp1Wd"
    out, found = redact_text("SHOPIFY_KEY=" + long_secret)
    assert long_secret not in out and sum(found.values()) == 1


def test_comma_values_stay_linear():
    import time
    for blob in ("password=a," * 20_000, "PASSWORD=" + "ab," * 60_000 + "(",
                 "token=" + "a" * 5 + "," * 100_000):
        t0 = time.perf_counter()
        redact_text(blob)
        assert time.perf_counter() - t0 < 2.0, blob[:12]


# ── v4.26.2: keys split by whitespace (snag-redactor-misses-whitespace-split-keys) ──
_G1, _G2 = "AIzaSyB1234567", "890abcdefghijklmnopqrstuv"   # 14 + 25 = 39, a Google shape once joined
assert len(_G1 + _G2) == 39


@pytest.mark.parametrize("text,out", [
    # the 09-24 leak: a file NAMED with the key, a space inside, echoed by ls
    (f"-rw-r--r-- 1 guy guy 290 Apr 14 13:43 {_G1} {_G2}.md\n",
     "-rw-r--r-- 1 guy guy 290 Apr 14 13:43 [REDACTED:google].md\n"),
    # a wrapped terminal line, and its JSON-escaped form in raw JSONL
    (f"key {_G1}\n{_G2} done", "key [REDACTED:google] done"),
    (f"key {_G1}\\n{_G2} done", "key [REDACTED:google] done"),
    ("token sk-ant-api03-" + "a1B" * 4 + "\n" + "c2D" * 7 + " next", "token [REDACTED:anthropic] next"),
    # prose after the key survives (a lowercase word ends the join)
    (f"{_G1} {_G2} and then some", "[REDACTED:google] and then some"),
    ("AIza" + "Q1w2e3r4t5" * 3 + "Zz9zz is my key", "[REDACTED:google] is my key"),
    # a LONG key wrapped mid-line: the first half is a key on its own, the
    # tail must go with it (review of the first draft)
    ("sk-ant-api03-" + "a1B" * 20 + "\n" + "c2D" * 16 + " next", "[REDACTED:anthropic] next"),
    # three fragments
    ("AIza" + "Q1w2e3r4t5" + " " + "Q1w2e3r4t5" + " " + "Q1w2e3r4t5Xy" + " ok", "[REDACTED:google] ok"),
    # a prefix in prose must not hide the key behind it
    (f"hf_ x {_G1} {_G2}", "hf_ x [REDACTED:google]"),
])
def test_split_vendor_key_redacted(text, out):
    clean, found = redact_text(text)
    assert clean == out
    assert sum(found.values()) == 1


@pytest.mark.parametrize("text", [
    "the AIza prefix marks a Google key",
    "hf_ models and npm_ tokens are prefixes",
    "sk-or- then nothing much",
    f"{_G1} short",                      # a half plus a word: 19 chars, not a key
    "[REDACTED-AIza-dYNs-20260925] marker text",   # CC2's marker shape
    # prose after a prefix (review of the first draft: words joined into a body)
    "sk-ant- keys are rotated automatically every quarter",
    "tskey- auth keys expire after ninety days",
    "xoxb- tokens are bot tokens here",
    "The sk-proj- prefix identifies project scoped credentials",
])
def test_split_scan_leaves_prose(text):
    assert redact_text(text) == (text, {})
