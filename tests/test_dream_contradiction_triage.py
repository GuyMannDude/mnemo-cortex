"""Contradiction triage — non-competing flags downgrade, real conflicts survive.

Before triage the detector's lifetime score was 0 real conflicts / 8 flags
(brain active-detail.md § [task:dreamer-generalisation-filter]). These tests
pin the two downgrade stages and the fail-toward-noise contract: a broken or
partial judge must KEEP flags, never swallow them. The specimen values are the
real ones from dreams 2026-08-20/25/27.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

# The dreamer is a top-level script with a hyphen in its name — load it by path.
_DREAM_PATH = Path(__file__).resolve().parent.parent / "mnemo-dream.py"
_spec = importlib.util.spec_from_file_location("mnemo_dream_triage", _DREAM_PATH)
dream = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dream)


def _flag(extracted, verified, entity="Opie", attribute="role"):
    return {
        "entity": entity,
        "attribute": attribute,
        "extracted_value": extracted,
        "existing_verified_value": verified,
        "evidence_source": "memory:test",
        "source_agent": "cc",
    }


# Real specimens
OPIE_ROLE = _flag(
    "approve and commit doctrines",
    "Architecture, strategy, planning. Authors and maintains Sparks Brain files.",
)
OPIE_LANE_PATH = _flag("opie.md", "brain/opie.md", attribute="lane_file")
IGOR_TYPE = _flag(
    "hybrid-graphics laptop",
    "Ubuntu laptop. Guy's primary dev machine.",
    entity="IGOR",
    attribute="type",
)
REAL_CONFLICT = _flag("decommissioned 2026-06-26", "active production host", entity="artforge", attribute="status")


def _judge(monkeypatch, responder):
    """Install a fake _call_llm; returns the call-count holder."""
    calls = {"n": 0, "user_content": None, "effort": None, "max_tokens": None}

    def fake(system_prompt, user_content, max_tokens=4096, effort=None):
        calls["n"] += 1
        calls["user_content"] = user_content
        calls["effort"] = effort
        calls["max_tokens"] = max_tokens
        return responder(user_content), {}

    monkeypatch.setattr(dream, "_call_llm", fake)
    return calls


def test_identifier_class_downgrades_without_llm(monkeypatch):
    calls = _judge(monkeypatch, lambda _: "[]")
    survivors, drift = dream.triage_contradictions([OPIE_LANE_PATH])
    assert survivors == []
    assert len(drift) == 1
    assert drift[0]["downgrade_class"] == "identifier"
    assert calls["n"] == 0  # deterministic stage must not spend an LLM call


def test_case_and_whitespace_normalize(monkeypatch):
    calls = _judge(monkeypatch, lambda _: "[]")
    survivors, drift = dream.triage_contradictions([_flag("  Gemini 3.7-Flash ", "gemini 3.7-flash")])
    assert survivors == [] and len(drift) == 1
    assert calls["n"] == 0


def test_compatible_verdict_downgrades(monkeypatch):
    _judge(monkeypatch, lambda _: json.dumps(
        [{"i": 1, "verdict": "compatible", "reason": "subset of the verified role"}]
    ))
    survivors, drift = dream.triage_contradictions([OPIE_ROLE])
    assert survivors == []
    assert len(drift) == 1
    assert drift[0]["downgrade_class"] == "compatible"
    assert "subset" in drift[0]["downgrade_reason"]


def test_conflict_verdict_survives(monkeypatch):
    _judge(monkeypatch, lambda _: json.dumps([{"i": 1, "verdict": "conflict", "reason": "cannot both hold"}]))
    survivors, drift = dream.triage_contradictions([REAL_CONFLICT])
    assert survivors == [REAL_CONFLICT]
    assert drift == []


def test_mixed_batch_splits_and_numbers_items(monkeypatch):
    calls = _judge(monkeypatch, lambda _: json.dumps([
        {"i": 1, "verdict": "compatible", "reason": "granularity"},
        {"i": 2, "verdict": "conflict", "reason": "mutually exclusive states"},
    ]))
    survivors, drift = dream.triage_contradictions([IGOR_TYPE, REAL_CONFLICT])
    assert survivors == [REAL_CONFLICT]
    assert [d["entity"] for d in drift] == ["IGOR"]
    assert calls["n"] == 1  # one batched call, not one per item
    assert "Item 2" in calls["user_content"]


def test_two_distinct_paths_sharing_basename_go_to_judge(monkeypatch):
    # "brain/opie.md" vs "archive/opie.md" is a real move, not a spelling —
    # it must reach the judge, never the identifier downgrade (review 2026-08-27).
    calls = _judge(monkeypatch, lambda _: json.dumps([{"i": 1, "verdict": "conflict", "reason": "moved"}]))
    moved = _flag("archive/opie.md", "brain/opie.md", attribute="lane_file")
    survivors, drift = dream.triage_contradictions([moved])
    assert survivors == [moved]
    assert drift == []
    assert calls["n"] == 1


def test_bare_name_vs_qualified_path_is_identifier(monkeypatch):
    calls = _judge(monkeypatch, lambda _: "[]")
    survivors, drift = dream.triage_contradictions([_flag("brain/opie.md", "opie.md", attribute="lane_file")])
    assert survivors == [] and len(drift) == 1
    assert drift[0]["downgrade_class"] == "identifier"
    assert calls["n"] == 0


def test_judge_none_content_keeps_all_flags(monkeypatch):
    # OpenRouter can return HTTP 200 with "content": null.
    _judge(monkeypatch, lambda _: None)
    survivors, drift = dream.triage_contradictions([OPIE_ROLE, REAL_CONFLICT])
    assert survivors == [OPIE_ROLE, REAL_CONFLICT]
    assert drift == []


def test_judge_runs_at_low_effort(monkeypatch):
    """CC ruling 10-09: the compatibility judge is a short JSON verdict list."""
    calls = _judge(monkeypatch, lambda _: json.dumps([{"i": 1, "verdict": "conflict", "reason": "real"}]))
    dream.triage_contradictions([REAL_CONFLICT])
    assert calls["n"] == 1
    assert calls["effort"] == "low"
    assert calls["max_tokens"] == 1024


def test_judge_http_error_keeps_all_flags(monkeypatch):
    def boom(*a, **k):
        raise dream.httpx.ConnectError("bus unreachable")

    monkeypatch.setattr(dream, "_call_llm", boom)
    survivors, drift = dream.triage_contradictions([OPIE_ROLE])
    assert survivors == [OPIE_ROLE] and drift == []


def test_judge_non_list_json_keeps_all_flags(monkeypatch):
    _judge(monkeypatch, lambda _: json.dumps({"verdict": "compatible"}))
    survivors, drift = dream.triage_contradictions([OPIE_ROLE])
    assert survivors == [OPIE_ROLE] and drift == []


def test_malformed_flag_dict_keeps_all_flags(monkeypatch):
    # Stage A has failure containment too: a dict missing a key must not
    # abort the unattended run (log-don't-raise contract).
    calls = _judge(monkeypatch, lambda _: "[]")
    broken = {"entity": "X", "attribute": "y", "extracted_value": "a"}  # no existing_verified_value
    survivors, drift = dream.triage_contradictions([broken, REAL_CONFLICT])
    assert survivors == [broken, REAL_CONFLICT]
    assert drift == []
    assert calls["n"] == 0  # aborted in stage A, before any LLM spend


def test_survivors_pass_through_unmodified(monkeypatch):
    # Bus consumers key on the per-item field names — survivors must be the
    # original dicts, no downgrade_* keys added.
    _judge(monkeypatch, lambda _: json.dumps([{"i": 1, "verdict": "conflict", "reason": "real"}]))
    survivors, _ = dream.triage_contradictions([dict(REAL_CONFLICT)])
    assert set(survivors[0].keys()) == set(REAL_CONFLICT.keys())


def test_judge_failure_keeps_all_flags(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("OpenRouter 500")

    monkeypatch.setattr(dream, "_call_llm", boom)
    survivors, drift = dream.triage_contradictions([OPIE_ROLE, REAL_CONFLICT])
    assert survivors == [OPIE_ROLE, REAL_CONFLICT]
    assert drift == []


def test_unparseable_judge_output_keeps_all_flags(monkeypatch):
    _judge(monkeypatch, lambda _: "I think they are compatible, probably.")
    survivors, drift = dream.triage_contradictions([OPIE_ROLE])
    assert survivors == [OPIE_ROLE]
    assert drift == []


def test_missing_verdict_keeps_that_flag(monkeypatch):
    _judge(monkeypatch, lambda _: json.dumps([{"i": 1, "verdict": "compatible", "reason": "granularity"}]))
    survivors, drift = dream.triage_contradictions([IGOR_TYPE, REAL_CONFLICT])
    assert survivors == [REAL_CONFLICT]
    assert len(drift) == 1


def test_code_fenced_json_parses(monkeypatch):
    _judge(monkeypatch, lambda _: "```json\n[{\"i\": 1, \"verdict\": \"compatible\", \"reason\": \"subset\"}]\n```")
    survivors, drift = dream.triage_contradictions([OPIE_ROLE])
    assert survivors == [] and len(drift) == 1


def test_empty_input_makes_no_llm_call(monkeypatch):
    calls = _judge(monkeypatch, lambda _: "[]")
    assert dream.triage_contradictions([]) == ([], [])
    assert calls["n"] == 0


def _capture_bus(monkeypatch, discord=False):
    """Point notify at a fake bus (and optionally Discord) and capture posts."""
    sent = []

    class _Resp:
        status_code = 200
        text = "ok"

    monkeypatch.setattr(dream, "MNEMO_DREAM_BUS_URL", "http://bus.test")
    monkeypatch.setattr(dream, "MNEMO_DREAM_BUS_FROM", "dreamer")
    monkeypatch.setattr(dream, "MNEMO_DREAM_BUS_TARGETS", ["CC"])
    monkeypatch.setattr(dream, "MNEMO_DREAM_DISCORD_WEBHOOK", "http://discord.test" if discord else "")
    monkeypatch.setattr(dream.httpx, "post", lambda url, **kw: (sent.append((url, kw.get("json"))), _Resp())[1])
    return sent


def test_notify_zero_survivors_sends_one_line_not_rejects(monkeypatch):
    sent = _capture_bus(monkeypatch)
    drift = [{**OPIE_ROLE, "downgrade_class": "compatible", "downgrade_reason": "subset"}]
    dream.notify_contradictions([], "2026-08-27", drift_notes=drift)
    assert len(sent) == 1
    body = sent[0][1]["body"]
    assert body["contradictions"] == []
    assert body["summary"].startswith("Dream 2026-08-27: 0 contradictions survived triage")
    assert body["drift_notes"] == ["Opie.role (compatible: subset)"]


def test_notify_zero_survivors_discord_is_one_line_too(monkeypatch):
    # The human channel must not receive the "facts conflict" preamble on a
    # run whose triage concluded none do (review 2026-08-27).
    sent = _capture_bus(monkeypatch, discord=True)
    drift = [{**OPIE_ROLE, "downgrade_class": "compatible", "downgrade_reason": "subset"}]
    dream.notify_contradictions([], "2026-08-27", drift_notes=drift)
    discord_posts = [payload for url, payload in sent if url == "http://discord.test"]
    assert len(discord_posts) == 1
    content = discord_posts[0]["content"]
    assert "\n" not in content
    assert "conflict with existing verified values" not in content
    assert "0 contradictions survived triage" in content


def test_notify_survivors_carry_drift_count(monkeypatch):
    sent = _capture_bus(monkeypatch)
    # Feed notify from the real triage output, not a hand-built dict, so the
    # per-item field-name contract is exercised end-to-end.
    _judge(monkeypatch, lambda _: json.dumps([
        {"i": 1, "verdict": "compatible", "reason": "granularity"},
        {"i": 2, "verdict": "conflict", "reason": "mutually exclusive"},
    ]))
    survivors, drift = dream.triage_contradictions([IGOR_TYPE, REAL_CONFLICT])
    dream.notify_contradictions(survivors, "2026-08-27", drift_notes=drift)
    assert len(sent) == 1
    body = sent[0][1]["body"]
    assert body["contradictions"] == [REAL_CONFLICT]
    assert body["drift_notes"] == ["IGOR.type (compatible: granularity)"]


def test_notify_all_quiet_sends_nothing(monkeypatch):
    sent = _capture_bus(monkeypatch)
    dream.notify_contradictions([], "2026-08-27", drift_notes=[])
    assert sent == []
