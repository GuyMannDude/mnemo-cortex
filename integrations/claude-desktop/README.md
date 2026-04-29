# Mnemo Cortex — Claude Desktop Integration

Persistent memory for Claude Desktop on **Windows, macOS, and Linux**.

> 📨 **Submitted to Anthropic's [Connectors Directory](https://claude.com/connectors/directory)** (2026-04-27) — once approved, you'll be able to install Mnemo Cortex directly from **Claude Desktop → Settings → Extensions → Browse extensions** without leaving the app. Until then, use one of the install paths below.

## Two install paths

> 🦞 **Most users**: drag-and-drop a `.mcpb` bundle. No clone, no Node, no JSON editing.
>
> 🛠️ **Developers / multi-host operators**: edit `claude_desktop_config.json` by hand and point at a checked-out repo.

---

## 🦞 One-click install (recommended)

1. Download [`mnemo-cortex.mcpb`](mnemo-cortex.mcpb) from this folder *(or grab it from the [latest release](https://github.com/GuyMannDude/mnemo-cortex/releases))*.
2. Open **Claude Desktop → Settings → Extensions**.
3. Drag `mnemo-cortex.mcpb` into the window.
4. When prompted, fill in the three fields:
   - **Mnemo Cortex Server URL** — defaults to `http://localhost:50001`. If your server is on another machine, use its address (e.g., `http://10.0.0.65:50001`).
   - **Agent ID** — defaults to `claude-desktop`. Use something distinct per host (`claude-code`, `lmstudio`, etc.) so memories don't collide.
   - **Cross-Agent Sharing** — `separate` (default, your own memory only), `always` (read every agent), or `never` (never share, can't be toggled later).
5. Done. The bundle ships its own Node runtime and dependencies — Claude Desktop spawns it on the next chat.

You'll need a **Mnemo Cortex server** reachable from this machine. Spin one up locally with the [main install guide](../../README.md#install-guide), or point at an existing instance on your network.

---

## 🛠️ Manual install (developers)

Use this path if you want to develop the bridge, run a custom build, or hold the install at a checked-out commit.

**Prereqs:** Node.js 18+, a clone of this repo, and a reachable Mnemo Cortex server.

```bash
git clone https://github.com/GuyMannDude/mnemo-cortex.git
cd mnemo-cortex/integrations/openclaw-mcp && npm install
```

Open Claude Desktop's config file:

| Platform | Path |
|---|---|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

Or open it via **Settings → Developer → Edit Config**. Add a `mnemo-cortex` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "mnemo-cortex": {
      "command": "node",
      "args": ["/ABSOLUTE/PATH/TO/mnemo-cortex/integrations/openclaw-mcp/server.js"],
      "env": {
        "MNEMO_URL": "http://localhost:50001",
        "MNEMO_AGENT_ID": "claude-desktop",
        "MNEMO_SHARE": "separate"
      }
    }
  }
}
```

Quit Claude Desktop **completely** (tray icon → Quit; closing the window isn't enough), re-launch.

---

## Verify

In a new chat, ask Claude: **"Use mnemo_save to remember that I tested the bundle today."** Then a fresh chat: **"Use mnemo_recall to find what I told you to remember."**

If the recall returns your phrase, you're live. If you only see the model agreeing without invoking a tool, the MCP isn't wired — recheck the steps.

---

## Default tools (9)

| Group | Tools |
|---|---|
| Memory | `mnemo_recall`, `mnemo_search`, `mnemo_save`, `mnemo_share` |
| [Developer's Passport](../../passport/) | `passport_get_user_context`, `passport_observe_behavior`, `passport_list_pending_observations`, `passport_promote_observation`, `passport_forget_or_override` |

Eight more tools auto-enable when matching directories exist on your machine — see the [main README](../../README.md#use-with-any-local-llm) for details.

---

## Environment variables (manual install only)

| Variable | Default | Notes |
|---|---|---|
| `MNEMO_URL` | `http://localhost:50001` | Where your Mnemo Cortex server is listening. |
| `MNEMO_AGENT_ID` | `openclaw` | Distinct per host so memories don't collide. |
| `MNEMO_SHARE` | `separate` | `separate`/`always`/`never`. |
| `BRAIN_DIR` | unset | Optional — auto-enables brain-file tools when set to an existing directory. Use a fork of [mnemo-plan](https://github.com/GuyMannDude/mnemo-plan) for project-context files. |
| `WIKI_DIR` | `~/wiki` | Optional — auto-enables wiki tools when present. |

---

## Privacy

The bundle connects to **one server only** — the Mnemo Cortex URL you provide at install time. No telemetry, no analytics, no third-party services.

- Run Mnemo Cortex locally → your data never leaves your machine.
- Run it on your home network → your data stays on your hardware.
- Point at a third-party-hosted instance → only if you explicitly choose to.

Memory entries are stored in a SQLite DB on your Mnemo Cortex server. You can read or delete them at any time. Full details: [PRIVACY.md](../../PRIVACY.md).

## Workflow

For day-to-day use patterns — when to recall, when to save, how to structure a brain file, common mistakes — see the [Session Guide](../../SESSION-GUIDE.md).

## Troubleshooting

**Tools don't appear** — Quit Claude Desktop completely (tray icon → Quit, not just close the window) and re-launch. The MCP only spawns at app start.

**Bridge crashed silently** — Bridge versions ≥2.6.4 log uncaught exceptions, unhandled rejections, signal exits, and stdin EOF to stderr (visible in Claude Desktop's MCP log: `mcp-server-mnemo-cortex.log`). If the bridge dies and the log is empty, you're on an older version — update via the manual install path or the `.mcpb` bundle.

**"Mnemo Cortex unreachable"** — Server isn't running or the URL is wrong. Test from a terminal:
```bash
curl http://localhost:50001/health
```

**Manifest parse error on bundle install** — Make sure you're on a recent Claude Desktop (>=1.0). Older builds may not understand `manifest_version: 0.3`.

**Manual install: Node not found** — Node.js 18+ must be on `PATH`. On Windows, you may need the full path to `node.exe` in the `command` field.

## Next Step

**Read [THE-LANE-PROTOCOL.md](../../THE-LANE-PROTOCOL.md) to learn the session ritual that makes Mnemo actually work.**

---

*Part of [Mnemo Cortex](https://github.com/GuyMannDude/mnemo-cortex) by [Project Sparks](https://projectsparks.ai).*
