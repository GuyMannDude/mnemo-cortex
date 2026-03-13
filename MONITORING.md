# Monitoring & Health Checks

Mnemo Cortex ships with two tools for keeping your memory system healthy.

## Doctor — Full Diagnostic

One command, ten checks, clear output.

```bash
# Auto-detect URL from MNEMO_URL env var (or defaults to localhost:50001)
mnemo-cortex-doctor

# Explicit URL
mnemo-cortex-doctor http://your-server:50001

# Machine-readable JSON output (for automation)
mnemo-cortex-doctor --json
```

What it checks:

| # | Check | What It Verifies |
|---|-------|-----------------|
| 1 | Server reachability | Can we connect at all? |
| 2 | API status | Is the server reporting "ok"? |
| 3 | Reasoning model | Is the LLM loaded and responding? |
| 4 | Embedding model | Is the embedding model loaded? |
| 5 | Registered agents | Which agent IDs are configured? |
| 6 | Session statistics | Hot/warm/cold session counts |
| 7 | Circuit breaker | Any failovers or tripped circuits? |
| 7 | Live context query | Can we actually run a semantic search? (not just /health) |
| 8 | Live ingest test | Can we write to the live wire? |
| 9 | Port conflict | Is a zombie server intercepting requests? |
| 10 | Daemon status | Are the watcher and refresher running? |

Exit codes: `0` = all passed, `1` = failures detected, `2` = server unreachable.

Example output:

```
  ╔═══════════════════════════════════════════════╗
  ║  🧠 Mnemo Cortex Doctor                      ║
  ║  Comprehensive Health Diagnostic              ║
  ╚═══════════════════════════════════════════════╝

  Target: http://artforge:50001

  1. Server Reachability
  ✅ Server responding at http://artforge:50001

  2. API Status
  ✅ Status: ok — Version: 0.6.0

  3. Reasoning Model
  ✅ Reasoning: ollama/qwen2.5:32b-instruct — healthy

  4. Embedding Model
  ✅ Embedding: ollama/nomic-embed-text — healthy

  5. Registered Agents
  ✅ 3 agents registered: rocky,default,alice

  ...

  ═══════════════════════════════════════════════
  🟢 ALL CHECKS PASSED
  Mnemo Cortex is fully healthy.

  Server:  http://artforge:50001
  Version: 0.6.0
  Agents:  rocky,default,alice
  Sessions: 8 hot / 8 warm / 0 cold
  ═══════════════════════════════════════════════
```

## Health Check Script — For Monitoring

A minimal script that returns exit code 0 (healthy) or 1 (unhealthy).
Works with any monitoring tool.

```bash
# Run directly
./mnemo-health-check.sh http://your-server:50001

# With cron (check every 5 minutes, email on failure)
*/5 * * * * /path/to/mnemo-health-check.sh http://your-server:50001 || mail -s "Mnemo DOWN" you@email.com

# With CronAlarm (recommended — Discord/SMS/Telegram alerts)
*/5 * * * * source ~/.cronalarm/env && ~/scripts/cronalarm "Mnemo Health" /path/to/mnemo-health-check.sh http://your-server:50001
```

What it checks (lightweight — four checks only):

1. Server is reachable
2. Status is "ok"
3. Both models (reasoning + embedding) are healthy
4. A real recall query succeeds

This is intentionally simple. For full diagnostics, use `mnemo-cortex-doctor`.

## Alerting Options

The health check script works with whatever monitoring you already use:

**Option 1: CronAlarm (recommended)**
Our companion project for cron job monitoring. One install, Discord/SMS/Telegram
alerts on any failure. [github.com/projectsparks/cronalarm](https://github.com/projectsparks/cronalarm)

**Option 2: Plain cron + email**
```bash
*/5 * * * * /path/to/mnemo-health-check.sh || mail -s "Mnemo Cortex DOWN" you@email.com
```

**Option 3: Your existing monitoring**
Point Nagios, Prometheus, Datadog, Uptime Kuma, or any monitoring tool at either:
- The health check script (checks exit code)
- The `/health` endpoint directly (checks HTTP 200 + JSON status)

## The Zombie Server Problem

If your agent host (e.g., IGOR) accidentally starts a local mnemo-cortex server
while the real server runs on a different machine (e.g., THE VAULT), requests to
`localhost:50001` hit the wrong server — one with no models loaded. This causes
mysterious timeouts even though `/health` returns "ok."

The doctor command detects this automatically (Check #10: Port Conflict). If you
see a port conflict warning, kill the local process and make sure your agent's
config points to the correct remote server.
