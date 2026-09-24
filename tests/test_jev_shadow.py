"""Offline tests for tools/experiments/jev_shadow.py — no network, no key."""
import importlib.util
import sqlite3
from pathlib import Path

import httpx
import pytest

SPEC = importlib.util.spec_from_file_location(
    "jev_shadow", Path(__file__).resolve().parents[1] / "tools" / "experiments" / "jev_shadow.py"
)
assert SPEC is not None and SPEC.loader is not None
js = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(js)


def test_request_shape_matches_api_doc():
    req = js.build_request("  some memory text  ")
    assert req["model"] == "jev-latest"
    assert req["state"] == "some memory text"
    q = req["questions"]["category"]
    assert q["type"] == "choice"
    assert "session_log" not in q["criteria"]
    assert set(q["criteria"]) == {
        "topology", "current_state", "doctrine", "incident",
        "identity", "relationship", "decision", "idea",
    }


def test_request_caps_input_like_live_classifier():
    req = js.build_request("x" * 5000)
    assert len(req["state"]) == js.DEFAULT_MAX_INPUT_CHARS


def test_parse_response_reads_documented_fields():
    body = {
        "model": "jev-1.13.0",
        "answers": {"category": {
            "type": "choice", "choice": "decision",
            "probabilities": {"decision": 0.7, "doctrine": 0.2, "idea": 0.1},
            "confidence": 0.66,
        }},
        "usage": {"input_tokens": 318, "output_tokens": 34},
    }
    p = js.parse_response(body)
    assert p["jev_choice"] == "decision"
    assert p["confidence"] == 0.66
    assert p["top2_margin"] == pytest.approx(0.5)
    assert p["input_tokens"] == 318


def test_parse_response_rejects_unexpected_shape():
    with pytest.raises(KeyError):
        js.parse_response({"answers": {}})


def _db(tmp_path, rows):
    db = tmp_path / "vec_index.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE vec_sources (memory_id TEXT PRIMARY KEY, text TEXT NOT NULL, "
                "source_file TEXT, created_at REAL NOT NULL, category TEXT)")
    con.executemany("INSERT INTO vec_sources VALUES (?,?,?,?,?)",
                    [(f"m{i}", t, None, 0.0, c) for i, (t, c) in enumerate(rows)])
    con.commit(); con.close()
    return db


def test_sample_is_stratified_and_skips_session_log(tmp_path):
    rows = [("d", "decision")] * 5 + [("t", "topology")] * 2 + [("s", "session_log")] * 50
    db = _db(tmp_path, rows)
    got = js.sample_memories(db, per_category=3, seed=7)
    cats = [r["stored_category"] for r in got]
    assert cats.count("decision") == 3
    assert cats.count("topology") == 2
    assert "session_log" not in cats


def test_sample_is_deterministic_for_a_seed_and_seed_sensitive(tmp_path):
    db = _db(tmp_path, [(f"t{i}", "incident") for i in range(20)])
    a = [r["memory_id"] for r in js.sample_memories(db, 5, seed=3)]
    b = [r["memory_id"] for r in js.sample_memories(db, 5, seed=3)]
    c = [r["memory_id"] for r in js.sample_memories(db, 5, seed=4)]
    assert a == b
    assert a != c


def test_sample_leaves_no_sidecars_beside_the_db(tmp_path):
    db = _db(tmp_path, [("t", "incident")])
    js.sample_memories(db, 1, seed=1)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["vec_index.sqlite"]


def test_summary_agreement_and_calibration():
    rows = [
        {"stored_category": "decision", "jev_choice": "decision", "confidence": 0.9,
         "latency_ms": 100, "input_tokens": 10},
        {"stored_category": "decision", "jev_choice": "doctrine", "confidence": 0.3,
         "latency_ms": 100, "input_tokens": 10},
        {"stored_category": "topology", "jev_choice": "topology", "confidence": 1.0,
         "latency_ms": 100, "input_tokens": 10},
        {"stored_category": "idea", "error": "boom"},
    ]
    s = js.summarize(rows)
    assert s["scored"] == 3 and s["errors"] == 1
    assert s["agree_rate"] == pytest.approx(0.667, abs=1e-3)
    assert s["mean_confidence_agree"] == 0.95
    assert s["mean_confidence_disagree"] == 0.3
    assert s["top_confusions"] == [{"stored": "decision", "jev": "doctrine", "n": 1}]
    assert s["calibration"] == {
        "0.3-0.4": {"n": 1, "agree_rate": 0.0},
        "0.9-1.0": {"n": 2, "agree_rate": 1.0},
    }
    assert s["input_tokens_total"] == 30
    assert s["per_category"]["decision"] == {"n": 2, "agree_rate": 0.5}


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer k"})


def _ok_body():
    return {"model": "jev-1", "answers": {"category": {"type": "choice", "choice": "decision",
            "probabilities": {"decision": 1.0}, "confidence": 0.9}}, "usage": {"input_tokens": 5}}


def test_call_jev_401_is_fatal_and_never_retries(monkeypatch):
    calls = []
    def handler(req):
        calls.append(1)
        return httpx.Response(401, json={"error": "bad key"})
    monkeypatch.setattr(js.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit), _client(handler) as c:
        js.call_jev(c, {"state": "x"})
    assert len(calls) == 1


def test_call_jev_retries_429_then_succeeds(monkeypatch):
    seq = [429, 429, 200]
    slept = []
    def handler(req):
        code = seq.pop(0)
        return httpx.Response(code, json=_ok_body() if code == 200 else {})
    monkeypatch.setattr(js.time, "sleep", lambda s: slept.append(s))
    with _client(handler) as c:
        body, latency = js.call_jev(c, {"state": "x"})
    assert body["answers"]["category"]["choice"] == "decision"
    assert slept == [1.0, 2.0]
    assert latency >= 0


def test_call_jev_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(js.time, "sleep", lambda s: None)
    with pytest.raises(httpx.HTTPStatusError), _client(lambda r: httpx.Response(529)) as c:
        js.call_jev(c, {"state": "x"}, retries=2)


def test_call_jev_422_error_carries_server_explanation():
    with pytest.raises(httpx.HTTPStatusError) as ei, \
            _client(lambda r: httpx.Response(422, text='{"detail":"criteria must be a map"}')) as c:
        js.call_jev(c, {"state": "x"})
    assert "criteria must be a map" in str(ei.value)
    assert "Bearer" not in str(ei.value)


def test_main_dry_run_prints_request_without_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    db = _db(tmp_path, [("a decision memory", "decision"), ("a host", "topology")])
    rc = js.main(["--db", str(db), "--per-category", "5", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"model": "jev-latest"' in out
    assert not list((tmp_path).glob("*.jsonl"))


def test_summary_with_nothing_scored():
    assert js.summarize([{"stored_category": "idea", "error": "x"}]) == {"scored": 0, "errors": 1}


def test_load_key_refuses_empty(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        js.load_key(None)


def test_load_key_takes_last_line_of_file(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    f = tmp_path / "k.txt"
    f.write_text("TypeSafe Jev key, 2026-09-23\nsk-test-123\n")
    assert js.load_key(str(f)) == "sk-test-123"
