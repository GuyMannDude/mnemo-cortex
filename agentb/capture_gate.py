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

Deliberate manual saves are NEVER gated — pausing ambient capture while saving
the *why* of the sensitive operation is exactly the intended workflow.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("agentb.capture_gate")

DEFAULT_PAUSE_MINUTES = 15
MAX_PAUSE_MINUTES = 240
FLEET_SCOPE = "*"          # the explicit server-wide pause
DEFAULT_SCOPE = "default"  # writes that carry no agent_id


def _scope(agent_id: Optional[str]) -> str:
    return agent_id if agent_id else DEFAULT_SCOPE


class CaptureGate:
    """File-backed, per-tenant pause switches with auto-resume on expiry."""

    def __init__(self, data_dir: Path):
        self.pause_file = Path(data_dir) / "capture-pause.json"

    # ── state file ──

    def _read(self) -> dict:
        """Return {scope: entry} with expired entries dropped.

        Accepts the pre-v4.28 single-entry file (top-level `resume_at`) as a
        fleet-wide pause, so a pause set before an upgrade still honours its
        expiry instead of vanishing or sticking.
        """
        if not self.pause_file.exists():
            return {}
        try:
            raw = json.loads(self.pause_file.read_text(encoding="utf-8"))
        except Exception as e:
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
            return {}
        now = time.time()
        live = {}
        for scope, entry in pauses.items():
            try:
                if now < float(entry.get("resume_at", 0)):
                    live[scope] = entry
                else:
                    log.warning(f"▶ Capture RESUMED for {scope} (pause expired — watchdog)")
            except (TypeError, ValueError, AttributeError):
                log.warning(f"capture-pause entry for {scope} malformed — dropped")
        if len(live) != len(pauses):
            self._write(live)
        return live

    def _write(self, pauses: dict) -> None:
        if not pauses:
            self.pause_file.unlink(missing_ok=True)
            return
        self.pause_file.parent.mkdir(parents=True, exist_ok=True)
        self.pause_file.write_text(
            json.dumps({"pauses": pauses}, indent=2), encoding="utf-8"
        )

    # ── control ──

    def pause(self, minutes: Optional[int] = None, reason: str = "",
              agent_id: Optional[str] = None) -> dict:
        """Pause ambient capture for `agent_id`; None = every agent (fleet-wide)."""
        mins = minutes if minutes and minutes > 0 else DEFAULT_PAUSE_MINUTES
        mins = min(mins, MAX_PAUSE_MINUTES)
        now = time.time()
        scope = FLEET_SCOPE if agent_id is None else agent_id
        pauses = self._read()
        resume_at = now + mins * 60
        prior = pauses.get(scope)
        if prior:
            # Several seats share one tenant (CC, CC2, CC3 are all `cc`). A
            # shorter re-pause must never cut a running longer pause short —
            # capture would restart in the middle of someone's secret work.
            resume_at = max(resume_at, float(prior.get("resume_at", 0)))
        pauses[scope] = {
            "paused_at": now,
            "resume_at": resume_at,
            "minutes": int(round((resume_at - now) / 60)),
            "reason": reason or "(no reason given)",
            "agent_id": None if scope == FLEET_SCOPE else agent_id,
        }
        self._write(pauses)
        who = "EVERY agent (fleet-wide)" if scope == FLEET_SCOPE else f"agent {agent_id}"
        log.warning(f"⏸ Capture PAUSED until +{pauses[scope]['minutes']} min — {who} — {pauses[scope]['reason']} (auto-resumes)")
        # F2: the reply describes THIS pause, not whichever scope status() leads with.
        st = self.status()
        st.update(st["scopes"][scope])
        st["scope"] = scope
        return st

    def resume(self, agent_id: Optional[str] = None, *, all: bool = False) -> dict:
        """Clear `agent_id`'s pause plus any fleet-wide one.

        A fleet-wide pause is owned by nobody, so any agent's resume clears it
        too — the pre-v4.28 behaviour, kept so a stuck fleet-wide pause can be
        lifted from any seat. `agent_id=None` (a pre-4.28 bridge, which could
        only ever have SET a fleet-wide pause) clears the fleet-wide and
        `default` scopes and nothing else — one old seat resuming must not
        lift another agent's scoped pause. `all=True` clears everything.
        """
        pauses = self._read()
        if all:
            cleared = list(pauses)
            pauses = {}
        else:
            targets = (FLEET_SCOPE, DEFAULT_SCOPE) if agent_id is None else (agent_id, FLEET_SCOPE)
            cleared = [s for s in targets if s in pauses]
            for s in cleared:
                pauses.pop(s, None)
        self._write(pauses)
        if cleared:
            log.warning(f"▶ Capture RESUMED (manual) for {', '.join(cleared)}")
        return self.status()

    # ── queries ──

    def is_paused(self, agent_id: Optional[str] = None) -> bool:
        """True if a fleet-wide pause is live, or `agent_id`'s own pause is.

        Writes that carry no agent_id are gated by the `default` scope only.
        The lazy watchdog runs inside `_read`.
        """
        pauses = self._read()
        return FLEET_SCOPE in pauses or _scope(agent_id) in pauses

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
