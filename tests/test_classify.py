"""Tests for Smart Ingestion classification (Mnemo v4.0)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentb.classify import (
    classify_category,
    is_routine_log,
    reclassify_memory_dir,
    _parse_category,
)


class FakeReasoner:
    """Stand-in for ResilientReasoning. Returns a canned reply or raises."""

    def __init__(self, reply: str | None = "topology", raises: bool = False):
        self.reply = reply
        self.raises = raises
        self.calls = 0

    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True):
        self.calls += 1
        if self.raises:
            raise RuntimeError("model down")
        return self.reply


# ── is_routine_log ──

@pytest.mark.parametrize("summary,facts,expected", [
    ("[AUTO-CAPTURE] 8 tool calls:\nx", ["auto_capture_flush"], True),
    ("CC session activity (auto-sync from JSONL, session=abc).", [], True),
    ("", [], True),
    ("CC invoked tool: Bash", [], True),
    ("Rocky is Hermes on IGOR; HUD at localhost:3001", [], False),
    ("Guy decided to keep Tampermonkey for MCP", ["v5.5.0 added MCP"], False),
])
def test_is_routine_log(summary, facts, expected):
    assert is_routine_log(summary, facts) is expected


# ── _parse_category ──

def test_parse_category_variants():
    assert _parse_category("topology") == "topology"
    assert _parse_category("Category: decision.") == "decision"
    assert _parse_category("  RELATIONSHIP ") == "relationship"
    assert _parse_category("unknown") is None          # not a valid target
    assert _parse_category("banana") is None
    assert _parse_category("") is None
    # ambiguous reply naming two categories → None (caller falls back to regex)
    assert _parse_category("not a topology issue, it's a decision") is None


# ── classify_category ──

@pytest.mark.asyncio
async def test_classify_noise_skips_llm():
    r = FakeReasoner()
    cat, method = await classify_category(r, "[AUTO-CAPTURE] 3 tool calls", ["auto_capture_flush"])
    assert (cat, method) == ("session_log", "noise-heuristic")
    assert r.calls == 0  # the firehose must never cost an LLM call


@pytest.mark.asyncio
async def test_classify_llm_path():
    r = FakeReasoner(reply="topology")
    cat, method = await classify_category(r, "artforge runs the mnemo server on port 50001", [])
    assert (cat, method) == ("topology", "llm")
    assert r.calls == 1


@pytest.mark.asyncio
async def test_classify_invalid_llm_output_falls_back_to_regex():
    r = FakeReasoner(reply="i have no idea")
    cat, method = await classify_category(r, "we decided to pick DeepSeek over Gemini", [])
    assert method == "regex"          # invalid LLM reply → regex suggester
    assert cat == "decision"          # regex still matches 'decided'/'pick'


@pytest.mark.asyncio
async def test_classify_llm_failure_falls_back_to_regex():
    r = FakeReasoner(raises=True)
    cat, method = await classify_category(r, "a customer named Hoffman Bedding", [])
    assert method == "regex"
    assert cat == "relationship"      # regex matches 'customer'


# ── reclassify_memory_dir ──

def _write(memory_dir: Path, mid: str, category, summary, source="tool"):
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / f"{mid}.json").write_text(json.dumps({
        "id": mid, "summary": summary, "key_facts": [],
        "category": category, "source": source, "created_at": 1.0,
        "schema_version": 3,
    }))


@pytest.mark.asyncio
async def test_reclassify_only_touches_candidates(tmp_path: Path):
    md = tmp_path / "memory"
    _write(md, "a", "unknown", "artforge runs on port 50001")          # candidate → llm
    _write(md, "b", "topology", "already categorized, leave me")       # skip
    _write(md, "c", None, "CC session activity (auto-sync from JSONL)")  # candidate → noise
    r = FakeReasoner(reply="topology")

    stats = await reclassify_memory_dir(md, r, use_breaker=False)

    assert stats["scanned"] == 3
    assert stats["reclassified"] == 2
    assert stats["skipped"] == 1
    # the already-topology file is untouched
    assert json.loads((md / "b.json").read_text())["category"] == "topology"
    # the unknown got the llm category, the autosync got demoted to session_log
    assert json.loads((md / "a.json").read_text())["category"] == "topology"
    assert json.loads((md / "a.json").read_text())["classified_by"] == "llm"
    assert json.loads((md / "c.json").read_text())["category"] == "session_log"


@pytest.mark.asyncio
async def test_reclassify_dry_run_writes_nothing(tmp_path: Path):
    md = tmp_path / "memory"
    _write(md, "a", "unknown", "artforge runs on port 50001")
    before = (md / "a.json").read_text()
    r = FakeReasoner(reply="topology")

    stats = await reclassify_memory_dir(md, r, dry_run=True)

    assert stats["reclassified"] == 1            # counted
    assert (md / "a.json").read_text() == before  # but nothing written


@pytest.mark.asyncio
async def test_reclassify_unknown_only_leaves_routine_logs(tmp_path: Path):
    md = tmp_path / "memory"
    _write(md, "c", "session_log", "CC session activity (auto-sync from JSONL)")
    _write(md, "d", "unknown", "Guy prefers zero-fat outputs")
    r = FakeReasoner(reply="doctrine")

    stats = await reclassify_memory_dir(md, r, include_routine=False)

    assert stats["reclassified"] == 1  # only the unknown; the tagged log is left alone
    assert json.loads((md / "d.json").read_text())["category"] == "doctrine"
