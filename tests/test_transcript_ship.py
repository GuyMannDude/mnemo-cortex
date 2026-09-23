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


def test_ship_conflict_is_partial_and_not_reposted(app_client, tmp_path, capsys):
    root = _tree(tmp_path)
    state = tmp_path / "state.json"
    assert _run(app_client, root, state) == ship.EXIT_CLEAN
    f = root / "proj" / f"{OTHER_SID}.jsonl"
    f.write_bytes(b"#" + f.read_bytes())                  # divergent rewrite
    assert _run(app_client, root, state) == ship.EXIT_PARTIAL
    assert "conflict=1" in capsys.readouterr().out
    os.utime(f, None)                                     # touched, same bytes
    assert _run(app_client, root, state) == ship.EXIT_CLEAN
    assert "conflict=0" in capsys.readouterr().out


def test_ship_pause_is_partial_and_state_untouched(app_client, tmp_path, capsys):
    root = _tree(tmp_path)
    state = tmp_path / "state.json"
    app_client[0].post("/capture/pause", json={"minutes": 5})
    assert _run(app_client, root, state) == ship.EXIT_PARTIAL
    assert "paused=3" in capsys.readouterr().out
    assert not state.exists()


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
