"""
Mnemo Cortex -- transcript archive tier (v4.25.0)
=================================================
The fourth session tier. hot/warm/cold (sessions.py) hold the exchanges the
live wire captured; this tier holds the CLIENT's own session transcript, every
line of it: user turns, assistant turns, thinking, tool inputs and outputs,
attachments. Full fidelity, secret-redacted, never embedded.

Layout per tenant:
  sessions/archive/
    <session_id>.jsonl.gz        redacted raw JSONL (unchanged lines kept byte-exact)
    <session_id>.manifest.json   what arrived, when, from where, and its sha256
    index.sqlite                 sessions + turns tables, FTS5 over turns.text

Recall never reads this tier. The only thing the recall store learns about an
archived session is one pointer memory the server writes on first upload.

Idempotency follows from the client file being append-only:
  same sha256                          -> unchanged (no-op)
  longer, and its prefix hashes to the
  stored sha256                        -> replaced (the session grew)
  anything else                        -> TranscriptConflict (HTTP 409)
Only the sha256 and byte count of the raw upload are kept, never the raw
bytes: the prefix check needs nothing else, and the unredacted text must not
reach disk.

The format is the Claude Code session JSONL (Cowork sessions are Claude Code
in a VM, so one parser covers Cowork, CC and CC2). Unknown line types are
archived and counted; a line that is not JSON is archived as redacted text
and counted; neither aborts the upload.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import re
import sqlite3
import threading
import zlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agentb.fsutil import atomic_write_bytes, atomic_write_text
from agentb.redact import redact_obj, redact_text

log = logging.getLogger("agentb.transcripts")

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
# A top-level session is its client UUID. A subagent (Task tool) transcript
# lives beside it as <uuid>/subagents/agent-<id>.jsonl and carries the PARENT
# sessionId on every line, so it is keyed <parent-uuid>_agent-<id>.
SESSION_KEY_RE = re.compile(rf"(?P<parent>{_UUID})(?:_agent-(?P<agent>[A-Za-z0-9]{{1,64}}))?")
UUID_RE = re.compile(_UUID)

SCHEMA_VERSION = 1
# Bump when redaction changes in a way archived sessions must be re-run
# through (TranscriptArchive.reprocess_all). 1 = 4.25.0 (values only);
# 2 = 4.25.1 (keys, duplicate keys, raw-line backstop).
# 3 = 4.25.2 (collision-safe keys, no text redaction over serialized JSON).
REDACTION_VERSION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    tenant TEXT, host TEXT,
    first_ts TEXT, last_ts TEXT,
    user_turns INTEGER, assistant_turns INTEGER,
    bytes INTEGER, sha256 TEXT, title TEXT,
    parent_session_id TEXT,
    uploaded_at TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT, role TEXT, kind TEXT, tool_name TEXT,
    text TEXT
);
CREATE INDEX IF NOT EXISTS turns_by_session ON turns(session_id, seq);
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    text, content='turns', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
    INSERT INTO turns_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""


class TranscriptConflict(Exception):
    """The upload is neither the stored transcript nor a superset of it."""

    def __init__(self, session_id: str, existing_sha256: str, new_sha256: str, reason: str):
        super().__init__(f"{session_id}: {reason}")
        self.session_id = session_id
        self.existing_sha256 = existing_sha256
        self.new_sha256 = new_sha256
        self.reason = reason

    def detail(self) -> dict:
        return {
            "error": "transcript conflict",
            "session_id": self.session_id,
            "reason": self.reason,
            "existing_sha256": self.existing_sha256,
            "new_sha256": self.new_sha256,
        }


class TranscriptCorrupt(Exception):
    """The stored .gz does not decode (e.g. torn by a power cut)."""

    def __init__(self, session_id: str, reason: str):
        super().__init__(f"{session_id}: archived transcript unreadable ({reason})")
        self.session_id = session_id
        self.reason = reason


def validate_transcript_id(session_id: str) -> str:
    """Return the id if it is a transcript key, else raise ValueError."""
    if not isinstance(session_id, str) or not SESSION_KEY_RE.fullmatch(session_id):
        raise ValueError(
            "Invalid transcript session_id: expected a UUID, or "
            f"<uuid>_agent-<id> for a subagent transcript, got {session_id!r}")
    return session_id


def parent_of(session_id: str) -> Optional[str]:
    """The parent session of a subagent key, None for a top-level session."""
    m = SESSION_KEY_RE.fullmatch(session_id)
    return m.group("parent") if m and m.group("agent") else None


# -------------------------------------------------------------------------
#  Parsing
# -------------------------------------------------------------------------

@dataclass
class Turn:
    seq: int
    ts: Optional[str]
    role: str
    kind: str
    tool_name: Optional[str]
    text: str


@dataclass
class ParsedTranscript:
    lines: list[str] = field(default_factory=list)   # redacted, in order
    turns: list[Turn] = field(default_factory=list)
    line_count: int = 0
    bad_lines: int = 0
    line_types: Counter = field(default_factory=Counter)
    user_turns: int = 0
    assistant_turns: int = 0
    tool_use: int = 0
    tool_result: int = 0
    first_ts: Optional[str] = None
    last_ts: Optional[str] = None
    client_version: Optional[str] = None
    cwd: Optional[str] = None
    title: Optional[str] = None
    session_ids: Counter = field(default_factory=Counter)
    first_user_text: Optional[str] = None
    redactions: Counter = field(default_factory=Counter)
    decode_errors: bool = False
    unfinished_tail_bytes: int = 0      # dropped: the client was mid-append


def _strings(node) -> list[str]:
    """Every string leaf, depth-first -- the searchable text of an odd shape."""
    out: list[str] = []

    def walk(n):
        if isinstance(n, str):
            if n:
                out.append(n)
        elif isinstance(n, list):
            for x in n:
                walk(x)
        elif isinstance(n, dict):
            for x in n.values():
                walk(x)

    walk(node)
    return out


def _tool_result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text") or "")
            elif isinstance(b, dict) and b.get("type") == "image":
                parts.append("[image]")
            else:
                parts.extend(_strings(b))
        return "\n".join(parts)
    return "\n".join(_strings(content))


def _is_prompt_text(text: str) -> bool:
    """A human prompt, not a harness wrapper (<command-name>, <system-reminder>, ...)."""
    t = text.lstrip()
    return bool(t) and not t.startswith("<")


def parse_transcript(raw: bytes) -> ParsedTranscript:
    """Parse and redact a Claude Code session JSONL. Never raises on content."""
    p = ParsedTranscript()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        p.decode_errors = True

    tool_names: dict[str, str] = {}
    assistant_ids: set[str] = set()
    assistant_lines_without_id = 0
    seq = 0

    def add(ts, role, kind, tool_name, body):
        nonlocal seq
        if body is None:
            return
        body = body if isinstance(body, str) else "\n".join(_strings(body))
        if not body.strip():
            return
        p.turns.append(Turn(seq, ts, role, kind, tool_name, body))
        seq += 1

    segments = text.split("\n")
    if not text.endswith("\n"):
        # The last segment has no newline: the client may still be writing it
        # (a big tool_result cut mid-way, e.g. a PEM before its END line, which
        # no pattern can recognise). A tail that parses as a complete JSON
        # object is kept; anything else is dropped -- it arrives whole in the
        # next upload, and sha256/bytes still cover the full raw upload.
        tail = segments.pop()
        if tail.strip():
            try:
                complete = isinstance(json.loads(tail), dict)
            except (ValueError, RecursionError):
                complete = False
            if complete:
                segments.append(tail)
            else:
                p.unfinished_tail_bytes = len(tail.encode("utf-8"))

    for line in segments:
        if line.endswith("\r"):
            line = line[:-1]
        if not line.strip():
            continue
        p.line_count += 1
        # Values a duplicated key threw away (json.loads keeps the last copy).
        # They are never stored; walking them gives the exact count of any
        # secret that re-serialization removes.
        dropped: list = []

        def _pairs(pairs):
            d = {}
            for k, v in pairs:
                if k in d:
                    dropped.append(d[k])
                d[k] = v
            return d

        try:
            obj = json.loads(line, object_pairs_hook=_pairs)
            if not isinstance(obj, dict):
                raise ValueError("line is not a JSON object")
        except (ValueError, RecursionError):
            p.bad_lines += 1
            clean, counts = redact_text(line)
            p.redactions.update(counts)
            p.lines.append(clean)
            continue

        # Keys and values both (redact_obj walks keys since 4.25.1). The line
        # is kept byte-exact ONLY when the walk finds nothing, no key was
        # duplicated, and a scan of the raw line finds nothing either.
        #
        # 4.25.2: the raw scan only DECIDES; it never edits and never counts.
        # Text patterns match across JSON field boundaries (a URL in one
        # field and an email in the next look like user:pass@host), so
        # running redact_text over serialized JSON wrote invalid JSON and
        # counted phantom redactions (review of 1717aeb). A rewrite is
        # json.dumps of the walked object: valid by construction, checked.
        clean_obj, counts = redact_obj(obj)
        _, dropped_counts = redact_obj(dropped) if dropped else (None, {})
        _, raw_counts = redact_text(line)
        if counts or dropped or raw_counts:
            out = json.dumps(clean_obj, ensure_ascii=False, separators=(",", ":"))
            json.loads(out)          # never store a line that does not parse
            p.redactions.update(counts)
            p.redactions.update(dropped_counts)
            p.lines.append(out)
        else:
            p.lines.append(line)
        obj = clean_obj

        ltype = obj.get("type") if isinstance(obj.get("type"), str) else "(none)"
        p.line_types[ltype] += 1
        ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else None
        if ts:
            if p.first_ts is None or ts < p.first_ts:
                p.first_ts = ts
            if p.last_ts is None or ts > p.last_ts:
                p.last_ts = ts
        if isinstance(obj.get("sessionId"), str):
            p.session_ids[obj["sessionId"]] += 1
        if isinstance(obj.get("version"), str):
            p.client_version = obj["version"]
        if p.cwd is None and isinstance(obj.get("cwd"), str):
            p.cwd = obj["cwd"]

        if ltype == "ai-title" and isinstance(obj.get("aiTitle"), str):
            p.title = obj["aiTitle"]
            continue

        msg = obj.get("message")
        if ltype in ("user", "assistant") and isinstance(msg, dict):
            role = msg.get("role") if isinstance(msg.get("role"), str) else ltype
            content = msg.get("content")
            has_prompt = False
            if ltype == "assistant":
                mid = msg.get("id")
                if isinstance(mid, str):
                    assistant_ids.add(mid)
                else:
                    assistant_lines_without_id += 1
            if isinstance(content, str):
                add(ts, role, "text", None, content)
                has_prompt = _is_prompt_text(content)
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        add(ts, role, "other", None, _strings(b))
                        continue
                    btype = b.get("type")
                    if btype == "text":
                        body = b.get("text") or ""
                        add(ts, role, "text", None, body)
                        has_prompt = has_prompt or _is_prompt_text(body)
                    elif btype == "thinking":
                        add(ts, role, "thinking", None, b.get("thinking") or "")
                    elif btype == "tool_use":
                        name = b.get("name") if isinstance(b.get("name"), str) else None
                        if isinstance(b.get("id"), str) and name:
                            tool_names[b["id"]] = name
                        p.tool_use += 1
                        add(ts, role, "tool_use", name,
                            json.dumps(b.get("input"), ensure_ascii=False))
                    elif btype == "tool_result":
                        p.tool_result += 1
                        add(ts, role, "tool_result", tool_names.get(b.get("tool_use_id")),
                            _tool_result_text(b.get("content")))
                    elif btype == "image":
                        add(ts, role, "image", None, "[image]")
                    else:
                        add(ts, role, str(btype or "other"), None, _strings(b))
            if ltype == "user" and has_prompt and not obj.get("isMeta"):
                p.user_turns += 1
                if p.first_user_text is None:
                    first = content if isinstance(content, str) else next(
                        (b.get("text") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"
                         and _is_prompt_text(b.get("text") or "")), "")
                    p.first_user_text = " ".join((first or "").split())
        elif ltype == "system":
            body = obj.get("content")
            add(ts, "system", str(obj.get("subtype") or "system"), None,
                body if isinstance(body, str) else None)
        elif ltype == "attachment" and isinstance(obj.get("attachment"), dict):
            att = obj["attachment"]
            add(ts, "attachment", str(att.get("type") or "attachment"), None, _strings(att))

    p.assistant_turns = len(assistant_ids) + assistant_lines_without_id
    return p


# -------------------------------------------------------------------------
#  Archive
# -------------------------------------------------------------------------

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TranscriptArchive:
    """One tenant's archive tier. Stateless between calls: every operation
    opens its own SQLite connection, so it is safe from asyncio.to_thread."""

    def __init__(self, data_dir: Path, tenant: str = "default"):
        self.dir = Path(data_dir) / "sessions" / "archive"
        self.index_path = self.dir / "index.sqlite"
        self.tenant = tenant

    # -- paths --
    def _gz(self, sid: str) -> Path:
        return self.dir / f"{sid}.jsonl.gz"

    def _manifest_path(self, sid: str) -> Path:
        return self.dir / f"{sid}.manifest.json"

    # -- sqlite --
    def _connect(self, create: bool = False) -> Optional[sqlite3.Connection]:
        if not create and not self.index_path.exists():
            return None
        self.dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.index_path, timeout=30)
        conn.row_factory = sqlite3.Row
        if create:
            conn.executescript(_SCHEMA)
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
            conn.commit()
        return conn

    # -- reads --
    def manifest(self, session_id: str) -> Optional[dict]:
        validate_transcript_id(session_id)
        path = self._manifest_path(session_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def raw(self, session_id: str) -> Optional[bytes]:
        """The redacted JSONL. Raises TranscriptCorrupt when the .gz does not
        decode, after marking the manifest so the next upload rewrites it."""
        validate_transcript_id(session_id)
        path = self._gz(session_id)
        if not path.exists() or not self._manifest_path(session_id).exists():
            return None
        try:
            with gzip.open(path, "rb") as f:
                return f.read()
        except (OSError, EOFError, zlib.error) as e:
            self._mark_corrupt(session_id, f"{type(e).__name__}: {e}")
            raise TranscriptCorrupt(session_id, str(e)) from e

    def _mark_corrupt(self, session_id: str, reason: str) -> None:
        with _lock_for(self.dir / session_id):
            m = self.manifest(session_id)
            if m is None:
                return
            m["gz_corrupt"] = reason[:300]
            atomic_write_text(self._manifest_path(session_id),
                              json.dumps(m, indent=2, ensure_ascii=False))
        log.error(f"Transcript archive CORRUPT: {session_id} ({reason}); "
                  "the next upload of this session rewrites it")

    def turns(self, session_id: str, from_seq: int = 0, to_seq: Optional[int] = None,
              limit: int = 500) -> Optional[list[dict]]:
        validate_transcript_id(session_id)
        if self.manifest(session_id) is None:
            return None
        conn = self._connect()
        if conn is None:
            return None
        try:
            sql = ("SELECT seq, ts, role, kind, tool_name, text FROM turns "
                   "WHERE session_id = ? AND seq >= ?")
            args: list = [session_id, from_seq]
            if to_seq is not None:
                sql += " AND seq <= ?"
                args.append(to_seq)
            sql += " ORDER BY seq LIMIT ?"
            args.append(limit)
            return [dict(r) for r in conn.execute(sql, args)]
        finally:
            conn.close()

    def search(self, q: str, limit: int = 20) -> list[dict]:
        """FTS5 over every turn. Words are quoted, so FTS syntax in the query
        is treated as text, never as operators."""
        terms = re.findall(r"\w+", q or "")[:16]
        if not terms:
            raise ValueError("search query has no searchable words")
        match = " ".join('"' + t.replace('"', '""') + '"' for t in terms)
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT t.session_id, t.seq, t.ts, t.role, t.kind, t.tool_name, s.title, "
                "snippet(turns_fts, 0, '[', ']', '...', 16) AS snippet "
                "FROM turns_fts JOIN turns t ON t.id = turns_fts.rowid "
                "LEFT JOIN sessions s ON s.session_id = t.session_id "
                "WHERE turns_fts MATCH ? ORDER BY bm25(turns_fts) LIMIT ?",
                (match, limit)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def list_sessions(self, limit: int = 20) -> list[dict]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT session_id, host, first_ts, last_ts, user_turns, assistant_turns, "
                "bytes, title, parent_session_id, uploaded_at FROM sessions "
                "ORDER BY COALESCE(last_ts, uploaded_at) DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # -- write --
    def _gz_intact(self, session_id: str, manifest: dict) -> bool:
        """Cheap check for the unchanged path: the file exists, is non-empty,
        and nobody has marked it corrupt. (A full decode is left to raw().)"""
        if manifest.get("gz_corrupt"):
            return False
        # 4.25.2: decode it whole. exists + non-empty let a truncated gz
        # answer "unchanged" until a read tripped over it (review of
        # 1717aeb); the upload already parsed the full body, so the extra
        # decode costs about the same again.
        try:
            with gzip.open(self._gz(session_id), "rb") as f:
                while f.read(1 << 20):
                    pass
            return True
        except (OSError, EOFError, zlib.error):
            return False

    def _store(self, session_id: str, parsed: ParsedTranscript, *, raw_sha: str,
               raw_bytes: int, agent_id: Optional[str], host: Optional[str],
               existing: Optional[dict], counts_upload: bool) -> dict:
        """Write gz (atomic + fsync) -> index -> manifest (the commit marker).
        Caller holds the session lock."""
        for stale in (self._gz(session_id).with_name(f"{session_id}.jsonl.gz.tmp"),
                      self._manifest_path(session_id).with_name(
                          f"{session_id}.manifest.json.tmp")):
            stale.unlink(missing_ok=True)      # a crash mid-write leaves these
        payload = ("\n".join(parsed.lines) + "\n").encode("utf-8")
        gz_path = self._gz(session_id)
        atomic_write_bytes(gz_path, gzip.compress(payload))

        now = _now_iso()
        parent = parent_of(session_id)
        prev = existing or {}
        manifest = {
            "session_id": session_id,
            "parent_session_id": parent,
            "agent_id": agent_id or prev.get("agent_id") or "default",
            "host": host if host is not None else prev.get("host"),
            "sha256": raw_sha,
            "bytes": raw_bytes,
            "archived_bytes": gz_path.stat().st_size,
            "lines": parsed.line_count,
            "bad_lines": parsed.bad_lines,
            "unfinished_tail_bytes": parsed.unfinished_tail_bytes,
            "decode_errors": parsed.decode_errors,
            "line_types": dict(parsed.line_types),
            "user_turns": parsed.user_turns,
            "assistant_turns": parsed.assistant_turns,
            "tool_use": parsed.tool_use,
            "tool_result": parsed.tool_result,
            "indexed_turns": len(parsed.turns),
            "first_ts": parsed.first_ts,
            "last_ts": parsed.last_ts,
            "client_version": parsed.client_version,
            "cwd": parsed.cwd,
            "title": parsed.title,
            "session_ids_seen": dict(parsed.session_ids),
            "first_user_text": (parsed.first_user_text or "")[:500],
            "redactions_applied": sum(parsed.redactions.values()),
            "redactions_by_kind": dict(parsed.redactions),
            "redaction_version": REDACTION_VERSION,
            "uploaded_at": now if counts_upload else prev.get("uploaded_at", now),
            "first_uploaded_at": prev.get("first_uploaded_at", now),
            "uploads": int(prev.get("uploads", 0)) + (1 if counts_upload else 0),
            "pointer_memory_id": prev.get("pointer_memory_id"),
            "schema_version": SCHEMA_VERSION,
        }
        if prev.get("pointer_error") and not prev.get("pointer_memory_id"):
            manifest["pointer_error"] = prev["pointer_error"]

        conn = self._connect(create=True)
        try:
            with conn:
                conn.execute("DELETE FROM turns WHERE session_id = ?", (session_id,))
                conn.executemany(
                    "INSERT INTO turns(session_id, seq, ts, role, kind, tool_name, text) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(session_id, t.seq, t.ts, t.role, t.kind, t.tool_name, t.text)
                     for t in parsed.turns])
                conn.execute(
                    "INSERT OR REPLACE INTO sessions(session_id, tenant, host, first_ts, "
                    "last_ts, user_turns, assistant_turns, bytes, sha256, title, "
                    "parent_session_id, uploaded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (session_id, manifest["agent_id"], manifest["host"], parsed.first_ts,
                     parsed.last_ts, parsed.user_turns, parsed.assistant_turns,
                     raw_bytes, raw_sha, parsed.title, parent, manifest["uploaded_at"]))
        finally:
            conn.close()

        # The manifest lands last: it is the commit marker. A crash before it
        # leaves the previous manifest, so the client's next upload redoes
        # this one (superset of the old, or first upload again).
        atomic_write_text(self._manifest_path(session_id),
                          json.dumps(manifest, indent=2, ensure_ascii=False))
        if manifest["redactions_applied"]:
            log.warning(
                f"Redacted {manifest['redactions_applied']} secret(s) in transcript "
                f"{session_id}: " + ", ".join(
                    f"{k}x{v}" for k, v in parsed.redactions.items()))
        return manifest

    def upload(self, session_id: str, raw: bytes, agent_id: Optional[str],
               host: Optional[str]) -> tuple[str, dict]:
        """Archive one transcript. Returns (status, manifest) where status is
        archived | replaced | unchanged | repaired. Raises TranscriptConflict
        (409) and ValueError (400: bad id, or nothing parseable).

        repaired = the same bytes as the archive, but the stored copy was
        corrupt or redacted by an older REDACTION_VERSION, so it was rebuilt
        from this upload."""
        validate_transcript_id(session_id)
        sha = hashlib.sha256(raw).hexdigest()
        self.dir.mkdir(parents=True, exist_ok=True)
        with _lock_for(self.dir / session_id):
            existing = self.manifest(session_id)
            status = "archived"
            if existing is not None:
                if existing.get("sha256") == sha:
                    if (self._gz_intact(session_id, existing)
                            and int(existing.get("redaction_version") or 1) >= REDACTION_VERSION):
                        return "unchanged", existing
                    status = "repaired"
                else:
                    old_bytes = int(existing.get("bytes") or 0)
                    if len(raw) <= old_bytes:
                        raise TranscriptConflict(
                            session_id, existing.get("sha256", ""), sha,
                            "upload is not longer than the archived transcript "
                            f"({len(raw)} <= {old_bytes} bytes) and differs from it")
                    if hashlib.sha256(raw[:old_bytes]).hexdigest() != existing.get("sha256"):
                        raise TranscriptConflict(
                            session_id, existing.get("sha256", ""), sha,
                            f"first {old_bytes} bytes differ from the archived transcript "
                            "(the client file is append-only, so this is a different file)")
                    status = "replaced"

            parsed = parse_transcript(raw)
            if parsed.line_count == 0 or parsed.bad_lines == parsed.line_count:
                raise ValueError("upload contains no complete JSON transcript lines")

            manifest = self._store(session_id, parsed, raw_sha=sha, raw_bytes=len(raw),
                                   agent_id=agent_id, host=host, existing=existing,
                                   counts_upload=status != "repaired")
            log.info(f"Transcript {status}: {session_id} ({len(raw)} bytes, "
                     f"{parsed.line_count} lines, {len(parsed.turns)} turns, "
                     f"tenant {agent_id or 'default'})")
            return status, manifest

    def reprocess(self, session_id: str) -> Optional[dict]:
        """Re-run the CURRENT redaction over one stored transcript whose
        manifest predates it. Rebuilds gz + turns + FTS from the stored
        (already redacted) lines; sha256/bytes of the original upload are
        kept, so the client's next upload still matches. Returns a result
        dict, or None when the session was already current. A corrupt gz is
        marked and skipped (the client's next upload repairs it).

        What this cannot fix: bytes 4.25.0 dropped or stored in a form no
        pattern now recognises beyond what re-parsing finds -- e.g. an
        unfinished tail line is only healed by the client re-uploading."""
        validate_transcript_id(session_id)
        with _lock_for(self.dir / session_id):
            m = self.manifest(session_id)
            if m is None:
                return None
            old_version = int(m.get("redaction_version") or 1)
            if old_version >= REDACTION_VERSION:
                return None
            gz = self._gz(session_id)
            try:
                with gzip.open(gz, "rb") as f:
                    stored = f.read()
            except (OSError, EOFError, zlib.error) as e:
                m["gz_corrupt"] = f"{type(e).__name__}: {e}"[:300]
                atomic_write_text(self._manifest_path(session_id),
                                  json.dumps(m, indent=2, ensure_ascii=False))
                log.error(f"Reprocess skipped {session_id}: stored gz unreadable ({e})")
                return {"session_id": session_id, "status": "corrupt", "reason": str(e)}
            parsed = parse_transcript(stored)
            new_redactions = dict(parsed.redactions)
            # The stored text was redacted once already; the manifest keeps
            # the running total of everything ever removed.
            merged = Counter(m.get("redactions_by_kind") or {})
            merged.update(parsed.redactions)
            parsed.redactions = merged
            manifest = self._store(session_id, parsed, raw_sha=m["sha256"],
                                   raw_bytes=int(m["bytes"]), agent_id=m.get("agent_id"),
                                   host=m.get("host"), existing=m, counts_upload=False)
            manifest["reprocessed_at"] = _now_iso()
            manifest["reprocessed_from_version"] = old_version
            atomic_write_text(self._manifest_path(session_id),
                              json.dumps(manifest, indent=2, ensure_ascii=False))
            log.info(f"Transcript reprocessed: {session_id} v{old_version}->"
                     f"v{REDACTION_VERSION}, {sum(new_redactions.values())} new redaction(s)")
            return {"session_id": session_id, "status": "reprocessed",
                    "from_version": old_version, "new_redactions": new_redactions}

    def reprocess_all(self) -> dict:
        """reprocess() every archived session in this tenant. Never raises
        on one bad session; reports it."""
        report = {"tenant": self.tenant, "checked": 0, "reprocessed": 0, "current": 0,
                  "corrupt": 0, "failed": 0, "new_redactions": 0, "sessions": []}
        if not self.dir.is_dir():
            return report
        for mpath in sorted(self.dir.glob("*.manifest.json")):
            sid = mpath.name[: -len(".manifest.json")]
            report["checked"] += 1
            try:
                res = self.reprocess(sid)
            except Exception as e:                 # one bad file never stops the sweep
                log.error(f"Reprocess FAILED for {sid}: {e!r}")
                report["failed"] += 1
                report["sessions"].append({"session_id": sid, "status": "failed",
                                           "reason": repr(e)[:300]})
                continue
            if res is None:
                report["current"] += 1
                continue
            report["sessions"].append(res)
            if res["status"] == "reprocessed":
                report["reprocessed"] += 1
                report["new_redactions"] += sum(res["new_redactions"].values())
            else:
                report["corrupt"] += 1
        return report

    def set_pointer(self, session_id: str, memory_id: Optional[str],
                    error: Optional[str] = None) -> dict:
        """Record the pointer memory's id (or why it failed) in the manifest."""
        with _lock_for(self.dir / session_id):
            m = self.manifest(session_id) or {}
            if memory_id:
                m["pointer_memory_id"] = memory_id
                m.pop("pointer_error", None)
            if error:
                m["pointer_error"] = error
            atomic_write_text(self._manifest_path(session_id),
                              json.dumps(m, indent=2, ensure_ascii=False))
            return m


def pointer_text(manifest: dict) -> str:
    """The pointer memory's summary -- the only text recall ever sees."""
    first = " ".join((manifest.get("first_user_text") or "").split())[:200]
    if not first:
        first = manifest.get("title") or "(no user prompt found)"
    return (f"Full transcript archived: {first} ... "
            f"<{manifest.get('user_turns', 0)} user turns, "
            f"{manifest.get('first_ts') or '?'} to {manifest.get('last_ts') or '?'}, "
            f"{manifest.get('host') or 'unknown host'}>")
