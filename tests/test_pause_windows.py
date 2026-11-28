"""Item 18b': a capture pause is a WINDOW on event time, keyed (agent, seat).

What the paused seat created inside the window never lands, whenever it is
posted; bystander seats' events land; a writer with no seat is over-gated,
never under-gated. The six endpoint tests are the ones CC ordered (#6936).
"""
from __future__ import annotations

import gzip
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentb.capture_gate import GUARD_SECONDS, CaptureGate
from agentb.config import (
    AgentBConfig, ResilientProviderConfig, ProviderConfig,
    CacheConfig, ServerConfig, ClassificationConfig, DEFAULT_PERSONAS,
)
from agentb.transcripts import STUB_TYPE, TranscriptArchive

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
SID2 = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
PAUSER = "cc-cc3"
BYSTANDER = "cc"

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
    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True): return "x"
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


# ── helpers ──

def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def pause(client, seat=PAUSER, minutes=5):
    r = client.post("/capture/pause", json={"minutes": minutes, "agent_id": "cc",
                                            "seat": seat, "reason": "key rotation"})
    assert r.status_code == 200, r.text
    return r.json()


def resume(client, seat=PAUSER):
    assert client.post("/capture/resume", json={"agent_id": "cc", "seat": seat}).status_code == 200


def shift_windows(tmp_path: Path, seconds: float) -> dict:
    """Move every CLOSED window `seconds` into the past (append-only: a later
    record for an id wins). Returns the last window. Lets a test post 'late'
    without sleeping."""
    log = Path(tmp_path) / "capture-windows.jsonl"
    recs = {}
    for rec in log.read_text().splitlines():
        r = json.loads(rec)
        recs[r["id"]] = r
    assert recs
    with open(log, "a") as f:
        for r in recs.values():
            assert r["end"] is not None, "shift only closed windows"
            r = {**r, "start": r["start"] - seconds, "end": r["end"] - seconds,
                 "resume_at": r["resume_at"] - seconds}
            f.write(json.dumps(r) + "\n")
    return r


def wb(client, summary, **kw):
    body = {"session_id": f"s-{time.time_ns()}", "summary": summary, "agent_id": "cc", **kw}
    r = client.post("/writeback", json=body)
    assert r.status_code == 200, r.text
    return r.json()["status"]


def line(t, kind="user", text="hello", sid=SID):
    obj = {"type": kind, "sessionId": sid, "uuid": f"u{time.time_ns()}",
           "message": {"role": "assistant" if kind == "assistant" else "user", "content": text}}
    if t is not None:
        obj["timestamp"] = iso(t)
    return json.dumps(obj, separators=(",", ":"))


def blob(text):   # an untimed, content-bearing line (Claude Code api-request-blob)
    return json.dumps({"type": "api-request-blob", "sessionId": SID, "hash": "h",
                       "message": text}, separators=(",", ":"))


def upload(client, lines, sid=SID, **params):
    raw = ("\n".join(lines) + "\n").encode()
    r = client.post("/transcripts", params={"agent_id": "cc", "session_id": sid, **params},
                    content=raw)
    assert r.status_code == 200, r.text
    return r.json()


def archived(tmp_path, sid=SID) -> bytes:
    return gzip.decompress((Path(tmp_path) / "agents" / "cc" / "sessions" / "archive"
                            / f"{sid}.jsonl.gz").read_bytes())


# ── CC's six (#6936) ──

def test_late_post_through_each_pipe_never_lands(client, tmp_path):
    pause(client)
    resume(client)
    w = shift_windows(tmp_path, 3600)          # the window closed an hour ago
    t_in = (w["start"] + w["end"]) / 2
    now = time.time()
    labelled = {"seat": PAUSER, "client_now": iso(now)}

    # bridge auto-capture flush
    assert wb(client, "[AUTO-CAPTURE] 3 tool calls: rotated key",
              event_span=[iso(t_in - 5), iso(t_in)], **labelled) == "paused"
    # jsonl sync
    assert wb(client, "CC session activity (auto-sync from JSONL) one", source="tool",
              category="session_log", event_span=[iso(t_in), iso(t_in + 1)], **labelled) == "paused"
    # mnemo_save source=tool, timestamp only
    assert wb(client, "Rotated the deploy key on the build box", source="tool",
              timestamp=iso(t_in), **labelled) == "paused"
    # transcript archive
    body = upload(client, [line(w["start"] - 600, text="before"),
                           line(t_in, text="SECRET-IN-WINDOW"),
                           line(now, text="after")], **labelled)
    assert body["status"] == "archived"
    assert body["manifest"]["withheld_lines"] == 1
    assert b"SECRET-IN-WINDOW" not in archived(tmp_path)
    assert client.get("/transcripts", params={"agent_id": "cc", "q": "SECRET"}).json()["results"] == []

    # control: the same pipes with times outside the window land
    assert wb(client, "[AUTO-CAPTURE] 4 tool calls: after the window",
              event_span=[iso(now - 5), iso(now)], **labelled) != "paused"
    assert wb(client, "Shipped the widget after the rotation", source="tool",
              timestamp=iso(now), **labelled) != "paused"


def test_bystander_seat_events_land(client, tmp_path):
    pause(client)                              # cc-cc3 pauses, open now
    now = time.time()
    assert wb(client, "[AUTO-CAPTURE] 2 tool calls: bystander work", seat=BYSTANDER,
              event_span=[iso(now - 1), iso(now)], client_now=iso(now)) != "paused"
    body = upload(client, [line(now - 1, text="bystander-line"), line(now)],
                  seat=BYSTANDER, client_now=iso(now))
    assert body["manifest"]["withheld_lines"] == 0
    assert b"bystander-line" in archived(tmp_path)
    # the pauser, same moment, is withheld
    assert wb(client, "[AUTO-CAPTURE] 2 tool calls: pauser work", seat=PAUSER,
              event_span=[iso(now - 1), iso(now)], client_now=iso(now)) == "paused"


def test_unlabelled_client_is_over_gated(client, tmp_path):
    pause(client)                              # a seat pause on the tenant
    now = time.time()
    # no seat: could be any seat, so every window on the tenant applies
    assert wb(client, "[AUTO-CAPTURE] 2 tool calls: old bridge",
              event_span=[iso(now - 1), iso(now)], client_now=iso(now)) == "paused"
    # no seat and no clock (a pre-18c' client): post-time LAG rule
    assert wb(client, "CC session activity (auto-sync from JSONL) old",
              source="tool", category="session_log") == "paused"
    body = upload(client, [line(now - 1, text="old-shipper-line"), line(now)])
    assert body["manifest"]["withheld_lines"] == 2
    assert b"old-shipper-line" not in archived(tmp_path)


def test_untimed_line_before_its_in_window_sibling_is_withheld(client, tmp_path):
    pause(client)
    resume(client)
    w = shift_windows(tmp_path, 3600)
    t_in = (w["start"] + w["end"]) / 2
    lines = [line(w["start"] - 600, text="before"),
             blob("BLOB-SECRET request body"),     # written BEFORE its timed sibling
             line(t_in, kind="api-request", text="x"),
             line(w["end"] + 600, text="after")]
    body = upload(client, lines, seat=PAUSER, client_now=iso(time.time()))
    assert b"BLOB-SECRET" not in archived(tmp_path)
    assert body["manifest"]["withheld_lines"] == 2
    # control: a seat the window does not cover keeps the blob
    upload(client, lines, sid=SID2, seat=BYSTANDER, client_now=iso(time.time()))
    assert b"BLOB-SECRET" in archived(tmp_path, SID2)


def test_file_spanning_a_window_uploaded_twice_no_409_stubs_stable(client, tmp_path):
    now = time.time()
    pause(client)
    lines = [line(now - 3600, text="before-1"), line(now - 3590, kind="assistant", text="before-2"),
             line(now, text="IN-WINDOW-1"), blob("IN-WINDOW-BLOB"),
             line(now + 1, kind="api-request", text="x")]
    first = upload(client, lines, seat=PAUSER, client_now=iso(now))
    assert first["status"] == "archived" and first["manifest"]["withheld_lines"] == 3
    a1 = archived(tmp_path)
    resume(client)
    end = json.loads((Path(tmp_path) / "capture-windows.jsonl").read_text().splitlines()[-1])["end"]
    second = upload(client, lines + [line(end + 10, text="after-window")],
                    seat=PAUSER, client_now=iso(time.time()))
    assert second["status"] == "replaced"
    a2 = archived(tmp_path)
    assert a2.startswith(a1), "the stubs and lines of the first upload are stable"
    assert second["manifest"]["withheld_lines"] == 3
    assert b"after-window" in a2 and b"IN-WINDOW" not in a2
    stubs = [json.loads(x) for x in a2.splitlines() if STUB_TYPE.encode() in x]
    assert [s.get("was") for s in stubs] == ["user", "api-request-blob", "api-request"]
    assert second["manifest"]["windows"][0]["reason"] == "key rotation"


def test_client_clock_behind_ten_minutes_falls_back(client, tmp_path):
    pause(client)
    resume(client)
    w = shift_windows(tmp_path, 300)           # closed 5 min ago: inside the LAG
    t_in = (w["start"] + w["end"]) / 2
    skew = -600
    behind = {"seat": PAUSER, "client_now": iso(time.time() + skew)}
    # the event really happened at t_in; the slow clock stamped it 10 min early
    assert wb(client, "[AUTO-CAPTURE] 1 tool call: slow clock",
              event_span=[iso(t_in + skew), iso(t_in + skew)], **behind) == "paused"
    body = upload(client, [line(t_in + skew, text="SLOW-CLOCK-SECRET"),
                           line(t_in + skew + 1)], **behind)
    assert body["status"] == "unchanged" and body["held"] is True
    assert body["manifest"]["held_for_clock"] == 2
    assert not (Path(tmp_path) / "agents" / "cc" / "sessions" / "archive" / f"{SID}.jsonl.gz").exists()
    # control: the same span from an honest clock is outside the window and lands
    assert wb(client, "[AUTO-CAPTURE] 1 tool call: honest clock", seat=PAUSER,
              event_span=[iso(t_in + skew), iso(t_in + skew)],
              client_now=iso(time.time())) != "paused"


# ── the window log ──

def test_window_log_is_append_only_and_never_pruned(tmp_path):
    gate = CaptureGate(tmp_path)
    gate.pause(minutes=5, agent_id="cc", seat=PAUSER)
    gate.resume(agent_id="cc", seat=PAUSER)
    gate.pause(minutes=5, agent_id="cc", seat=PAUSER)
    gate.resume(agent_id="cc", seat=PAUSER)
    ws = gate.windows_for("cc", PAUSER)
    assert len(ws) == 2 and all(w["end"] is not None for w in ws)
    assert len(gate.window_log.read_text().splitlines()) == 4   # 2 opens + 2 closes
    w = ws[0]
    assert w["end"] - w["start"] >= 2 * GUARD_SECONDS


def test_repause_extends_the_open_window(tmp_path):
    gate = CaptureGate(tmp_path)
    a = gate.pause(minutes=5, agent_id="cc", seat=PAUSER)["window"]
    b = gate.pause(minutes=30, agent_id="cc", seat=PAUSER)["window"]
    assert a == b and len(gate.windows_for("cc", PAUSER)) == 1


def test_expired_pause_closes_its_window_at_resume_at_plus_guard(tmp_path):
    gate = CaptureGate(tmp_path)
    gate.pause(minutes=5, agent_id="cc", seat=PAUSER)
    state = json.loads(gate.pause_file.read_text())
    key = f"cc@{PAUSER}"
    state["pauses"][key]["resume_at"] = time.time() - 100
    gate.pause_file.write_text(json.dumps(state))
    (w,) = gate.windows_for("cc", PAUSER)
    assert abs(w["end"] - (state["pauses"][key]["resume_at"] + GUARD_SECONDS)) < 1


def test_pause_set_before_windows_existed_gets_one(tmp_path):
    gate = CaptureGate(tmp_path)
    now = time.time()
    gate.pause_file.write_text(json.dumps({"pauses": {"cc": {
        "paused_at": now - 60, "resume_at": now + 600, "reason": "old"}}}))
    (w,) = gate.windows_for("cc", PAUSER)
    assert w["end"] is None and abs(w["start"] - (now - 60 - GUARD_SECONDS)) < 1
    assert gate.covers(now - 30, "cc", PAUSER) == w["id"]


def test_seat_pause_is_not_lifted_by_another_seats_resume(tmp_path):
    gate = CaptureGate(tmp_path)
    gate.pause(minutes=5, agent_id="cc", seat=PAUSER)
    gate.resume(agent_id="cc", seat=BYSTANDER)
    gate.resume(agent_id="cc")
    assert gate.is_paused("cc")
    assert gate.windows_for("cc", PAUSER)[0]["end"] is None


# ── the transcript filter ──

def test_line_archived_before_a_pause_stays_archived(client, tmp_path):
    now = time.time()
    first = [line(now - 30, text="ALREADY-UPLOADED"), line(now - 20)]
    upload(client, first, seat=PAUSER, client_now=iso(now))
    pause(client)                    # padded start (now - GUARD) covers those lines
    body = upload(client, first + [line(time.time(), text="NEW-IN-WINDOW")],
                  seat=PAUSER, client_now=iso(time.time()))
    a = archived(tmp_path)
    assert b"ALREADY-UPLOADED" in a and b"NEW-IN-WINDOW" not in a
    assert body["manifest"]["withheld_lines"] == 1


def test_trailing_untimed_run_held_only_while_a_window_can_reach_it(client, tmp_path):
    now = time.time()
    cost = json.dumps({"type": "cost-state", "sessionId": SID, "totalCostUSD": 1})
    body = upload(client, [line(now - 10), cost], seat=PAUSER, client_now=iso(now))
    assert body["manifest"]["trailing_untimed_held"] == 0 and b"cost-state" in archived(tmp_path)
    pause(client)
    body = upload(client, [line(now - 10), cost], sid=SID2, seat=PAUSER, client_now=iso(now))
    assert body["manifest"]["trailing_untimed_held"] == 1
    assert b"cost-state" not in archived(tmp_path, SID2)


def test_archive_unit_without_windows_is_byte_identical(tmp_path):
    raw = ("\n".join([line(1.0e9), blob("b"), line(1.0e9 + 1)]) + "\n").encode()
    arc = TranscriptArchive(tmp_path, "cc")
    _, m = arc.upload(SID, raw, "cc", None)
    assert arc.raw(SID) == raw and m["withheld_lines"] == 0


# ── the endpoints ──

def test_capture_windows_endpoint_and_pause_reply(client):
    st = pause(client)
    assert "anything already uploaded before the pause stays uploaded" in st["not_withheld"]
    assert st["seat"] == PAUSER and st["window"]
    r = client.get("/capture/windows", params={"agent_id": "cc", "seat": PAUSER}).json()
    assert r["guard_seconds"] == GUARD_SECONDS
    assert [w["id"] for w in r["windows"]] == [st["window"]]
    assert client.get("/capture/windows", params={"agent_id": "opie"}).json()["windows"] == []
    assert client.get("/capture/windows", params={"seat": "bad seat"}).status_code == 400


def test_source_tool_is_gated_on_now_or_its_timestamp(client):
    pause(client)                              # open now
    now = time.time()
    # pre-window timestamp, trusted clock, but posted INSIDE the window
    assert wb(client, "Noted the old deploy before the rotation", source="tool", seat=PAUSER,
              timestamp=iso(now - 3600), client_now=iso(now)) == "paused"
    # control: a deliberate save (source=user) is not gated at all
    assert wb(client, "Chose to rotate every key on the box", source="user", seat=PAUSER,
              client_now=iso(now)) != "paused"


# ── review findings (code-reviewer pass on the first draft) ──

def test_last_prompt_copying_an_in_window_prompt_is_withheld(client, tmp_path):
    pause(client)
    resume(client)
    w = shift_windows(tmp_path, 3600)
    t_in = (w["start"] + w["end"]) / 2
    lp = json.dumps({"type": "last-prompt", "sessionId": SID, "leafUuid": "x",
                     "lastPrompt": "rotate KEY-IN-PROMPT now"})
    title = json.dumps({"type": "ai-title", "sessionId": SID, "aiTitle": "KEY-IN-TITLE"})
    mode = json.dumps({"type": "mode", "sessionId": SID, "mode": "normal"})
    upload(client, [line(w["start"] - 600), line(t_in, text="rotate KEY-IN-PROMPT now"),
                    line(w["end"] + 600, kind="assistant"), lp, title, mode,
                    line(w["end"] + 700)], seat=PAUSER, client_now=iso(time.time()))
    a = archived(tmp_path)
    assert b"KEY-IN-PROMPT" not in a and b"KEY-IN-TITLE" not in a
    assert any(json.loads(x).get("type") == "mode" for x in a.splitlines())  # content-free: kept


def test_untimed_line_in_an_idle_stretch_inside_a_window_is_withheld(client, tmp_path):
    pause(client)
    resume(client)
    w = shift_windows(tmp_path, 3600)
    first = [line(w["start"] - 600), blob("IDLE-IN-WINDOW")]
    body = upload(client, first, seat=PAUSER, client_now=iso(time.time()))
    assert body["manifest"]["trailing_untimed_held"] == 1
    upload(client, first + [line(w["end"] + 600)], seat=PAUSER, client_now=iso(time.time()))
    assert b"IDLE-IN-WINDOW" not in archived(tmp_path)


def test_one_unreadable_pause_file_read_does_not_close_a_live_window(tmp_path):
    gate = CaptureGate(tmp_path)
    gate.pause(minutes=60, agent_id="cc", seat=PAUSER)
    good = gate.pause_file.read_text()
    gate.pause_file.write_text(good[:10])                # a torn read
    gate.windows_for("cc", PAUSER)
    gate.pause_file.write_text(good)
    assert gate.covers(time.time() + 300, "cc", PAUSER) is not None


def test_timestamp_only_save_started_before_the_window_is_judged_to_its_post(client, tmp_path):
    pause(client)
    resume(client)
    w = shift_windows(tmp_path, 3600)
    assert wb(client, "Batch from before and during the rotation", source="tool", seat=PAUSER,
              timestamp=iso(w["start"] - 30), client_now=iso(time.time())) == "paused"


def test_no_window_partial_line_growth_answers_unchanged_not_an_error(client, tmp_path):
    now = time.time()
    two = [line(now - 20), line(now - 10)]
    base = "\n".join(two) + "\n"
    upload(client, two)
    raw = (base + '{"type":"user","message":"half').encode()
    r = client.post("/transcripts", params={"agent_id": "cc", "session_id": SID}, content=raw)
    assert r.status_code == 200 and r.json()["status"] in ("unchanged", "replaced")


# ── CC review #6942 ──
# F1: the transcript pointer is built by the server from the FILTERED archive,
# so it holds no in-window text; it is exempt from the window gate. The
# exemption is internal: nothing a client sends opens it.

def _on_disk(tmp_path, needle: bytes) -> list:
    """Every file under the data dir holding `needle` (gzip read through)."""
    hits = []
    for p in Path(tmp_path).rglob("*"):
        if not p.is_file():
            continue
        data = p.read_bytes()
        if p.suffix == ".gz":
            data = gzip.decompress(data)
        if needle in data:
            hits.append(str(p))
    return hits


def test_session_spanning_a_closed_window_gets_its_pointer_first_upload(client, tmp_path):
    pause(client)
    resume(client)
    w = shift_windows(tmp_path, 3600)
    t_in = (w["start"] + w["end"]) / 2
    now = time.time()
    body = upload(client, [line(w["start"] - 600, text="before the window"),
                           line(t_in, text="in the window"), line(now, text="now")],
                  seat=PAUSER, client_now=iso(now))
    assert body["status"] == "archived"
    assert body["pointer"] not in ("paused", "failed"), body["pointer"]
    assert body["manifest"]["pointer_memory_id"]


def test_pointer_never_holds_a_first_prompt_from_the_window(client, tmp_path):
    pause(client)
    resume(client)
    w = shift_windows(tmp_path, 3600)
    t_in = (w["start"] + w["end"]) / 2
    now = time.time()
    # the session starts before the window (no prompt yet); its first prompt
    # is in it, so the pointer must fall back to a later one
    body = upload(client, [line(w["start"] - 600, kind="assistant", text="ready"),
                           line(t_in, text="FIRST-PROMPT-SECRET"),
                           line(w["end"] + 600, text="a later prompt")],
                  seat=PAUSER, client_now=iso(now))
    assert body["manifest"]["pointer_memory_id"]
    assert "FIRST-PROMPT-SECRET" not in json.dumps(body)
    assert _on_disk(tmp_path, b"a later prompt")          # presence: the pointer landed
    assert _on_disk(tmp_path, b"FIRST-PROMPT-SECRET") == []


def test_pointer_written_while_the_window_is_live_holds_no_window_text(client, tmp_path):
    pause(client)                                          # open now
    now = time.time()
    body = upload(client, [line(now - 2, text="LIVE-SECRET-ONE"),
                           line(now - 1, text="LIVE-SECRET-TWO")],
                  seat=PAUSER, client_now=iso(now))
    assert body["manifest"]["withheld_lines"] == 2
    assert body["manifest"]["pointer_memory_id"]
    assert _on_disk(tmp_path, b"Full transcript archived")
    assert _on_disk(tmp_path, b"LIVE-SECRET") == []


def test_client_cannot_dress_a_save_as_a_pointer_to_pass_the_gate(client, tmp_path):
    pause(client)
    resume(client)
    w = shift_windows(tmp_path, 3600)
    t_in = (w["start"] + w["end"]) / 2
    now = time.time()
    body = upload(client, [line(w["start"] - 600), line(t_in), line(now)],
                  seat=PAUSER, client_now=iso(now))
    first_ts = body["manifest"]["first_ts"]
    pointer_shaped = {
        "session_id": f"transcript-{SID}", "agent_id": "cc", "seat": PAUSER,
        "summary": "Full transcript archived: rotate the key ...",
        "source": "tool", "category": "session_log", "timestamp": first_ts,
        "additional_tags": ["transcript-archive", f"session:{SID}"],
        "client_now": iso(now),
    }
    for params in ({}, {"window_exempt": "true", "exempt": "1", "pointer": "1"}):
        r = client.post("/writeback", params=params, json=pointer_shaped)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "paused"
    # extra fields on the body are not a way in either
    r = client.post("/writeback", json={**pointer_shaped, "window_exempt": True,
                                        "_window_exempt": True, "pointer": True})
    assert r.status_code == 200 and r.json()["status"] == "paused"


# H1: an event time in epoch MILLISECONDS read as seconds is ~57,000 years
# ahead: trusted, outside every window. A number that is not a plausible
# epoch-seconds value is "no event time".

def test_epoch_milliseconds_are_not_an_event_time(client, tmp_path):
    from agentb.capture_gate import clock_trusted, to_epoch
    pause(client)
    resume(client)
    w = shift_windows(tmp_path, 1200)          # closed 20 min ago: past the lookback
    t_in = (w["start"] + w["end"]) / 2
    now = time.time()
    # read as seconds, a ms span was far-future and landed; read as "no event
    # time" it would fall to the 10-minute lookback and land too. Refused.
    r = client.post("/writeback", json={
        "session_id": "s-ms", "agent_id": "cc", "seat": PAUSER, "client_now": now,
        "summary": "[AUTO-CAPTURE] 2 tool calls: ms span",
        "event_span": [t_in * 1000, t_in * 1000 + 5]})
    assert r.status_code == 422, r.text
    # control: the same span in seconds is judged, and withheld
    assert wb(client, "[AUTO-CAPTURE] 2 tool calls: s span", seat=PAUSER,
              event_span=[t_in, t_in + 5], client_now=now) == "paused"
    assert to_epoch(now) == now and to_epoch(iso(now)) is not None
    assert to_epoch(now * 1000) is None                    # ms
    assert to_epoch(5) is None                             # 1970: no real event time
    assert clock_trusted(now * 1000) is False


def test_old_shipper_with_a_skewed_clock_gets_a_text_free_pointer(client, tmp_path):
    # Reviewer I1: no client_now, clock 10 min slow. The filter trusts its line
    # times, so the archive keeps the line (the documented old-shipper gap);
    # the pointer, which recall serves, must not carry it. #7142: it is
    # saved TEXT-FREE, exempt from the gate.
    pause(client)                                          # open now
    now = time.time()
    body = upload(client, [line(now - 600, text="SKEW-SECRET")], seat=PAUSER)
    assert body["pointer"] == "archived"
    assert body["manifest"]["pointer_memory_id"]
    assert _on_disk(tmp_path, b"text withheld: line times not clock-trusted")
    assert [h for h in _on_disk(tmp_path, b"SKEW-SECRET") if "/sessions/" not in h] == []


# ── CC review #6944 (F2): the exemption needs EVERY archived line trusted ──
# A line archived by an old shipper (no client_now) is frozen; a later upload
# with a trusted clock must not exempt a pointer that can carry it. The
# control is test_session_spanning_a_closed_window_gets_its_pointer_first_upload.

def _outside_archive(tmp_path, needle: bytes) -> list:
    return [h for h in _on_disk(tmp_path, needle) if "/sessions/" not in h]


def test_untrusted_line_then_trusted_growth_keeps_the_pointer_text_free(client, tmp_path):
    pause(client)
    now = time.time()
    first = [line(now - 600, text="SKEW-SECRET")]          # old shipper, clock 10 min slow
    assert upload(client, first, seat=PAUSER)["pointer"] == "archived"   # text-free
    t = time.time()
    body = upload(client, first + [line(t, kind="assistant", text="ok")],
                  seat=PAUSER, client_now=iso(t))
    assert body["status"] == "replaced"
    assert body["manifest"]["lines_clock_trusted"] is False
    assert body["pointer"] is None                          # final: no second pointer
    assert _on_disk(tmp_path, b"SKEW-SECRET")               # presence: the archive has it
    assert _outside_archive(tmp_path, b"SKEW-SECRET") == []


def test_untrusted_line_then_trusted_identical_resend_keeps_the_pointer_text_free(client, tmp_path):
    pause(client)
    first = [line(time.time() - 600, text="SKEW-SECRET")]
    assert upload(client, first, seat=PAUSER)["pointer"] == "archived"   # text-free
    body = upload(client, first, seat=PAUSER, client_now=iso(time.time()))
    assert body["status"] == "unchanged"
    assert body["pointer"] is None                          # final: no second pointer
    assert _outside_archive(tmp_path, b"SKEW-SECRET") == []


def test_manifest_with_no_trust_marker_is_untrusted(client, tmp_path):
    pause(client)
    resume(client)
    w = shift_windows(tmp_path, 3600)
    t_in = (w["start"] + w["end"]) / 2
    lines = [line(w["start"] - 600), line(t_in), line(time.time())]
    body = upload(client, lines, seat=PAUSER, client_now=iso(time.time()))
    assert body["manifest"]["lines_clock_trusted"] is True    # all-trusted: exempt
    assert body["manifest"]["pointer_memory_id"]
    # an archive made before this build: no marker, pointer not yet written
    mpath = Path(tmp_path) / "agents" / "cc" / "sessions" / "archive" / f"{SID}.manifest.json"
    m = json.loads(mpath.read_text())
    del m["lines_clock_trusted"]
    m["pointer_memory_id"] = None
    mpath.write_text(json.dumps(m))
    body = upload(client, lines, seat=PAUSER, client_now=iso(time.time()))
    assert body["status"] == "unchanged"
    assert body["pointer"] == "archived"                    # untrusted: the text-free one
    assert _on_disk(tmp_path, b"text withheld: line times not clock-trusted")
    # and it stays untrusted when the file grows under a trusted clock
    t = time.time()
    body = upload(client, lines + [line(t, kind="assistant")], seat=PAUSER, client_now=iso(t))
    assert body["status"] == "replaced"
    assert body["manifest"]["lines_clock_trusted"] is False
    assert body["pointer"] is None                          # final


def test_ai_title_never_reaches_the_pointer_through_the_title_fallback(client, tmp_path):
    # CC's probe (#6944): the only prompt is in the window, so the pointer
    # falls back to the title; a title written after it copies the window.
    pause(client)
    resume(client)
    w = shift_windows(tmp_path, 3600)
    t_in = (w["start"] + w["end"]) / 2
    title = json.dumps({"type": "ai-title", "sessionId": SID, "aiTitle": "TITLE-SECRET"})
    body = upload(client, [line(w["start"] - 600, kind="assistant", text="ready"),
                           line(t_in, text="PROMPT-SECRET"), title,
                           line(w["end"] + 600, kind="assistant", text="done")],
                  seat=PAUSER, client_now=iso(time.time()))
    assert body["manifest"]["pointer_memory_id"]
    assert _on_disk(tmp_path, b"(no user prompt found)")
    assert _on_disk(tmp_path, b"TITLE-SECRET") == [] and _on_disk(tmp_path, b"PROMPT-SECRET") == []


# Reviewer pass on 888aa97: pin the clock-off and repaired paths, and reprocess.

def test_clock_sent_but_off_archives_lines_as_untrusted(client, tmp_path):
    now = time.time()                                      # no window: nothing held
    body = upload(client, [line(now - 5)], seat=PAUSER, client_now=iso(now - 600))
    assert body["status"] == "archived"
    assert body["manifest"]["lines_clock_trusted"] is False


def test_repair_that_archives_a_held_line_without_a_trusted_clock_is_untrusted(tmp_path):
    now = time.time()
    arch = TranscriptArchive(tmp_path)
    live = lambda lo, hi=None: "w1" if (lo if hi is None else hi) >= now - 100 else None
    none = lambda lo, hi=None: None
    lp = json.dumps({"type": "last-prompt", "sessionId": SID, "lastPrompt": "TRAIL"})
    raw = (line(now - 1000) + "\n" + lp + "\n").encode()
    st, m = arch.upload(SID, raw, "cc", None, covers=live, clock_trusted=True)
    assert m["lines_clock_trusted"] is True and m["trailing_untimed_held"] == 1
    arch._gz(SID).write_bytes(b"garbage")                  # corrupt: same bytes repair
    st, m = arch.upload(SID, raw, "cc", None, covers=none, clock_trusted=False)
    assert st == "repaired"
    assert m["lines_clock_trusted"] is False               # the held line was archived untrusted


def test_reprocess_keeps_the_trust_marker(tmp_path):
    arch = TranscriptArchive(tmp_path)
    arch.upload(SID, (line(time.time() - 10) + "\n").encode(), "cc", None, clock_trusted=True)
    mp = arch._manifest_path(SID)
    m = json.loads(mp.read_text())
    m["redaction_version"] = 0
    mp.write_text(json.dumps(m))
    assert arch.reprocess(SID)["status"] == "reprocessed"
    assert arch.manifest(SID)["lines_clock_trusted"] is True


# ── #7142: a session whose lines are not all clock-trusted gets a TEXT-FREE
# pointer, exempt from the gate. Before, the gate discarded its pointer, and
# every upload retried it for good (Winston's 755aa9c2 on the IGOR-2 host).

def _pointers(tmp_path) -> list[dict]:
    mem = Path(tmp_path) / "agents" / "cc" / "memory"
    out = [json.loads(p.read_text(encoding="utf-8")) for p in mem.glob("*.json")]
    return [m for m in out if "transcript-archive" in (m.get("additional_tags") or [])]


def _leaves(obj, skip=("sessionId", "timestamp", "ts")):
    """Every string value in a parsed line, except the keys whose values the
    text-free pointer may carry by design (the session id; times, which it
    re-renders)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k not in skip:
                yield from _leaves(v, skip)
    elif isinstance(obj, list):
        for v in obj:
            yield from _leaves(v, skip)
    elif isinstance(obj, str):
        yield obj


def _assert_no_archived_text(tmp_path, ptr):
    text = ptr["summary"] + "\n" + "\n".join(ptr.get("key_facts") or [])
    lines = archived(tmp_path).decode().splitlines()
    assert lines                                            # presence: there is an archive
    for raw in lines:                                       # every line, not a sample
        assert raw not in text
        for leaf in _leaves(json.loads(raw)):
            if len(leaf) >= 4:
                assert leaf not in text, (leaf, text)


def _old_shipper_lines(now):
    title = json.dumps({"type": "ai-title", "sessionId": SID, "aiTitle": "TITLE-SECRET"})
    cwd = json.dumps({"type": "system", "sessionId": SID, "cwd": "/srv/CWD-SECRET",
                      "timestamp": iso(now - 900)})
    hostile = json.dumps({"type": "system", "sessionId": SID, "content": "x",
                          "timestamp": "TS-SECRET-not-a-time"})
    # the hostile time sits between two lines timed outside the window, so
    # the archive keeps it verbatim (and it sorts last: it becomes last_ts)
    return [line(now - 1000, text="PROMPT-SECRET-OUTSIDE"), hostile, cwd, title,
            line(now - 1, text="LIVE-SECRET")]


def test_stuck_old_shipper_session_gets_a_text_free_pointer_on_next_upload(client, tmp_path):
    pause(client, seat=BYSTANDER)                           # live window on the tenant, seat cc
    now = time.time()
    lines = _old_shipper_lines(now)
    body = upload(client, lines, host="igor-2")             # old shipper: no client_now, no seat
    assert body["manifest"]["lines_clock_trusted"] is False
    # the state a pre-#7142 host left: no pointer, the gate's refusal recorded
    mpath = Path(tmp_path) / "agents" / "cc" / "sessions" / "archive" / f"{SID}.manifest.json"
    m = json.loads(mpath.read_text())
    for p in _pointers(tmp_path):
        (Path(tmp_path) / "agents" / "cc" / "memory" / f"{p['id']}.json").unlink()
    m["pointer_memory_id"] = None
    m["pointer_error"] = "writeback status paused"
    mpath.write_text(json.dumps(m))
    assert _pointers(tmp_path) == []

    lines.append(line(time.time(), kind="assistant", text="more"))
    body = upload(client, lines, host="igor-2")
    assert body["status"] == "replaced"
    assert body["pointer"] == "archived"
    assert body["manifest"]["pointer_memory_id"]
    assert "pointer_error" not in body["manifest"]
    [ptr] = _pointers(tmp_path)
    assert ptr["summary"].startswith(
        "Full transcript archived (text withheld: line times not clock-trusted): "
        f"session {SID} <")
    assert "igor-2" in ptr["summary"] and "seat none" in ptr["summary"]
    assert f"{body['manifest']['withheld_lines']} withheld" in ptr["summary"]
    _assert_no_archived_text(tmp_path, ptr)
    assert body["manifest"]["last_ts"] == "TS-SECRET-not-a-time"
    for needle in (b"PROMPT-SECRET", b"CWD-SECRET", b"TS-SECRET"):   # outside the window
        assert _on_disk(tmp_path, needle)                   # presence: the archive has them
    for needle in (b"PROMPT-SECRET", b"TITLE-SECRET", b"CWD-SECRET", b"TS-SECRET",
                   b"LIVE-SECRET"):
        assert [h for h in _on_disk(tmp_path, needle) if "/sessions/" not in h] == [], needle
    # final: the next upload writes no second pointer
    body = upload(client, lines + [line(time.time(), kind="assistant", text="again")],
                  host="igor-2", client_now=iso(time.time()))
    assert body["pointer"] is None and len(_pointers(tmp_path)) == 1


def test_untrusted_session_with_no_window_gets_the_text_free_pointer(client, tmp_path):
    # No window in the way today proves nothing: the gate judges the same
    # untrusted line times (the two skew tests below), so an untrusted
    # session never gets the text pointer.
    now = time.time()
    body = upload(client, [line(now - 1000, text="Rebuild the zebra"),
                           line(now - 5, kind="assistant", text="ok")])
    assert body["manifest"]["lines_clock_trusted"] is False
    [ptr] = _pointers(tmp_path)
    assert ptr["summary"].startswith("Full transcript archived (text withheld")
    assert _on_disk(tmp_path, b"Rebuild the zebra")                 # presence: archived
    assert _outside_archive(tmp_path, b"Rebuild the zebra") == []


# Reviewer pass on the #7142 build: two ways an untrusted line clock hides a
# window from the gate. Both put window text in a text pointer on master.

def test_fast_old_shipper_clock_cannot_put_window_text_in_the_pointer(client, tmp_path):
    pause(client)
    resume(client)
    shift_windows(tmp_path, 7200)                         # closed 2 h ago
    now = time.time()
    # typed inside that window; the shipper's clock is 1.5 h fast, so the
    # line says now-1800: outside the window, archived verbatim
    body = upload(client, [line(now - 1800, text="FAST-SECRET")])
    assert body["manifest"]["lines_clock_trusted"] is False
    assert body["manifest"]["pointer_memory_id"]
    assert _on_disk(tmp_path, b"FAST-SECRET")             # presence: the archive has it
    assert _outside_archive(tmp_path, b"FAST-SECRET") == []


def test_unparseable_first_ts_cannot_open_the_gate_for_window_text(client, tmp_path):
    pause(client)
    resume(client)
    shift_windows(tmp_path, 7200)
    now = time.time()
    bang = json.dumps({"type": "system", "sessionId": SID, "content": "x", "timestamp": "!"})
    body = upload(client, [bang, line(now - 4 * 3600, text="SLOW-SECRET")])
    assert body["manifest"]["first_ts"] == "!"
    assert body["manifest"]["pointer_memory_id"]
    assert _on_disk(tmp_path, b"SLOW-SECRET")
    assert _outside_archive(tmp_path, b"SLOW-SECRET") == []
    [ptr] = _pointers(tmp_path)
    assert " ? to " in ptr["summary"]                     # "!" is not a time: never printed


def test_out_of_range_first_ts_still_gets_a_pointer(client, tmp_path):
    pause(client)                                          # live window
    now = time.time()
    early = json.dumps({"type": "system", "sessionId": SID, "content": "x",
                        "timestamp": "0001-01-01T00:00:00+05:00"})
    body = upload(client, [early, line(now - 1000, text="hi")])
    assert body["manifest"]["first_ts"] == "0001-01-01T00:00:00+05:00"
    assert body["pointer"] == "archived", body["manifest"].get("pointer_error")
    assert body["manifest"]["pointer_memory_id"]


def test_trusted_session_still_writes_the_text_pointer_inside_a_live_window(client, tmp_path):
    pause(client)                                           # open now, seat cc-cc3
    now = time.time()
    body = upload(client, [line(now - 1000, text="OUTSIDE-PROMPT"),
                           line(now - 1, text="LIVE-SECRET")],
                  seat=PAUSER, client_now=iso(now))
    assert body["manifest"]["lines_clock_trusted"] is True
    [ptr] = _pointers(tmp_path)
    assert ptr["summary"].startswith("Full transcript archived: OUTSIDE-PROMPT")
    assert _on_disk(tmp_path, b"LIVE-SECRET") == []


def test_held_upload_on_the_untrusted_path_writes_no_pointer(client, tmp_path):
    pause(client)
    now = time.time()
    body = upload(client, [line(now - 1, text="LIVE-SECRET")],
                  seat=PAUSER, client_now=iso(now - 600))   # clock 10 min off: not trusted
    assert body["status"] == "unchanged" and body["held"] is True
    assert body["pointer"] is None
    assert _pointers(tmp_path) == []


def test_subagent_transcript_on_the_untrusted_path_writes_no_pointer(client, tmp_path):
    pause(client, seat=BYSTANDER)
    sub = f"{SID}_agent-a3181710c8d1f6bbc"
    body = upload(client, [line(time.time() - 1000, text="sub prompt")], sid=sub)
    assert body["manifest"]["parent_session_id"] == SID
    assert body["manifest"]["lines_clock_trusted"] is False
    assert body["pointer"] is None
    assert _pointers(tmp_path) == []


def test_pointer_time_keeps_only_a_parsed_time():
    from agentb.transcripts import pointer_time
    assert pointer_time("2026-11-24T00:40:38.025Z") == "2026-11-24T00:40:38.025000Z"
    assert pointer_time("2026-11-24T00:40:38") == "2026-11-24T00:40:38Z"
    for bad in ("TS-SECRET", "", None, 5, "!", "2026-11-24T00:40:38Z SECRET",
                "0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"):
        assert pointer_time(bad) is None
