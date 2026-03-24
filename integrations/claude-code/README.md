# Mnemo Cortex — Claude Code Integration

Gives Claude Code persistent memory that survives across sessions. Every session starts with context from your previous work. Every session ends by archiving what happened. Total recall.

## What You Need

- **Mnemo Cortex** running somewhere (this machine or a remote server)
- **Claude Code** installed
- **curl** and **python3** on your PATH

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
| **Cloud** | OpenAI text-embedding-3-small | OpenAI gpt-4o-mini | ~$0.10/mo | API key |
| **Custom** | Any endpoint | Any endpoint | Varies | You configure |

Model configuration is done in Mnemo Cortex itself (see main README), not in these hooks.

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

## Troubleshooting

**"Mnemo Cortex unreachable"** — The server isn't running or the URL is wrong. Check with:
```bash
curl http://localhost:50001/health
```

**No context on startup** — You haven't run any writeback yet. After your first session writeback, future startups will have context.

**Writeback fails** — Check the server logs. If the server is temporarily down, the writeback is saved to `~/.mnemo-cc/queue/` and can be replayed later.
