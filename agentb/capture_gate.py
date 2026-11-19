"""
Mnemo Cortex — capture pause gate (v4.1, scoped v4.28)
======================================================
"I'm about to do something sensitive — stop recording for a bit."

The auto-capture pipeline syncs terminal activity every 60 seconds. During a
credential rotation or any secret-handling work, even redaction-pattern misses
are a risk — the only safe capture is no capture. The gate pauses ambient
capture (/ingest, auto-capture-shaped /writeback, /chat saves, transcript
uploads).

Scope (v4.28). A pause belongs to ONE tenant by default: the agent that asked
for it. Until 2026-10-23 the gate was a single server-wide switch, and one
agent's hygiene pause silently discarded every other agent's checkpoints for
up to four hours (Opie #5185). Now `pause(agent_id=...)` gates only that
agent's writes; `pause(agent_id=None)` is the explicit fleet-wide form and is
reported as such. Several agents may hold their own pauses at once.

Forgetting to unpause would silently lobotomize the memory system, so the gate
is a dead-man switch, not a toggle: every pause carries an expiry (default 15
minutes, hard cap 4 hours) and the watchdog is lazy — any request that checks
the gate after expiry drops the expired entry. No background thread to crash,
and the state survives server restarts because it lives in a file.

What the gate does NOT cover: a deliberate save (source user or inferred, a
real summary, not category session_log) still lands while paused, so the *why*
of the sensitive operation can be saved. Every save with source=tool, category
session_log, or the shape of a routine log IS gated, deliberate or not.

Windows (item 18b', v4.29). A live pause answers "is capture off NOW"; it is
dropped at expiry, so it cannot answer "was 22:01 inside a pause" for a line
that arrives late. Every pause therefore also writes a WINDOW to an
append-only log beside the pause file (capture-windows.jsonl), keyed
(agent, seat), on event time, padded GUARD_SECONDS both sides. Writers that
carry event times are judged by the window, whenever they are posted. The log
is never pruned: a pruned window is a way for an old file to leak.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Union

from agentb.fsutil import atomic_write_text

log = logging.getLogger("agentb.capture_gate")

DEFAULT_PAUSE_MINUTES = 15
MAX_PAUSE_MINUTES = 240
FLEET_SCOPE = "*"          # the explicit server-wide pause
DEFAULT_SCOPE = "default"  # writes that carry no agent_id
SEAT_SEP = "@"             # live-pause key for a seated pause: "<scope>@<seat>"
SEAT_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")

# Event times are client clocks, windows are the server's. GUARD pads every
# window on both sides so skew errs toward withholding the pauser's own lines,
# never toward landing them. Measured 2026-11-19 on a two-machine install: the
# Mnemo host ran 1.90 s ahead of the agents' laptop (30 samples of /health,
# worst bound 1.92 s). Rule: at
# least 4x the worst measured skew (7.7 s), floor 120 s. The floor rules.
GUARD_SECONDS = 120
# A labelled client sends its own clock ("client_now"). Further than this from
# the server's clock, its event times are not trusted (item 18b' change B).
CLIENT_CLOCK_TOLERANCE_S = GUARD_SECONDS / 2
# A write with no trusted event time is gated by any window overlapping
# [now - LAG, now]: the longest a pre-pause event can wait in a client buffer
# (bridge: 2 min idle flush + buffer; jsonl sync: 300 s idle + ticks).
WRITEBACK_LAG_S = 600

TimeLike = Union[str, float, int, None]


def _scope(agent_id: Optional[str]) -> str:
    return agent_id if agent_id else DEFAULT_SCOPE


def _key(scope: str, seat: Optional[str]) -> str:
    return f"{scope}{SEAT_SEP}{seat}" if seat else scope


def _split_key(key: str) -> tuple[str, Optional[str]]:
    scope, _, seat = key.partition(SEAT_SEP)
    return scope, (seat or None)


def validate_seat(seat: Optional[str]) -> Optional[str]:
    if seat is not None and not SEAT_RE.fullmatch(seat):
        raise ValueError("seat must match [A-Za-z0-9_.-] (1-64 chars)")
    return seat


# A plausible event time, epoch seconds: from 2020-01-01T00:00Z up to 1e11
# (year 5138). Epoch MILLISECONDS (1.6e12 and up since 2020) read as seconds
# would be a far-future time outside every window: trusted, and landing.
EPOCH_MIN = 1_577_836_800.0
EPOCH_MAX = 1e11


def to_epoch(value: TimeLike) -> Optional[float]:
    """ISO-8601 string (a naive one is read as UTC) or epoch seconds -> epoch.
    None for anything else, or for a time outside [EPOCH_MIN, EPOCH_MAX): the
    caller treats it as 'no event time'."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        t = float(value)
    elif not isinstance(value, str) or not value.strip():
        return None
    else:
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        t = dt.timestamp()
    return t if EPOCH_MIN <= t < EPOCH_MAX else None


def clock_trusted(client_now: TimeLike, now: Optional[float] = None) -> bool:
    """True when the client sent its clock and it is within tolerance of ours."""
    c = to_epoch(client_now)
    if c is None:
        return False
    return abs(c - (time.time() if now is None else now)) <= CLIENT_CLOCK_TOLERANCE_S


def window_overlapping(windows: Iterable[dict], lo: float, hi: float) -> Optional[str]:
    """Id of the first window overlapping [lo, hi] (an open window has no end)."""
    for w in windows:
        end = w.get("end")
        if w["start"] <= hi and (end is None or lo <= end):
            return w["id"]
    return None


class CaptureGate:
    """File-backed, per-tenant pause switches with auto-resume on expiry."""

    def __init__(self, data_dir: Path):
        self.pause_file = Path(data_dir) / "capture-pause.json"
        self.window_log = Path(data_dir) / "capture-windows.jsonl"

    # ── window log (append-only; the last record for an id wins) ──

    def _append_window(self, rec: dict) -> None:
        self.window_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.window_log, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _windows(self) -> dict:
        """{id: window}. A line that does not parse is skipped LOUDLY: losing
        one record must not stop the rest of the log from gating."""
        if not self.window_log.exists():
            return {}
        out: dict = {}
        for n, line in enumerate(self.window_log.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                out[rec["id"]] = rec
            except (ValueError, KeyError, TypeError) as e:
                log.error(f"capture-windows.jsonl line {n} unreadable ({e}) — skipped")
        return out

    def _open_window(self, key: str, now: float, resume_at: float, reason: str,
                     started: Optional[float] = None) -> str:
        """Open a window for `key`, or extend the one already open (never a
        second overlapping one). Returns its id."""
        scope, seat = _split_key(key)
        for w in self._windows().values():
            if w.get("end") is None and w["agent"] == scope and w.get("seat") == seat:
                if resume_at > float(w.get("resume_at") or 0):
                    self._append_window({**w, "resume_at": resume_at})
                return w["id"]
        wid = f"w-{int(now)}-{os.urandom(3).hex()}"
        self._append_window({
            "id": wid, "agent": scope, "seat": seat,
            "start": (now if started is None else started) - GUARD_SECONDS, "end": None,
            "resume_at": resume_at, "reason": reason, "opened_at": now,
        })
        return wid

    def _close_window(self, wid: Optional[str], key: str, end_at: float) -> None:
        """Close `wid` (or, for an entry written before windows existed, every
        open window on `key`) at end_at + GUARD."""
        scope, seat = _split_key(key)
        for w in self._windows().values():
            if w.get("end") is not None:
                continue
            if (w["id"] == wid) or (wid is None and w["agent"] == scope and w.get("seat") == seat):
                self._append_window({**w, "end": end_at + GUARD_SECONDS, "closed_at": time.time()})

    # ── state file ──

    def _read(self) -> dict:
        """Return {scope: entry} with expired entries dropped.

        `_read_ok` is instance state shared by request threads. A race (one
        thread's failed read seen as ok by another) can only close a live
        window early, at now + GUARD, and the next windows_for reopens it from
        the live pause: benign (CC review #6942).

        Accepts the pre-v4.28 single-entry file (top-level `resume_at`) as a
        fleet-wide pause, so a pause set before an upgrade still honours its
        expiry instead of vanishing or sticking.
        """
        self._read_ok = True
        if not self.pause_file.exists():
            return {}
        try:
            raw = json.loads(self.pause_file.read_text(encoding="utf-8"))
        except Exception as e:
            self._read_ok = False
            # Unreadable state file: fail toward capturing (the system's normal
            # mode) but say so — a corrupt pause file must not be a silent
            # permanent pause.
            log.warning(f"capture-pause file unreadable ({e}) — treating as not paused")
            return {}
        if isinstance(raw, dict) and "pauses" in raw and isinstance(raw["pauses"], dict):
            pauses = raw["pauses"]
        elif isinstance(raw, dict) and "resume_at" in raw:
            pauses = {FLEET_SCOPE: raw}  # legacy single switch
        else:
            log.warning("capture-pause file has no recognisable shape — treating as not paused")
            self._read_ok = False
            return {}
        now = time.time()
        live = {}
        for scope, entry in pauses.items():
            try:
                if now < float(entry.get("resume_at", 0)):
                    live[scope] = entry
                else:
                    log.warning(f"▶ Capture RESUMED for {scope} (pause expired — watchdog)")
                    self._close_window(entry.get("window"), scope, float(entry["resume_at"]))
            except (TypeError, ValueError, AttributeError, KeyError):
                log.warning(f"capture-pause entry for {scope} malformed — dropped")
        if len(live) != len(pauses):
            self._write(live)
        return live

    def _write(self, pauses: dict) -> None:
        if not pauses:
            self.pause_file.unlink(missing_ok=True)
            return
        self.pause_file.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: a reader must never see a half-written file (an unreadable
        # file reads as "not paused").
        atomic_write_text(self.pause_file, json.dumps({"pauses": pauses}, indent=2))

    # ── control ──

    def pause(self, minutes: Optional[int] = None, reason: str = "",
              agent_id: Optional[str] = None, seat: Optional[str] = None) -> dict:
        """Pause ambient capture for `agent_id`; None = every agent (fleet-wide).
        `seat` narrows it to one seat of that tenant (no seat = every seat)."""
        validate_seat(seat)
        mins = minutes if minutes and minutes > 0 else DEFAULT_PAUSE_MINUTES
        mins = min(mins, MAX_PAUSE_MINUTES)
        now = time.time()
        if agent_id is None:
            seat = None   # a fleet-wide pause has no seat
        scope = _key(FLEET_SCOPE if agent_id is None else agent_id, seat)
        pauses = self._read()
        resume_at = now + mins * 60
        prior = pauses.get(scope)
        if prior:
            # Several seats share one tenant (CC, CC2, CC3 are all `cc`). A
            # shorter re-pause must never cut a running longer pause short —
            # capture would restart in the middle of someone's secret work.
            resume_at = max(resume_at, float(prior.get("resume_at", 0)))
        why = reason or "(no reason given)"
        wid = self._open_window(scope, now, resume_at, why)
        pauses[scope] = {
            "paused_at": now,
            "resume_at": resume_at,
            "minutes": int(round((resume_at - now) / 60)),
            "reason": why,
            "agent_id": None if scope == FLEET_SCOPE else agent_id,
            "seat": seat,
            "window": wid,
        }
        self._write(pauses)
        who = ("EVERY agent (fleet-wide)" if scope == FLEET_SCOPE
               else f"agent {agent_id}" + (f" seat {seat}" if seat else " (every seat)"))
        log.warning(f"⏸ Capture PAUSED until +{pauses[scope]['minutes']} min — {who} — {pauses[scope]['reason']} (auto-resumes)")
        # F2: the reply describes THIS pause, not whichever scope status() leads with.
        st = self.status()
        st.update(st["scopes"][scope])
        st["scope"] = scope
        return st

    def resume(self, agent_id: Optional[str] = None, *, all: bool = False,
               seat: Optional[str] = None) -> dict:
        """Clear `agent_id`'s pause plus any fleet-wide one.

        A fleet-wide pause is owned by nobody, so any agent's resume clears it
        too — the pre-v4.28 behaviour, kept so a stuck fleet-wide pause can be
        lifted from any seat. `agent_id=None` (a pre-4.28 bridge, which could
        only ever have SET a fleet-wide pause) clears the fleet-wide and
        `default` scopes and nothing else — one old seat resuming must not
        lift another agent's scoped pause. `all=True` clears everything.
        `seat` also clears that seat's own pause; a seated pause is never
        lifted by another seat's (or a seatless) resume.
        """
        validate_seat(seat)
        pauses = self._read()
        if all:
            cleared = list(pauses)
        else:
            targets = [FLEET_SCOPE, DEFAULT_SCOPE] if agent_id is None else [agent_id, FLEET_SCOPE]
            if agent_id is not None and seat:
                targets.append(_key(agent_id, seat))
            cleared = [s for s in targets if s in pauses]
        now = time.time()
        for s in cleared:
            entry = pauses.pop(s)
            try:
                end_at = min(now, float(entry.get("resume_at", now)))
            except (TypeError, ValueError):
                end_at = now
            self._close_window(entry.get("window"), s, end_at)
        self._write(pauses)
        if cleared:
            log.warning(f"▶ Capture RESUMED (manual) for {', '.join(cleared)}")
        return self.status()

    # ── queries ──

    def is_paused(self, agent_id: Optional[str] = None) -> bool:
        """True if a fleet-wide pause is live, or any pause on `agent_id`'s
        tenant is (its own seat's or another's: a caller with no seat could be
        ANY seat, so it is gated by every one — over-gate, never under-gate).

        Writes that carry no agent_id are gated by the `default` scope only.
        The lazy watchdog runs inside `_read`. Post-time only: /ingest and the
        /chat save post as the event happens.
        """
        pauses = self._read()
        scope = _scope(agent_id)
        return any(k == FLEET_SCOPE or _split_key(k)[0] == scope for k in pauses)

    def windows_for(self, agent_id: Optional[str], seat: Optional[str] = None) -> list[dict]:
        """Every window that applies to a writer: fleet-wide ones, its
        tenant's whole-tenant ones, and its own seat's. A writer with NO seat
        gets every window on its tenant (Q5: choose loss). Expired pauses are
        closed first, so an end is never left open by a lapsed pause."""
        live = self._read()
        # A pause set before windows existed (an upgrade mid-pause) has none:
        # give it one from its own paused_at, so its late lines are covered too.
        for k, e in live.items():
            if not e.get("window"):
                now = time.time()
                started = e.get("paused_at")
                self._open_window(k, now, float(e["resume_at"]), e.get("reason", ""),
                                  started=float(started) if isinstance(started, (int, float)) else now)
        windows = self._windows()
        # A live pause whose window was closed (a read glitch closed it before
        # this fix, or a clock jump): reopen it. A live pause is never uncovered.
        for k, e in live.items():
            w = windows.get(e.get("window"))
            if w is not None and w.get("end") is not None:
                windows[w["id"]] = {**w, "end": None, "resume_at": float(e["resume_at"])}
                self._append_window(windows[w["id"]])
        # A window left open with no live pause behind it (a crash between the
        # two writes, a deleted pause file): close it where it would have ended.
        # Never on a failed read: one unreadable moment must not close a live
        # window for good (the log is append-only).
        for w in list(windows.values()):
            if not self._read_ok:
                break
            if w.get("end") is None and _key(w["agent"], w.get("seat")) not in live:
                end_at = min(time.time(), float(w.get("resume_at") or time.time()))
                self._close_window(w["id"], _key(w["agent"], w.get("seat")), end_at)
                windows[w["id"]] = {**w, "end": end_at + GUARD_SECONDS}
        scope = _scope(agent_id)
        out = []
        for w in windows.values():
            if w["agent"] == FLEET_SCOPE:
                out.append(w)
            elif w["agent"] == scope and (w.get("seat") is None or seat is None
                                          or w.get("seat") == seat):
                out.append(w)
        return sorted(out, key=lambda w: w["start"])

    def covers(self, ts: TimeLike, agent_id: Optional[str],
               seat: Optional[str] = None) -> Optional[str]:
        """Id of a window holding event time `ts` for this writer, else None."""
        t = to_epoch(ts)
        if t is None:
            return None
        return window_overlapping(self.windows_for(agent_id, seat), t, t)

    def status(self) -> dict:
        """`paused` = any pause live. `scopes` lists every live pause.

        The legacy top-level fields (reason, resume_at, remaining_seconds)
        describe the fleet-wide pause when one is live, else the first scoped
        one — existing readers (health, the bridge's pause reply) keep working.
        """
        pauses = self._read()
        if not pauses:
            return {"paused": False, "fleet_wide": False, "scopes": {}}
        now = time.time()
        scopes = {
            s: {
                "reason": e.get("reason", ""),
                "paused_at": e.get("paused_at"),
                "resume_at": e.get("resume_at"),
                "remaining_seconds": int(float(e["resume_at"]) - now),
                "seat": e.get("seat"),
                "window": e.get("window"),
            }
            for s, e in pauses.items()
        }
        lead = scopes[FLEET_SCOPE] if FLEET_SCOPE in scopes else next(iter(scopes.values()))
        return {
            "paused": True,
            "fleet_wide": FLEET_SCOPE in scopes,
            "scopes": scopes,
            **lead,
        }
