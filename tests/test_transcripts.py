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


# -------------------------------------------------------------------------
#  4.25.1 -- review of 3138ba7
# -------------------------------------------------------------------------

import time

from agentb.redact import redact_obj
from agentb.transcripts import REDACTION_VERSION, TranscriptCorrupt

PEM_HEAD = "-----BEGIN " + "RSA PRIVATE KEY-----"
PEM_TAIL = "-----END " + "RSA PRIVATE KEY-----"
PEM_BODY = "MIIEowIBAAKCAQEA" + "q1w2e3r4t5y6u7i8o9p0" * 3


def _keyed_lines(sid: str = SID) -> list[str]:
    base = {"sessionId": sid, "timestamp": "2026-09-22T12:00:00.000Z"}
    return [
        _line({**base, "type": "user",
               "message": {"role": "user", "content": "look at the lemur config"}}),
        _line({**base, "type": "assistant", "message": {"id": "m9", "role": "assistant", "content": [
            {"type": "tool_use", "id": "t9", "name": "Write",
             "input": {FAKE_KEY: "value-under-a-secret-key"}}]}}),
        _line({**base, "type": "attachment",
               "attachment": {"type": "map", "content": {FAKE_KEY: "attached"}}}),
        # duplicated top-level key: json.loads keeps the LAST copy
        '{"type":"system","content":"' + FAKE_KEY + '","content":"harmless","sessionId":"' + sid + '"}',
    ]


def _assert_nowhere(arc: TranscriptArchive, sid: str, secret: str):
    assert secret.encode() not in arc.raw(sid)
    assert all(secret not in (t["text"] or "") for t in arc.turns(sid, 0, None, 100000))
    body = secret.split("-")[-1]
    assert arc.search(body) == [], "secret body reachable through FTS"


def test_redact_obj_redacts_keys():
    clean, counts = redact_obj({FAKE_KEY: {"nested": [{FAKE_KEY: 1}]}})
    assert FAKE_KEY not in json.dumps(clean)
    assert counts.get("anthropic") == 2


def test_secret_as_key_and_duplicate_key_are_redacted(tmp_path):
    arc = TranscriptArchive(tmp_path, "cc")
    raw = ("\n".join(_keyed_lines()) + "\n").encode()
    _, m = arc.upload(SID, raw, "cc", None)
    _assert_nowhere(arc, SID, FAKE_KEY)
    # tool_use key + attachment key + the duplicate key's dropped first copy
    assert m["redactions_applied"] >= 3
    assert m["redaction_version"] == REDACTION_VERSION


def test_unfinished_tail_is_dropped_and_healed_by_next_upload(tmp_path):
    arc = TranscriptArchive(tmp_path, "cc")
    partial_tail = ('{"type":"user","sessionId":"' + SID + '","message":{"role":"user","content":'
                    '[{"type":"tool_result","tool_use_id":"x","content":"'
                    + PEM_HEAD + "\\n" + PEM_BODY)
    raw = specimen() + partial_tail.encode()
    status, m = arc.upload(SID, raw, "cc", None)
    assert status == "archived"
    assert m["unfinished_tail_bytes"] == len(partial_tail.encode())
    assert m["bytes"] == len(raw) and m["sha256"] == hashlib.sha256(raw).hexdigest()
    assert PEM_BODY.encode() not in arc.raw(SID)
    assert arc.search(PEM_BODY) == []

    grown = raw + ("\\n" + PEM_TAIL + '"}]}}\n').encode()
    status, m2 = arc.upload(SID, grown, "cc", None)
    assert status == "replaced" and m2["unfinished_tail_bytes"] == 0
    assert PEM_BODY.encode() not in arc.raw(SID)
    assert m2["redactions_by_kind"].get("private-key") == 1


def test_complete_tail_without_newline_is_kept(tmp_path):
    arc = TranscriptArchive(tmp_path, "cc")
    _, m = arc.upload(SID, specimen().rstrip(b"\n"), "cc", None)
    assert m["unfinished_tail_bytes"] == 0 and m["user_turns"] == 2


def test_corrupt_gz_is_reported_and_repaired_by_same_bytes(tmp_path):
    arc = TranscriptArchive(tmp_path, "cc")
    raw = specimen()
    arc.upload(SID, raw, "cc", None)
    gz = tmp_path / "sessions" / "archive" / f"{SID}.jsonl.gz"
    gz.write_bytes(b"\x1f\x8b torn by a power cut")
    gz.with_name(gz.name + ".tmp").write_bytes(b"leftover")
    with pytest.raises(TranscriptCorrupt):
        arc.raw(SID)
    assert arc.manifest(SID)["gz_corrupt"]
    status, m = arc.upload(SID, raw, "cc", None)
    assert status == "repaired" and "gz_corrupt" not in m and m["uploads"] == 1
    assert b"zebra" in arc.raw(SID)
    assert not gz.with_name(gz.name + ".tmp").exists()


def test_missing_gz_is_repaired_by_same_bytes(tmp_path):
    arc = TranscriptArchive(tmp_path, "cc")
    arc.upload(SID, specimen(), "cc", None)
    (tmp_path / "sessions" / "archive" / f"{SID}.jsonl.gz").unlink()
    assert arc.upload(SID, specimen(), "cc", None)[0] == "repaired"


def _age_to_v1(arc: TranscriptArchive, sid: str, lines: list[str]) -> None:
    """Make an archive look like 4.25.0 wrote it: a v1 manifest over a gz
    holding lines 4.25.0 would have kept verbatim."""
    d = arc.dir
    (d / f"{sid}.jsonl.gz").write_bytes(gzip.compress(("\n".join(lines) + "\n").encode()))
    m = json.loads((d / f"{sid}.manifest.json").read_text(encoding="utf-8"))
    m.pop("redaction_version", None)
    m["redactions_applied"], m["redactions_by_kind"] = 0, {}
    (d / f"{sid}.manifest.json").write_text(json.dumps(m), encoding="utf-8")


def test_reprocess_upgrades_v1_archives_and_is_idempotent(tmp_path):
    arc = TranscriptArchive(tmp_path, "cc")
    raw = specimen()
    _, before = arc.upload(SID, raw, "cc", "igor")
    _age_to_v1(arc, SID, specimen_lines()[:2] + _keyed_lines())
    rep = arc.reprocess_all()
    assert rep["reprocessed"] == 1 and rep["new_redactions"] >= 3
    m = arc.manifest(SID)
    assert m["redaction_version"] == REDACTION_VERSION
    assert m["reprocessed_from_version"] == 1
    assert m["sha256"] == before["sha256"] and m["bytes"] == before["bytes"]
    assert m["host"] == "igor" and m["uploads"] == 1
    _assert_nowhere(arc, SID, FAKE_KEY)
    assert arc.reprocess_all()["current"] == 1
    # the client's next upload of the same bytes still matches
    assert arc.upload(SID, raw, "cc", None)[0] == "unchanged"


def test_same_bytes_on_a_v1_archive_repairs_from_the_upload(tmp_path):
    arc = TranscriptArchive(tmp_path, "cc")
    raw = ("\n".join(_keyed_lines()) + "\n").encode()
    arc.upload(SID, raw, "cc", None)
    _age_to_v1(arc, SID, _keyed_lines())
    status, m = arc.upload(SID, raw, "cc", None)
    assert status == "repaired" and m["redaction_version"] == REDACTION_VERSION
    _assert_nowhere(arc, SID, FAKE_KEY)


def test_endpoint_reprocess_and_startup_sweep(tmp_path, make_client):
    with make_client() as c:
        c.post("/transcripts", params={"agent_id": "cc"}, content=specimen())
        r = c.post("/transcripts/reprocess", params={"agent_id": "cc"})
        assert r.status_code == 200 and r.json()["current"] == 1
    arc = TranscriptArchive(tmp_path / "agents" / "cc", "cc")
    _age_to_v1(arc, SID, _keyed_lines())
    with make_client() as c:                     # the startup sweep runs off-loop
        deadline = time.time() + 10
        while (time.time() < deadline
               and arc.manifest(SID).get("redaction_version") != REDACTION_VERSION):
            time.sleep(0.05)
        assert arc.manifest(SID)["redaction_version"] == REDACTION_VERSION
        raw = c.get(f"/transcripts/{SID}", params={"agent_id": "cc", "format": "raw"})
        assert FAKE_KEY not in raw.text


def test_endpoint_corrupt_raw_is_500(client, tmp_path):
    client.post("/transcripts", params={"agent_id": "cc"}, content=specimen())
    (tmp_path / "agents" / "cc" / "sessions" / "archive" / f"{SID}.jsonl.gz").write_bytes(b"junk")
    r = client.get(f"/transcripts/{SID}", params={"agent_id": "cc", "format": "raw"})
    assert r.status_code == 500 and "unreadable" in r.text
    assert client.post("/transcripts", params={"agent_id": "cc"},
                       content=specimen()).json()["status"] == "repaired"


def test_endpoint_failed_pointer_is_retried_on_unchanged_upload(client, tmp_path):
    import agentb.server as srv
    real = srv._atomic_write_text
    calls = {"n": 0}

    def flaky(path, text):
        if Path(path).parent.name == "memory" and calls["n"] == 0:
            calls["n"] += 1
            raise OSError("disk hiccup")
        return real(path, text)

    with patch.object(srv, "_atomic_write_text", flaky):
        r1 = client.post("/transcripts", params={"agent_id": "cc"}, content=specimen())
    assert r1.json()["pointer"] == "failed"
    assert r1.json()["manifest"]["pointer_error"]
    assert _memories(tmp_path) == []
    r2 = client.post("/transcripts", params={"agent_id": "cc"}, content=specimen())
    assert r2.json()["status"] == "unchanged" and r2.json()["pointer"] == "archived"
    assert r2.json()["manifest"]["pointer_memory_id"]
    assert "pointer_error" not in r2.json()["manifest"]
    assert len(_memories(tmp_path)) == 1
    r3 = client.post("/transcripts", params={"agent_id": "cc"}, content=specimen())
    assert r3.json()["pointer"] is None and len(_memories(tmp_path)) == 1


# -------------------------------------------------------------------------
#  4.25.2 -- review of 1717aeb
# -------------------------------------------------------------------------

FAKE_KEY_2 = "sk-" + "ant-" + "api03-" + "Z9y8X7w6V5u4T3s2R1q0" * 2


def _stored_lines(arc: TranscriptArchive, sid: str) -> list[str]:
    return [ln for ln in arc.raw(sid).decode("utf-8").split("\n") if ln]


def test_cross_field_raw_match_keeps_valid_json_and_counts_nothing(tmp_path):
    arc = TranscriptArchive(tmp_path, "cc")
    line = _line({"type": "assistant", "sessionId": SID, "message": {
        "id": "m1", "role": "assistant", "content": [{"type": "tool_result_like",
        "repo": "https://github.com", "author": "guy@example.com"}]}})
    from agentb.redact import redact_text
    assert redact_text(line)[1], "precondition: the raw scan must hit across fields"
    raw = (specimen_lines()[1] + "\n" + line + "\n").encode()
    _, m = arc.upload(SID, raw, "cc", None)
    for stored in _stored_lines(arc, SID):
        json.loads(stored)                        # every stored line parses
    assert m["redactions_applied"] == 0 and m["bad_lines"] == 0
    assert b"guy@example.com" in arc.raw(SID)
    # and a reprocess of it stays valid and still counts nothing
    _age_to_v1(arc, SID, _stored_lines(arc, SID))
    assert arc.reprocess_all()["new_redactions"] == 0
    for stored in _stored_lines(arc, SID):
        json.loads(stored)
    assert arc.manifest(SID)["bad_lines"] == 0


def test_colliding_redacted_keys_keep_every_value():
    clean, counts = redact_obj({FAKE_KEY: "first", FAKE_KEY_2: "second"})
    assert sorted(clean.values()) == ["first", "second"]
    assert set(clean) == {"[REDACTED:anthropic]", "[REDACTED:anthropic]#2"}
    assert counts == {"anthropic": 2}


def test_colliding_keys_survive_the_archive(tmp_path):
    arc = TranscriptArchive(tmp_path, "cc")
    line = _line({"type": "assistant", "sessionId": SID, "message": {"id": "m1", "role": "assistant",
        "content": [{"type": "tool_use", "id": "t1", "name": "Write",
                     "input": {FAKE_KEY: "okapi-one", FAKE_KEY_2: "okapi-two"}}]}})
    arc.upload(SID, (line + "\n").encode(), "cc", None)
    raw = arc.raw(SID)
    assert b"okapi-one" in raw and b"okapi-two" in raw
    assert FAKE_KEY.encode() not in raw and FAKE_KEY_2.encode() not in raw
    assert {h["session_id"] for h in arc.search("okapi")} == {SID}


def test_dup_key_secret_plus_another_secret_counts_both(tmp_path):
    arc = TranscriptArchive(tmp_path, "cc")
    line = ('{"type":"system","content":"' + FAKE_KEY + '","content":"harmless",'
            '"note":"' + FAKE_KEY_2 + '","sessionId":"' + SID + '"}')
    _, m = arc.upload(SID, (line + "\n").encode(), "cc", None)
    assert m["redactions_applied"] == 2
    assert FAKE_KEY.encode() not in arc.raw(SID) and FAKE_KEY_2.encode() not in arc.raw(SID)


def test_truncated_nonempty_gz_is_not_unchanged(tmp_path):
    arc = TranscriptArchive(tmp_path, "cc")
    raw = specimen()
    arc.upload(SID, raw, "cc", None)
    gz = tmp_path / "sessions" / "archive" / f"{SID}.jsonl.gz"
    data = gz.read_bytes()
    gz.write_bytes(data[: len(data) // 2])
    mtmp = tmp_path / "sessions" / "archive" / f"{SID}.manifest.json.tmp"
    mtmp.write_text("{half", encoding="utf-8")
    status, _ = arc.upload(SID, raw, "cc", None)
    assert status == "repaired"
    assert b"zebra" in arc.raw(SID)
    assert not mtmp.exists()


def test_reprocess_refuses_read_only_agent(tmp_path):
    from agentb.config import AgentConfig
    cfg = _cfg(tmp_path)
    cfg.agents = {"ro": AgentConfig(read_only=True)}
    with patch("agentb.server.create_resilient_embedding", return_value=FakeEmbedding()), \
         patch("agentb.server.create_resilient_reasoning", return_value=FakeReasoning()):
        from agentb.server import create_app
        with TestClient(create_app(cfg)) as c:
            assert c.post("/transcripts/reprocess", params={"agent_id": "ro"}).status_code == 403
