"""v4.25 transcript archive tier -- parser, archive, endpoints.

Contract: a client session transcript (Claude Code JSONL) is archived whole,
secret-redacted before it touches disk, indexed for FTS, never embedded.
Same bytes = no-op; a longer upload whose prefix matches = replace; anything
else = 409. The first upload of a top-level session writes exactly one
pointer memory; nothing else from this tier reaches recall.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentb.config import (
    AgentBConfig, ResilientProviderConfig, ProviderConfig,
    CacheConfig, ServerConfig, ClassificationConfig, DEFAULT_PERSONAS,
)
from agentb.transcripts import (
    TranscriptArchive, TranscriptConflict, parse_transcript,
    validate_transcript_id, parent_of, pointer_text,
)

SID = "0f8fad5b-d9cb-469f-a165-70867728950e"
OTHER_SID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
# Built from pieces so this file never holds a literal key shape.
FAKE_KEY = "sk-" + "ant-" + "api03-" + "A1b2C3d4E5f6G7h8I9j0" * 2

VEC = [0.0] * 768
VEC[0] = 1.0
_STATUS = {"primary": "fake", "active": "fake", "failed_over": False,
           "circuit_open": False, "primary_retry_in": None, "fallback_count": 0}


class FakeEmbedding:
    active_label = "fake/embed"
    calls: list = []

    @property
    def status(self): return _STATUS

    async def embed(self, text, *, use_breaker=True, task_type="document"):
        FakeEmbedding.calls.append(text)
        return list(VEC)

    async def health_check(self): return True


class FakeReasoning:
    active_label = "fake/reason"

    @property
    def status(self): return _STATUS

    async def generate(self, prompt, system="", max_tokens=2048, *, use_breaker=True):
        return "decision"

    async def health_check(self): return True


def _line(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def specimen_lines(sid: str = SID) -> list[str]:
    base = {"sessionId": sid, "cwd": "/home/claude", "version": "2.1.280",
            "gitBranch": "main", "isSidechain": False}
    return [
        _line({"type": "permission-mode", "permissionMode": "default", "sessionId": sid}),
        _line({**base, "type": "user", "uuid": "u1", "timestamp": "2026-09-22T10:00:00.000Z",
               "message": {"role": "user", "content": "Rebuild the zebra-crossing widget please"}}),
        _line({**base, "type": "user", "uuid": "u1m", "isMeta": True,
               "timestamp": "2026-09-22T10:00:00.100Z",
               "message": {"role": "user", "content": "<local-command-caveat>x</local-command-caveat>"}}),
        _line({**base, "type": "assistant", "uuid": "a1", "timestamp": "2026-09-22T10:00:01.000Z",
               "message": {"id": "msg_1", "role": "assistant", "content": [
                   {"type": "thinking", "thinking": "Consider the quokka path first",
                    "signature": "c2ln"}]}}),
        _line({**base, "type": "assistant", "uuid": "a2", "timestamp": "2026-09-22T10:00:02.000Z",
               "message": {"id": "msg_1", "role": "assistant", "content": [
                   {"type": "tool_use", "id": "toolu_1", "name": "Bash",
                    "input": {"command": "grep -r marmoset src/"}}]}}),
        _line({**base, "type": "user", "uuid": "u2", "timestamp": "2026-09-22T10:00:03.000Z",
               "toolUseResult": {"stdout": f"ANTHROPIC_API_KEY={FAKE_KEY}"},
               "message": {"role": "user", "content": [
                   {"type": "tool_result", "tool_use_id": "toolu_1",
                    "content": [{"type": "text", "text": f"found key {FAKE_KEY} in env"},
                                {"type": "image", "source": {"data": "AAAA"}}]}]}}),
        _line({**base, "type": "assistant", "uuid": "a3", "timestamp": "2026-09-22T10:00:04.000Z",
               "message": {"id": "msg_2", "role": "assistant", "content": [
                   {"type": "text", "text": "Done: the pangolin handler is fixed."}]}}),
        _line({**base, "type": "system", "subtype": "compact_boundary",
               "timestamp": "2026-09-22T10:00:05.000Z", "content": "Conversation compacted"}),
        _line({**base, "type": "attachment", "timestamp": "2026-09-22T10:00:06.000Z",
               "attachment": {"type": "hook_additional_context", "content": ["capybara note"]}}),
        _line({"type": "ai-title", "aiTitle": "Zebra widget rebuild", "sessionId": sid}),
        _line({"type": "brand-new-line-type", "sessionId": sid, "payload": {"x": 1}}),
        '{"type": "user", "message": {"role": "user", "content": "trunc',   # bad line
        _line({**base, "type": "user", "uuid": "u3", "timestamp": "2026-09-22T10:05:00.000Z",
               "message": {"role": "user", "content": [
                   {"type": "text", "text": "Now ship it"}]}}),
    ]


def specimen(sid: str = SID, extra: list[str] | None = None) -> bytes:
    return ("\n".join(specimen_lines(sid) + (extra or [])) + "\n").encode("utf-8")


# -------------------------------------------------------------------------
#  Parser
# -------------------------------------------------------------------------

def test_parser_covers_every_block_type_bad_and_unknown_lines():
    p = parse_transcript(specimen())
    kinds = {(t.role, t.kind) for t in p.turns}
    assert ("user", "text") in kinds
    assert ("assistant", "thinking") in kinds
    assert ("assistant", "tool_use") in kinds
    assert ("user", "tool_result") in kinds
    assert ("assistant", "text") in kinds
    assert ("system", "compact_boundary") in kinds
    assert ("attachment", "hook_additional_context") in kinds

    assert p.line_count == 13
    assert p.bad_lines == 1
    assert p.line_types["brand-new-line-type"] == 1
    assert p.user_turns == 2            # meta line and tool_result line are not prompts
    assert p.assistant_turns == 2       # msg_1 spans two lines
    assert p.tool_use == 1 and p.tool_result == 1
    assert p.first_ts == "2026-09-22T10:00:00.000Z"
    assert p.last_ts == "2026-09-22T10:05:00.000Z"
    assert p.title == "Zebra widget rebuild"
    assert p.client_version == "2.1.280"
    assert p.cwd == "/home/claude"
    assert p.first_user_text == "Rebuild the zebra-crossing widget please"
    tool_result = next(t for t in p.turns if t.kind == "tool_result")
    assert tool_result.tool_name == "Bash"          # resolved through tool_use_id
    assert "[image]" in tool_result.text


def test_parser_redacts_every_string_including_tool_use_result():
    p = parse_transcript(specimen())
    blob = "\n".join(p.lines)
    assert FAKE_KEY not in blob
    assert "[REDACTED:" in blob
    # the message text AND the structured toolUseResult copy both counted
    assert sum(p.redactions.values()) >= 2
    assert all(FAKE_KEY not in t.text for t in p.turns)


def test_parser_keeps_unredacted_lines_byte_exact():
    raw = specimen()
    p = parse_transcript(raw)
    first = raw.decode().split("\n")[0]
    assert p.lines[0] == first


def test_transcript_id_rules():
    assert validate_transcript_id(SID) == SID
    sub = f"{SID}_agent-a3181710c8d1f6bbc"
    assert validate_transcript_id(sub) == sub
    assert parent_of(sub) == SID and parent_of(SID) is None
    for bad in ("../etc", "2026-09-22_101010_abc123", SID + "x", ""):
        with pytest.raises(ValueError):
            validate_transcript_id(bad)


# -------------------------------------------------------------------------
#  Archive
# -------------------------------------------------------------------------

def test_archive_idempotent_superset_and_divergent(tmp_path):
    arc = TranscriptArchive(tmp_path, "cc")
    raw = specimen()
    status, m = arc.upload(SID, raw, "cc", "igor-2")
    assert status == "archived"
    assert m["sha256"] == hashlib.sha256(raw).hexdigest() and m["bytes"] == len(raw)

    status, m2 = arc.upload(SID, raw, "cc", "igor-2")
    assert status == "unchanged" and m2["uploads"] == 1

    grown = raw + (_line({"type": "user", "sessionId": SID,
                          "timestamp": "2026-09-22T11:00:00.000Z",
                          "message": {"role": "user", "content": "one more ocelot"}}) + "\n").encode()
    status, m3 = arc.upload(SID, grown, "cc", "igor-2")
    assert status == "replaced" and m3["uploads"] == 2 and m3["user_turns"] == 3
    assert m3["first_uploaded_at"] == m["first_uploaded_at"]

    with pytest.raises(TranscriptConflict) as smaller:
        arc.upload(SID, raw, "cc", "igor-2")
    assert smaller.value.existing_sha256 == m3["sha256"]
    assert smaller.value.new_sha256 == hashlib.sha256(raw).hexdigest()

    divergent = b"X" + grown
    with pytest.raises(TranscriptConflict) as div:
        arc.upload(SID, divergent, "cc", "igor-2")
    assert "differ" in div.value.reason


def test_archive_on_disk_is_redacted_and_gz(tmp_path):
    arc = TranscriptArchive(tmp_path, "cc")
    _, m = arc.upload(SID, specimen(), "cc", None)
    assert m["redactions_applied"] >= 2
    gz = tmp_path / "sessions" / "archive" / f"{SID}.jsonl.gz"
    with gzip.open(gz, "rb") as f:
        disk = f.read()
    assert FAKE_KEY.encode() not in disk
    for path in (tmp_path / "sessions" / "archive").iterdir():
        if path.suffix != ".gz":
            assert FAKE_KEY.encode() not in path.read_bytes(), path.name
    assert arc.raw(SID) == disk


def test_archive_fts_search_and_paging(tmp_path):
    arc = TranscriptArchive(tmp_path, "cc")
    arc.upload(SID, specimen(), "cc", "igor")
    arc.upload(OTHER_SID, specimen(OTHER_SID), "cc", "igor")
    hits = arc.search("marmoset")
    assert {h["session_id"] for h in hits} == {SID, OTHER_SID}
    assert all(h["kind"] == "tool_use" and "[marmoset]" in h["snippet"] for h in hits)
    assert arc.search("pangolin handler")[0]["role"] == "assistant"
    # FTS operators in a query are text, not syntax
    assert arc.search('"quokka" ( *') and arc.search("zzzznothing") == []
    with pytest.raises(ValueError):
        arc.search("  ?? ")
    page = arc.turns(SID, from_seq=1, to_seq=2)
    assert [t["seq"] for t in page] == [1, 2]
    assert arc.turns(OTHER_SID.replace("7", "8"), 0) is None


def test_archive_replace_reindexes_without_duplicates(tmp_path):
    arc = TranscriptArchive(tmp_path, "cc")
    raw = specimen()
    arc.upload(SID, raw, "cc", None)
    before = len(arc.search("marmoset"))
    arc.upload(SID, raw + b'{"type":"system","content":"tail"}\n', "cc", None)
    assert len(arc.search("marmoset")) == before == 1


def test_archive_rejects_nothing_parseable(tmp_path):
    arc = TranscriptArchive(tmp_path, "cc")
    with pytest.raises(ValueError):
        arc.upload(SID, b"not json\nstill not\n", "cc", None)
    assert arc.manifest(SID) is None


def test_pointer_text_shape():
    text = pointer_text({"first_user_text": "a " * 300, "user_turns": 4,
                         "first_ts": "T1", "last_ts": "T2", "host": "igor"})
    assert text.startswith("Full transcript archived: ")
    assert "<4 user turns, T1 to T2, igor>" in text
    assert len(text) < 300


# -------------------------------------------------------------------------
#  Endpoints
# -------------------------------------------------------------------------

def _cfg(tmp_path, **server_kw):
    return AgentBConfig(
        reasoning=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="x")),
        embedding=ResilientProviderConfig(primary=ProviderConfig(provider="ollama", model="nomic-embed-text")),
        cache=CacheConfig(), server=ServerConfig(host="127.0.0.1", port=50099, **server_kw),
        data_dir=str(tmp_path),
        classification=ClassificationConfig(enabled=False),
        personas=dict(DEFAULT_PERSONAS),
    )


@pytest.fixture
def make_client(tmp_path):
    def _make(**server_kw):
        FakeEmbedding.calls = []
        with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
             patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
            from agentb.server import create_app
            return TestClient(create_app(_cfg(tmp_path, **server_kw)))
    return _make


@pytest.fixture
def client(make_client):
    with make_client() as c:
        yield c


def _memories(tmp_path, agent="cc") -> list[dict]:
    mem = Path(tmp_path) / "agents" / agent / "memory"
    return [json.loads(p.read_text(encoding="utf-8")) for p in mem.glob("*.json")]


def test_endpoint_upload_writes_one_pointer_and_embeds_only_it(client, tmp_path):
    raw = specimen()
    r = client.post("/transcripts", params={"agent_id": "cc", "host": "igor-2"}, content=raw)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "archived" and body["pointer"] == "archived"
    m = body["manifest"]
    assert m["session_id"] == SID                   # derived from the body
    assert m["pointer_memory_id"]

    mems = _memories(tmp_path)
    assert len(mems) == 1
    ptr = mems[0]
    assert ptr["category"] == "session_log" and ptr["source"] == "tool"
    assert "transcript-archive" in ptr["additional_tags"]
    assert f"session:{SID}" in ptr["additional_tags"]
    assert ptr["summary"].startswith("Full transcript archived: Rebuild the zebra")
    # the ONLY embedding the tier caused is the pointer's
    assert len(FakeEmbedding.calls) == 1
    assert "marmoset" not in FakeEmbedding.calls[0]

    # same bytes: no-op, no second pointer
    r2 = client.post("/transcripts", params={"agent_id": "cc"}, content=raw)
    assert r2.json()["status"] == "unchanged" and r2.json()["pointer"] is None
    # superset: replaced, still no second pointer
    r3 = client.post("/transcripts", params={"agent_id": "cc"},
                     content=raw + b'{"type":"system","content":"more"}\n')
    assert r3.json()["status"] == "replaced" and r3.json()["pointer"] is None
    assert len(_memories(tmp_path)) == 1


def test_endpoint_conflict_is_409_with_both_shas(client):
    raw = specimen()
    client.post("/transcripts", params={"agent_id": "cc"}, content=raw)
    bad = b"#" + raw
    r = client.post("/transcripts", params={"agent_id": "cc", "session_id": SID}, content=bad)
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["existing_sha256"] == hashlib.sha256(raw).hexdigest()
    assert d["new_sha256"] == hashlib.sha256(bad).hexdigest()


def test_endpoint_gzip_body_and_reads(client):
    raw = specimen()
    r = client.post("/transcripts", params={"agent_id": "cc", "session_id": SID},
                    content=gzip.compress(raw), headers={"Content-Encoding": "gzip"})
    assert r.status_code == 200, r.text
    assert r.json()["manifest"]["sha256"] == hashlib.sha256(raw).hexdigest()

    man = client.get(f"/transcripts/{SID}", params={"agent_id": "cc"})
    assert man.status_code == 200 and man.json()["user_turns"] == 2
    turns = client.get(f"/transcripts/{SID}", params={"agent_id": "cc", "format": "turns"}).json()
    assert turns["turns"][0]["text"].startswith("Rebuild the zebra")
    rawr = client.get(f"/transcripts/{SID}", params={"agent_id": "cc", "format": "raw"})
    assert rawr.status_code == 200 and FAKE_KEY not in rawr.text
    assert rawr.headers["content-type"].startswith("application/x-ndjson")
    page = client.get(f"/transcripts/{SID}/turns",
                      params={"agent_id": "cc", "from": 2, "limit": 2}).json()
    assert [t["seq"] for t in page["turns"]] == [2, 3] and page["next_from"] == 4

    hits = client.get("/transcripts", params={"agent_id": "cc", "q": "capybara"}).json()
    assert hits["results"][0]["session_id"] == SID
    listing = client.get("/transcripts", params={"agent_id": "cc"}).json()
    assert listing["sessions"][0]["title"] == "Zebra widget rebuild"


def test_endpoint_404_400_and_415(client):
    assert client.get(f"/transcripts/{SID}", params={"agent_id": "cc"}).status_code == 404
    assert client.get(f"/transcripts/{SID}/turns", params={"agent_id": "cc"}).status_code == 404
    assert client.get("/transcripts/not-a-uuid", params={"agent_id": "cc"}).status_code == 400
    assert client.post("/transcripts", params={"agent_id": "cc", "session_id": "../x"},
                       content=specimen()).status_code == 400
    assert client.post("/transcripts", params={"agent_id": "cc"},
                       content=b'{"type":"user"}\n').status_code == 400   # no sessionId
    assert client.post("/transcripts", params={"agent_id": "cc"}, content=b"").status_code == 400
    assert client.post("/transcripts", params={"agent_id": "cc"}, content=specimen(),
                       headers={"Content-Encoding": "br"}).status_code == 415
    assert client.post("/transcripts", params={"agent_id": "cc", "host": "a b"},
                       content=specimen()).status_code == 400
    assert client.get("/transcripts", params={"agent_id": "cc", "q": "?!"}).status_code == 400


def test_endpoint_subagent_transcript_writes_no_pointer(client, tmp_path):
    sub = f"{SID}_agent-a3181710c8d1f6bbc"
    r = client.post("/transcripts", params={"agent_id": "cc", "session_id": sub},
                    content=specimen())
    assert r.status_code == 200, r.text
    assert r.json()["pointer"] is None
    assert r.json()["manifest"]["parent_session_id"] == SID
    assert _memories(tmp_path) == []


def test_endpoint_capture_gate_pauses_uploads(client, tmp_path):
    client.post("/capture/pause", json={"minutes": 5, "reason": "rotation"})
    r = client.post("/transcripts", params={"agent_id": "cc"}, content=specimen())
    assert r.status_code == 200 and r.json()["status"] == "paused"
    assert not (tmp_path / "agents" / "cc" / "sessions" / "archive").exists()
    client.post("/capture/resume")
    assert client.post("/transcripts", params={"agent_id": "cc"},
                       content=specimen()).json()["status"] == "archived"


def test_endpoint_tenants_are_isolated(client):
    client.post("/transcripts", params={"agent_id": "cc"}, content=specimen())
    assert client.get(f"/transcripts/{SID}", params={"agent_id": "opie"}).status_code == 404
    assert client.get("/transcripts", params={"agent_id": "opie", "q": "marmoset"}).json()["results"] == []
    # agent_id may not be omitted once named tenants exist
    assert client.get("/transcripts", params={"q": "marmoset"}).status_code == 400


def test_endpoint_transcript_body_cap_is_its_own(make_client):
    raw = specimen()
    with make_client(max_body_bytes=100, max_transcript_bytes=len(raw) + 10) as c:
        assert c.post("/transcripts", params={"agent_id": "cc"}, content=raw).status_code == 200
        # other endpoints keep the general cap
        assert c.post("/ingest", json={"prompt": "x" * 200, "response": "y"}).status_code == 413
    with make_client(max_transcript_bytes=50) as c:
        assert c.post("/transcripts", params={"agent_id": "cc"}, content=raw).status_code == 413


def test_endpoint_gzip_bomb_is_capped(make_client):
    bomb = gzip.compress(b"\n" * 5000)
    with make_client(max_transcript_decompressed_bytes=1000) as c:
        r = c.post("/transcripts", params={"agent_id": "cc", "session_id": SID},
                   content=bomb, headers={"Content-Encoding": "gzip"})
        assert r.status_code == 413
        r = c.post("/transcripts", params={"agent_id": "cc", "session_id": SID},
                   content=gzip.compress(specimen())[:-12], headers={"Content-Encoding": "gzip"})
        assert r.status_code in (400, 413)
