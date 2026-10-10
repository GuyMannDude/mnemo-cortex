"""Dreamer LLM provider (2026-10-09): Claude API direct when ANTHROPIC_API_KEY
is set, else the old OpenRouter path with ONE loud warning.

HTTP is faked at httpx.post throughout: no test makes a live call.
"""
from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path

import pytest

_DREAM_PATH = Path(__file__).resolve().parent.parent / "mnemo-dream.py"


def _load(monkeypatch, anthropic_key: str | None, model: str | None = None):
    """A fresh module so the import-time provider/model selection is under test."""
    for name, value in (("ANTHROPIC_API_KEY", anthropic_key), ("MNEMO_DREAM_MODEL", model)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location("mnemo_dream_provider", _DREAM_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _reply(text: str, stop_reason: str = "end_turn", stop_details=None) -> _Resp:
    """A Messages API reply opening with an (omitted-display) thinking block,
    as Haiku 5.5's default adaptive thinking returns."""
    content = [{"type": "thinking", "thinking": "", "signature": "sig"}]
    if text:
        content.append({"type": "text", "text": text})
    return _Resp(200, {"content": content, "stop_reason": stop_reason,
                       "stop_details": stop_details,
                       "usage": {"input_tokens": 120, "output_tokens": 34}})


def _capture_post(monkeypatch, dream, responses):
    sent = []
    queue = list(responses)

    def fake_post(url, **kw):
        sent.append((url, kw))
        return queue.pop(0)

    monkeypatch.setattr(dream.httpx, "post", fake_post)
    return sent


def test_default_model_follows_the_provider(monkeypatch):
    assert _load(monkeypatch, "k").DREAM_MODEL == "claude-haiku-5-5"
    assert _load(monkeypatch, None).DREAM_MODEL == "google/gemini-2.5-flash"
    assert _load(monkeypatch, "k", model="claude-sonnet-5-5").DREAM_MODEL == "claude-sonnet-5-5"


def test_anthropic_request_shape_and_usage_mapping(monkeypatch):
    dream = _load(monkeypatch, "test-key")
    sent = _capture_post(monkeypatch, dream, [_reply("the brief")])
    text, usage = dream._call_llm("SYS", "USER", max_tokens=2048)

    assert text == "the brief"
    # The dream log reads prompt_tokens/completion_tokens; keep its cost line working.
    assert usage == {"prompt_tokens": 120, "completion_tokens": 34}
    url, kw = sent[0]
    assert url == "https://api.anthropic.com/v1/messages"
    assert kw["headers"]["x-api-key"] == "test-key"
    assert kw["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in kw["headers"]
    body = kw["json"]
    assert body["model"] == "claude-haiku-5-5"
    assert body["max_tokens"] == 2048
    assert body["system"] == "SYS"
    assert body["messages"] == [{"role": "user", "content": "USER"}]
    assert "temperature" not in body  # Haiku 5.5: non-default value = 400


def test_refusal_raises_and_screams(monkeypatch, caplog):
    dream = _load(monkeypatch, "k")
    details = {"type": "refusal", "category": "cyber", "explanation": None}
    _capture_post(monkeypatch, dream, [_reply("", "refusal", details)])
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="refusal"):
        dream._call_llm("s", "u")
    assert any('"category": "cyber"' in r.getMessage() for r in caplog.records)


def test_thinking_only_reply_is_the_200_but_empty_guard(monkeypatch):
    """Thinking can spend the whole budget: stop_reason=max_tokens, no text."""
    dream = _load(monkeypatch, "k")
    _capture_post(monkeypatch, dream, [_reply("", "max_tokens")])
    with pytest.raises(RuntimeError, match="200 but empty text"):
        dream._call_llm("s", "u")


def test_truncated_text_is_returned_with_a_warning(monkeypatch, caplog):
    dream = _load(monkeypatch, "k")
    _capture_post(monkeypatch, dream, [_reply("partial br", "max_tokens")])
    with caplog.at_level(logging.WARNING):
        text, _ = dream._call_llm("s", "u")
    assert text == "partial br"
    assert any("TRUNCATED" in r.getMessage() for r in caplog.records)


def test_prompt_too_long_400_halves_and_retries(monkeypatch):
    dream = _load(monkeypatch, "k")
    too_long = _Resp(400, {"type": "error", "error": {
        "type": "invalid_request_error",
        "message": "prompt is too long: 1048577 tokens > 1000000 maximum"}})
    sent = _capture_post(monkeypatch, dream, [too_long, _reply("ok")])
    content = "HEAD" + "q" * 60_000 + "TAIL"
    text, _ = dream._call_llm_adaptive("s", content, min_chars=20_000)

    assert text == "ok"
    second = sent[1][1]["json"]["messages"][0]["content"]
    assert len(second) == len(content) // 2
    assert second.endswith("TAIL")


def test_request_too_large_413_halves_and_retries(monkeypatch):
    dream = _load(monkeypatch, "k")
    too_big = _Resp(413, {"type": "error", "error": {
        "type": "request_too_large", "message": "Request exceeds the maximum allowed number of bytes."}})
    sent = _capture_post(monkeypatch, dream, [too_big, _reply("ok")])
    assert dream._call_llm_adaptive("s", "x" * 50_000)[0] == "ok"
    assert len(sent) == 2


def test_other_anthropic_error_is_not_retried(monkeypatch):
    dream = _load(monkeypatch, "k")
    overloaded = _Resp(529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
    sent = _capture_post(monkeypatch, dream, [overloaded])
    with pytest.raises(RuntimeError, match="Anthropic 529"):
        dream._call_llm_adaptive("s", "x" * 50_000)
    assert len(sent) == 1


def test_openrouter_fallback_warns_once_and_keeps_old_path(monkeypatch, caplog):
    dream = _load(monkeypatch, None)
    calls = []
    monkeypatch.setattr(dream, "_call_openrouter",
                        lambda s, u, max_tokens=4096: (calls.append(u), ("or", {}))[1])
    monkeypatch.setattr(dream, "_call_anthropic",
                        lambda *a, **k: pytest.fail("Anthropic path used without a key"))
    with caplog.at_level(logging.WARNING):
        dream._call_llm("s", "one")
        dream._call_llm("s", "two")
    assert calls == ["one", "two"]
    warnings = [r for r in caplog.records if "OpenRouter fallback" in r.getMessage()]
    assert len(warnings) == 1


def test_openrouter_path_unchanged(monkeypatch):
    """The fallback still sends today's OpenRouter request, temperature included."""
    dream = _load(monkeypatch, None)
    reply = _Resp(200, {"choices": [{"message": {"content": "hi"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 2}})
    sent = _capture_post(monkeypatch, dream, [reply])
    assert dream._call_llm("s", "u") == ("hi", {"prompt_tokens": 1, "completion_tokens": 2})
    url, kw = sent[0]
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert kw["json"]["model"] == "google/gemini-2.5-flash"
    assert kw["json"]["temperature"] == 0.3


def test_openrouter_model_id_with_anthropic_key_screams_and_uses_haiku(monkeypatch, caplog):
    dream = _load(monkeypatch, "k", model="google/gemini-2.5-flash")
    with caplog.at_level(logging.ERROR):
        dream._check_dream_model()
    assert dream.DREAM_MODEL == "claude-haiku-5-5"
    assert any("OpenRouter id" in r.getMessage() for r in caplog.records)


def test_transport_error_is_a_runtime_error(monkeypatch):
    """Per-stage isolation catches RuntimeError only: a timeout must cost one call."""
    dream = _load(monkeypatch, "k")

    def timeout(url, **kw):
        raise dream.httpx.ReadTimeout("read timed out")

    monkeypatch.setattr(dream.httpx, "post", timeout)
    with pytest.raises(RuntimeError, match="transport error: ReadTimeout"):
        dream._call_llm("s", "u")


def test_non_json_200_is_a_runtime_error(monkeypatch):
    dream = _load(monkeypatch, "k")

    class _Html(_Resp):
        def json(self):
            raise json.JSONDecodeError("Expecting value", "<html>", 0)

    _capture_post(monkeypatch, dream, [_Html(200, {})])
    with pytest.raises(RuntimeError, match="not JSON"):
        dream._call_llm("s", "u")


# ── Effort + caps (CC ruling 10-09: thinking spends from max_tokens) ──

def test_effort_is_sent_only_when_asked(monkeypatch):
    dream = _load(monkeypatch, "k")
    sent = _capture_post(monkeypatch, dream, [_reply("a"), _reply("b")])
    dream._call_llm("s", "u", max_tokens=1024, effort="low")
    dream._call_llm("s", "u")
    assert sent[0][1]["json"]["output_config"] == {"effort": "low"}
    assert "output_config" not in sent[1][1]["json"]  # model default (medium)


def test_openrouter_fallback_ignores_effort(monkeypatch):
    dream = _load(monkeypatch, None)
    reply = _Resp(200, {"choices": [{"message": {"content": "hi"}}], "usage": {}})
    sent = _capture_post(monkeypatch, dream, [reply])
    dream._call_llm("s", "u", effort="low")
    assert "output_config" not in sent[0][1]["json"]


def test_synthesis_calls_keep_default_effort_with_doubled_caps(monkeypatch, tmp_path):
    """Stage 1 per-agent brief 2048 -> 4096, stage 2 rollup 4096 -> 8192; no effort."""
    dream = _load(monkeypatch, "k")
    monkeypatch.setattr(dream, "_build_agent_section", lambda a, m: "section")
    monkeypatch.setattr(dream, "DREAM_DIR", tmp_path)
    monkeypatch.setattr(dream, "_validate_stated_lines", lambda payload: None)
    sent = _capture_post(monkeypatch, dream, [_reply("agent brief"), _reply("rollup")])
    dream.synthesize([{"agent_id": "cc", "summary": "did a thing"}])

    bodies = [kw["json"] for _, kw in sent]
    assert [b["system"] for b in bodies] == [dream.PER_AGENT_SYSTEM_PROMPT, dream.ROLLUP_SYSTEM_PROMPT]
    assert [b["max_tokens"] for b in bodies] == [4096, 8192]
    assert all("output_config" not in b for b in bodies)


def test_rollup_retry_cap_is_doubled_too(monkeypatch, tmp_path):
    dream = _load(monkeypatch, "k")
    monkeypatch.setattr(dream, "_build_agent_section", lambda a, m: "section")
    monkeypatch.setattr(dream, "DREAM_DIR", tmp_path)

    def validate(payload):
        if payload == "bad rollup":
            raise RuntimeError("stated-line validation failed:\nSUBJECT claim starts lowercase: cc")

    monkeypatch.setattr(dream, "_validate_stated_lines", validate)
    sent = _capture_post(monkeypatch, dream,
                         [_reply("agent brief"), _reply("bad rollup"), _reply("good rollup")])
    assert dream.synthesize([{"agent_id": "cc", "summary": "did a thing"}]) == "good rollup"
    assert [kw["json"]["max_tokens"] for _, kw in sent] == [4096, 8192, 8192]
    assert all("output_config" not in kw["json"] for _, kw in sent)


# ---------------------------------------------------------------------------
# Brief header stamp (snag-dream-brief-header-no-model-stamp, CC #4597): the
# brief an agent boots on names its provider, model, and whether any call
# was truncated, in the markdown header AND the dreamer JSON.
# ---------------------------------------------------------------------------

def _write_brief(monkeypatch, tmp_path, dream):
    """write_dream() into temp dirs; the bridge writeback is faked, never sent."""
    monkeypatch.setattr(dream, "DREAM_DIR", tmp_path / "dreams")
    monkeypatch.setattr(dream, "DREAMER_MEMORY_DIR", tmp_path / "dreamer")
    monkeypatch.setenv("MNEMO_RULEKEEPER_ADVISORY", str(tmp_path / "no-advisory.md"))
    monkeypatch.setattr(dream.httpx, "post", lambda url, **kw: _Resp(503, {}))
    since = dream.datetime(2026, 10, 9, tzinfo=dream.timezone.utc)
    dream_id = dream.write_dream("brief body", [{"agent_id": "cc"}], since)
    md = next((tmp_path / "dreams").glob("*[0-9].md")).read_text(encoding="utf-8")
    entry = json.loads((tmp_path / "dreamer" / f"{dream_id}.json").read_text(encoding="utf-8"))
    return md, entry


def _header(md: str) -> str:
    return md.split("\n---\n", 1)[0]


def test_brief_header_stamps_anthropic_provider_model_untruncated(monkeypatch, tmp_path):
    dream = _load(monkeypatch, "k")
    md, entry = _write_brief(monkeypatch, tmp_path, dream)
    assert "_Provider: anthropic · Model: claude-haiku-5-5 · Truncated: no_" in _header(md)
    assert (entry["provider"], entry["model"], entry["truncated"]) == (
        "anthropic", "claude-haiku-5-5", False)


def test_brief_header_stamps_openrouter_truncation_as_unchecked(monkeypatch, tmp_path):
    # The OpenRouter path never inspects finish_reason, so "no" would be a
    # claim nobody checked: the stamp says so instead.
    dream = _load(monkeypatch, None)
    md, entry = _write_brief(monkeypatch, tmp_path, dream)
    assert ("_Provider: openrouter · Model: google/gemini-2.5-flash · Truncated: unchecked_"
            in _header(md))
    assert (entry["provider"], entry["model"], entry["truncated"]) == (
        "openrouter", "google/gemini-2.5-flash", None)


def test_brief_header_truncated_flips_to_yes_after_a_max_tokens_call(monkeypatch, tmp_path):
    dream = _load(monkeypatch, "k")
    _capture_post(monkeypatch, dream, [_reply("whole", "end_turn"),
                                       _reply("partial br", "max_tokens"),
                                       _reply("whole again", "end_turn")])
    for _ in range(3):
        dream._call_llm("s", "u")
    md, entry = _write_brief(monkeypatch, tmp_path, dream)
    assert "· Truncated: yes_" in _header(md)
    assert entry["truncated"] is True


def test_thinking_only_max_tokens_reply_also_counts_as_truncated(monkeypatch):
    dream = _load(monkeypatch, "k")
    _capture_post(monkeypatch, dream, [_reply("", "max_tokens")])
    with pytest.raises(RuntimeError):
        dream._call_llm("s", "u")
    assert dream._brief_stamp()["truncated"] is True
