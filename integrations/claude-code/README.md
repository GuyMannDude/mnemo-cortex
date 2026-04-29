# Mnemo Cortex — Claude Code Integration

Gives Claude Code persistent memory that survives across sessions. Every session starts with context from your previous work. Every session ends by archiving what happened. Total recall.

## What You Need

- **Mnemo Cortex** running somewhere (this machine or a remote server)
- **Claude Code** installed
- **curl** and **python3** on your PATH

## Choose Your Path

Two install paths, both work — pick one or run both:

- **Hooks** (explicit, on session boundaries) — `mnemo-startup.sh` runs at session start, `mnemo-writeback.sh` at session end. Setup: `bash install.sh`. Manual writeback at session end is required. Best for high-signal session summaries.
- **Sync service** (automatic, every 60s) — A systemd service POSTs your session activity to Mnemo continuously. Setup: copy a service template. Runs in the background. Best for unattended capture between manual saves.

If you're not sure, install **hooks** first — fewer moving parts. Add the **sync service** later when you want automatic continuous capture as a safety net. The full comparison table is in [Hooks vs Sync](#hooks-vs-sync) further down.

> ⚠️ The legacy `mnemo-watcher-cc.sh` is **deprecated** and should not be used in v2.6+ setups. It writes to a local SQLite that the central Mnemo Cortex API doesn't read. Migrate to the sync service if you were using it.

## Install

From the repo:

```bash
cd integrations/claude-code
bash install.sh
```

Or one-liner (downloads from GitHub):

```bash
curl -fsSL https://raw.githubusercontent.com/GuyMannDude/mnemo-cortex/master/integrations/claude-code/install.sh | bash
```

The installer will:
1. Ask where Mnemo Cortex is running
2. Ask for your agent ID
3. Install hook scripts to `~/.mnemo-cc/hooks/`
4. Add startup/writeback instructions to your `CLAUDE.md`
5. Test the connection

## How It Works

**Session start:** `mnemo-startup.sh` runs and pulls relevant context from Mnemo Cortex — recent activity, semantic matches for current priorities, and key decisions. This context is injected into Claude Code's prompt.

**Session end:** You tell Claude Code to run `mnemo-writeback.sh` with a summary of what happened. The summary is archived in Mnemo Cortex with your agent ID, indexed for semantic search, and available to future sessions.

```
Session 1                    Mnemo Cortex                    Session 2
┌──────────┐                ┌──────────┐                ┌──────────┐
│ CC works  │──writeback──▶│  memory   │──startup────▶│ CC knows  │
│ on task   │               │  stored   │               │ history   │
└──────────┘                └──────────┘                └──────────┘
```

## Model Tiers

Mnemo Cortex needs two models: one for embeddings (semantic search) and one for reasoning (summarization). Three options:

| Tier | Embedding | Reasoning | Cost | Setup |
|------|-----------|-----------|------|-------|
| **Local** | Ollama nomic-embed-text | Ollama qwen2.5:32b-instruct | $0 | GPU + Ollama |
| **Cloud (OpenAI)** | OpenAI text-embedding-3-small | OpenAI gpt-4.1-nano | ~$0.05/mo | API key |
| **Cloud (Google)** | Google gemini-embedding-001 | Google gemini-2.5-flash | ~$0.05/mo | API key |
| **Custom** | Any endpoint | Any endpoint | Varies | You configure |

> **Google users:** If you see `text-embedding-004` referenced anywhere, that model was shut down January 2026. Use `gemini-embedding-001` instead.

Model configuration is done in Mnemo Cortex itself (see main README), not in these hooks.

## Automatic Mode (Session Sync)

The hooks above require Claude Code to run the writeback manually at session end. For fully automatic ingestion, use the **session sync** — it reads Claude Code's JSONL session files and POSTs structured memories to Mnemo Cortex's `/writeback` endpoint every 60 seconds. Memories are immediately recallable across agents (no overnight summarization wait).

> 📦 **Upgrading from `mnemo-cc-artforge-sync.*`?** The scripts were renamed (the literal "artforge" was an internal hostname leak). The Python sync auto-migrates your offset file on first run. You'll need to re-create your systemd unit from the new `mnemo-cc-sync.service.template`; the old `mnemo-cc-artforge-sync.service` unit won't auto-rename.

### Setup

1. Make sure Mnemo Cortex is reachable from this machine. Quick check:
   ```bash
   curl -s "${MNEMO_URL:-http://localhost:50001}/health"
   ```

2. Test the sync manually (`--force` flushes regardless of message count):
   ```bash
   python3 integrations/claude-code/mnemo-cc-sync.py --force
   ```
   Expected output: `[cc-sync] posted N msgs → memory_id=<id>`.

3. Verify the memory landed:
   ```bash
   curl -s -X POST "${MNEMO_URL:-http://localhost:50001}/recall" \
     -H 'Content-Type: application/json' \
     -d '{"agent_id":"cc","query":"session activity"}'
   ```

### Configuration

All env-var, all optional:

| Variable | Default | Purpose |
|---|---|---|
| `MNEMO_URL` | `http://localhost:50001` | Mnemo Cortex base URL |
| `MNEMO_AGENT_ID` | `cc` | Agent ID for writebacks |
| `MNEMO_CC_SESSIONS_DIR` | `~/.claude/projects` | Where Claude Code stores `.jsonl` |
| `MNEMO_CC_OFFSET_FILE` | `~/.mnemo-cc/cc-sync.offset.json` | Sync offset state |
| `MNEMO_CC_SYNC_INTERVAL` | `60` | Seconds between syncs (loop only) |

### Install as a systemd service (Linux)

A template is provided at `mnemo-cc-sync.service.template`. Copy it into your user units dir, edit any env vars you need to override, then enable:

```bash
cp integrations/claude-code/mnemo-cc-sync.service.template \
   ~/.config/systemd/user/mnemo-cc-sync.service

# Optional: edit env vars
$EDITOR ~/.config/systemd/user/mnemo-cc-sync.service

systemctl --user daemon-reload
systemctl --user enable --now mnemo-cc-sync.service
systemctl --user status mnemo-cc-sync.service
```

The service runs `mnemo-cc-sync-loop.sh` which calls the Python sync every 60s and force-flushes on `SIGTERM` so nothing is stranded on shutdown.

### Health monitoring (recommended)

Silent failure modes are the worst kind — a sync that stops working without alerting wastes the entire memory-parity guarantee. The repo ships `mnemo-cc-sync-watchdog.sh` which exits non-zero when the service is down or the sync is stuck, so any cron / scheduler can alert.

**Plain cron:**
```cron
*/15 * * * * /path/to/mnemo-cortex/integrations/claude-code/mnemo-cc-sync-watchdog.sh || \
  /usr/bin/curl -fsS --data-urlencode "payload={\"text\":\"Mnemo CC sync watchdog FAIL on $(hostname)\"}" \
  "$YOUR_WEBHOOK_URL"
```

**healthchecks.io:**
```cron
*/15 * * * * /path/to/.../mnemo-cc-sync-watchdog.sh && curl -fsS https://hc-ping.com/<your-uuid>
```

**systemd OnFailure=** — wrap the watchdog in a oneshot unit + timer; OnFailure triggers your alert unit.

The watchdog exits OK when (a) the service is active and (b) either Claude Code is idle (no JSONL writes in 30 min) or the sync has posted within 30 min.

### Hooks vs Sync

| | Hooks (manual) | Sync (automatic) |
|---|---|---|
| **Ingestion** | On writeback only | Every 60s, real-time |
| **Trigger** | Claude Code calls writeback | systemd loop |
| **Format** | Summary + key facts | Structured memory with last 20 turns |
| **Setup** | `install.sh` | systemd service |
| **Overhead** | None between sessions | One Python invocation per minute |
| **Cross-agent visibility** | Summary at session end | Continuous |

You can run both — the sync captures continuously, and the hooks add high-signal summaries at session boundaries.

### Legacy: `mnemo-watcher-cc.sh` (deprecated)

The older `mnemo-watcher-cc.sh` writes raw messages to a local SQLite via the `mnemo-cortex-v2` codebase. **It does not feed the central Mnemo Cortex API**, so memories ingested by it are not recallable across agents in v2.6+. The script is preserved for users still running v2 setups but will be removed in a future release. Migrate to `mnemo-cc-sync` per the steps above.

## Files

```
~/.mnemo-cc/
├── env                          # MNEMO_URL and MNEMO_AGENT_ID
├── hooks/
│   ├── mnemo-startup.sh         # Runs at session start
│   └── mnemo-writeback.sh       # Runs at session end
└── queue/                       # Offline writeback queue (if server unreachable)
```

## Configuration

Edit `~/.mnemo-cc/env`:

```bash
MNEMO_URL="http://localhost:50001"    # Your server
MNEMO_AGENT_ID="cc"                   # Your agent ID
```

## Uninstall

```bash
rm -rf ~/.mnemo-cc
```

Then remove the `MNEMO CORTEX` block from your `CLAUDE.md`.

## Workflow

For day-to-day use patterns — when to recall, when to save, how to structure a brain file, common mistakes — see the [Session Guide](../../SESSION-GUIDE.md).

## Troubleshooting

**"Mnemo Cortex unreachable"** — The server isn't running or the URL is wrong. Check with:
```bash
curl http://localhost:50001/health
```

**No context on startup** — You haven't run any writeback yet. After your first session writeback, future startups will have context.

**Writeback fails** — Check the server logs. If the server is temporarily down, the writeback is saved to `~/.mnemo-cc/queue/` and can be replayed later.

## Next Step

**Read [THE-LANE-PROTOCOL.md](../../THE-LANE-PROTOCOL.md) to learn the session ritual that makes Mnemo actually work.**
