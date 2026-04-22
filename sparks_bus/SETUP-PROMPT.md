# Sparks Bus — Setup Prompt for an AI Agent

Copy everything between the fences below into your AI agent (Claude Code, OpenClaw, Codex, ChatGPT with code execution, etc.) and ask it to bootstrap a Sparks Bus deployment for you. The prompt is self-contained: architecture, schema, lifecycle, A2A mapping, and concrete next-step actions.

---

```
You are bootstrapping a Sparks Bus deployment for me. Sparks Bus is a multi-agent
message bus with delivery confirmation. The full doctrine in one line:

  Discord is the doorbell. Mnemo (or any semantic store) is the mailbox.
  The tracking ID is the receipt.

Discord never carries the payload (in full mode). Mnemo carries the package.
Discord notifications confirm: package was sent, package was picked up,
package's reply closed the loop, or something went wrong. The sender stops
having to ask "did you get that?" — the answer is in one channel.

═══════════════════════════════════════════════════════════════════════════════
ARCHITECTURE
═══════════════════════════════════════════════════════════════════════════════

  ┌────────────────┐   poll 30s    ┌────────────────┐
  │  bus.sqlite    │◄──────────────│  Bus Watcher   │
  │  (messages)    │               │  (daemon)      │
  └────────────────┘               └───┬────────┬───┘
                                       │        │
                            ┌──────────┘        └──────────┐
                            ▼                              ▼
                   ┌────────────────┐            ┌────────────────┐
                   │ Mnemo Cortex   │            │ Discord (Bot)  │
                   │ payload by     │            │ #dispatch ACKs │
                   │ tracking_id    │            │ #alerts        │
                   │ (FULL only)    │            │                │
                   └────────────────┘            └───────┬────────┘
                                                         │
                                          ┌──────┬───────┴──────┬──────┐
                                          ▼      ▼              ▼      ▼
                                       Agent A  Agent B      Agent C  Agent D
                                       claude   discord       http    queue

═══════════════════════════════════════════════════════════════════════════════
TWO MODES
═══════════════════════════════════════════════════════════════════════════════

The watcher detects whether Mnemo is reachable at startup and picks a mode:

  FULL        — Mnemo at the configured URL responds to GET /health.
                Each message's payload is POSTed to /writeback by tracking_id.
                Discord notifications carry just the receipt.

  STANDALONE  — Mnemo unreachable. Payload travels in the Discord notification
                itself (appended below the receipt). No semantic recall, but
                the delivery → notify → pickup → reply lifecycle still works.

Pick standalone if you don't yet have a semantic memory layer. Drop in Mnemo
later without changing any agent code.

═══════════════════════════════════════════════════════════════════════════════
SCHEMA (bus.sqlite)
═══════════════════════════════════════════════════════════════════════════════

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '{}',          -- JSON
    reply_to INTEGER REFERENCES messages(id),
    read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    read_at TEXT,
    -- Notification-loop state. NULL = step not done yet.
    tracking_id TEXT,            -- bus-{id}-{iso} or bus-reply-{id}-{iso}
    mnemo_saved_at TEXT,         -- 'standalone' sentinel in standalone mode
    notified_at TEXT,            -- 📬 / 🔄 in #dispatch
    pickup_notified_at TEXT,     -- ✅ in #dispatch
    stale_notified_at TEXT,      -- ⚠️ in #alerts
    delivery_failed_at TEXT      -- ⚠️ in #alerts; row excluded from retry
);

═══════════════════════════════════════════════════════════════════════════════
MESSAGE LIFECYCLE
═══════════════════════════════════════════════════════════════════════════════

  1. CREATED     bus_send inserts a row in bus.sqlite
  2. SAVED       (full mode) watcher POSTs payload to Mnemo by tracking_id
  3. DELIVERED   📬 in #dispatch — bus has it, mailbox stocked
  4. NOTIFIED    same step (DELIVERED *is* the notification)
  5. PICKED UP   ✅ in #dispatch — recipient has read it
  6. PROCESSING  recipient does work
  7. REPLIED     recipient calls bus_reply (new row, reply_to set)
  8. CLOSED      🔄 in #dispatch on the reply, references original tracking ID

Failure path → ⚠️ DELIVERY FAILED in #alerts, one shot, no retries until
delivery_failed_at is cleared. Stale path → ⚠️ STALE in #alerts after the
configured threshold (default 1h). No retry storms in either case.

═══════════════════════════════════════════════════════════════════════════════
A2A COMPATIBILITY
═══════════════════════════════════════════════════════════════════════════════

Sparks Bus speaks Google A2A's data shapes. Each agent has an Agent Card under
agent-cards/ with name, description, url, capabilities, protocol. Bus rows
map cleanly to A2A Tasks:

  tracking_id  → task.id
  subject      → task.name
  body         → task.input
  lifecycle    → task.state (CREATED/DELIVERED→submitted, PICKED UP→working,
                              REPLIED→completed, DELIVERY FAILED→failed)
  reply_to     → task.metadata.reply_to
  Mnemo session_id → task.artifact

Transport (HTTPS/JSON-RPC) is the v2 roadmap; the data shapes are correct now.

═══════════════════════════════════════════════════════════════════════════════
WHAT TO DO
═══════════════════════════════════════════════════════════════════════════════

1. Decide the mode I should run in:
   - Do I have Mnemo Cortex (or any service exposing GET /health and
     POST /writeback with {session_id, agent_id, summary, key_facts})
     reachable from where the watcher will run? If yes, FULL. If no,
     STANDALONE.

2. Plan the directory layout. Default: ~/sparks-bus/ on the host that runs
   the watcher. Inside it I want:

     sparks-bus-watcher.py       (the daemon)
     schema.sql                  (bus DB schema; idempotent)
     config.json                 (my config)
     discord-channels.json       (slug → Discord channel ID map)
     agent-cards/*.json          (one per agent in my deployment)
     hooks/bus-pending.sh        (optional: SessionStart hook for CC-style agents)
     systemd/sparks-bus-watcher.service  (optional)

3. Ask me for, or infer:
   - Discord bot token (and verify the bot has Send Messages in the dispatch
     and alerts channels of my server).
   - Discord channel IDs for at least: dispatch, alerts. More if multiple
     agents will be reached via Discord.
   - Agents in my fleet, and how each is woken:
       claude   = spawn `claude --print` with the body as the prompt
       http     = POST {text} to {url}/api_message  (Agent Zero shape)
       discord  = post to a channel; the agent listens there
       queue    = pull-mode; watcher only notifies, agent's MCP/SDK reads later

4. Write out config.json from my answers. Use ~/.sparks/bus.sqlite as the DB
   path unless I specify otherwise. Use ~/.sparks/discord-token (chmod 600)
   for the bot token. Default poll_interval_seconds=30, stale_seconds=3600.

5. Create one Agent Card per agent under agent-cards/ following this shape:

     {
       "name": "AgentName",
       "description": "one line of what this agent does",
       "url": "https://your-host/agents/agentname",
       "capabilities": ["skill1", "skill2"],
       "inputModes": ["text/plain", "application/json"],
       "outputModes": ["text/plain", "application/json"],
       "protocol": "sparks-bus-a2a",
       "delivery": {
         "method": "claude" | "http" | "discord" | "queue",
         "channel": "channel-slug-if-method-is-discord",
         "url": "endpoint-if-method-is-http",
         "notes": "anything that helps a future operator"
       }
     }

6. Initialize the DB:
     mkdir -p ~/.sparks
     sqlite3 ~/.sparks/bus.sqlite < schema.sql

7. Drop the systemd unit and enable it (or just run the watcher in a tmux
   for a one-off):
     cp systemd/sparks-bus-watcher.service ~/.config/systemd/user/
     systemctl --user daemon-reload
     systemctl --user enable --now sparks-bus-watcher
     journalctl --user -u sparks-bus-watcher -f

8. Verify mode from the first two log lines. Send a test message:
     sqlite3 ~/.sparks/bus.sqlite \
       "INSERT INTO messages (from_agent, to_agent, subject, body)
        VALUES ('You', 'CC', 'hello', '{\"text\":\"verifying\"}');"

   Within one poll cycle (≤30s) you should see 📬 in #dispatch. When the
   agent reads the row, ✅ follows.

═══════════════════════════════════════════════════════════════════════════════
GUARDRAILS
═══════════════════════════════════════════════════════════════════════════════

- Never bypass the watcher to post fake ACKs to #dispatch. The bus is the
  source of truth; the watcher is the only writer of lifecycle ACKs.
- Failed deliveries fire ONE alert; do not retry in a loop. To retry, clear
  delivery_failed_at. This is the Scream-on-Failure doctrine: alert, then
  go quiet so the operator can act without being deafened.
- Don't rename the lifecycle columns or change emoji conventions. Operators
  scan #dispatch by the pictograms.
- Add new wake methods (e.g., 'webhook', 'sqs') by extending process_message;
  do not bolt on parallel delivery code paths in different files.

═══════════════════════════════════════════════════════════════════════════════

Ready? Ask me what you need: my mode (full/standalone), my Discord setup,
my agents and how they wake. Then write the files and start the watcher.
```

---

## Notes for the human reading this

- This prompt is self-contained — paste it into a fresh AI session and answer the questions it asks.
- It assumes the AI can run shell commands or guide you through them.
- For an AI without code execution, replace step 6/7 with copy-paste-able snippets the AI gives you to run yourself.
- The prompt deliberately does not include the watcher source. The AI should fetch `sparks-bus-watcher.py` from this repo (or you paste it in) — keeping the prompt itself small and durable.
