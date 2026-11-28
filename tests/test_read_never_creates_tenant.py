"""A READ naming an unknown agent creates nothing (snag
mnemo-read-creates-tenant, ruling #7146 / #7159 / #7168).

Read endpoints take their tenant from TenantManager.peek: a known tenant
(cached, configured, or its data dir on disk) is the real one; an unknown one
is a throwaway built by the SAME code as a real tenant, in a fresh temp dir
outside the data dir, removed when the request ends. Writes keep get-or-create.

The same-answer test compares a throwaway's reply with the reply the same
build gives through get-or-create (peek patched to get), against the SAME data
dir path, so every field, paths included, must match.
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentb import ledger as ledger_mod
from agentb.config import (
    AgentBConfig, AgentConfig, ResilientProviderConfig, ProviderConfig,
    CacheConfig, ServerConfig, ClassificationConfig, DEFAULT_PERSONAS,
)

VEC = [0.0] * 768
VEC[0] = 1.0
_STATUS = {"primary": "fake", "active": "fake", "failed_over": False,
           "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}
NEW = "newbie"
SID = str(uuid.UUID(int=7))


class FakeEmbedding:
    active_label = "fake/embed"

    @property
    def status(self):
        return _STATUS

    async def embed(self, text, *, use_breaker=True, task_type="document"):
        return list(VEC)

    async def health_check(self):
        return True


class FakeReasoning:
    active_label = "fake/reason"

    @property
    def status(self):
        return _STATUS

    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True):
        return "VERDICT: PASS\nCONFIDENCE: 0.9\nREASON: fine"

    async def health_check(self):
        return True


def _cfg(data_dir: Path, agents=None) -> AgentBConfig:
    return AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50099),
        data_dir=str(data_dir),
        classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS),
        agents=agents or {},
    )


def _client(cfg, **kw):
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        return TestClient(create_app(cfg), **kw)


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """The 'system temp dir' for this test: a dir of its own, so its entry
    count is exact, and outside the data dir."""
    root = tmp_path / "systmp"
    root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(root))
    return root


def _tree(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))


def _q(agent_id):
    return {} if agent_id is None else {"agent_id": agent_id}


def _body(agent_id, **extra):
    return {**extra, **({} if agent_id is None else {"agent_id": agent_id})}


# The 11 read endpoints (plus the variants that reach a different read path).
READS = {
    "context": lambda c, a: c.post("/context", json=_body(a, prompt="what was I doing")),
    "preflight": lambda c, a: c.post("/preflight", json=_body(a, prompt="p", draft_response="d")),
    "trajectory_recall": lambda c, a: c.post("/trajectory/recall", json=_body(a, query="fix the bus")),
    "sessions": lambda c, a: c.get("/sessions", params=_q(a)),
    "session_transcript": lambda c, a: c.get("/sessions/s-1/transcript", params=_q(a)),
    "sessions_recent": lambda c, a: c.get("/sessions/recent", params=_q(a)),
    "transcripts_list": lambda c, a: c.get("/transcripts", params=_q(a)),
    "transcripts_search": lambda c, a: c.get("/transcripts", params={**_q(a), "q": "bus"}),
    "transcript_manifest": lambda c, a: c.get(f"/transcripts/{SID}", params=_q(a)),
    "transcript_raw": lambda c, a: c.get(f"/transcripts/{SID}", params={**_q(a), "format": "raw"}),
    "transcript_turns": lambda c, a: c.get(f"/transcripts/{SID}/turns", params=_q(a)),
    "vec_status": lambda c, a: c.get("/vec/status", params=_q(a)),
    "ledger_verify": lambda c, a: c.get("/ledger/verify", params=_q(a)),
}
VOLATILE = {"latency_ms"}
# presence: each read really ran (not a 403/422 that would match trivially)
EXPECT = {name: 200 for name in READS} | {
    "session_transcript": 404, "transcript_manifest": 404,
    "transcript_raw": 404, "transcript_turns": 404}


def _norm(resp):
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if k not in VOLATILE}
    return resp.status_code, body


def _read_all(client, agent_id):
    return {name: _norm(fn(client, agent_id)) for name, fn in READS.items()}


@pytest.mark.parametrize("agent_id", [NEW, None], ids=["named", "default"])
def test_unknown_read_gives_get_or_create_answer_and_creates_nothing(tmp_path, temp_root, agent_id):
    data = tmp_path / "data"
    data.mkdir()

    with _client(_cfg(data)) as c:
        before = _tree(data)  # after startup (facts.sqlite, capture gate)
        assert not (data / "agents").exists()
        tenants = c.app.state.tenants
        registry_before = list(tenants.active_tenants)
        health_before = c.get("/health").json()["agents_configured"]
        peeked = _read_all(c, agent_id)
        assert _tree(data) == before, "a read created something in the data dir"
        assert tenants.active_tenants == registry_before
        assert c.get("/health").json()["agents_configured"] == health_before
        assert _tree(temp_root) == [], "a throwaway temp dir outlived its request"
    assert not any(str(temp_root) in str(p) for p in ledger_mod._REGISTRY), \
        "a throwaway ledger stayed registered"

    # Reference: the same build, the same data dir path, through get-or-create.
    from agentb.server import TenantManager
    with patch.object(TenantManager, "peek", lambda self, a=None: (self.get(a), None)):
        with _client(_cfg(data)) as c:
            created = _read_all(c, agent_id)
    assert (data / "agents").is_dir(), "control: get-or-create must create the tenant"

    for name in READS:
        assert peeked[name][0] == EXPECT[name], (name, peeked[name])
        assert peeked[name] == created[name], name
    # presence: the path fields are real paths in the real data dir
    assert peeked["vec_status"][1]["db_path"].startswith(data.as_posix())
    assert peeked["ledger_verify"][1]["ledger"].startswith(data.resolve().as_posix())


def test_a_read_that_raises_still_removes_its_temp_dir(tmp_path, temp_root):
    data = tmp_path / "data"
    from agentb.transcripts import TranscriptArchive

    def boom(self, *a, **k):
        assert Path(self.dir).is_relative_to(temp_root)  # it really ran on a throwaway
        raise RuntimeError("forced")

    with _client(_cfg(data), raise_server_exceptions=False) as c, \
            patch.object(TranscriptArchive, "list_sessions", boom):
        r = c.get("/transcripts", params={"agent_id": NEW})
        assert r.status_code == 500
    assert _tree(temp_root) == []
    assert not (data / "agents" / NEW).exists()


def test_write_after_an_unknown_read_lands_in_a_real_tenant(tmp_path, temp_root):
    data = tmp_path / "data"
    with _client(_cfg(data)) as c:
        assert c.get("/vec/status", params={"agent_id": NEW}).json()["memory_entries_on_disk"] == 0
        assert not (data / "agents" / NEW).exists()
        r = c.post("/writeback", json={
            "session_id": "s-1", "agent_id": NEW,
            "summary": "the bus path points at the wrong database",
            "key_facts": ["bus db moved"]})
        assert r.status_code == 200, r.text
        assert list((data / "agents" / NEW / "memory").glob("*.json")), "write did not land on disk"
        assert NEW in c.app.state.tenants.active_tenants
        # the next read is the real tenant, and sees the write
        assert c.get("/vec/status", params={"agent_id": NEW}).json()["memory_entries_on_disk"] == 1
    assert _tree(temp_root) == []


def test_two_reads_at_once_get_two_temp_dirs(tmp_path, temp_root):
    from agentb.server import TenantManager
    tm = TenantManager(_cfg(tmp_path / "data"))
    (a, close_a), (b, close_b) = tm.peek(NEW), tm.peek(NEW)
    try:
        assert close_a is not None and close_b is not None
        assert a["data_dir"] != b["data_dir"]
        assert a["vec"] is not b["vec"]
        for t in (a, b):
            assert Path(t["data_dir"]).is_relative_to(temp_root)
            assert Path(t["data_dir"]).parent.name.startswith("mnemo-peek-")
        assert tm.active_tenants == []
    finally:
        close_a()
        close_b()
    assert _tree(temp_root) == []
    # every OS, not only Windows (where rmtree of an open file fails): the
    # sqlite handles are really closed
    for t in (a, b):
        for conn in (t["vec"]._conn, t["trajectories"].vec._conn):
            with pytest.raises(sqlite3.ProgrammingError):
                conn.execute("select 1")


def test_two_reads_racing_on_threads_share_nothing(tmp_path, temp_root):
    from agentb.server import TenantManager
    tm = TenantManager(_cfg(tmp_path / "data"))
    seen, gate = [], threading.Barrier(2)

    def one():
        t, close = tm.peek(NEW)
        gate.wait(timeout=10)  # both throwaways exist at the same moment
        seen.append(t["data_dir"])
        close()

    ts = [threading.Thread(target=one) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=20)
    assert len(set(seen)) == 2
    assert _tree(temp_root) == []


@pytest.mark.parametrize("how", ["data_dir", "config"])
def test_a_known_tenant_is_the_real_one(tmp_path, temp_root, how):
    from agentb.server import TenantManager
    data = tmp_path / "data"
    agents = {}
    if how == "data_dir":
        (data / "agents" / NEW).mkdir(parents=True)
    else:
        agents = {NEW: AgentConfig()}
    tm = TenantManager(_cfg(data, agents))
    t, close = tm.peek(NEW)
    try:
        assert close is None
        assert t is tm.get(NEW)
        assert Path(t["data_dir"]) == data / "agents" / NEW
    finally:
        tm.close()
    assert _tree(temp_root) == []


def test_a_bad_agent_id_is_still_400_and_creates_nothing(tmp_path, temp_root):
    data = tmp_path / "data"
    with _client(_cfg(data)) as c:
        before = _tree(data)
        for bad in ("../x", "a/b", "x" * 300):
            assert c.get("/sessions", params={"agent_id": bad}).status_code in (400, 422)
            assert c.get("/vec/status", params={"agent_id": bad}).status_code in (400, 422)
        assert _tree(data) == before
    assert _tree(temp_root) == []


def test_chat_keeps_get_or_create(tmp_path, temp_root):
    """/chat can save, so its recall leg stays on get-or-create (the write
    path); only the 11 pure reads moved to peek."""
    data = tmp_path / "data"
    with _client(_cfg(data)) as c:
        r = c.post("/chat", json={"agent_id": NEW, "message": "hi", "save": False})
        assert r.status_code == 200, r.text
        assert (data / "agents" / NEW).is_dir()


def test_the_temp_root_is_never_under_the_data_dir(tmp_path, monkeypatch):
    """peek() uses the system temp dir; with TMPDIR pointed inside the data
    dir it would still work, but our default never is. This pins that the
    throwaway path comes from tempfile, not from config.data_dir."""
    from agentb.server import TenantManager
    data = tmp_path / "data"
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("TEMP", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("TMP", str(tmp_path / "elsewhere"))
    (tmp_path / "elsewhere").mkdir()
    tm = TenantManager(_cfg(data))
    t, close = tm.peek(NEW)
    try:
        assert Path(t["data_dir"]).is_relative_to(tmp_path / "elsewhere")
        assert not Path(t["data_dir"]).is_relative_to(data)
    finally:
        close()
    assert not data.exists() or _tree(data) == []


def test_a_build_that_fails_partway_leaves_nothing(tmp_path, temp_root):
    """_build raises after VecStore opened its file: peek re-raises and its
    except branch removes the half-built temp dir (CC #7184 mutant A)."""
    import agentb.server as srv
    data = tmp_path / "data"
    ran = []

    def broken_traj(path):
        ran.append(Path(path))
        assert any(p.name == "vec_index.sqlite" for p in Path(path).parent.iterdir())
        raise RuntimeError("forced build failure")

    tm = srv.TenantManager(_cfg(data))
    with patch.object(srv, "TrajectoryStore", broken_traj):
        with pytest.raises(RuntimeError, match="forced build failure"):
            tm.peek(NEW)
    assert ran and ran[0].is_relative_to(temp_root)        # it really ran in a throwaway
    assert _tree(temp_root) == []
    assert not data.exists() or _tree(data) == []
    assert tm.active_tenants == []
    assert not any(str(temp_root) in str(p) for p in ledger_mod._REGISTRY)


def test_after_a_restart_a_tenant_on_disk_reads_as_real(tmp_path, temp_root):
    """A tenant written before a restart is KNOWN by its data dir: the new
    process reads the real data, never an empty throwaway (CC #7184 mutant B)."""
    data = tmp_path / "data"
    with _client(_cfg(data)) as c:
        r = c.post("/writeback", json={
            "session_id": "s-1", "agent_id": NEW,
            "summary": "the bus path points at the wrong database",
            "key_facts": ["bus db moved"]})
        assert r.status_code == 200, r.text
    with _client(_cfg(data)) as c:                          # a restart: new app, same dir
        assert NEW not in c.app.state.tenants.active_tenants
        assert c.get("/vec/status", params={"agent_id": NEW}).json()["memory_entries_on_disk"] == 1
        ctx = c.post("/context", json={"prompt": "bus database path", "agent_id": NEW}).json()
        assert any("wrong database" in ch["content"] for ch in ctx["chunks"]), ctx
    assert _tree(temp_root) == []
