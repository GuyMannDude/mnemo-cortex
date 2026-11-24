"""v4.25 tools/transcript-ship.py -- the client half of the archive tier.

Runs the real shipper against the real server app (TestClient stands in for
httpx.Client), so the seam between them is exercised, not two mocks.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.test_transcripts import (
    FakeEmbedding, FakeReasoning, SID, OTHER_SID, _cfg, specimen,
)

TOOL = Path(__file__).resolve().parent.parent / "tools" / "transcript-ship.py"


def _load_ship():
    spec = importlib.util.spec_from_file_location("transcript_ship", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ship = _load_ship()


def test_tool_is_pure_ascii():
    data = TOOL.read_bytes()
    assert all(b < 128 for b in data), "transcript-ship.py must stay pure ASCII"


def test_session_key_layouts(tmp_path):
    assert ship.session_key(tmp_path / "proj" / f"{SID}.jsonl") == SID
    sub = tmp_path / "proj" / SID / "subagents" / "agent-a5053797b5eff1463.jsonl"
    assert ship.session_key(sub) == f"{SID}_agent-a5053797b5eff1463"
    assert ship.session_key(tmp_path / "proj" / "agent-abc.jsonl") is None
    assert ship.session_key(tmp_path / "history.jsonl") is None


@pytest.fixture
def app_client(tmp_path):
    data = tmp_path / "server"
    data.mkdir()
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        with TestClient(create_app(_cfg(data))) as c:
            yield c, data


def _tree(tmp_path) -> Path:
    root = tmp_path / "projects"
    (root / "proj" / SID / "subagents").mkdir(parents=True)
    (root / "proj" / f"{SID}.jsonl").write_bytes(specimen())
    (root / "proj" / f"{OTHER_SID}.jsonl").write_bytes(specimen(OTHER_SID))
    (root / "proj" / SID / "subagents" / "agent-a1b2c3.jsonl").write_bytes(specimen())
    (root / "proj" / "notes.jsonl").write_bytes(b'{"x":1}\n')
    return root


class _Shared:
    """The shipper closes its client; the test's TestClient must outlive it."""

    def __init__(self, client):
        self.get, self.post = client.get, client.post

    def close(self):
        pass


def _run(app_client, root, state, *extra):
    client, _ = app_client
    with patch.object(ship.httpx, "Client", lambda base_url, timeout: _Shared(client)):
        return ship.main(["--root", str(root), "--agent", "cc", "--host", "igor-2",
                          "--server", "http://testserver", "--state", str(state), *extra])


def test_ship_end_to_end_then_skip_then_grow(app_client, tmp_path, capsys):
    root = _tree(tmp_path)
    state = tmp_path / "state.json"
    client = app_client[0]

    assert _run(app_client, root, state) == ship.EXIT_CLEAN
    out = capsys.readouterr().out
    assert "archived=3" in out and "not_a_session=1" in out
    listing = client.get("/transcripts", params={"agent_id": "cc"}).json()["sessions"]
    assert {s["session_id"] for s in listing} == {
        SID, OTHER_SID, f"{SID}_agent-a1b2c3"}

    # unchanged tree: nothing is read or posted
    assert _run(app_client, root, state) == ship.EXIT_CLEAN
    assert "skipped_same=3" in capsys.readouterr().out

    # a session grows: replaced, the rest skipped
    f = root / "proj" / f"{SID}.jsonl"
    f.write_bytes(f.read_bytes() + b'{"type":"system","content":"later"}\n')
    assert _run(app_client, root, state) == ship.EXIT_CLEAN
    out = capsys.readouterr().out
    assert "replaced=1" in out and "skipped_same=2" in out
    saved = json.loads(state.read_text(encoding="utf-8"))["files"]
    assert saved[str(f)]["status"] == "replaced"


def test_ship_file_vanished_after_listing_is_gone_not_failed(app_client, tmp_path, capsys):
    """Claude Code sweeps old sessions while the shipper runs: a file the walk
    listed can be gone by stat time. That is GONE (dropped from state, exit
    clean), never FAIL (2026-10-02: 8 of these turned an hourly cron red)."""
    root = _tree(tmp_path)
    state = tmp_path / "state.json"
    assert _run(app_client, root, state) == ship.EXIT_CLEAN
    capsys.readouterr()

    victim = root / "proj" / f"{OTHER_SID}.jsonl"
    real_key = ship.session_key

    def key_then_vanish(path):
        key = real_key(path)
        if path == victim:
            victim.unlink()          # between the walk and the stat
        return key

    with patch.object(ship, "session_key", key_then_vanish):
        assert _run(app_client, root, state) == ship.EXIT_CLEAN
    out = capsys.readouterr().out
    assert f"GONE {OTHER_SID}" in out
    assert "gone=1" in out and "failed=0" in out and "skipped_same=2" in out
    saved = json.loads(state.read_text(encoding="utf-8"))["files"]
    assert str(victim) not in saved


def test_ship_conflict_is_partial_and_not_reposted(app_client, tmp_path, capsys):
    root = _tree(tmp_path)
    state = tmp_path / "state.json"
    assert _run(app_client, root, state) == ship.EXIT_CLEAN
    f = root / "proj" / f"{OTHER_SID}.jsonl"
    f.write_bytes(b"#" + f.read_bytes())                  # divergent rewrite
    assert _run(app_client, root, state) == ship.EXIT_PARTIAL
    assert "conflict=1" in capsys.readouterr().out
    os.utime(f, None)                                     # touched, same bytes
    assert _run(app_client, root, state) == ship.EXIT_PARTIAL   # still red, not re-POSTed
    out = capsys.readouterr()
    assert "conflict=1" in out.out and "unresolved since" in out.err
    assert _run(app_client, root, state) == ship.EXIT_PARTIAL   # untouched: same verdict


def test_ship_through_a_pause_archives_lines_outside_the_window(app_client, tmp_path, capsys):
    # item 18b': the server no longer answers "paused" to an upload; it stubs
    # the window's lines itself, so the shipper (unchanged) archives and moves on.
    root = _tree(tmp_path)
    state = tmp_path / "state.json"
    app_client[0].post("/capture/pause", json={"minutes": 5})
    assert _run(app_client, root, state) == ship.EXIT_CLEAN
    out = capsys.readouterr().out
    assert "archived=3" in out and "paused=0" in out
    assert state.exists()


def test_ship_dry_run_sends_and_writes_nothing(app_client, tmp_path, capsys):
    root = _tree(tmp_path)
    state = tmp_path / "state.json"
    assert _run(app_client, root, state, "--dry-run") == ship.EXIT_CLEAN
    assert "would_ship=3" in capsys.readouterr().out
    assert not state.exists()
    assert app_client[0].get("/transcripts", params={"agent_id": "cc"}).json()["sessions"] == []


def test_ship_server_down_exits_2(tmp_path, capsys):
    root = _tree(tmp_path)
    rc = ship.main(["--root", str(root), "--agent", "cc", "--host", "igor-2",
                    "--server", "http://127.0.0.1:9", "--state", str(tmp_path / "s.json"),
                    "--timeout", "3"])
    assert rc == ship.EXIT_DOWN
    assert "server down" in capsys.readouterr().out


# ── item 18c': seat, client_now, the clock hold, old-shipper sessions ──

def _now_lines(sid: str, t: float, n: int = 4) -> bytes:
    from datetime import datetime as _dt, timezone as _tz
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(json.dumps({"sessionId": sid, "type": role, "uuid": f"n{i}",
                               "timestamp": _dt.fromtimestamp(t + i, _tz.utc).isoformat(),
                               "message": {"role": role, "content": f"now-line-{i}"}}))
    return ("\n".join(out) + "\n").encode()


class _Spy(_Shared):
    def __init__(self, client, seen):
        super().__init__(client)
        real = client.post

        def post(url, **kw):
            seen.append(kw.get("params") or {})
            return real(url, **kw)
        self.post = post


def _run_spy(app_client, root, state, seen, *extra):
    client, _ = app_client
    with patch.object(ship.httpx, "Client", lambda base_url, timeout: _Spy(client, seen)):
        return ship.main(["--root", str(root), "--agent", "cc", "--host", "igor",
                          "--server", "http://testserver", "--state", str(state), *extra])


def test_ship_sends_seat_and_iso_client_now(app_client, tmp_path):
    from datetime import datetime as _dt
    root = _tree(tmp_path)
    seen = []
    assert _run_spy(app_client, root, tmp_path / "s.json", seen, "--seat", "cc-cc3") == ship.EXIT_CLEAN
    assert seen and all(p["seat"] == "cc-cc3" for p in seen)
    for p in seen:
        assert isinstance(p["client_now"], str)
        assert _dt.fromisoformat(p["client_now"]).tzinfo is not None
    seen.clear()
    (root / "proj" / f"{SID}.jsonl").write_bytes(specimen() + b'{"type":"system","content":"x"}\n')
    _run_spy(app_client, root, tmp_path / "s.json", seen)
    assert seen and "seat" not in seen[0], "no --seat: no seat on the wire"


def test_ship_seat_bystander_lands_pauser_is_stubbed(app_client, tmp_path):
    client = app_client[0]
    client.post("/capture/pause", json={"agent_id": "cc", "seat": "linus-a", "minutes": 5})
    t = __import__("time").time()
    for seat, sid in (("linus-a", SID), ("linus-b", OTHER_SID)):
        root = tmp_path / seat
        (root / "proj").mkdir(parents=True)
        (root / "proj" / f"{sid}.jsonl").write_bytes(_now_lines(sid, t))
        assert _run(app_client, root, tmp_path / f"{seat}.json", "--seat", seat) == ship.EXIT_CLEAN
    got = {s["session_id"]: s for s in client.get(
        "/transcripts", params={"agent_id": "cc"}).json()["sessions"]}
    m_a = client.get(f"/transcripts/{SID}", params={"agent_id": "cc"}).json()
    m_b = client.get(f"/transcripts/{OTHER_SID}", params={"agent_id": "cc"}).json()
    assert SID in got and OTHER_SID in got
    assert (m_a.get("manifest") or m_a).get("withheld_lines") == 4
    assert (m_b.get("manifest") or m_b).get("withheld_lines", 0) == 0


class _SkewedDatetime(ship.datetime):
    @classmethod
    def now(cls, tz=None):
        return super().now(tz) + __import__("datetime").timedelta(minutes=10)


def test_ship_held_for_clock_is_resent_not_stored(app_client, tmp_path, capsys):
    """A reply held FOR CLOCK must not be stored as shipped: a file that does
    not grow would never ship its lines (#7039 q3)."""
    client = app_client[0]
    root = _tree(tmp_path)
    state = tmp_path / "state.json"
    assert _run(app_client, root, state) == ship.EXIT_CLEAN          # trusted first ship
    capsys.readouterr()
    client.post("/capture/pause", json={"agent_id": "cc", "minutes": 5})
    f = root / "proj" / f"{SID}.jsonl"
    f.write_bytes(f.read_bytes() + b'{"type":"system","content":"grown",'
                  b'"timestamp":"2026-09-22T10:10:00.000Z"}\n')
    with patch.object(ship, "datetime", _SkewedDatetime):
        assert _run(app_client, root, state) == ship.EXIT_PARTIAL    # clock 10 min fast: held
    cap = capsys.readouterr()
    assert f"HELD-CLOCK {SID}" in cap.err and "CLOCK: 1 file(s)" in cap.err
    assert "held_clock=1" in cap.out
    saved = json.loads(state.read_text(encoding="utf-8"))["files"]
    assert saved[str(f)]["status"] == "archived", "the held upload was not stored as shipped"
    client.post("/capture/resume", json={"all": True})
    # clock fixed, file unchanged (same mtime and size): it is re-sent and lands
    assert _run(app_client, root, state) == ship.EXIT_CLEAN
    assert "replaced=1" in capsys.readouterr().out


def test_ship_other_holds_stay_as_before(app_client, tmp_path, capsys):
    """An unfinished last line is a hold that needs growth: stored as before."""
    root = tmp_path / "projects"
    (root / "proj").mkdir(parents=True)
    f = root / "proj" / f"{SID}.jsonl"
    f.write_bytes(specimen())
    state = tmp_path / "state.json"
    assert _run(app_client, root, state) == ship.EXIT_CLEAN
    f.write_bytes(f.read_bytes() + b'{"type":"user","unfinished')
    assert _run(app_client, root, state) == ship.EXIT_CLEAN
    assert "held_clock=0" in capsys.readouterr().out


def test_old_shipper_session_stays_gated_after_new_shipper(app_client, tmp_path):
    """A session an old (no client_now) shipper archived a line of keeps its
    pointer gated after the new shipper re-ships it (server marker, 18b' F2)."""
    client = app_client[0]
    root = _tree(tmp_path)
    f = root / "proj" / f"{SID}.jsonl"
    r = client.post("/transcripts", params={"agent_id": "cc", "host": "igor", "session_id": SID},
                    content=f.read_bytes())                           # old shipper: no clock
    assert r.status_code == 200
    f.write_bytes(f.read_bytes() + b'{"type":"system","content":"later"}\n')
    assert _run(app_client, root, tmp_path / "s.json", "--seat", "cc-cc3") == ship.EXIT_CLEAN
    m = client.get(f"/transcripts/{SID}", params={"agent_id": "cc"}).json()
    assert (m.get("manifest") or m).get("lines_clock_trusted") is False
