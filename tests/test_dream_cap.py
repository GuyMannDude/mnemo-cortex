"""Dreamer overflow guards — the per-agent section cap + adaptive-halving backstop.

These are the two defenses v4.2.1 added so a single high-volume agent (opie's
auto-capture once hit ~19MB / ~4.9M tokens) can't 400 the whole nightly run.
They are *insurance*: in normal nightly operation each window is ~24h and stays
well under the cap, so the cap never fires — which is exactly why it needs a
test rather than waiting for the next stuck-window incident to exercise it.

  - _build_agent_section: bounds one agent's brief to MAX_AGENT_SECTION_CHARS,
    recency-first (drop oldest), announcing the drop (never silent truncation).
  - _call_openrouter_adaptive: belt-and-suspenders for token-density spikes —
    halve the input and retry on a context-length 400 (incl. the provider-side
    400 OpenRouter wraps in a 200), keeping the most-recent tail.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

# The dreamer is a top-level script with a hyphen in its name — load it by path.
_DREAM_PATH = Path(__file__).resolve().parent.parent / "mnemo-dream.py"
_spec = importlib.util.spec_from_file_location("mnemo_dream", _DREAM_PATH)
dream = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dream)


def test_dream_prompts_require_stated_line_grammar():
    for prompt in (dream.PER_AGENT_SYSTEM_PROMPT, dream.ROLLUP_SYSTEM_PROMPT):
        assert "explicit subject" in prompt
        assert " · " in prompt
        assert "no shorthand" in prompt
        assert "parentheticals" in prompt
        # First live night of the gate: source data names agents in lowercase
        # ids and the validator rejects lowercase subjects — the prompt must
        # bridge that seam explicitly.
        assert "capitalized subject" in prompt
        assert "CC, Cody, Opie, Rocky, Dave" in prompt


def test_stated_line_gate_pipes_utf8_bytes(monkeypatch, tmp_path):
    checker = tmp_path / "tools" / "stated-line-check.py"
    checker.parent.mkdir()
    checker.write_text("# test placeholder", encoding="utf-8")
    monkeypatch.setenv("BRAIN_DIR", str(tmp_path))
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["input"] = kwargs["input"]
        return SimpleNamespace(returncode=0, stdout=b"1 stated line(s)\nAll stated lines conform.")

    monkeypatch.setattr(dream.subprocess, "run", fake_run)
    dream._validate_stated_lines("Mnemo shipped UTF-8 support · Cody · 2026-08-20 · shipped")
    assert seen["argv"][-2:] == ["--check", "-"]
    assert isinstance(seen["input"], bytes)
    assert "·" in seen["input"].decode("utf-8")


@pytest.mark.parametrize("returncode, output", [
    (1, b"FIELDS violation"),
    (0, b"note: ZERO stated lines found -- nothing was validated here"),
])
def test_stated_line_gate_fails_closed(monkeypatch, tmp_path, returncode, output):
    checker = tmp_path / "tools" / "stated-line-check.py"
    checker.parent.mkdir()
    checker.write_text("# test placeholder", encoding="utf-8")
    monkeypatch.setenv("BRAIN_DIR", str(tmp_path))
    monkeypatch.setattr(
        dream.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=returncode, stdout=output),
    )
    with pytest.raises(RuntimeError, match="stated-line validation"):
        dream._validate_stated_lines("bad payload")


def _mem(i: int, summary: str) -> dict:
    """One AgentB-shaped memory entry, timestamp increasing with i (newest = highest i)."""
    return {
        "timestamp": f"2026-06-{i + 1:02d}T03:00:00+00:00",
        "session_id": f"sess-{i}",
        "summary": summary,
        "key_facts": [],
    }


# ── _build_agent_section: the cap ──

def test_section_caps_oversized_input(monkeypatch):
    """10 entries × ~2KB each = ~20KB; a 5KB cap must bound the section."""
    monkeypatch.setattr(dream, "MAX_AGENT_SECTION_CHARS", 5_000)
    mems = [_mem(i, f"<<E{i}>>" + "z" * 2_000) for i in range(10)]

    section = dream._build_agent_section("opie", mems)

    # Body (everything the cap governs) stays within budget; header + newlines
    # are the only slack, and they're tiny.
    assert len(section) <= 5_000 + 500, f"section not capped: {len(section):,} chars"
    assert "omitted to fit" in section, "the drop must be announced, never silent"


def test_section_keeps_most_recent_drops_oldest(monkeypatch):
    """Recency-first: 'since last dream' cares about the newest entries."""
    monkeypatch.setattr(dream, "MAX_AGENT_SECTION_CHARS", 5_000)
    mems = [_mem(i, f"<<E{i}>>" + "z" * 2_000) for i in range(10)]

    section = dream._build_agent_section("opie", mems)

    assert "<<E9>>" in section, "newest entry must survive the cap"
    assert "<<E8>>" in section, "second-newest entry must survive the cap"
    assert "<<E0>>" not in section, "oldest entry must be dropped first"


def test_section_no_cap_when_under_budget(monkeypatch):
    """Under budget → every entry kept, no 'omitted' notice."""
    monkeypatch.setattr(dream, "MAX_AGENT_SECTION_CHARS", 1_000_000)
    mems = [_mem(i, f"<<E{i}>> small entry") for i in range(5)]

    section = dream._build_agent_section("cc", mems)

    assert "omitted" not in section
    for i in range(5):
        assert f"<<E{i}>>" in section
    assert "# Agent: cc (5 entries)" in section


# ── _call_openrouter_adaptive: the halving backstop ──

def _big_content() -> str:
    """200KB with distinct head/tail markers so we can prove the tail is kept."""
    return "HEAD-MARKER" + "q" * (200_000 - 22) + "TAIL-MARKER"


def test_adaptive_halves_until_under_limit(monkeypatch):
    """Oversize 400 → halve + retry; succeed once small enough, keeping the tail."""
    seen: list[int] = []
    final = {}

    def fake_call(system, content, max_tokens=4096):
        seen.append(len(content))
        if len(content) > 60_000:
            raise RuntimeError(
                "OpenRouter 400: This endpoint's maximum context length is "
                "1048576 tokens. However, you requested about 4926022 tokens."
            )
        final["content"] = content
        return "synthesized brief", {"prompt_tokens": 100}

    monkeypatch.setattr(dream, "_call_openrouter", fake_call)
    out, usage = dream._call_openrouter_adaptive("sys", _big_content(), max_tokens=2048)

    assert out == "synthesized brief"
    assert seen == [200_000, 100_000, 50_000], f"unexpected halving path: {seen}"
    assert final["content"].endswith("TAIL-MARKER"), "must keep the most-recent tail"
    assert "HEAD-MARKER" not in final["content"], "oldest head should be dropped on halving"


def test_adaptive_retries_on_200_wrapped_400(monkeypatch):
    """OpenRouter's provider-side 400 wrapped in a 200 ('no choices') is oversize too."""
    calls = {"n": 0}

    def fake_call(system, content, max_tokens=4096):
        calls["n"] += 1
        if len(content) > 60_000:
            raise RuntimeError('OpenRouter 200 but no choices: {"error": {"code": 400}}')
        return "ok", {}

    monkeypatch.setattr(dream, "_call_openrouter", fake_call)
    out, _ = dream._call_openrouter_adaptive("sys", _big_content())

    assert out == "ok"
    assert calls["n"] > 1, "the 200-wrapped-400 must trigger a smaller retry"


def test_adaptive_reraises_non_size_error(monkeypatch):
    """A non-size failure must propagate immediately — no pointless shrinking."""
    calls = {"n": 0}

    def fake_call(system, content, max_tokens=4096):
        calls["n"] += 1
        raise RuntimeError("network exploded")

    monkeypatch.setattr(dream, "_call_openrouter", fake_call)
    with pytest.raises(RuntimeError, match="network exploded"):
        dream._call_openrouter_adaptive("sys", _big_content())
    assert calls["n"] == 1, "must not retry on a non-size error"


def test_adaptive_gives_up_at_min_chars(monkeypatch):
    """Already at/under min_chars and still oversize → raise, don't loop forever."""
    calls = {"n": 0}

    def fake_call(system, content, max_tokens=4096):
        calls["n"] += 1
        raise RuntimeError("maximum context length exceeded")

    monkeypatch.setattr(dream, "_call_openrouter", fake_call)
    with pytest.raises(RuntimeError, match="maximum context"):
        dream._call_openrouter_adaptive("sys", "x" * 10_000, min_chars=20_000)
    assert calls["n"] == 1, "content below min_chars must not be halved again"


# ── Stage 0.5 fact extraction: chunking (the 2026-06-13 fix) ──
#
# The bug: one big batch (cc's 165-entry / 64K-char day) was sent in a single
# call capped at max_tokens=4096 output. The fact array overran the output cap,
# truncated mid-string, json.loads failed, and the WHOLE agent's facts were lost.
# Fix: chunk by input chars so each call's output fits, and isolate a parse
# failure to one chunk instead of the whole agent.

def test_chunk_splits_over_budget():
    """Input above the chunk budget is split into >1 chunk; nothing is dropped."""
    mems = [_mem(i, "z" * 1_000) for i in range(10)]
    chunks = dream._chunk_memories_by_chars(mems, budget=3_000)
    assert len(chunks) > 1, "oversized input must be chunked"
    assert sum(len(c) for c in chunks) == 10, "chunking must not drop entries"


def test_chunk_single_when_under_budget():
    mems = [_mem(i, "small") for i in range(5)]
    chunks = dream._chunk_memories_by_chars(mems, budget=1_000_000)
    assert len(chunks) == 1 and len(chunks[0]) == 5


def test_chunk_oversized_single_memory_becomes_own_chunk():
    """A lone memory bigger than the budget is its own chunk — never silently dropped."""
    mems = [_mem(0, "z" * 50_000)]
    chunks = dream._chunk_memories_by_chars(mems, budget=10_000)
    assert len(chunks) == 1 and len(chunks[0]) == 1


def test_chunk_preserves_chronological_order():
    mems = [_mem(i, f"<<E{i}>>" + "z" * 1_000) for i in range(6)]
    chunks = dream._chunk_memories_by_chars(mems, budget=2_500)
    flat = [m for c in chunks for m in c]
    ts = [m["timestamp"] for m in flat]
    assert ts == sorted(ts), "chronological order must survive chunking"


def test_extract_chunks_big_input(monkeypatch):
    """Big input → multiple extraction calls (the fix). Mutation: disable chunking
    (huge budget) and this drops to 1 call, failing the assertion — a real guard."""
    monkeypatch.setattr(dream, "FACT_EXTRACTION_CHUNK_CHARS", 3_000)
    calls: list[str] = []

    def fake(agent_id, section, label=""):
        calls.append(label)
        return [{"entity": "e", "attribute": "a", "value": "v", "evidence_source": "t"}]

    monkeypatch.setattr(dream, "_extract_facts_from_section", fake)
    mems = [_mem(i, "z" * 1_000) for i in range(10)]

    facts = dream.extract_facts_for_agent("cc", mems)

    assert len(calls) > 1, "a large day must be split into multiple LLM calls"
    assert len(facts) == len(calls), "one fact per successful chunk, accumulated"


def test_extract_one_bad_chunk_does_not_drop_agent(monkeypatch):
    """A parse failure in one chunk keeps every other chunk's facts — the exact
    regression from 2026-06-13 where one truncation lost all of cc's facts."""
    monkeypatch.setattr(dream, "FACT_EXTRACTION_CHUNK_CHARS", 3_000)
    seen: list[str] = []

    def fake(agent_id, section, label=""):
        seen.append(label)
        if len(seen) == 2:  # second chunk fails to parse
            return None
        return [{"entity": "e", "attribute": "a", "value": "v", "evidence_source": "t"}]

    monkeypatch.setattr(dream, "_extract_facts_from_section", fake)
    mems = [_mem(i, "z" * 1_000) for i in range(10)]

    facts = dream.extract_facts_for_agent("cc", mems)

    assert len(seen) >= 3, "expected several chunks"
    assert len(facts) == len(seen) - 1, "one bad chunk must not zero out the agent"


# ── Stage 0.5 fact extraction: salvage truncated arrays (the 2026-06-14 fix) ──
#
# Follow-up to the chunking fix: 20K-char input chunks STILL overran the 4096-token
# output cap (truncated at output char ~10-13K). The v4.2.2 chunking kept one bad
# chunk from dropping the whole agent, but a truncated chunk still lost ALL its facts
# — including the complete objects before the cut. _parse_fact_array recovers them.

def test_salvage_clean_array_not_flagged():
    raw = '[{"entity":"a","attribute":"b","value":"v1"},{"entity":"c","attribute":"d","value":"v2"}]'
    facts, salvaged = dream._parse_fact_array(raw)
    assert salvaged is False and len(facts) == 2


def test_salvage_truncated_string_keeps_complete_objects():
    """The exact 2026-06-14 shape: array truncated mid-string ('Unterminated string').
    Plain json.loads would lose all 3; salvage keeps the 2 complete objects."""
    raw = ('[\n {"entity":"a","attribute":"b","value":"v1"},\n'
           ' {"entity":"c","attribute":"d","value":"v2"},\n'
           ' {"entity":"e","attribute":"f","value":"unterminated stri')
    facts, salvaged = dream._parse_fact_array(raw)
    assert salvaged is True
    assert [f["value"] for f in facts] == ["v1", "v2"]


def test_salvage_truncated_after_comma_keeps_complete_objects():
    """The other 2026-06-14 shape: cut right after a comma ('Expecting property name')."""
    raw = ('[{"entity":"a","attribute":"b","value":"v1"},'
           '{"entity":"c","attribute":"d","value":"v2"},{')
    facts, salvaged = dream._parse_fact_array(raw)
    assert salvaged is True and len(facts) == 2


def test_salvage_bare_object_wrapped():
    """A lone object (not an array) is wrapped, not lost."""
    facts, salvaged = dream._parse_fact_array('{"entity":"x","attribute":"y","value":"z"}')
    assert salvaged is False and len(facts) == 1


def test_salvage_unrecoverable_returns_empty():
    facts, salvaged = dream._parse_fact_array("I could not find any facts.")
    assert salvaged is True and facts == []


def test_salvage_skips_stray_preamble_bracket():
    """A '[' in LLM preamble before a truncated array must not mis-anchor the scan
    and discard recoverable facts — the salvage must skip to the real array."""
    raw = ('Facts [extracted] from session:\n'
           '[{"entity":"a","attribute":"b","value":"v1"},'
           '{"entity":"c","attribute":"d","value":"v2"},{"entity":"e","attr')
    facts, salvaged = dream._parse_fact_array(raw)
    assert salvaged is True
    assert [f["value"] for f in facts] == ["v1", "v2"]


def test_salvage_empty_array_is_clean():
    facts, salvaged = dream._parse_fact_array("[]")
    assert salvaged is False and facts == []


def test_extract_section_salvages_truncated_call(monkeypatch):
    """End-to-end: a truncated LLM response yields the complete facts, not None."""
    truncated = ('[{"entity":"a","attribute":"b","value":"v1"},'
                 '{"entity":"c","attribute":"d","value":"v2"},{"entity":"e","attr')
    monkeypatch.setattr(dream, "_call_openrouter_adaptive", lambda *a, **k: (truncated, {}))
    facts = dream._extract_facts_from_section("cc", "section", label=" chunk 1/4")
    assert facts is not None
    assert [f["value"] for f in facts] == ["v1", "v2"]


def test_boot_budget_is_read_from_canonical_bridge_file():
    assert dream._boot_budget("dream") == 3500


def test_boot_dream_prioritizes_changed_then_open_and_names_drops():
    source = (
        "# Narrative\n\n" + ("n" * 100) + "\n\n"
        "# What was built or shipped\n\nnewest change\n\n"
        "# What's blocked or pending\n\nopen item\n"
    )
    result = dream._compose_boot_dream(source, 150)
    assert result.startswith("# What was built or shipped")
    assert "# What's blocked or pending" in result
    assert result.endswith("DROPPED: Narrative")
    assert len(result) <= 150


def test_boot_dream_always_states_when_nothing_dropped():
    result = dream._compose_boot_dream("# What changed\n\nsmall", 200)
    assert result.endswith("DROPPED: nothing")


def test_boot_dream_preserves_advisory_preamble_at_highest_priority():
    source = "ADVISORY PREAMBLE with no heading\n\n---\n\n# What was built\n\nbody"
    result = dream._compose_boot_dream(source, 500)
    assert result.startswith("ADVISORY PREAMBLE")
    assert result.endswith("DROPPED: nothing")


def test_boot_dream_budget_is_utf16_units_near_emoji_boundary():
    source = "# What changed\n\n" + ("x" * 35) + ("😀" * 10)
    result = dream._compose_boot_dream(source, 70)
    assert len(result.encode("utf-16-le")) // 2 <= 70


def test_boot_dream_trims_oversized_sections_instead_of_dropping_everything():
    source = (
        "# What was built or shipped\n\n"
        "Cody shipped the first important change.\n"
        "Cody shipped the second important change.\n"
        "Cody shipped the third important change.\n\n"
        "Cody shipped the fourth important change.\n"
        "Cody shipped the fifth important change.\n\n"
        "# Lessons learned\n\n" + ("background line\n" * 20)
    )
    result = dream._compose_boot_dream(source, 190)
    assert "# What was built or shipped" in result
    assert "Cody shipped the first important change." in result
    assert "(trimmed)" in result
    assert len(result.encode("utf-16-le")) // 2 <= 190


# ---------------------------------------------------------------------------
# Rollup validation retry + quarantine (added after the gate's first live
# night, 2026-08-21: 79 SUBJECT violations from lowercase agent ids discarded
# the whole paid synthesis — degrade to raw, never to nothing).
# ---------------------------------------------------------------------------

def _synthesize_with_fakes(monkeypatch, tmp_path, rollup_results, failing_texts):
    """Run synthesize() with the LLM and validator faked.

    rollup_results: successive stage-2 responses — a str is returned, a
    RuntimeError instance is raised (fault injection for the retry call).
    failing_texts: payloads the fake validator rejects.
    Returns (result, rollup_inputs).
    """
    monkeypatch.setattr(dream, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(dream, "_build_agent_section", lambda a, m: "section")
    monkeypatch.setattr(dream, "DREAM_DIR", tmp_path)
    remaining = list(rollup_results)
    rollup_inputs = []

    def fake_call(system, content, max_tokens=4096):
        if system is dream.PER_AGENT_SYSTEM_PROMPT:
            return "agent brief", {}
        rollup_inputs.append(content)
        result = remaining.pop(0)
        if isinstance(result, RuntimeError):
            raise result
        return result, {}

    def fake_validate(payload):
        if payload in failing_texts:
            raise RuntimeError("stated-line validation failed:\nSUBJECT claim starts lowercase: cc")

    monkeypatch.setattr(dream, "_call_openrouter_adaptive", fake_call)
    monkeypatch.setattr(dream, "_validate_stated_lines", fake_validate)
    return dream.synthesize([{"agent_id": "cc", "summary": "did a thing"}]), rollup_inputs


def _read_single_quarantine(tmp_path):
    rejected = list(tmp_path.glob("rejected-*"))
    assert len(rejected) == 1
    # The quarantine must stay invisible to the real consumers' globs:
    # agentb/server.py /dream/latest globs *.md, the dreamer memory dir *.json.
    assert not list(tmp_path.glob("*.md"))
    assert not list(tmp_path.glob("*.json"))
    return rejected[0].read_text(encoding="utf-8")


def test_rollup_retry_recovers_from_validation_failure(monkeypatch, tmp_path):
    result, rollup_inputs = _synthesize_with_fakes(
        monkeypatch, tmp_path, ["bad rollup", "good rollup"], {"bad rollup"})
    assert result == "good rollup"
    # The retry prompt must carry the validator's report back to the model.
    assert len(rollup_inputs) == 2
    assert "REJECTED" in rollup_inputs[1]
    assert "starts lowercase" in rollup_inputs[1]
    # A recovered run leaves no quarantine file behind.
    assert not list(tmp_path.glob("rejected-*"))


def test_rollup_double_failure_quarantines_and_raises(monkeypatch, tmp_path):
    with pytest.raises(RuntimeError, match="stated-line validation"):
        _synthesize_with_fakes(
            monkeypatch, tmp_path, ["bad one", "bad two"], {"bad one", "bad two"})
    body = _read_single_quarantine(tmp_path)
    assert "bad two" in body           # the final attempt is preserved
    assert "RETRY attempt" in body     # and labeled as the retry's text
    assert "starts lowercase" in body  # with the validator's report


def test_rollup_retry_call_failure_quarantines_first_attempt(monkeypatch, tmp_path):
    # The retry API call dying must not masquerade as a validation failure:
    # the FIRST attempt's text is preserved and labeled as such.
    with pytest.raises(RuntimeError, match="OpenRouter 502"):
        _synthesize_with_fakes(
            monkeypatch, tmp_path,
            ["bad one", RuntimeError("OpenRouter 502: bad gateway")], {"bad one"})
    body = _read_single_quarantine(tmp_path)
    assert "bad one" in body
    assert "FIRST attempt" in body
    assert "OpenRouter 502" in body


def test_rollup_double_failure_drops_only_named_bad_lines(monkeypatch, tmp_path):
    monkeypatch.setattr(dream, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(dream, "_build_agent_section", lambda a, m: "section")
    monkeypatch.setattr(dream, "DREAM_DIR", tmp_path)
    responses = iter(["agent brief", "bad first", "# Decisions\ngood line\nbad line"])
    monkeypatch.setattr(
        dream, "_call_openrouter_adaptive",
        lambda *args, **kwargs: (next(responses), {}))

    def validate(payload):
        if payload == "bad first":
            raise RuntimeError("stated-line validation failed:\nline 1 FIELDS")
        if payload.endswith("bad line"):
            raise RuntimeError("stated-line validation failed:\n         line 3    STATUS")

    monkeypatch.setattr(dream, "_validate_stated_lines", validate)
    result = dream.synthesize([{"agent_id": "cc", "summary": "did a thing"}])
    assert "good line" in result
    assert "bad line" not in result
    assert result.endswith("DROPPED: 1 invalid stated line(s) after corrective retry")
    assert not list(tmp_path.glob("rejected-*"))


_BRAIN_DIR = os.environ.get("BRAIN_DIR", "").strip()
_REAL_CHECKER = Path(_BRAIN_DIR) / "tools" / "stated-line-check.py" if _BRAIN_DIR else None


@pytest.mark.skipif(not (_REAL_CHECKER and _REAL_CHECKER.is_file()),
                    reason="BRAIN_DIR with tools/stated-line-check.py not available")
def test_capitalized_agent_lines_pass_the_real_checker():
    # The 2026-08-21 failure shipped because producer and validator had only
    # ever met through fakes. Where the real checker is present (dev hosts,
    # the deployed runtime), prove lines shaped like the new prompt rules
    # actually pass it.
    lines = "\n".join([
        "CC used Bash to set a watcher for the sixteen hundred digest · CC · 2026-08-20T22:01:11 · done",
        "Cody accepted the cadence split after reviewing the spec · Cody · 2026-08-20 · accepted",
        "Opie shipped the bounded build specification for sec-watch · Opie · 2026-08-20 · shipped",
    ])
    proc = subprocess.run(
        [sys.executable, str(_REAL_CHECKER), "--check", "-"],
        input=lines.encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.returncode == 0, proc.stdout.decode("utf-8", errors="replace")
