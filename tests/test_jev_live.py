"""4.26.0 Jev live-path shadow — the proposal envelope's first writer.

Every call goes to a fake httpx transport; no key in this file is real and
none is secret-shaped. Spec: brain/spec-blend-build1-proposal-envelope.md,
acceptance items 5, 6, 8b, 8c, 8d.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import threading
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from agentb.config import (
    AgentBConfig, ResilientProviderConfig, ProviderConfig,
    CacheConfig, ServerConfig, ClassificationConfig, DEFAULT_PERSONAS,
)
from agentb.facts_store import FactsStore
from agentb.jev_live import JevShadow, confidence_bucket, day_verdict, utc_day
from tests.test_context_authority import FakeEmbedding, FakeReasoning

FAKE_KEY = "fake-jev-key-for-tests"
SECRET = "pass" + "word=" + "Zq7" * 6          # 18 chars: redactable (16+ floor)

_DREAM_PATH = Path(__file__).resolve().parent.parent / "mnemo-dream.py"


def _dream():
    spec = importlib.util.spec_from_file_location("mnemo_dream_jev", _DREAM_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeJev:
    """Records every request; answers `choice`, or fails, or waits on a gate."""

    def __init__(self, choice="decision", confidence=0.93, status=200, fail=False, gate=None,
                 delay=0.0):
        self.choice, self.confidence, self.status, self.fail = choice, confidence, status, fail
        self.gate, self.delay = gate, delay
        self.bodies: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.open_now = 0
        self.max_open = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(json.loads(request.content))
        self.open_now += 1
        self.max_open = max(self.max_open, self.open_now)
        try:
            while self.gate is not None and not self.gate.is_set():
                await asyncio.sleep(0.01)
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail:
                raise httpx.ConnectError("dead endpoint", request=request)
            other = "topology" if self.choice != "topology" else "decision"
            return httpx.Response(self.status, json={
                "model": "jev-latest",
                "answers": {"category": {"choice": self.choice, "confidence": self.confidence,
                                         "probabilities": {self.choice: self.confidence,
                                                           other: 1 - self.confidence}}},
                "usage": {"input_tokens": 12}})
        finally:
            self.open_now -= 1

    def transport(self):
        return httpx.MockTransport(self.handler)


def _wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    """make_client(fake, key=True, tenants=None) -> (TestClient, tmp_path)."""
    stack = []

    def _make(fake: FakeJev | None = None, key: bool = True, tenants: str | None = None,
              key_path: str | None = None):
        if key_path is not None:
            monkeypatch.setenv("MNEMO_JEV_KEY_FILE", key_path)
        elif key:
            kf = tmp_path / "jev_key.txt"
            kf.write_text(FAKE_KEY + "\n", encoding="utf-8")
            monkeypatch.setenv("MNEMO_JEV_KEY_FILE", str(kf))
        else:
            monkeypatch.delenv("MNEMO_JEV_KEY_FILE", raising=False)
        if tenants is None:
            monkeypatch.delenv("MNEMO_JEV_TENANTS", raising=False)
        else:
            monkeypatch.setenv("MNEMO_JEV_TENANTS", tenants)
        real = JevShadow.from_env.__func__
        fake_transport = fake.transport() if fake else None
        monkeypatch.setattr(JevShadow, "from_env", classmethod(
            lambda cls, facts, env=None, transport=None: real(cls, facts, env, transport=fake_transport)))
        cfg = AgentBConfig(
            reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
            embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
            cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50097),
            data_dir=str(tmp_path),
            classification=ClassificationConfig(enabled=False),
            personas=dict(DEFAULT_PERSONAS),
        )
        p1 = patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding())
        p2 = patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning())
        p1.start()
        p2.start()
        from agentb.server import create_app
        c = TestClient(create_app(cfg))
        c.__enter__()
        stack.append((c, p1, p2))
        return c, tmp_path

    yield _make
    for c, p1, p2 in reversed(stack):
        c.__exit__(None, None, None)
        p1.stop()
        p2.stop()


def _writeback(c, summary, category="topology", agent="cc", **extra):
    r = c.post("/writeback", json={"session_id": f"s-{abs(hash(summary))}", "summary": summary,
                                   "key_facts": [], "category": category, "source": "inferred",
                                   "force": True, "agent_id": agent, **extra})
    assert r.status_code == 200, r.text
    return r.json()["memory_id"]


def _today(c) -> dict:
    days = c.get("/proposals/jev-stats", params={"days": 1}).json()["days"]
    return next((d for d in days if d["day"] == utc_day()), {})


def _record(tmp, mid):
    (path,) = list(Path(tmp).rglob(f"{mid}.json"))
    return json.loads(path.read_text(encoding="utf-8"))


# ── acceptance 5: Jev off → nothing at all ──

def test_jev_off_zero_calls_zero_rows(make_client):
    fake = FakeJev()
    c, _ = make_client(fake, key=False)
    _writeback(c, "IGOR runs the dreamer nightly")
    time.sleep(0.2)
    assert fake.bodies == []
    stats = c.get("/proposals/jev-stats").json()
    assert stats["enabled"] is False and stats["error"] is None
    assert stats["days"] == [] and stats["cells"] == []
    assert c.get("/proposals").json()["count"] == 0


def test_unreadable_or_empty_key_file_is_off_and_loud(tmp_path, caplog):
    facts = FactsStore(tmp_path / "facts.sqlite")
    with caplog.at_level(logging.ERROR):
        assert JevShadow.from_env(facts, env={"MNEMO_JEV_KEY_FILE": str(tmp_path / "nope")}) is None
        (tmp_path / "empty").write_text("\n", encoding="utf-8")
        assert JevShadow.from_env(facts, env={"MNEMO_JEV_KEY_FILE": str(tmp_path / "empty")}) is None
    assert "unreadable" in caplog.text and "empty" in caplog.text


# ── acceptance 6: disagreement → a proposal row, after the response ──

def test_jev_disagreement_row_after_response_category_unchanged(make_client):
    gate = threading.Event()
    fake = FakeJev(choice="decision", confidence=0.93, gate=gate)
    c, tmp = make_client(fake)
    summary = "We chose SQLite over Postgres for the facts store"
    mid = _writeback(c, summary, category="topology")
    # the response is back while Jev is still answering: nothing filed yet
    assert _wait_for(lambda: len(fake.bodies) == 1)
    assert c.get("/proposals").json()["count"] == 0
    gate.set()
    assert _wait_for(lambda: c.get("/proposals").json()["count"] == 1)
    row = c.get("/proposals", params={"kind": "memory_category"}).json()["proposals"][0]
    assert row["target_ref"] == f"memory:cc/{mid}" and row["entity"] == f"memory:cc/{mid}"
    assert row["proposed_value"] == "decision" and row["current_value"] == "topology"
    assert row["confidence_score"] == pytest.approx(0.93)
    assert row["source_run"] == "jev:shadow:v1"
    assert row["evidence_quotes"] == [summary[:300]]
    assert _record(tmp, mid)["category"] == "topology"          # stored category never moves
    d = _today(c)
    assert (d["eligible"], d["attempted"], d["ok"], d["disagree"], d["agree"]) == (1, 1, 1, 1, 0)


def test_jev_agree_writes_no_row(make_client):
    fake = FakeJev(choice="topology", confidence=0.97)
    c, _ = make_client(fake)
    _writeback(c, "IGOR-2 hosts Mnemo on port 50001", category="topology")
    assert _wait_for(lambda: _today(c).get("agree") == 1)
    assert _wait_for(lambda: c.get("/proposals/jev-stats").json()["in_flight"] == 0)
    assert _today(c)["disagree"] == 0
    assert c.get("/proposals").json()["count"] == 0
    cells = c.get("/proposals/jev-stats").json()["cells"]
    assert any(x["bucket"] == "0.9" and x["agree"] == 1 for x in cells)


def test_identical_jev_disagreement_twice_is_one_row(make_client):
    """Acceptance 4 through the live path: the same memory shadowed twice."""
    fake = FakeJev(choice="decision")
    c, tmp = make_client(fake)
    ts = "2026-09-24T01:00:00+00:00"
    mid = _writeback(c, "We chose SQLite over Postgres", timestamp=ts)
    assert _wait_for(lambda: c.get("/proposals").json()["count"] == 1)
    # the identical record again (same session, stamp, text → same id)
    assert _writeback(c, "We chose SQLite over Postgres", timestamp=ts) == mid
    assert _wait_for(lambda: c.get("/proposals").json()["proposals"][0]["seen_count"] == 2)
    assert c.get("/proposals").json()["count"] == 1
    assert _record(tmp, mid)["category"] == "topology"


def test_jev_skips_session_log_and_regex_placeholder(tmp_path):
    fake = FakeJev()
    facts = FactsStore(tmp_path / "facts.sqlite")

    async def go():
        jev = JevShadow(FAKE_KEY, ["cc"], facts, transport=fake.transport())
        jev.submit("cc", tmp_path, {"id": "a1", "summary": "x", "category": "session_log"})
        jev.submit("cc", tmp_path, {"id": "a2", "summary": "x", "category": "topology",
                                    "needs_reclassification": True})
        jev.submit("cc", tmp_path, {"id": "a3", "summary": "x", "category": "unknown"})
        await jev.aclose()
    asyncio.run(go())
    assert fake.bodies == [] and facts.jev_stats("2000-01-01") == []


# ── acceptance 8b: egress redaction + tenant allowlist ──

def test_jev_payload_redacted_on_the_wire(tmp_path):
    """Wiring, not threshold: the egress door runs redact_text itself, even
    on an entry that reached it unredacted. 18-char secret — the redactor
    floor is 16 (snag-redactor-short-kv-secret-passes.md, batch 2 #4)."""
    fake = FakeJev(choice="decision")
    facts = FactsStore(tmp_path / "facts.sqlite")
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "m1.json").write_text("{}", encoding="utf-8")

    async def go():
        jev = JevShadow(FAKE_KEY, ["cc"], facts, transport=fake.transport())
        jev.submit("cc", mem, {"id": "m1", "summary": f"login used {SECRET} today",
                               "key_facts": [f"again {SECRET}"], "category": "topology"})
        await jev.aclose()
    asyncio.run(go())
    wire = fake.requests[0].content.decode()
    assert SECRET not in wire and "[REDACTED:" in wire
    (row,) = facts.proposals(kind="memory_category")
    assert SECRET not in json.dumps(row)


def test_jev_payload_redacted_end_to_end(make_client):
    fake = FakeJev(choice="decision")
    c, _ = make_client(fake)
    _writeback(c, f"deploy note: {SECRET} was rotated")
    assert _wait_for(lambda: len(fake.requests) == 1)
    assert SECRET not in fake.requests[0].content.decode()


def test_tenant_not_allowlisted_zero_calls(make_client):
    fake = FakeJev()
    c, _ = make_client(fake)                    # default allowlist: cc only
    _writeback(c, "Opie's genealogy note", agent="opie")
    time.sleep(0.3)
    assert fake.bodies == []
    stats = c.get("/proposals/jev-stats").json()
    assert stats["tenants"] == ["cc"]
    assert [x for x in stats["cells"] if x["tenant"] == "opie"] == []
    assert _today(c).get("attempted", 0) == 0


def test_tenant_allowlist_env_is_honoured(make_client):
    fake = FakeJev(choice="topology")
    c, _ = make_client(fake, tenants="opie")
    _writeback(c, "a cc memory", agent="cc")
    _writeback(c, "an opie memory", agent="opie")
    assert _wait_for(lambda: len(fake.bodies) == 1)
    time.sleep(0.2)
    assert len(fake.bodies) == 1
    assert {x["tenant"] for x in c.get("/proposals/jev-stats").json()["cells"]} == {"opie"}


# ── acceptance 8c: at most 8 in flight, overflow dropped and counted ──

def test_nine_concurrent_writebacks_cap_eight_drop_one(make_client):
    gate = threading.Event()
    fake = FakeJev(choice="topology", gate=gate)
    c, _ = make_client(fake)
    _writeback(c, "warm-up: tenant init, never shadowed", category="session_log")
    durations = []
    for i in range(9):
        t0 = time.monotonic()
        _writeback(c, f"memory number {i} about host igor-{i}")
        durations.append(time.monotonic() - t0)
    assert _wait_for(lambda: len(fake.bodies) == 8)
    stats = c.get("/proposals/jev-stats").json()
    assert stats["in_flight"] == 8
    d = _today(c)
    assert (d["eligible"], d["attempted"], d["dropped"]) == (9, 8, 1)
    assert max(durations) < 2.0              # Jev is stuck; no writeback waited on it
    gate.set()
    assert _wait_for(lambda: _today(c).get("ok") == 8)
    assert fake.max_open == 8 and len(fake.bodies) == 8


def test_jev_timeout_is_counted(tmp_path):
    fake = FakeJev(delay=1.0)
    facts = FactsStore(tmp_path / "facts.sqlite")

    async def go():
        jev = JevShadow(FAKE_KEY, ["cc"], facts, transport=fake.transport(), timeout=0.05)
        jev.submit("cc", tmp_path, {"id": "t1", "summary": "x", "category": "topology"})
        await asyncio.sleep(0.5)
        await jev.aclose()
    asyncio.run(go())
    (cell,) = facts.jev_stats("2000-01-01")
    assert (cell["attempted"], cell["timeout"], cell["error"], cell["ok"]) == (1, 1, 0, 0)


# ── acceptance 8d: a dead endpoint is counted, and the brief says RED ──

def test_dead_jev_counts_errors_and_brief_red(make_client):
    fake = FakeJev(fail=True)
    c, _ = make_client(fake)
    for i in range(5):
        _writeback(c, f"writeback {i} on a day Jev is down")
    assert _wait_for(lambda: _today(c).get("error") == 5)
    d = _today(c)
    assert (d["attempted"], d["error"], d["agree"], d["disagree"]) == (5, 5, 0, 0)
    assert d["red"] is True
    assert c.get("/proposals").json()["count"] == 0
    line = _dream().jev_line(c.get("/proposals/jev-stats").json(), utc_day())
    assert "RED" in line and "5 calls" in line and "5 err/timeout" in line


def test_jev_key_never_logged(make_client, caplog):
    fake = FakeJev(fail=True)
    with caplog.at_level(logging.DEBUG):
        c, _ = make_client(fake)
        _writeback(c, "a memory while Jev is down")
        assert _wait_for(lambda: _today(c).get("error") == 1)
    assert FAKE_KEY not in caplog.text
    req = fake.requests[0]
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}"    # the key rides the header…
    assert FAKE_KEY not in str(req.url)                            # …never the URL
    shadow = JevShadow(FAKE_KEY, ["cc"], None)
    assert FAKE_KEY not in repr(shadow)
    asyncio.run(shadow.aclose())


# ── the RED rule and the brief line, pure ──

@pytest.mark.parametrize("totals,red", [
    ({"eligible": 5, "attempted": 0, "error": 0, "timeout": 0}, True),     # zero calls on a day with writebacks
    ({"eligible": 5, "attempted": 5, "error": 1, "timeout": 0}, False),    # 20% is not > 20%
    ({"eligible": 5, "attempted": 5, "error": 1, "timeout": 1}, True),
    ({"eligible": 0, "attempted": 0, "error": 0, "timeout": 0}, False),
])
def test_day_verdict(totals, red):
    assert day_verdict(totals)[0] is red


def test_brief_jev_line_off_and_quiet_day():
    dream = _dream()
    assert dream.jev_line({"enabled": False}, "2026-09-24") == "jev shadow: OFF"
    assert dream.jev_line({"enabled": True, "days": []}, "2026-09-24").startswith(
        "jev shadow (2026-09-24 UTC): 0 calls")


def test_confidence_bucket():
    assert confidence_bucket(1.0) == "0.9" and confidence_bucket(0.0) == "0.0"
    assert confidence_bucket(0.45) == "0.4"


# ── review fixes: every call ends in exactly one terminal counter ──

def _one_call(tmp_path, transport, entry=None, facts=None):
    facts = facts or FactsStore(tmp_path / "facts.sqlite")

    async def go():
        jev = JevShadow(FAKE_KEY, ["cc"], facts, transport=transport)
        jev.submit("cc", tmp_path, entry or {"id": "m1", "summary": "x", "category": "topology"})
        await jev.aclose()
    asyncio.run(go())
    return facts


def _totals(facts):
    t = {k: 0 for k in FactsStore.JEV_COUNTERS}
    for cell in facts.jev_stats("2000-01-01"):
        for k in t:
            t[k] += cell[k]
    return t


@pytest.mark.parametrize("answer", [
    {"choice": "decision", "confidence": 0.9, "probabilities": {}},          # IndexError
    {"choice": "decision", "confidence": "nan", "probabilities": {"decision": 0.9}},
    {"choice": "decision", "confidence": 1.5, "probabilities": {"decision": 0.9}},
    {"choice": None, "confidence": 0.9, "probabilities": {"decision": 0.9}},
])
def test_malformed_200_counts_as_error(tmp_path, answer):
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"answers": {"category": answer}}))
    t = _totals(_one_call(tmp_path, transport))
    assert (t["attempted"], t["error"], t["ok"]) == (1, 1, 0)


def test_unexpected_exception_counts_as_error(tmp_path):
    def boom(req):
        raise RuntimeError("not an httpx error")
    t = _totals(_one_call(tmp_path, httpx.MockTransport(boom)))
    assert (t["attempted"], t["error"]) == (1, 1)


def test_crash_after_attempted_counts_as_error(tmp_path, monkeypatch):
    import agentb.jev_live as jl
    monkeypatch.setattr(jl, "build_request", lambda text: 1 / 0)
    t = _totals(_one_call(tmp_path, FakeJev().transport()))
    assert (t["eligible"], t["attempted"], t["error"]) == (1, 1, 1)


def test_lost_proposal_is_logged(tmp_path, caplog, monkeypatch):
    import sqlite3
    facts = FactsStore(tmp_path / "facts.sqlite")

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(facts, "propose_memory", locked)
    with caplog.at_level(logging.ERROR):
        _one_call(tmp_path, FakeJev(choice="decision").transport(), facts=facts)
    assert "proposal was LOST" in caplog.text
    assert _totals(facts)["disagree"] == 1


def test_flush_failure_keeps_the_drop_counts(tmp_path):
    class Flaky:
        def __init__(self): self.calls, self.rows = 0, []
        def jev_bump(self, day, tenant, cat, bucket="-", **counts):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("disk full")
            self.rows.append(counts)

    flaky = Flaky()

    async def go():
        jev = JevShadow(FAKE_KEY, ["cc"], flaky, max_in_flight=0)
        jev.submit("cc", tmp_path, {"id": "d1", "summary": "x", "category": "topology"})
        with pytest.raises(RuntimeError):
            await jev.flush()
        assert jev.pending_drops == 1                 # kept, not lost
        await jev.flush()
        assert jev.pending_drops == 0
        await jev.aclose()
    asyncio.run(go())
    assert flaky.rows == [{"eligible": 1, "dropped": 1}]


def test_aclose_waits_for_cancelled_tasks(tmp_path):
    gate = threading.Event()                         # never opened
    fake = FakeJev(gate=gate)
    facts = FactsStore(tmp_path / "facts.sqlite")
    seen = []

    async def go():
        jev = JevShadow(FAKE_KEY, ["cc"], facts, transport=fake.transport())
        jev.submit("cc", tmp_path, {"id": "a1", "summary": "x", "category": "topology"})
        seen.extend(jev._tasks)
        await asyncio.sleep(0.05)
        await jev.aclose(grace=0.05)
        assert all(t.done() for t in seen)
    asyncio.run(go())
    assert len(seen) == 1


def test_key_file_set_but_not_loaded_reads_red(make_client, tmp_path):
    c, _ = make_client(FakeJev(), key_path=str(tmp_path / "missing-key.txt"))
    stats = c.get("/proposals/jev-stats").json()
    assert stats["enabled"] is False and "did not load" in stats["error"]
    assert _dream().jev_line(stats, utc_day()).startswith("jev shadow: RED")


def test_brief_line_shows_unflushed_drops():
    dream = _dream()
    line = dream.jev_line({"enabled": True, "days": [], "unflushed_drops": 3}, "2026-09-24")
    assert line.endswith("3 drop(s) not yet written")
