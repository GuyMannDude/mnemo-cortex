# Sparks Bus

A multi-agent message bus with delivery confirmation. Discord is the doorbell, Mnemo is the mailbox, the tracking ID is the receipt.

Every message produces a visible lifecycle in your Discord:

```
📬 DELIVERED        → bus saved the payload, posted the receipt
✅ PICKED UP        → recipient agent read the message
🔄 LOOP CLOSED     → recipient replied; sender's task is done
⚠️ DELIVERY FAILED  → wake-up failed; one alert, no retries
⚠️ STALE            → DELIVERED but un-ACKd for too long
```

You stop asking agents "did you get that?" — you watch one channel and see every package move.

## Two install modes

The watcher detects whether Mnemo Cortex is reachable at startup and picks a mode:

| Mode | Mnemo? | What carries the payload? | What you lose without Mnemo |
|---|---|---|---|
| **Full** | yes | Mnemo (recallable by tracking ID) | nothing — full doctrine |
| **Standalone** | no | Discord notification body | semantic recall and cross-agent memory |

In both modes the delivery → notify → pickup → reply lifecycle works identically. Standalone is the on-ramp; you can drop in Mnemo later without changing any agent code.

## Architecture

```
                   ┌──────────────────┐
                   │   Sparks Bus     │
                   │   (sqlite WAL)   │
                   └────────┬─────────┘
                            │ poll every 30s
                            ▼
                   ┌──────────────────┐
                   │   Bus Watcher    │
                   │  (this daemon)   │
                   └────┬─────────┬───┘
                        │         │
              ┌─────────┘         └─────────┐
              ▼                             ▼
   ┌────────────────────┐         ┌────────────────────┐
   │   Mnemo Cortex     │         │   Discord (Bot)    │
   │   payload by       │         │   #dispatch + ACKs │
   │   tracking_id      │         │   #alerts          │
   │   (FULL mode only) │         │                    │
   └────────────────────┘         └────────┬───────────┘
                                           │
                              ┌────────────┼────────────┐
                              ▼            ▼            ▼
                          [Agent A]    [Agent B]    [Agent C]
                          (claude)     (discord)    (http/queue)
```

## Message lifecycle

```
1. CREATED     bus_send inserts a row in bus.sqlite
2. SAVED       (full mode) watcher POSTs payload to Mnemo by tracking_id
3. DELIVERED   📬 in #dispatch — bus knows about it, mailbox is stocked
4. NOTIFIED    same step — DELIVERED *is* the notification
5. PICKED UP   ✅ in #dispatch — recipient agent has read it
6. PROCESSING  recipient does work
7. REPLIED     recipient calls bus_reply (a new row, reply_to set)
8. CLOSED      🔄 in #dispatch on the reply, references original tracking ID
```

Failure paths produce a one-shot ⚠️ in `#alerts` with the diagnostic, then quiet down — no retry storms.

## Prerequisites

- Python 3.10+ (uses PEP 604 union syntax)
- SQLite (CLI for init only; the watcher uses the stdlib)
- `pip install -r requirements.txt` — only `requests`
- A Discord bot token with permission to post in your server's `#dispatch` and `#alerts` channels
- (Full mode only) [Mnemo Cortex](https://github.com/GuyMannDude/mnemo-cortex) reachable over HTTP. Any service that exposes `GET /health` and `POST /writeback` with the same shape works as a substitute.

## Step-by-step setup

1. **Get the code.** Either install mnemo-cortex (`pip install mnemo-cortex`) and use `sparks_bus/` from the package, or just copy the directory anywhere on disk and run from there. The watcher has zero hard dependency on the rest of mnemo-cortex.

2. **Initialize the bus database.** The watcher applies `schema.sql` automatically on every startup, so you can skip this step — but if you want the DB ready before the first run:

   ```bash
   mkdir -p ~/.sparks
   sqlite3 ~/.sparks/bus.sqlite < schema.sql
   ```

3. **Drop in the Discord bot token.**

   ```bash
   echo "YOUR_BOT_TOKEN" > ~/.sparks/discord-token
   chmod 600 ~/.sparks/discord-token
   ```

4. **Map your channels.** Copy `discord-channels.example.json` to `discord-channels.json` and fill in your guild's channel IDs (right-click channel → Copy Channel ID with developer mode on).

5. **Configure agents.** Copy `config.example.json` to `config.json` and edit the `agents` block. Each agent picks a wake method:
   - `claude` — spawn `claude --print` with the message body as the prompt
   - `http` — POST to an Agent Zero–style API (set `url`)
   - `discord` — post to a channel (set `channel`); the agent must be listening there
   - `queue` — pull mode; the watcher just notifies and waits for the agent's MCP/SDK to read the row

6. **Start the watcher.** For a one-off:

   ```bash
   python3 sparks-bus-watcher.py
   ```

   For systemd (user-level):

   ```bash
   cp systemd/sparks-bus-watcher.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now sparks-bus-watcher
   journalctl --user -u sparks-bus-watcher -f
   ```

7. **Verify mode.** First two log lines tell you everything:

   ```
   Mode: FULL (Mnemo + Discord)
   Mnemo: http://localhost:50001  reachable=True
   ```

   …or, if Mnemo is unreachable:

   ```
   Mode: STANDALONE (Discord only — payload in notifications)
   Mnemo: http://localhost:50001  reachable=False
   ```

8. **Send your first message.** From any shell on the same host:

   ```bash
   sqlite3 ~/.sparks/bus.sqlite \
     "INSERT INTO messages (from_agent, to_agent, subject, body)
      VALUES ('You', 'CC', 'hello', '{\"text\":\"verifying the bus\"}');"
   ```

   Within one poll cycle you should see 📬 in `#dispatch`. Once the agent reads the row, ✅ follows. Reply with `reply_to` set and you'll get 🔄.

## Configuration reference

`config.json` keys (every key can also be set by the matching `BUS_*` env var — see `.env.example`):

| Key | Env | Default | Notes |
|---|---|---|---|
| `db_path` | `BUS_DB_PATH` | `~/.sparks/bus.sqlite` | sqlite file; created if missing |
| `poll_interval_seconds` | `BUS_POLL_INTERVAL_SECONDS` | `30` | Lower = faster ACKs, more CPU |
| `stale_seconds` | `BUS_STALE_SECONDS` | `3600` | DELIVERED-too-long threshold |
| `mnemo.url` | `BUS_MNEMO_URL` | `http://localhost:50001` | Probed at startup; unreachable = standalone mode |
| `mnemo.agent_id` | `BUS_MNEMO_AGENT_ID` | `bus` | Tenant ID for Mnemo writeback |
| `mnemo.writeback_endpoint` | — | `/writeback` | Override only if your backend uses a different path |
| `discord.token_file` | `BUS_DISCORD_TOKEN_FILE` | `~/.sparks/discord-token` | One line: the bot token |
| `discord.channels_file` | `BUS_DISCORD_CHANNELS_FILE` | `./discord-channels.json` | Slug → ID map |
| `discord.dispatch_channel` | `BUS_DISPATCH_CHANNEL` | `dispatch` | Where 📬 / ✅ / 🔄 land |
| `discord.alerts_channel` | `BUS_ALERTS_CHANNEL` | `alerts` | Where ⚠️ lands |

Schema changes are non-destructive — `schema.sql` uses `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS`, so you can run the watcher against an existing bus DB.

## CC / Claude Code session-start hook

Drop `hooks/bus-pending.sh` into a Claude Code SessionStart hook so unread messages surface at the top of every session:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear",
        "hooks": [{ "type": "command", "command": "/path/to/sparks_bus/hooks/bus-pending.sh" }]
      }
    ]
  }
}
```

Override the agent ID with `BUS_AGENT=YourName` if the hook should report for a different name.

## A2A compatibility

Sparks Bus speaks the [Google A2A](https://google.github.io/A2A/) protocol's data shapes today; full transport is on the v2 roadmap.

| Sparks Bus | A2A | Notes |
|---|---|---|
| `tracking_id` | `task.id` | Globally unique receipt |
| `subject` | `task.name` | |
| `body` | `task.input` | JSON or text |
| lifecycle (CREATED→DELIVERED→PICKED UP→REPLIED) | `task.state` (submitted→working→completed) | See `A2A.md` |
| Mnemo `session_id` | `task.artifact` | Same value as tracking_id |
| `reply_to` | `task.metadata.reply_to` | |

Each agent has a card in `agent-cards/` describing identity, capabilities, and delivery method. These follow the [A2A Agent Card](https://google.github.io/A2A/specification/agent-card/) shape so external A2A clients can discover what each agent does.

What's *not* yet here: HTTPS/JSON-RPC transport endpoints, registration with external A2A directories, capability negotiation. Those land when Sparks Bus v2 ships an external transport. Until then, agents inside one Sparks deployment know each other through `config.json` and the bus DB.

See `A2A.md` for the full mapping reference.

## Operations notes

- **Failed deliveries** are one-shot: ⚠️ in `#alerts`, then `delivery_failed_at` is stamped and that row is excluded from retries, pickup ACKs, and stale alerts. Operator clears the column to retry: `UPDATE messages SET delivery_failed_at=NULL WHERE id=?;`
- **Backlog recovery** is automatic. Stop the watcher, queue messages, restart — every backlogged row is processed on the first poll cycle.
- **Schema changes** historically: the bus DB is upgraded by `ALTER TABLE ADD COLUMN` only. New columns default to `NULL`, treated as "step not yet done" by the scans. To upgrade an existing deployment to a new column set, just restart the watcher with the new `schema.sql`.
- **Backfill before upgrading.** If your bus has historical rows that should *not* trigger retroactive notifications, mark them as already-handled before the first run with the new code:
  ```sql
  UPDATE messages SET notified_at = created_at, mnemo_saved_at = 'backfilled',
                      pickup_notified_at = COALESCE(read_at, created_at),
                      stale_notified_at = created_at
   WHERE notified_at IS NULL;
  ```

## License

MIT (inherits from mnemo-cortex).
