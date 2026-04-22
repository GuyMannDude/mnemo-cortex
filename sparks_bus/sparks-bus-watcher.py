#!/usr/bin/env python3
"""
Sparks Bus Watcher — daemon that drives the bus.sqlite delivery lifecycle.

Two modes (auto-detected at startup by probing the Mnemo URL):

  full        Mnemo Cortex reachable. Each message's payload is saved to Mnemo
              by tracking_id; Discord notifications carry just the receipt.
              The full doctrine: doorbell + mailbox + tracking number.

  standalone  No Mnemo. Payload travels in the Discord notification itself.
              Delivery + ACK lifecycle still works; semantic recall does not.

Per poll cycle:
  1. notify    — for each new row: (full mode) save payload to Mnemo;
                 always: post 📬 DELIVERED (or 🔄 LOOP CLOSED for replies)
                 to the dispatch channel.
  2. deliver   — wake target agents (claude / http / discord) using
                 the configured per-agent method.
  3. pickup    — for any row that has been read since last cycle, post
                 ✅ PICKED UP.
  4. stale     — for any DELIVERED-but-unread row older than stale_seconds,
                 post ⚠️ STALE to the alerts channel.
  5. failure   — handled inline during deliver: post ⚠️ DELIVERY FAILED
                 once and stamp delivery_failed_at so we don't spam retries.

Config: path resolution order is BUS_CONFIG env var → ./config.json →
        ./config.example.json (fallback only). All config keys can be
        individually overridden by env vars (BUS_DB_PATH, BUS_MNEMO_URL,
        BUS_STALE_SECONDS, etc.). See config.example.json for the full list.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("sparks-bus")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "db_path": "~/.sparks/bus.sqlite",
    "poll_interval_seconds": 30,
    "stale_seconds": 3600,
    "mnemo": {
        "url": "http://localhost:50001",
        "agent_id": "bus",
        "writeback_endpoint": "/writeback",
        "health_endpoint": "/health",
        "timeout_seconds": 30,
    },
    "discord": {
        "token_file": "~/.sparks/discord-token",
        "channels_file": "./discord-channels.json",
        "dispatch_channel": "dispatch",
        "alerts_channel": "alerts",
        "post_timeout_seconds": 15,
    },
    "agents": {},
}


def _expand(path: str) -> str:
    return os.path.expanduser(os.path.expandvars(path))


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overrides() -> dict:
    """Map BUS_* env vars onto config dict keys. Only known keys are honored."""
    overrides: dict[str, Any] = {}
    if v := os.environ.get("BUS_DB_PATH"):
        overrides["db_path"] = v
    if v := os.environ.get("BUS_POLL_INTERVAL_SECONDS"):
        overrides["poll_interval_seconds"] = int(v)
    if v := os.environ.get("BUS_STALE_SECONDS"):
        overrides["stale_seconds"] = int(v)
    mnemo: dict[str, Any] = {}
    if v := os.environ.get("BUS_MNEMO_URL"):
        mnemo["url"] = v
    if v := os.environ.get("BUS_MNEMO_AGENT_ID"):
        mnemo["agent_id"] = v
    if mnemo:
        overrides["mnemo"] = mnemo
    discord: dict[str, Any] = {}
    if v := os.environ.get("BUS_DISCORD_TOKEN_FILE"):
        discord["token_file"] = v
    if v := os.environ.get("BUS_DISCORD_CHANNELS_FILE"):
        discord["channels_file"] = v
    if v := os.environ.get("BUS_DISPATCH_CHANNEL"):
        discord["dispatch_channel"] = v
    if v := os.environ.get("BUS_ALERTS_CHANNEL"):
        discord["alerts_channel"] = v
    if discord:
        overrides["discord"] = discord
    return overrides


def load_config() -> dict:
    """Resolve config from BUS_CONFIG | ./config.json | ./config.example.json."""
    here = Path(__file__).parent
    candidates = []
    if env_path := os.environ.get("BUS_CONFIG"):
        candidates.append(Path(_expand(env_path)))
    candidates.extend([here / "config.json", here / "config.example.json"])

    chosen: Path | None = None
    raw: dict = {}
    for path in candidates:
        if path.is_file():
            chosen = path
            raw = json.loads(path.read_text())
            break

    cfg = _deep_merge(DEFAULT_CONFIG, raw)
    cfg = _deep_merge(cfg, _env_overrides())

    # Expand user paths once, here.
    cfg["db_path"] = _expand(cfg["db_path"])
    cfg["discord"]["token_file"] = _expand(cfg["discord"]["token_file"])
    channels_path = cfg["discord"]["channels_file"]
    if not os.path.isabs(channels_path) and not channels_path.startswith("~"):
        channels_path = str((here / channels_path).resolve())
    cfg["discord"]["channels_file"] = _expand(channels_path)
    cfg["_config_source"] = str(chosen) if chosen else "<defaults only>"
    return cfg


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text() if (Path(__file__).parent / "schema.sql").is_file() else ""


def open_db(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    if SCHEMA_SQL:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    return conn


def get_unread(db: sqlite3.Connection, agent: str) -> list[dict]:
    cur = db.execute(
        "SELECT id, from_agent, subject, body, created_at, tracking_id FROM messages "
        "WHERE to_agent = ? AND read = 0 AND delivery_failed_at IS NULL "
        "ORDER BY created_at ASC",
        (agent,),
    )
    return [
        {"id": r[0], "from": r[1], "subject": r[2], "body": r[3], "time": r[4], "tracking_id": r[5]}
        for r in cur.fetchall()
    ]


def mark_read(db: sqlite3.Connection, msg_id: int) -> None:
    db.execute(
        "UPDATE messages SET read = 1, read_at = datetime('now') WHERE id = ?",
        (msg_id,),
    )
    db.commit()


def save_reply(db: sqlite3.Connection, from_agent: str, to_agent: str, subject: str, body: str, reply_to: int) -> None:
    db.execute(
        "INSERT INTO messages (from_agent, to_agent, subject, body, reply_to) VALUES (?, ?, ?, ?, ?)",
        (from_agent, to_agent, subject, body, reply_to),
    )
    db.commit()


def tracking_id_for(msg_id: int, created_at: str, is_reply: bool) -> str:
    iso = str(created_at).replace(" ", "T").replace(":", "").replace("-", "")
    prefix = "bus-reply" if is_reply else "bus"
    return f"{prefix}-{msg_id}-{iso}"


# ---------------------------------------------------------------------------
# Discord helpers
# ---------------------------------------------------------------------------


class DiscordClient:
    def __init__(self, token_file: str, channels_file: str, post_timeout: int):
        self._token_file = token_file
        self._channels_file = channels_file
        self._timeout = post_timeout
        self._token: str | None = None
        self._channels: dict[str, str] | None = None

    def _token_value(self) -> str:
        if self._token is None:
            self._token = Path(self._token_file).read_text().strip()
        return self._token

    def _channel_map(self) -> dict[str, str]:
        if self._channels is None:
            data = json.loads(Path(self._channels_file).read_text())
            self._channels = {k: str(v) for k, v in data.get("channels", {}).items()}
        return self._channels

    def post(self, channel_name: str, content: str) -> tuple[bool, str]:
        try:
            channel_id = self._channel_map().get(channel_name)
            if not channel_id:
                return False, f"Unknown channel: {channel_name}"
            r = requests.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {self._token_value()}", "Content-Type": "application/json"},
                json={"content": content[:1900]},
                timeout=self._timeout,
            )
            if r.ok:
                return True, f"Posted to #{channel_name}"
            return False, f"Discord HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            return False, f"Discord error: {e}"


# ---------------------------------------------------------------------------
# Mnemo client (full-mode only; no-op in standalone mode)
# ---------------------------------------------------------------------------


class MnemoClient:
    """Posts payloads to Mnemo Cortex by tracking_id. In standalone mode, an
    instance with available=False is created and save() is a no-op."""

    def __init__(self, url: str, agent_id: str, writeback_endpoint: str, timeout: int):
        self.url = url.rstrip("/")
        self.agent_id = agent_id
        self.writeback = writeback_endpoint
        self.timeout = timeout
        self.available = False

    @classmethod
    def probe(cls, mnemo_cfg: dict) -> "MnemoClient":
        client = cls(
            url=mnemo_cfg["url"],
            agent_id=mnemo_cfg["agent_id"],
            writeback_endpoint=mnemo_cfg["writeback_endpoint"],
            timeout=mnemo_cfg["timeout_seconds"],
        )
        try:
            r = requests.get(f"{client.url}{mnemo_cfg['health_endpoint']}", timeout=5)
            client.available = r.ok
        except Exception:
            client.available = False
        return client

    def save(self, tracking_id: str, from_agent: str, to_agent: str, subject: str, body_raw, is_reply: bool) -> bool:
        if not self.available:
            return True  # standalone mode: nothing to save, treat as success
        try:
            body_text = body_raw if isinstance(body_raw, str) else json.dumps(body_raw)
            label = "Bus reply" if is_reply else "Bus message"
            payload = {
                "session_id": tracking_id,
                "agent_id": self.agent_id,
                "summary": f"{label} from {from_agent} to {to_agent}: {subject}",
                "key_facts": [
                    f"from: {from_agent}",
                    f"to: {to_agent}",
                    f"subject: {subject}",
                    f"body: {body_text[:1800]}",
                ],
            }
            r = requests.post(f"{self.url}{self.writeback}", json=payload, timeout=self.timeout)
            if not r.ok:
                log.error(f"Mnemo writeback {tracking_id} failed: {r.status_code} {r.text[:200]}")
                return False
            return True
        except Exception as e:
            log.error(f"Mnemo writeback {tracking_id} error: {e}")
            return False


# ---------------------------------------------------------------------------
# A2A task-shape translation
# ---------------------------------------------------------------------------

A2A_LIFECYCLE = {
    "created": "submitted",
    "delivered": "submitted",
    "picked_up": "working",
    "replied": "completed",
    "delivery_failed": "failed",
    "stale": "submitted",  # never moved past submitted
}


def to_a2a_task(msg_row: dict, lifecycle_state: str = "delivered") -> dict:
    """Render a bus message row as an A2A-compatible Task object.

    Mapping:
      tracking_id     -> task.id            (globally unique)
      subject         -> task.name
      body            -> task.input
      from_agent      -> task.metadata.from
      to_agent        -> task.metadata.to
      mnemo_saved_at  -> task.artifact      (Mnemo session_id == tracking_id)
      lifecycle       -> task.state         (see A2A_LIFECYCLE)
    """
    body_raw = msg_row.get("body") or "{}"
    try:
        body = json.loads(body_raw) if isinstance(body_raw, str) else body_raw
    except (json.JSONDecodeError, TypeError):
        body = {"text": str(body_raw)}
    artifact = None
    if msg_row.get("mnemo_saved_at"):
        artifact = {
            "type": "mnemo-session",
            "session_id": msg_row.get("tracking_id"),
        }
    return {
        "id": msg_row.get("tracking_id") or f"bus-{msg_row.get('id')}",
        "name": msg_row.get("subject"),
        "input": body,
        "state": A2A_LIFECYCLE.get(lifecycle_state, lifecycle_state),
        "metadata": {
            "from": msg_row.get("from_agent") or msg_row.get("from"),
            "to": msg_row.get("to_agent"),
            "reply_to": msg_row.get("reply_to"),
            "created_at": msg_row.get("created_at"),
        },
        "artifact": artifact,
        "protocol": "sparks-bus-a2a",
    }


# ---------------------------------------------------------------------------
# Notification phases
# ---------------------------------------------------------------------------


def _embed_payload_for_standalone(body_raw) -> str:
    try:
        body = json.loads(body_raw) if isinstance(body_raw, str) else body_raw
    except (json.JSONDecodeError, TypeError):
        return str(body_raw)[:600]
    if isinstance(body, dict):
        text = body.get("text") or body.get("summary") or body.get("task")
        if text:
            return str(text)[:600]
        return json.dumps(body)[:600]
    return str(body)[:600]


def scan_new_and_notify(db: sqlite3.Connection, mnemo: MnemoClient, discord: DiscordClient, dispatch_channel: str) -> int:
    """Phase 1: save payload to Mnemo (full mode only) and post 📬/🔄."""
    rows = db.execute("""
        SELECT id, from_agent, to_agent, subject, body, created_at, reply_to,
               tracking_id, mnemo_saved_at, notified_at
        FROM messages
        WHERE mnemo_saved_at IS NULL OR notified_at IS NULL
        ORDER BY id ASC
    """).fetchall()

    count = 0
    for row in rows:
        msg_id, from_agent, to_agent, subject, body, created_at, reply_to, \
            tracking_id, mnemo_saved_at, notified_at = row
        is_reply = reply_to is not None

        if not tracking_id:
            tracking_id = tracking_id_for(msg_id, created_at, is_reply)
            db.execute("UPDATE messages SET tracking_id = ? WHERE id = ?", (tracking_id, msg_id))
            db.commit()

        if not mnemo_saved_at:
            ok = mnemo.save(tracking_id, from_agent, to_agent, subject, body, is_reply)
            if not ok:
                continue  # retry next cycle; don't lie about delivery
            stamp = "standalone" if not mnemo.available else None
            db.execute(
                "UPDATE messages SET mnemo_saved_at = COALESCE(?, datetime('now')) WHERE id = ?",
                (stamp, msg_id),
            )
            db.commit()

        if not notified_at:
            if is_reply:
                orig_row = db.execute(
                    "SELECT tracking_id FROM messages WHERE id = ?", (reply_to,)
                ).fetchone()
                orig_tracking = (orig_row[0] if orig_row else None) or f"bus-{reply_to}-unknown"
                content = (
                    f"🔄 [Bus] {from_agent} replied to {to_agent}\n"
                    f"Subject: {subject}\n"
                    f"Tracking: {tracking_id}\n"
                    f"Original: {orig_tracking}\n"
                    f"Status: LOOP CLOSED ✅"
                )
            else:
                content = (
                    f"📬 [Bus] {from_agent} → {to_agent}\n"
                    f"Subject: {subject}\n"
                    f"Tracking: {tracking_id}\n"
                    f"Status: DELIVERED — awaiting pickup"
                )
            if not mnemo.available:
                # standalone mode: payload travels with the notification
                content += f"\n\n— payload —\n{_embed_payload_for_standalone(body)}"

            ok, _ = discord.post(dispatch_channel, content)
            if ok:
                db.execute(
                    "UPDATE messages SET notified_at = datetime('now') WHERE id = ?",
                    (msg_id,),
                )
                db.commit()
                count += 1
    return count


def scan_pickups(db: sqlite3.Connection, discord: DiscordClient, dispatch_channel: str) -> int:
    """Phase 3: ✅ for any row freshly read. Skips delivery-failed rows."""
    rows = db.execute("""
        SELECT id, from_agent, to_agent, subject, tracking_id
        FROM messages
        WHERE read = 1
          AND pickup_notified_at IS NULL
          AND notified_at IS NOT NULL
          AND delivery_failed_at IS NULL
        ORDER BY id ASC
    """).fetchall()
    count = 0
    for msg_id, from_agent, to_agent, subject, tracking_id in rows:
        content = (
            f"✅ [Bus] {to_agent} picked up message from {from_agent}\n"
            f"Subject: {subject}\n"
            f"Tracking: {tracking_id or f'bus-{msg_id}'}\n"
            f"Status: PICKED UP"
        )
        ok, _ = discord.post(dispatch_channel, content)
        if ok:
            db.execute(
                "UPDATE messages SET pickup_notified_at = datetime('now') WHERE id = ?",
                (msg_id,),
            )
            db.commit()
            count += 1
    return count


def scan_stales(db: sqlite3.Connection, discord: DiscordClient, alerts_channel: str, stale_seconds: int) -> int:
    """Phase 4: ⚠️ for DELIVERED-too-long-ago messages."""
    rows = db.execute("""
        SELECT id, from_agent, to_agent, subject, tracking_id,
               CAST((julianday('now') - julianday(notified_at)) * 86400 AS INTEGER) AS age_sec
        FROM messages
        WHERE read = 0
          AND notified_at IS NOT NULL
          AND stale_notified_at IS NULL
          AND delivery_failed_at IS NULL
          AND (julianday('now') - julianday(notified_at)) * 86400 >= ?
        ORDER BY id ASC
    """, (stale_seconds,)).fetchall()
    count = 0
    for msg_id, from_agent, to_agent, subject, tracking_id, age_sec in rows:
        age_str = f"{age_sec // 60}m" if age_sec < 3600 else f"{age_sec // 3600}h"
        content = (
            f"⚠️ [Bus] STALE: {tracking_id or f'bus-{msg_id}'} delivered to {to_agent} "
            f"{age_str} ago, no pickup ACK\n"
            f"From: {from_agent}\n"
            f"Subject: {subject}"
        )
        ok, _ = discord.post(alerts_channel, content)
        if ok:
            db.execute(
                "UPDATE messages SET stale_notified_at = datetime('now') WHERE id = ?",
                (msg_id,),
            )
            db.commit()
            count += 1
    return count


def notify_delivery_failure(db: sqlite3.Connection, discord: DiscordClient, alerts_channel: str, msg: dict, agent: str, error_text: str) -> bool:
    tracking = msg.get("tracking_id")
    if not tracking:
        row = db.execute("SELECT tracking_id FROM messages WHERE id=?", (msg["id"],)).fetchone()
        tracking = (row[0] if row else None) or f"bus-{msg['id']}"
    content = (
        f"⚠️ [Bus] DELIVERY FAILED: {tracking}\n"
        f"From: {msg['from']}\n"
        f"To: {agent}\n"
        f"Subject: {msg['subject']}\n"
        f"Error: {error_text[:400]}\n"
        f"Status: undeliverable — message stays in DB, no retries until cleared"
    )
    posted, _ = discord.post(alerts_channel, content)
    db.execute(
        "UPDATE messages SET delivery_failed_at = datetime('now') WHERE id = ?",
        (msg["id"],),
    )
    db.commit()
    log.error(f"[{agent}] DELIVERY FAILED #{msg['id']} ({tracking}): {error_text}")
    return posted


# ---------------------------------------------------------------------------
# Agent wakers
# ---------------------------------------------------------------------------


def wake_cc(msg: dict) -> tuple[bool, str]:
    try:
        body = json.loads(msg["body"]) if isinstance(msg["body"], str) else msg["body"]
    except (json.JSONDecodeError, TypeError):
        body = {"text": str(msg["body"])}
    prompt = (
        "You are CC (Claude Code), responding to a bus message.\n"
        f"From: {msg['from']}\nSubject: {msg['subject']}\nTime: {msg['time']}\n"
        f"Body: {json.dumps(body, indent=2)}\n\n"
        "Handle this message. Be concise."
    )
    try:
        result = subprocess.run(
            ["claude", "--print", "--max-turns", "3", "--max-budget-usd", "2.00", prompt],
            capture_output=True, text=True, timeout=300, cwd=os.path.expanduser("~"),
        )
        if result.returncode != 0:
            return False, f"CC returncode={result.returncode}: {result.stderr.strip()[:200]}"
        return True, result.stdout.strip() or "(CC returned empty)"
    except subprocess.TimeoutExpired:
        return False, "CC timed out (max 300s)"
    except Exception as e:
        return False, f"CC error: {e}"


def wake_agent_zero(msg: dict, url: str) -> tuple[bool, str]:
    try:
        body = json.loads(msg["body"]) if isinstance(msg["body"], str) else msg["body"]
    except (json.JSONDecodeError, TypeError):
        body = {"text": str(msg["body"])}
    text = (
        f"[Sparks Bus] Message from {msg['from']}:\n"
        f"Subject: {msg['subject']}\n\n{json.dumps(body, indent=2)}\n\n"
        f"Handle this and reply via bus_send to {msg['from']}."
    )
    try:
        r = requests.post(f"{url}/api_message", json={"text": text}, timeout=30)
        if r.ok:
            return True, f"Delivered to Agent Zero at {url}"
        return False, f"Agent Zero HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"Agent Zero delivery error: {e}"


def wake_discord_agent(msg: dict, channel_name: str, discord: DiscordClient) -> tuple[bool, str]:
    try:
        body = json.loads(msg["body"]) if isinstance(msg["body"], str) else msg["body"]
    except (json.JSONDecodeError, TypeError):
        body = {"text": str(msg["body"])}
    summary = body.get("text") or body.get("summary") or body.get("task") or json.dumps(body)[:200]
    content = f"**[Bus]** Message from {msg['from']}: **{msg['subject']}**\n{summary}"
    if len(content) > 1900:
        content = content[:1900] + "..."
    return discord.post(channel_name, content)


def process_message(db: sqlite3.Connection, agents: dict, agent: str, msg: dict, discord: DiscordClient, alerts_channel: str) -> None:
    config = agents.get(agent)
    if not config:
        log.warning(f"Unknown agent: {agent}")
        return
    method = config["method"]
    log.info(f"[{agent}] Delivering: {msg['subject']} (from {msg['from']})")

    if method == "claude":
        ok, response = wake_cc(msg)
        if ok:
            mark_read(db, msg["id"])
            save_reply(
                db, "CC", msg["from"],
                f"re: {msg['subject']}",
                json.dumps({"status": "handled", "response": response[:2000]}),
                msg["id"],
            )
            log.info(f"[CC] Handled and replied: {msg['subject']}")
        else:
            notify_delivery_failure(db, discord, alerts_channel, msg, agent, response)

    elif method == "http":
        ok, result = wake_agent_zero(msg, config["url"])
        if ok:
            mark_read(db, msg["id"])
            log.info(f"[{agent}] {result}")
        else:
            notify_delivery_failure(db, discord, alerts_channel, msg, agent, result)

    elif method == "discord":
        ok, result = wake_discord_agent(msg, config["channel"], discord)
        if ok:
            mark_read(db, msg["id"])
            log.info(f"[{agent}] {result}")
        else:
            notify_delivery_failure(db, discord, alerts_channel, msg, agent, result)

    elif method == "queue":
        # Pull-mode agent (e.g., Opie via MCP). Watcher never marks read.
        log.info(f"[{agent}] Queued (pull-mode): {msg['subject']}")


def deliver_cycle(db: sqlite3.Connection, agents: dict, discord: DiscordClient, alerts_channel: str) -> int:
    count = 0
    for agent, config in agents.items():
        if config["method"] == "queue":
            continue
        for msg in get_unread(db, agent):
            try:
                process_message(db, agents, agent, msg, discord, alerts_channel)
                count += 1
            except Exception as e:
                log.error(f"[{agent}] Failed processing msg #{msg['id']}: {e}")
    return count


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------


def poll_cycle(db: sqlite3.Connection, mnemo: MnemoClient, discord: DiscordClient, cfg: dict) -> None:
    notified = scan_new_and_notify(db, mnemo, discord, cfg["discord"]["dispatch_channel"])
    delivered = deliver_cycle(db, cfg["agents"], discord, cfg["discord"]["alerts_channel"])
    picked_up = scan_pickups(db, discord, cfg["discord"]["dispatch_channel"])
    stale = scan_stales(db, discord, cfg["discord"]["alerts_channel"], cfg["stale_seconds"])
    if notified or delivered or picked_up or stale:
        log.info(
            f"cycle: notified={notified} delivered={delivered} "
            f"picked_up={picked_up} stale={stale}"
        )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [Sparks-Bus] %(message)s",
        handlers=[logging.StreamHandler()],
    )
    cfg = load_config()
    log.info(f"Config: {cfg['_config_source']}")
    log.info(f"DB: {cfg['db_path']}")

    discord = DiscordClient(
        token_file=cfg["discord"]["token_file"],
        channels_file=cfg["discord"]["channels_file"],
        post_timeout=cfg["discord"]["post_timeout_seconds"],
    )
    mnemo = MnemoClient.probe(cfg["mnemo"])
    mode = "FULL (Mnemo + Discord)" if mnemo.available else "STANDALONE (Discord only — payload in notifications)"
    log.info(f"Mode: {mode}")
    log.info(f"Mnemo: {cfg['mnemo']['url']}  reachable={mnemo.available}")

    if not Path(cfg["db_path"]).is_file() and not SCHEMA_SQL:
        log.error(
            f"DB at {cfg['db_path']} does not exist and schema.sql not found. "
            "Initialize the DB first (see README)."
        )
        return 1

    db = open_db(cfg["db_path"])
    interval = int(cfg["poll_interval_seconds"])
    log.info(f"Polling every {interval}s")
    try:
        while True:
            try:
                poll_cycle(db, mnemo, discord, cfg)
            except Exception as e:
                log.error(f"Poll cycle error: {e}")
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("Shutting down")
        return 0


if __name__ == "__main__":
    sys.exit(main())
