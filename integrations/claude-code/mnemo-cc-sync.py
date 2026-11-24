#!/usr/bin/env python3
"""
mnemo-cc-sync — push Claude Code session activity to Mnemo Cortex.

This is the modern session-watcher path. It reads Claude Code's JSONL session
files and POSTs structured memories to Mnemo Cortex's /writeback endpoint, so
the memories are immediately recallable by other agents (Opie, Rocky, etc.)
without waiting for an overnight summarization pass.

Replaces the legacy `mnemo-watcher-cc.sh` which wrote raw messages to a
local SQLite that the central Mnemo did not read from.

Configuration (all via env vars, all optional):
    MNEMO_URL              Mnemo Cortex base URL (default: http://localhost:50001)
    MNEMO_AGENT_ID         Agent ID for writebacks (default: cc)
    MNEMO_CC_SESSIONS_DIR  Where Claude Code stores .jsonl session files
                           (default: ~/.claude/projects)
    MNEMO_CC_OFFSET_FILE   Sync offset state file
                           (default: ~/.mnemo-cc/cc-sync.offset.json)
    MNEMO_AUTH_TOKEN       API token sent as X-API-KEY; falls back to
                           ~/.mnemo-auth-token (needed when the server
                           enforces auth, ignored otherwise)
    MNEMO_CC_IDLE_FLUSH_S  Flush a sub-batch pending tail once the session
                           JSONL has been idle this long (default: 300)
    MNEMO_CC_SEATS         Which seat each projects folder is (item 18c'), e.g.
                           "-home-guy=cc,-home-guy-CC3=cc-cc3". Must match the
                           seat that session's bridge sends (MNEMO_SEAT, else
                           its session prefix): a pause covers lines of ITS
                           seat and seatless lines, not another seat's. An
                           unmapped folder sends no seat, so every pause on the
                           tenant covers it (the safe default).

Capture pauses (item 18c'): each tick reads the tenant's pause windows and
drops messages timed inside one for their seat; the rest are posted with
seat, client_now and event_span (ISO). Dropped messages are consumed, never
re-sent; the watchdog reports them as PAUSED. A 422 (the server refused the
event_span) follows CC ruling #7033: re-sent bare only when no window exists
on the tenant since the session's start, else spooled to 422-spool/ beside
the offset file; either way the watchdog stays red until someone looks.

Run modes:
    python3 mnemo-cc-sync.py            # batched: post when >=6 new msgs
    python3 mnemo-cc-sync.py --force    # force-flush regardless of count

Use the companion `mnemo-cc-sync-loop.sh` for periodic invocation
under systemd, or invoke from a cron / scheduler of your choice.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MNEMO_URL = os.environ.get("MNEMO_URL", "http://localhost:50001").rstrip("/")
AGENT_ID = os.environ.get("MNEMO_AGENT_ID", "cc")
SESSIONS_DIR = Path(os.environ.get(
    "MNEMO_CC_SESSIONS_DIR",
    str(Path.home() / ".claude/projects"),
))
OFFSET_FILE = Path(os.environ.get(
    "MNEMO_CC_OFFSET_FILE",
    str(Path.home() / ".mnemo-cc/cc-sync.offset.json"),
))


def _resolve_auth_token() -> str:
    """Mnemo API token: env MNEMO_AUTH_TOKEN first, else ~/.mnemo-auth-token
    (mode 0600). Sent as X-API-KEY so the sync authenticates against a server
    with auth enforced; harmless (ignored) when the server doesn't check."""
    tok = os.environ.get("MNEMO_AUTH_TOKEN", "").strip()
    if tok:
        return tok
    try:
        return (Path.home() / ".mnemo-auth-token").read_text().strip()
    except OSError:
        return ""


AUTH_TOKEN = _resolve_auth_token()

SEAT_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def parse_seats(raw: str) -> dict:
    """MNEMO_CC_SEATS "folder=seat,folder=seat" -> {folder: seat}. A bad pair
    is skipped loudly: that folder then sends no seat (covered by every
    pause on the tenant), never a wrong one."""
    seats = {}
    for pair in (raw or "").split(","):
        if not pair.strip():
            continue
        folder, _, seat = pair.partition("=")
        folder, seat = folder.strip(), seat.strip()
        if not folder or not SEAT_RE.fullmatch(seat):
            print(f"[cc-sync] MNEMO_CC_SEATS: ignoring {pair.strip()!r} "
                  f"(want folder=seat, seat [A-Za-z0-9_.-]{{1,64}})", file=sys.stderr)
            continue
        seats[folder] = seat
    return seats


SEATS = parse_seats(os.environ.get("MNEMO_CC_SEATS", ""))

# One-time migration from the pre-rename default path. The script used to be
# named mnemo-cc-artforge-sync.py and wrote its offset file to
# ~/.mnemo-cc/cc-artforge-sync.offset.json. If we find the old file and the
# user hasn't overridden MNEMO_CC_OFFSET_FILE, move it to the new default
# so existing installs don't reprocess their entire JSONL backlog.
_LEGACY_OFFSET = Path.home() / ".mnemo-cc/cc-artforge-sync.offset.json"
if (
    not os.environ.get("MNEMO_CC_OFFSET_FILE")
    and not OFFSET_FILE.exists()
    and _LEGACY_OFFSET.exists()
):
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LEGACY_OFFSET.rename(OFFSET_FILE)

# Batching policy
MIN_TURNS_PER_BATCH = 6
MAX_TURNS_PER_BATCH = 20
SUMMARY_MAX_CHARS = 12000

# A session that goes quiet with a sub-batch tail still deserves its post —
# flush pending messages once the JSONL has been idle this long. Without
# this, a conversation that ends on 1-5 unsynced turns holds them forever
# (and a sync watchdog sees "session active, nothing posted" and pages).
IDLE_FLUSH_S = float(os.environ.get("MNEMO_CC_IDLE_FLUSH_S", "300"))

# Role-aware snippet budgets (v4.8 creative harness). The old flat 300 chars
# per turn treated conversation as noise and tool calls as signal — inverted
# for creative users: a 10-minute riff was amputated to a series of turn-heads
# while "[tool: Bash]" survived intact. Conversation IS the signal; tool
# mechanics stay terse.
TURN_BUDGET = {
    "user": 2000,       # the user's riff is the most valuable text in the stream
    "assistant": 1200,  # narrated reasoning (worth keeping whole paragraphs of)
}
TOOL_ECHO_BUDGET = 300  # turns that are purely [tool: X]/[tool_result] lines


def _turn_budget(role: str, content: str) -> int:
    """Chars of a turn worth keeping: conversation gets room, tool echoes don't."""
    has_conversation = any(
        line.strip() and not line.strip().startswith("[tool")
        for line in content.splitlines()
    )
    if not has_conversation:
        return TOOL_ECHO_BUDGET
    return TURN_BUDGET.get(role, TOOL_ECHO_BUDGET)


# Only sessions touched inside this window are synced. Keeps each tick's
# work bounded and stops a months-old file from flooding in when touched.
ACTIVE_HOURS = float(os.environ.get("MNEMO_CC_ACTIVE_HOURS", "24"))


def list_session_jsonls() -> list[Path]:
    """One tree walk per tick — both the active filter and the prune use it."""
    if not SESSIONS_DIR.exists():
        return []
    return list(SESSIONS_DIR.rglob("*.jsonl"))


def load_state() -> dict:
    """Load state, migrating the legacy single-session schema to the
    per-file offset map ({"files": {relpath: {"byte_offset": N}}})."""
    state = {}
    if OFFSET_FILE.exists():
        try:
            state = json.loads(OFFSET_FILE.read_text())
        except Exception:
            state = {}

    if "files" not in state:
        files = {}
        # Legacy schema: {"session_id": stem, "byte_offset": N}. Carry the
        # offset over to that session's file or its whole backlog re-floods.
        legacy_stem = state.pop("session_id", None)
        legacy_offset = state.pop("byte_offset", 0)
        if legacy_stem and SESSIONS_DIR.exists():
            for p in SESSIONS_DIR.rglob(f"{legacy_stem}.jsonl"):
                files[str(p.relative_to(SESSIONS_DIR))] = {"byte_offset": legacy_offset}
                break
        state["files"] = files
    return state


def save_state(state: dict) -> None:
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OFFSET_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(OFFSET_FILE)


def extract_text(content) -> str:
    """Flatten Claude Code message content into plain text. Skips thinking parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces = []
        for part in content:
            if not isinstance(part, dict):
                continue
            t = part.get("type")
            if t == "text":
                pieces.append(part.get("text", ""))
            elif t == "tool_use":
                name = part.get("name", "?")
                pieces.append(f"[tool: {name}]")
            elif t == "tool_result":
                pieces.append("[tool_result]")
        return "\n".join(p for p in pieces if p)
    return ""


def parse_new_messages(jsonl_path: Path, byte_offset: int) -> tuple[list, int]:
    """Returns (messages, new_byte_offset).

    Consumes only newline-terminated bytes — a tick landing while Claude Code
    is mid-append must leave the torn final line unread (same fix class as
    the v4.9.7 watcher H2), instead of JSONDecodeError-skipping it forever.
    """
    messages = []
    with jsonl_path.open("rb") as fh:
        fh.seek(byte_offset)
        chunk = fh.read()

    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        return [], byte_offset
    new_offset = byte_offset + last_nl + 1

    # split("\n"), NOT splitlines(): Claude Code emits raw U+2028/U+2029 inside
    # JSON strings, and splitlines() would break such a record into fragments
    # that all fail json.loads — silently dropping the message.
    for line in chunk[:last_nl + 1].decode("utf-8", errors="replace").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") not in ("user", "assistant", "message"):
            continue
        msg = payload.get("message") or {}
        role = msg.get("role")
        if not role:
            continue
        content = extract_text(msg.get("content", ""))
        if not content.strip():
            continue
        messages.append({
            "role": role,
            "content": content,
            "timestamp": payload.get("timestamp", ""),
        })
    return messages, new_offset


def build_summary(messages: list, session_id: str) -> tuple[str, list]:
    """Build a structured summary + key facts from a batch of messages."""
    parts = [
        f"Claude Code session activity (auto-sync from JSONL, session={session_id[:8]}).",
        f"{len(messages)} new message(s) since last sync.",
        "",
        "Turns:",
    ]
    used_chars = sum(len(p) for p in parts)

    for m in messages[-MAX_TURNS_PER_BATCH:]:
        role = m["role"]
        content = m["content"]
        budget = _turn_budget(role, content)
        snippet = content[:budget] + ("…" if len(content) > budget else "")
        line = f"- [{role}] {snippet}"
        if used_chars + len(line) > SUMMARY_MAX_CHARS:
            parts.append(f"... ({len(messages) - len(parts) + 4} more turns truncated)")
            break
        parts.append(line)
        used_chars += len(line)

    summary = "\n".join(parts)

    # Surface tool invocations as recall-friendly key facts
    key_facts = []
    for m in messages:
        if m["role"] == "assistant" and "[tool:" in m["content"]:
            tools = [
                line.split("[tool:")[1].split("]")[0].strip()
                for line in m["content"].split("\n")
                if "[tool:" in line
            ]
            for t in tools:
                fact = f"{AGENT_ID} invoked tool: {t}"
                if fact not in key_facts:
                    key_facts.append(fact)

    if not key_facts:
        key_facts = [f"{AGENT_ID} session {session_id[:8]} activity sync — no tool invocations in this batch"]

    return summary[:SUMMARY_MAX_CHARS], key_facts[:10]


def _headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if AUTH_TOKEN:
        headers["X-API-KEY"] = AUTH_TOKEN
    return headers


def post_to_mnemo(session_id: str, summary: str, key_facts: list,
                  wire: dict | None = None, *, bare: bool = False) -> dict:
    """POST /writeback. `wire` = the seat / client_now / event_span fields;
    bare=True sends none of them (the 422 rule's re-send)."""
    payload = {
        "session_id": f"{AGENT_ID}-jsonl-{session_id[:12]}",
        "summary": summary,
        "key_facts": key_facts,
        "projects_referenced": [],
        "decisions_made": [],
        "agent_id": AGENT_ID,
    }
    if wire and not bare:
        payload.update(wire)
    req = urllib.request.Request(
        f"{MNEMO_URL}/writeback",
        data=json.dumps(payload).encode(),
        headers=_headers(),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def read_windows(since: float | None = None) -> list:
    """GET /capture/windows for the whole tenant (no seat: every window on
    it, its seats', tenant-wide and fleet-wide). `since` is epoch SECONDS (the
    endpoint's type). Raises on any failure."""
    q = {"agent_id": AGENT_ID}
    if since is not None:
        q["since"] = repr(float(since))
    req = urllib.request.Request(
        f"{MNEMO_URL}/capture/windows?{urllib.parse.urlencode(q)}", headers=_headers())
    with urllib.request.urlopen(req, timeout=15) as resp:
        windows = json.loads(resp.read()).get("windows")
    if not isinstance(windows, list):
        raise ValueError("reply has no windows list")
    return windows


def to_epoch(value) -> float | None:
    """An ISO-8601 line time -> epoch seconds; None when it cannot be read."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def seat_for(key: str) -> str | None:
    """The seat of a session file, from its projects folder (MNEMO_CC_SEATS)."""
    return SEATS.get(Path(key).parts[0]) if Path(key).parts else None


def windows_for_seat(windows: list, seat: str | None) -> list:
    """The server's rule (capture_gate.windows_for): a window applies when
    its seat or the writer's seat is None, or they match."""
    return [w for w in windows
            if w.get("seat") is None or seat is None or w.get("seat") == seat]


def covering(windows: list, lo: float, hi: float) -> str | None:
    """Id of a window that overlaps [lo, hi], or None."""
    for w in windows:
        end = w.get("end")
        if w["start"] <= hi and (end is None or end >= lo):
            return w["id"]
    return None


def split_by_windows(messages: list, windows: list) -> tuple[list, list, list]:
    """(kept, dropped, window_ids). A timed message is dropped when a window
    covers its time; an untimed one when a window touches the gap between its
    timed neighbours (none before / after = open-ended), the server's rule."""
    times = [to_epoch(m["timestamp"]) for m in messages]
    kept, dropped, wids = [], [], []
    for i, m in enumerate(messages):
        t = times[i]
        if t is not None:
            lo = hi = t
        else:
            lo = next((times[j] for j in range(i - 1, -1, -1) if times[j] is not None),
                      float("-inf"))
            hi = next((times[j] for j in range(i + 1, len(messages)) if times[j] is not None),
                      float("inf"))
        wid = covering(windows, lo, hi)
        if wid:
            dropped.append(m)
            if wid not in wids:
                wids.append(wid)
        else:
            kept.append(m)
    return kept, dropped, wids


def event_span(messages: list) -> list | None:
    """[first, last] event time of what a post carries, ISO; a trailing
    untimed message reaches to now. None when no message carries a time."""
    times = [t for t in (to_epoch(m["timestamp"]) for m in messages) if t is not None]
    if not times:
        return None
    hi = time.time() if to_epoch(messages[-1]["timestamp"]) is None else max(times)
    return [iso(min(times)), iso(hi)]


def session_start(jsonl: Path, entry: dict) -> float | None:
    """The session's start: the first timed line of its file, read once and
    kept in the file's offset entry. None when no line carries a time."""
    if entry.get("session_start") is not None:
        return entry["session_start"]
    try:
        with jsonl.open("rb") as fh:
            for raw in fh:
                try:
                    t = to_epoch(json.loads(raw).get("timestamp"))
                except (ValueError, AttributeError):
                    continue
                if t is not None:
                    entry["session_start"] = t
                    return t
    except OSError:
        return None
    return None


def spool_dir() -> Path:
    return OFFSET_FILE.parent / "422-spool"


def note_withheld(state: dict, wids: list, lines: int) -> None:
    w = state.setdefault("withheld", {"window_ids": [], "lines": 0})
    w["window_ids"] = (w.get("window_ids", []) + [x for x in wids if x not in w.get("window_ids", [])])[-10:]
    w["lines"] = int(w.get("lines", 0)) + lines
    w["last_at"] = iso(time.time())


def resolve_422(session_id: str, summary: str, key_facts: list, wire: dict,
                since: float | None, state: dict, err) -> bool:
    """CC ruling #7033. Returns True when the save is now accounted for (re-
    sent bare, or spooled) so the offset may advance."""
    detail = ""
    try:
        detail = err.read().decode("utf-8", "replace")[:200]
    except Exception:
        pass
    why = f"the server refused event_span {wire.get('event_span')} (422: {detail})"
    windows, read_error = None, None
    if since is None:
        read_error = "the session's start cannot be told"
    else:
        try:
            windows = read_windows(since)
        except Exception as e:
            read_error = str(e)
    if windows == []:
        try:
            post_to_mnemo(session_id, summary, key_facts, wire, bare=True)
        except Exception as e:
            print(f"[cc-sync] 422 field event_span on {session_id[:8]}: {why}; "
                  f"the bare re-send FAILED too ({e}); will retry", file=sys.stderr)
            return False
        print(f"[cc-sync] 422 field event_span on {session_id[:8]}: {why}. No pause "
              f"window on tenant {AGENT_ID} since the session started, so it was "
              f"RE-SENT BARE (no seat, no clock). Client bug: report it.", file=sys.stderr)
        return True
    cause = (f"{len(windows)} pause window(s) on tenant {AGENT_ID} since the session "
             f"started ({', '.join(w.get('id', '?') for w in windows)})"
             if windows else f"the windows read failed ({read_error})")
    path = spool_dir() / f"{session_id[:12]}-{int(time.time() * 1000)}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"session_id": session_id, "summary": summary,
                                    "key_facts": key_facts, "agent_id": AGENT_ID, **wire}))
        os.chmod(path, 0o600)
    except OSError as e:
        print(f"[cc-sync] 422 field event_span on {session_id[:8]}: {why}; NOT re-sent "
              f"({cause}) and the spool write FAILED ({e}); will retry", file=sys.stderr)
        return False
    sp = state.setdefault("spool_422", {"count": 0})
    sp["count"] = int(sp.get("count", 0)) + 1
    sp["last_at"] = iso(time.time())
    sp["path"] = str(path)
    print(f"[cc-sync] 422 field event_span on {session_id[:8]}: {why}. NOT re-sent: "
          f"{cause}. The raw save is spooled at {path}. Client bug: report it.",
          file=sys.stderr)
    return True


def sync_file(jsonl: Path, entry: dict, force: bool, state: dict | None = None,
              windows: list | None = None, key: str = "") -> tuple[bool, bool]:
    """Sync one session file against its own offset entry (mutated in place).
    `windows`: the tenant's pause windows this tick (None = the read failed:
    no client filter, the server still enforces). Returns (posted, failed)."""
    state = state if state is not None else {}
    session_id = jsonl.stem
    seat = seat_for(key)
    offset = entry.get("byte_offset", 0)
    if jsonl.stat().st_size < offset:
        offset = 0  # truncated/rotated — re-read rather than wedge

    messages, new_offset = parse_new_messages(jsonl, offset)

    if not messages:
        # Nothing ingestable, but housekeeping lines may still be consumable.
        entry["byte_offset"] = new_offset
        return False, False

    if not force and len(messages) < MIN_TURNS_PER_BATCH:
        idle_s = time.time() - jsonl.stat().st_mtime
        if idle_s < IDLE_FLUSH_S:
            return False, False  # Defer — wait for more activity; offset unchanged
        # JSONL idle with a pending tail — flush it rather than hold forever.

    dropped, wids = [], []
    if windows:
        messages, dropped, wids = split_by_windows(messages, windows_for_seat(windows, seat))
    if dropped:
        # Consumed, never re-sent: what a pause covers never lands.
        note_withheld(state, wids, len(dropped))
        print(f"[cc-sync] WITHHELD {len(dropped)} msgs from {session_id[:8]} "
              f"(seat {seat or 'none'}) by pause window(s) {', '.join(wids)}")
    if not messages:
        entry["byte_offset"] = new_offset
        return False, False

    summary, key_facts = build_summary(messages, session_id)
    wire = {"client_now": iso(time.time())}
    if seat:
        wire["seat"] = seat
    span = event_span(messages)
    if span:
        wire["event_span"] = span

    try:
        result = post_to_mnemo(session_id, summary, key_facts, wire)
    except urllib.error.HTTPError as e:
        if e.code != 422:
            print(f"[cc-sync] POST to {MNEMO_URL}/writeback failed for {session_id[:8]}: {e}",
                  file=sys.stderr)
            return False, True
        if resolve_422(session_id, summary, key_facts, wire,
                       session_start(jsonl, entry), state, e):
            entry["byte_offset"] = new_offset
            if "files" in state:
                # Persist now, like a post: a crash later in the tick must not
                # re-send bare twice or spool twice (no retry dedup).
                save_state(state)
        return False, True  # loud: rc 1 this tick; the watchdog keeps it red
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"[cc-sync] POST to {MNEMO_URL}/writeback failed for {session_id[:8]}: {e}",
              file=sys.stderr)
        return False, True  # Don't update offset — try again next tick

    if result.get("status") == "paused":
        # Delivered, not stored: the server's own window check (backstop).
        entry["byte_offset"] = new_offset
        m = re.search(r"window (w-[A-Za-z0-9_-]+)", result.get("message", ""))
        note_withheld(state, [m.group(1)] if m else [], len(messages))
        print(f"[cc-sync] DISCARDED {len(messages)} msgs from {session_id[:8]} "
              f"by capture pause{' window ' + m.group(1) if m else ''} (server)")
        return False, False

    entry.update({
        "byte_offset": new_offset,
        "last_post_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "last_memory_id": result.get("memory_id", ""),
    })
    print(f"[cc-sync] posted {len(messages)} msgs from {session_id[:8]} "
          f"→ memory_id={result.get('memory_id', '?')}")
    return True, False


def main(force: bool = False) -> int:
    all_files = list_session_jsonls()

    def mtime(p: Path) -> float | None:
        try:
            return p.stat().st_mtime
        except OSError:
            return None  # vanished between walk and stat

    # Every file touched inside the active window syncs with its own offset —
    # following only the single newest file made two live sessions alternate
    # as "newest", resetting the offset each flip (floods + skipped tails).
    cutoff = time.time() - ACTIVE_HOURS * 3600
    stamped = [(p, m) for p in all_files if (m := mtime(p)) is not None]
    active = [p for p, m in sorted(stamped, key=lambda pm: pm[1]) if m >= cutoff]

    state = load_state()
    files = state["files"]

    # One-time seed on install/schema-migration: files already on disk start
    # at their current end (sync forward only). The old single-file regime
    # already posted parts of them — starting at 0 would re-flood duplicates.
    # After seeding, an unseen file is a genuinely new session: start at 0.
    # (The legacy-migrated entry keeps its carried offset via setdefault.)
    if not state.get("seeded"):
        for jsonl in active:
            key = str(jsonl.relative_to(SESSIONS_DIR))
            try:
                size = jsonl.stat().st_size
            except OSError:
                continue
            files.setdefault(key, {"byte_offset": size})
        state["seeded"] = True

    # The seat map, for the bridge's pause-reply seat check (the seam).
    state["seat_map"] = SEATS
    windows = None
    if active:
        try:
            windows = read_windows()
        except Exception as e:
            print(f"[cc-sync] pause windows read failed ({e}): posting unfiltered, "
                  f"the server still withholds by window", file=sys.stderr)

    any_failed = False
    for jsonl in active:
        key = str(jsonl.relative_to(SESSIONS_DIR))
        entry = files.setdefault(key, {"byte_offset": 0})
        try:
            posted, failed = sync_file(jsonl, entry, force, state, windows, key)
        except OSError as e:
            # One vanished/unreadable file must not abort the other sessions.
            print(f"[cc-sync] skipping {key}: {e}", file=sys.stderr)
            any_failed = True
            continue
        if posted:
            # Top-level mirror — the sync-watchdog reads last_post_at directly.
            state["last_post_at"] = entry["last_post_at"]
            state["last_memory_id"] = entry.get("last_memory_id", "")
            # Persist per post: a crash later in the tick must not re-post
            # this batch next tick (/writeback has no retry dedup).
            save_state(state)
        any_failed = any_failed or failed

    # Drop entries for files that no longer exist so state stays bounded.
    live = {str(p.relative_to(SESSIONS_DIR)) for p in all_files}
    for key in list(files):
        if key not in live:
            del files[key]

    save_state(state)
    return 1 if any_failed else 0


if __name__ == "__main__":
    force = "--force" in sys.argv
    sys.exit(main(force=force))
