"""Jev live-path shadow (4.26.0) — the proposal envelope's first writer.

After a writeback has committed, a background task asks TypeSafe's Jev which
category fits the new memory. When Jev disagrees with the stored category it
files a `memory_category` proposal; when it agrees it writes nothing. The
stored category NEVER changes here — only Guy's accept moves it (S357).

The shadow sends memory text to TypeSafe's cloud, so:
- `redact_text` runs on the outbound payload, not only on stored quotes;
- only tenants in MNEMO_JEV_TENANTS (default `cc`) are ever sent;
- at most MAX_IN_FLIGHT calls are open; overflow is dropped and counted,
  never queued — an outage cannot pile tasks on the server;
- every outcome is counted (jev_shadow_stats) so silence is never read as
  agreement (doctrine-clean-is-not-a-check).

The key is read once from MNEMO_JEV_KEY_FILE at startup. It rides only the
Authorization header; it is never logged, never in a URL, never in a repr.
Spec: brain/spec-blend-build1-proposal-envelope.md.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

import httpx

from agentb.classify import CLASSIFIABLE_CATEGORIES, DEFAULT_MAX_INPUT_CHARS
from agentb.redact import redact_text

log = logging.getLogger(__name__)

# ── Copied from tools/experiments/jev_shadow.py (the offline spike, ea89ebb).
# This module is the LIVE copy; edit here for the server. The spike keeps
# its own for offline runs.
API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
# Tier-1 targets only. session_log is the free noise heuristic's job and Jev
# should never be asked about it — same exclusion the LLM prompt makes.
CRITERIA = {name: desc for name, desc in CLASSIFIABLE_CATEGORIES if name != "session_log"}
INSTRUCTIONS = "Which single category best fits this memory for a memory system?"


def build_request(text: str, max_chars: int = DEFAULT_MAX_INPUT_CHARS) -> dict:
    """The exact JSON Jev gets. Same input cap as the live classifier."""
    return {
        "state": text.strip()[:max_chars],
        "model": MODEL,
        "questions": {
            "category": {
                "type": "choice",
                "instructions": INSTRUCTIONS,
                "criteria": CRITERIA,
            }
        },
    }


def parse_response(body: dict) -> dict:
    """Pull the fields we keep. Raises KeyError on an unexpected shape —
    the caller records that as an error rather than guessing."""
    ans = body["answers"]["category"]
    probs = {k: float(v) for k, v in ans["probabilities"].items()}
    ranked = sorted(probs.values(), reverse=True)
    margin = ranked[0] - ranked[1] if len(ranked) > 1 else ranked[0]
    return {
        "jev_choice": ans["choice"],
        "confidence": float(ans["confidence"]),
        "probabilities": probs,
        "top2_margin": round(margin, 4),
        "jev_model": body.get("model"),
        "input_tokens": (body.get("usage") or {}).get("input_tokens"),
    }
# ── end of the copy ──

SOURCE_RUN = "jev:shadow:v1"
MAX_IN_FLIGHT = 8
TIMEOUT_S = 5.0
QUOTE_CHARS = 300
DEFAULT_TENANTS = ("cc",)
ERROR_RATE_RED = 0.20


def confidence_bucket(conf: float) -> str:
    """0.1-wide band; confidence 1.0 lands in 0.9."""
    return f"{min(max(int(conf * 10), 0), 9) / 10:.1f}"


def utc_day(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() if ts is None else ts))


def day_verdict(totals: dict) -> tuple[bool, list[str]]:
    """(red, reasons) for one day's summed counters. RED when writebacks
    qualified but no call was made, or when more than 20% of the calls
    made failed (error + timeout)."""
    reasons = []
    eligible, attempted = totals.get("eligible", 0), totals.get("attempted", 0)
    failed = totals.get("error", 0) + totals.get("timeout", 0)
    if eligible > 0 and attempted == 0:
        reasons.append(f"{eligible} eligible writeback(s), zero calls")
    if attempted and failed / attempted > ERROR_RATE_RED:
        reasons.append(f"error+timeout {failed}/{attempted} > {int(ERROR_RATE_RED * 100)}%")
    return bool(reasons), reasons


class JevShadow:
    """One per server. `submit` is called on the event loop after a
    writeback commits; it never awaits, never raises into the request."""

    def __init__(self, key: str, tenants, facts, *, transport=None,
                 timeout: float = TIMEOUT_S, max_in_flight: int = MAX_IN_FLIGHT):
        if not key:
            raise ValueError("JevShadow needs a key")
        self.tenants = frozenset(tenants)
        self.facts = facts
        self.max_in_flight = max_in_flight
        self.timeout = timeout
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {key}"}, timeout=timeout, transport=transport)
        self._in_flight = 0
        self._tasks: set[asyncio.Task] = set()
        # drops wait here and ride the next counter write: bumping the table
        # per drop would spawn exactly the unbounded work the cap forbids
        self._pending_drops: dict[tuple[str, str, str], int] = defaultdict(int)

    def __repr__(self) -> str:  # never the key
        return f"JevShadow(tenants={sorted(self.tenants)}, in_flight={self._in_flight})"

    @classmethod
    def from_env(cls, facts, env=None, transport=None) -> Optional["JevShadow"]:
        """None when MNEMO_JEV_KEY_FILE is unset (the shadow is off). A set
        but unreadable or empty key file is logged loudly and also yields
        None: the server serves, the shadow stays off, the stats say so."""
        env = os.environ if env is None else env
        key_file = (env.get("MNEMO_JEV_KEY_FILE") or "").strip()
        if not key_file:
            return None
        try:
            lines = Path(key_file).read_text(encoding="utf-8").strip().splitlines()
        except OSError as e:
            log.error(f"Jev shadow OFF: key file unreadable ({type(e).__name__})")
            return None
        key = lines[-1].strip() if lines else ""
        if not key:
            log.error("Jev shadow OFF: key file is empty")
            return None
        raw = env.get("MNEMO_JEV_TENANTS")
        tenants = ([t.strip() for t in raw.split(",") if t.strip()]
                   if raw is not None else list(DEFAULT_TENANTS))
        log.info(f"Jev shadow ON for tenant(s) {sorted(tenants)} (in-flight cap {MAX_IN_FLIGHT})")
        return cls(key, tenants, facts, transport=transport)

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def submit(self, tenant: str, memory_dir: Path, entry: dict) -> None:
        """Shadow one committed memory, off the request path."""
        if tenant not in self.tenants:
            return
        category = entry.get("category")
        # session_log is never asked; a regex placeholder will be replaced
        # by the reclassify pass, so a disagreement with it is noise (D5)
        if category not in CRITERIA or entry.get("needs_reclassification"):
            return
        day = utc_day()
        if self._in_flight >= self.max_in_flight:
            self._pending_drops[(day, tenant, category)] += 1
            return
        self._in_flight += 1
        task = asyncio.create_task(self._run(day, tenant, memory_dir, dict(entry)))
        self._tasks.add(task)
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        self._in_flight -= 1
        if not task.cancelled() and task.exception() is not None:
            log.error(f"Jev shadow task died: {type(task.exception()).__name__}")

    def _write_drops(self, drops: dict) -> None:
        for (day, tenant, cat), n in drops.items():
            self.facts.jev_bump(day, tenant, cat, eligible=n, dropped=n)

    @property
    def pending_drops(self) -> int:
        return sum(self._pending_drops.values())

    async def flush(self) -> None:
        """Write held drop counts now (GET /proposals/jev-stats calls this).
        The swap happens here, on the loop — submit() adds on the loop too.
        A failed write puts the counts back, to be retried; it re-raises.
        (A write that fails part-way can re-add a cell it already wrote —
        over-counting drops beats losing them.)"""
        if self._pending_drops:
            drops, self._pending_drops = self._pending_drops, defaultdict(int)
            try:
                await asyncio.to_thread(self._write_drops, drops)
            except Exception:
                for k, n in drops.items():
                    self._pending_drops[k] += n
                raise

    async def _bump(self, day, tenant, cat, bucket="-", **counts) -> None:
        await asyncio.to_thread(self.facts.jev_bump, day, tenant, cat, bucket, **counts)

    async def _run(self, day: str, tenant: str, memory_dir: Path, entry: dict) -> None:
        """Every call ends in exactly one terminal counter — ok (with agree or
        disagree), timeout, or error — so silence is never read as agreement.
        Anything unexpected is an error, never a dead task with no outcome."""
        cat = entry["category"]
        try:
            await self.flush()
        except Exception as e:  # the drops are kept for the next flush
            log.error(f"Jev shadow: drop counts not written ({type(e).__name__})")
        attempted = False
        try:
            await self._bump(day, tenant, cat, eligible=1, attempted=1)
            attempted = True
            outcome, bucket, choice, conf = await self._ask(entry)
            await self._bump(day, tenant, cat, bucket, **outcome)
        except Exception as e:
            log.error(f"Jev shadow: unexpected {type(e).__name__}; counted as error")
            try:
                await self._bump(day, tenant, cat, error=1,
                                 **({} if attempted else {"eligible": 1, "attempted": 1}))
            except Exception as e2:
                log.error(f"Jev shadow: counters unwritable ({type(e2).__name__}); "
                          f"this call is uncounted")
            return
        if outcome.get("disagree"):
            await self._propose(tenant, memory_dir, entry, choice, conf)

    async def _ask(self, entry: dict) -> tuple[dict, str, Optional[str], float]:
        """One Jev call -> (terminal counters, bucket, choice, confidence).
        Expected failures come back as counters; anything else raises."""
        text = (entry.get("summary") or "") + "\n" + "\n".join(entry.get("key_facts") or [])
        # the writeback already redacted; this is the egress door, so again
        payload = build_request(redact_text(text)[0])
        try:
            # httpx timeouts are per network operation; a server that trickles
            # bytes could hold the call far longer. wait_for is the total cap.
            r = await asyncio.wait_for(self._client.post(API_URL, json=payload), self.timeout)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            return {"timeout": 1}, "-", None, 0.0
        except httpx.HTTPError as e:
            log.warning(f"Jev shadow call failed: {type(e).__name__}")
            return {"error": 1}, "-", None, 0.0
        if r.status_code != 200:
            log.warning(f"Jev shadow: HTTP {r.status_code}")
            return {"error": 1}, "-", None, 0.0
        try:
            parsed = parse_response(r.json())
            choice, conf = parsed["jev_choice"], parsed["confidence"]
            if not isinstance(choice, str) or not 0.0 <= conf <= 1.0:  # NaN fails this too
                raise ValueError("choice or confidence out of range")
            bucket = confidence_bucket(conf)
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            log.warning("Jev shadow: unexpected response shape")
            return {"error": 1}, "-", None, 0.0
        verdict = "agree" if choice == entry["category"] else "disagree"
        return {"ok": 1, verdict: 1}, bucket, choice, conf

    async def _propose(self, tenant: str, memory_dir: Path, entry: dict,
                       choice: str, conf: float) -> None:
        quote = redact_text(entry.get("summary") or "")[0][:QUOTE_CHARS] or "(empty summary)"
        try:
            await asyncio.to_thread(
                self.facts.propose_memory, "memory_category", tenant, entry["id"], memory_dir,
                choice, [quote], conf, SOURCE_RUN, "jev", entry["category"])
        except ValueError as e:
            # Jev named a label we do not have, or the memory left the disk
            log.warning(f"Jev shadow proposal refused: {e}")
        except Exception as e:
            # counted as a disagreement already; the row is what was lost
            log.error(f"Jev shadow: disagreement on {entry['id']} counted but its "
                      f"proposal was LOST ({type(e).__name__})")

    async def aclose(self, grace: float = 5.0) -> None:
        tasks = set(self._tasks)
        if tasks:
            await asyncio.wait(tasks, timeout=grace)
        for t in tasks:
            t.cancel()
        # a cancelled task still has to unwind before the client closes
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.flush()
        except Exception as e:
            log.error(f"Jev shadow: {self.pending_drops} drop count(s) lost at "
                      f"shutdown ({type(e).__name__})")
        await self._client.aclose()
