"""item 18c' — the jsonl sync and its watchdog under capture-pause windows.

The real sync runs against the real server app: its urllib calls are routed
into a TestClient, so the seat / client_now / event_span seam and the 422 rule
(CC ruling #7033) meet the server's own validator and window gate, not a mock.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from tests.test_transcripts import FakeEmbedding, FakeReasoning, _cfg

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "integrations/claude-code/mnemo-cc-sync.py"
WATCHDOG = ROOT / "integrations/claude-code/mnemo-cc-sync-watchdog.sh"
TENANT = "scratch"
PAUSER, BYSTANDER = "linus-a", "linus-b"


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def msg(role: str, text: str, t: float | None) -> str:
    d = {"type": role, "message": {"role": role, "content": text}}
    if t is not None:
        d["timestamp"] = iso(t)
    return json.dumps(d) + "\n"


def turns(tag: str, t: float, n: int = 6) -> str:
    return "".join(msg("user" if i % 2 == 0 else "assistant", f"{tag}-{i}", t + i)
                   for i in range(n))


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """(sync module, TestClient, record of /writeback replies)."""
    data = tmp_path / "server"
    data.mkdir()
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        with TestClient(create_app(_cfg(data))) as client:
            spec = importlib.util.spec_from_file_location("ccsync_pause_under_test", SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            sessions = tmp_path / "projects"
            sessions.mkdir()
            monkeypatch.setattr(mod, "SESSIONS_DIR", sessions)
            monkeypatch.setattr(mod, "OFFSET_FILE", tmp_path / "state" / "offset.json")
            monkeypatch.setattr(mod, "AGENT_ID", TENANT)
            monkeypatch.setattr(mod, "SEATS", {"A": PAUSER, "B": BYSTANDER})
            replies = []

            def urlopen(req, timeout=None):
                u = urlsplit(req.full_url)
                path = u.path + (f"?{u.query}" if u.query else "")
                body = json.loads(req.data) if req.data else None
                r = client.request(req.get_method(), path, content=req.data,
                                   headers={"Content-Type": "application/json"})
                if u.path == "/writeback":
                    replies.append({"code": r.status_code, "sent": body,
                                    "reply": r.json() if r.status_code == 200 else r.text})
                if r.status_code >= 400:
                    raise urllib.error.HTTPError(req.full_url, r.status_code, "err", {},
                                                 io.BytesIO(r.content))
                return _Resp(r.content)

            monkeypatch.setattr(mod.urllib.request, "urlopen", urlopen)
            mod.main()   # seed on an empty tree: new files start at 0
            yield mod, client, replies


def landed(replies) -> list:
    return [r for r in replies if r["code"] == 200 and r["reply"].get("memory_id")]


def write(mod, folder: str, name: str, text: str) -> Path:
    d = mod.SESSIONS_DIR / folder
    d.mkdir(exist_ok=True)
    f = d / f"{name}.jsonl"
    with f.open("a") as fh:
        fh.write(text)
    return f


def state(mod) -> dict:
    return json.loads(mod.OFFSET_FILE.read_text())


# ── wire ──

def test_post_carries_seat_iso_clock_and_span(world):
    mod, _, replies = world
    now = time.time()
    write(mod, "A", "aaaa0001", turns("a", now - 30))
    assert mod.main(force=True) == 0
    sent = replies[-1]["sent"]
    assert sent["seat"] == PAUSER
    for v in [sent["client_now"], *sent["event_span"]]:
        assert isinstance(v, str), "ISO only, never epoch"
        assert datetime.fromisoformat(v.replace("Z", "+00:00"))
    assert mod.to_epoch(sent["event_span"][0]) == pytest.approx(now - 30, abs=1)
    assert mod.to_epoch(sent["event_span"][1]) == pytest.approx(now - 25, abs=1)
    assert state(mod)["seat_map"] == {"A": PAUSER, "B": BYSTANDER}


def test_unmapped_folder_sends_no_seat(world):
    mod, _, replies = world
    write(mod, "C", "cccc0001", turns("c", time.time() - 30))
    mod.main(force=True)
    assert "seat" not in replies[-1]["sent"]


# ── pause windows: the pauser's lines never land, the bystander's do ──

def test_pause_withholds_pauser_and_seatless_lands_bystander(world, capsys):
    mod, client, replies = world
    client.post("/capture/pause", json={"agent_id": TENANT, "seat": PAUSER, "minutes": 5})
    now = time.time()
    write(mod, "A", "aaaa0002", turns("pauser", now))
    write(mod, "B", "bbbb0002", turns("bystander", now))
    write(mod, "C", "cccc0002", turns("seatless", now))
    assert mod.main(force=True) == 0
    texts = " ".join(r["sent"]["summary"] for r in landed(replies))
    assert "bystander-0" in texts
    assert "pauser-0" not in texts and "seatless-0" not in texts
    out = capsys.readouterr().out
    assert "WITHHELD 6 msgs from aaaa0002 (seat linus-a)" in out
    assert "WITHHELD 6 msgs from cccc0002 (seat none)" in out
    w = state(mod)["withheld"]
    assert w["lines"] == 12 and w["window_ids"] and w["last_at"]
    # consumed, never re-sent
    replies.clear()
    assert mod.main(force=True) == 0
    assert replies == []


def test_mismatched_seat_is_not_covered(world):
    """Why the folder map must match the bridge's seat: a wrong seat leaks."""
    mod, client, replies = world
    mod.SEATS["A"] = "not-the-pauser"
    client.post("/capture/pause", json={"agent_id": TENANT, "seat": PAUSER, "minutes": 5})
    write(mod, "A", "aaaa0003", turns("wrongseat", time.time()))
    mod.main(force=True)
    assert any("wrongseat-0" in r["sent"]["summary"] for r in landed(replies))


def test_late_post_after_resume_never_lands(world):
    mod, client, replies = world
    client.post("/capture/pause", json={"agent_id": TENANT, "seat": PAUSER, "minutes": 5})
    t_in = time.time()
    client.post("/capture/resume", json={"agent_id": TENANT, "seat": PAUSER})
    time.sleep(0.01)
    write(mod, "A", "aaaa0004", turns("inwindow", t_in))
    mod.main(force=True)
    assert not any("inwindow" in r["sent"]["summary"] for r in landed(replies))


def test_server_backstop_when_the_windows_read_fails(world, monkeypatch, capsys):
    """No client filter (read failed): the server's own gate discards by span,
    the sync consumes it and records it for the watchdog."""
    mod, client, replies = world
    client.post("/capture/pause", json={"agent_id": TENANT, "seat": PAUSER, "minutes": 5})

    def down(since=None):
        raise urllib.error.URLError("windows down")

    monkeypatch.setattr(mod, "read_windows", down)
    write(mod, "A", "aaaa0005", turns("backstop", time.time()))
    assert mod.main(force=True) == 0
    assert replies[-1]["reply"]["status"] == "paused"
    out = capsys.readouterr()
    assert "DISCARDED 6 msgs from aaaa0005 by capture pause window w-" in out.out
    assert "memory_id=" not in out.out, "no 'posted' line after a discarded post"
    assert state(mod)["withheld"]["lines"] == 6


def test_untimed_message_beside_an_in_window_one_is_withheld(world):
    mod, client, replies = world
    client.post("/capture/pause", json={"agent_id": TENANT, "seat": PAUSER, "minutes": 5})
    now = time.time()
    body = (msg("user", "old-0", now - 3600) + msg("assistant", "old-1", now - 3599)
            + msg("user", "untimed-secret", None) + msg("assistant", "in-0", now)
            + msg("user", "in-1", now + 1) + msg("assistant", "in-2", now + 2))
    write(mod, "A", "aaaa0006", body)
    mod.main(force=True)
    # the CLIENT filter: what was sent (a post inside the window, GUARD-padded,
    # is discarded by the server whatever its span; that is the backstop test)
    texts = " ".join(r["sent"]["summary"] for r in replies)
    assert "old-0" in texts and "untimed-secret" not in texts and "in-0" not in texts


# ── the 422 rule ──

MS_SPAN = [1795476000000, 1795476000001]   # epoch MILLISECONDS: the server refuses it


def bad_span(monkeypatch, mod):
    monkeypatch.setattr(mod, "event_span", lambda messages: MS_SPAN)


def test_422_no_window_lands_bare_with_the_loud_line(world, monkeypatch, capsys):
    mod, _, replies = world
    bad_span(monkeypatch, mod)
    write(mod, "A", "aaaa0007", turns("bare", time.time() - 30))
    assert mod.main(force=True) == 1
    assert replies[0]["code"] == 422
    bare = landed(replies)
    assert len(bare) == 1 and "bare-0" in bare[0]["sent"]["summary"]
    assert not {"seat", "client_now", "event_span"} & set(bare[0]["sent"])
    err = capsys.readouterr().err
    assert "422 field event_span" in err and "RE-SENT BARE" in err
    replies.clear()
    assert mod.main(force=True) == 0 and replies == [], "offset advanced: not re-sent"


def test_422_with_a_window_lands_nothing_and_spools(world, monkeypatch, capsys):
    mod, client, replies = world
    # any window on the tenant since the session started, even another seat's
    client.post("/capture/pause", json={"agent_id": TENANT, "seat": BYSTANDER, "minutes": 5})
    bad_span(monkeypatch, mod)
    write(mod, "A", "aaaa0008", turns("spooled", time.time() - 30))
    assert mod.main(force=True) == 1
    assert landed(replies) == []
    spool = list((mod.OFFSET_FILE.parent / "422-spool").glob("*.json"))
    assert len(spool) == 1
    held = json.loads(spool[0].read_text())
    assert "spooled-0" in held["summary"] and held["event_span"] == MS_SPAN
    assert oct(spool[0].stat().st_mode & 0o777) == "0o600"
    err = capsys.readouterr().err
    assert "422 field event_span" in err and str(spool[0]) in err and "NOT re-sent" in err
    assert state(mod)["spool_422"]["count"] == 1


def test_422_windows_read_fails_spools_like_a_window(world, monkeypatch, capsys):
    mod, _, replies = world
    bad_span(monkeypatch, mod)

    def down(since=None):
        raise urllib.error.URLError("windows down")

    monkeypatch.setattr(mod, "read_windows", down)
    write(mod, "A", "aaaa0009", turns("readfail", time.time() - 30))
    assert mod.main(force=True) == 1
    assert landed(replies) == []
    assert len(list((mod.OFFSET_FILE.parent / "422-spool").glob("*.json"))) == 1
    assert "windows read failed" in capsys.readouterr().err


def test_422_since_is_the_sessions_first_timed_line(world, monkeypatch):
    mod, client, replies = world
    seen = []
    real = mod.read_windows

    def spy(since=None):
        seen.append(since)
        return real(since)

    monkeypatch.setattr(mod, "read_windows", spy)
    bad_span(monkeypatch, mod)
    start = time.time() - 600
    write(mod, "A", "aaaa0010", msg("user", "first", start) + turns("x", time.time() - 30))
    mod.main(force=True)
    assert seen[-1] == pytest.approx(start, abs=0.01)


def test_control_good_span_lands_quietly(world, capsys):
    mod, _, replies = world
    write(mod, "A", "aaaa0011", turns("good", time.time() - 30))
    assert mod.main(force=True) == 0
    assert len(landed(replies)) == 1 and replies[0]["code"] == 200
    assert "422" not in capsys.readouterr().err
    assert not (mod.OFFSET_FILE.parent / "422-spool").exists()


# ── watchdog: OK / PAUSED / FAIL ──

def run_watchdog(tmp_path, offset: Path, sessions: Path) -> subprocess.CompletedProcess:
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    (fake / "systemctl").write_text("#!/bin/sh\nexit 0\n")
    (fake / "systemctl").chmod(0o755)
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "HOME": str(tmp_path),
           "MNEMO_CC_OFFSET_FILE": str(offset), "MNEMO_CC_SESSIONS_DIR": str(sessions)}
    return subprocess.run(["bash", str(WATCHDOG)], env=env, capture_output=True, text=True)


@pytest.fixture
def wd(tmp_path):
    sessions = tmp_path / "projects" / "A"
    sessions.mkdir(parents=True)
    f = sessions / "s1.jsonl"
    f.write_text(turns("w", time.time()))
    offset = tmp_path / "state" / "offset.json"
    offset.parent.mkdir()

    def put(**extra):
        offset.write_text(json.dumps({"files": {"A/s1.jsonl": {"byte_offset": f.stat().st_size}},
                                      **extra}))
    return tmp_path, offset, tmp_path / "projects", put, f


def test_watchdog_ok(wd):
    tmp, offset, sessions, put, _ = wd
    put()
    r = run_watchdog(tmp, offset, sessions)
    assert r.returncode == 0 and r.stdout.startswith("OK:")


def test_watchdog_paused_is_exit_0_with_the_line(wd):
    tmp, offset, sessions, put, _ = wd
    put(withheld={"window_ids": ["w-1"], "lines": 6, "last_at": iso(time.time() - 30)})
    r = run_watchdog(tmp, offset, sessions)
    assert r.returncode == 0, r.stdout
    assert r.stdout.startswith("WATCHDOG PAUSED:") and "w-1" in r.stdout and "6 line(s)" in r.stdout


def test_watchdog_old_withhold_is_plain_ok(wd):
    tmp, offset, sessions, put, _ = wd
    put(withheld={"window_ids": ["w-1"], "lines": 6, "last_at": iso(time.time() - 3600)})
    r = run_watchdog(tmp, offset, sessions)
    assert r.returncode == 0 and r.stdout.startswith("OK:")


def test_watchdog_422_spool_fails_and_outranks_paused(wd):
    tmp, offset, sessions, put, _ = wd
    put(withheld={"window_ids": ["w-1"], "lines": 6, "last_at": iso(time.time() - 30)})
    (offset.parent / "422-spool").mkdir()
    (offset.parent / "422-spool" / "x.json").write_text("{}")
    r = run_watchdog(tmp, offset, sessions)
    assert r.returncode == 1
    assert "WATCHDOG FAIL" in r.stdout and "422" in r.stdout and "PAUSED" not in r.stdout


def test_watchdog_422_spool_fails_with_no_active_session(wd):
    tmp, offset, sessions, put, f = wd
    put()
    old = time.time() - 48 * 3600
    os.utime(f, (old, old))
    (offset.parent / "422-spool").mkdir()
    (offset.parent / "422-spool" / "x.json").write_text("{}")
    r = run_watchdog(tmp, offset, sessions)
    assert r.returncode == 1 and "422" in r.stdout


def test_watchdog_backlog_still_fails_during_a_pause(wd):
    tmp, offset, sessions, put, f = wd
    put(withheld={"window_ids": ["w-1"], "lines": 6, "last_at": iso(time.time() - 30)})
    with f.open("a") as fh:
        fh.write(turns("stuck", time.time()))
    old = time.time() - 1200
    os.utime(f, (old, old))
    r = run_watchdog(tmp, offset, sessions)
    assert r.returncode == 1 and "stuck" in r.stdout


# ── event_span is what stops a truly late post (review finding 4) ──
# Every clock (server, gate, sync) jumps an hour past the window and the
# client filter is off: only the span can catch the in-window lines.

def _shift_clocks(monkeypatch, mod, late: float):
    import types
    import agentb.capture_gate as G
    import agentb.server as S
    real = time.time
    fake = types.SimpleNamespace(**{k: getattr(time, k) for k in dir(time) if not k.startswith("__")})
    fake.time = lambda: real() + late
    for m in (S, G, mod):
        monkeypatch.setattr(m, "time", fake)


def _windows_down(since=None):
    raise urllib.error.URLError("no client filter")


def test_truly_late_post_is_judged_by_its_span(world, monkeypatch):
    mod, client, replies = world
    client.post("/capture/pause", json={"agent_id": TENANT, "seat": PAUSER, "minutes": 5})
    t_in = time.time()
    client.post("/capture/resume", json={"agent_id": TENANT, "seat": PAUSER})
    write(mod, "A", "aaaa9999", turns("inwindow", t_in))
    _shift_clocks(monkeypatch, mod, 3600.0)
    monkeypatch.setattr(mod, "read_windows", _windows_down)
    mod.main(force=True)
    assert replies and replies[-1]["code"] == 200, replies
    assert replies[-1]["reply"]["status"] == "paused", replies[-1]
    assert not landed(replies)


def test_control_the_same_late_post_without_a_span_lands(world, monkeypatch):
    mod, client, replies = world
    client.post("/capture/pause", json={"agent_id": TENANT, "seat": PAUSER, "minutes": 5})
    t_in = time.time()
    client.post("/capture/resume", json={"agent_id": TENANT, "seat": PAUSER})
    write(mod, "A", "aaaa9998", turns("inwindow", t_in))
    _shift_clocks(monkeypatch, mod, 3600.0)
    monkeypatch.setattr(mod, "read_windows", _windows_down)
    monkeypatch.setattr(mod, "event_span", lambda m: None)
    mod.main(force=True)
    assert landed(replies), replies


def test_422_outcome_is_persisted_before_the_tick_ends(world, monkeypatch):
    """A crash after the bare re-send must not re-send it again (finding 5)."""
    mod, _, replies = world
    bad_span(monkeypatch, mod)
    write(mod, "A", "aaaa0012", turns("persist", time.time() - 30))
    real_save, saves = mod.save_state, []
    monkeypatch.setattr(mod, "save_state", lambda st: (saves.append(json.loads(json.dumps(st))), real_save(st)))
    mod.main(force=True)
    assert len(saves) >= 2, "a save before the tick's final one"
    entry = saves[0]["files"]["A/aaaa0012.jsonl"]
    assert entry["byte_offset"] > 0, "offset saved right after the 422 outcome"
