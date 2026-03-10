# ⚡ Mnemo Cortex

> *Every AI agent has amnesia. Mnemo Cortex is the cure.*

**Drop-in memory superhero for AI agents.** Four endpoints. Any LLM. Total recall.

```
Your Agent                    Mnemo Cortex (port 50001)
    │                              │
    │── POST /context ────────────▶│  "What do you remember about Easter?"
    │◀── memory chunks ────────────│  L1 cache → L2 index → L3 scan
    │                              │
    │── POST /preflight ──────────▶│  "Check my draft response"
    │◀── PASS / ENRICH / WARN ─────│  "Wait. Take this with you."
    │                              │
    │── POST /writeback ──────────▶│  "Archive this session"
    │◀── confirmed ────────────────│  Indexed for future recall
```

## The Moves

- **PASS** — "You're good, go."
- **ENRICH** — "Wait. Take this with you." *(injects missing context)*
- **WARN** — "Hold up. Something's off." *(flags potential errors)*
- **BLOCK** — "No. Sit down." *(stops factual mistakes)*

## Why Mnemo Cortex?

AI agents forget everything between sessions. Mnemo Cortex gives them a brain that persists.

- **4 endpoints.** Context retrieval, preflight validation, session archiving, health check.
- **Any LLM.** Ollama (free/local), OpenAI, Anthropic, Google Gemini, OpenRouter.
- **Resilient.** Circuit-breaker fallback chains. If Ollama dies, it hot-swaps to your API backup.
- **Persona modes.** Strict for business, Creative for brainstorming, or build your own.
- **Multi-tenant.** Rocky's memories never leak into BW's. Filesystem-level isolation.
- **L1/L2/L3 cache hierarchy.** Pre-built bundles → semantic search → full scan.
- **Framework adapters.** OpenClaw hook, Agent Zero skill, or raw HTTP from anything.
- **Zero cloud lock-in.** Runs fully local with Ollama, or use any API provider.

## The Live Wire (`/ingest`) + The Watcher

This is the feature that changes everything. `mnemo-cortex watch` runs a lightweight daemon outside your agent that silently watches its session transcripts. The millisecond your agent replies, the watcher captures the user's prompt and the agent's response, strips any internal noise/metadata, and pipes it straight to `/ingest`. 

If Anthropic pulls the plug, if the server crashes, if the power goes out — every conversation up to the exact last letter is already on disk in your Cortex.

**Session Lifecycle:**
| Tier | Age | Storage | Search Speed | What Happens |
|------|-----|---------|-------------|--------------|
| **HOT** | Days 1-3 | Raw JSONL | Instant (keyword) | Every exchange, as it happens |
| **WARM** | Days 4-30 | Summarized + compressed | Fast (L2 semantic) | Auto-summarized, embedded, indexed |
| **COLD** | Day 30+ | Compressed archive | Slow (L3 scan) | Deep storage, still searchable |

No manual saves. No handoff scripts. No lost sessions. The watcher never sleeps.

## Quick Start

### Option 1: pip install (recommended)
```bash
pip install mnemo-cortex
mnemo-cortex init          # interactive wizard — pick providers, enter keys, done
mnemo-cortex start         # server starts in background
mnemo-cortex watch --backfill # captures TO Mnemo
mnemo-cortex refresh --watch # writes FROM Mnemo to workspace
```

### Option 2: Docker
```bash
docker run -p 50001:50001 ghcr.io/guymanndude/mnemo-cortex
```

### Option 3: From source
```bash
git clone https://github.com/GuyMannDude/mnemo-cortex.git
cd mnemo-cortex
pip install -e ".[dev]"
mnemo-cortex init
mnemo-cortex start
mnemo-cortex watch --backfill
mnemo-cortex refresh --watch
```

## CLI Commands

```bash
mnemo-cortex init      # Interactive setup wizard
mnemo-cortex start     # Start server (background)
mnemo-cortex start -f  # Start in foreground
mnemo-cortex stop      # Stop server
mnemo-cortex watch     # Start the session watcher (auto-capture)
mnemo-cortex unwatch   # Stop the session watcher
mnemo-cortex refresh   # Write MNEMO-CONTEXT.md to workspace
mnemo-cortex unrefresh # Stop the refresh daemon
mnemo-cortex status    # Health check + session stats + watcher status
mnemo-cortex logs      # View server logs
mnemo-cortex logs -f   # Follow logs live
mnemo-cortex test      # Quick connectivity test
```

## Configuration

```yaml
# Free local setup (requires Ollama)
reasoning:
  provider: ollama
  model: qwen2.5:32b-instruct
  api_base: http://localhost:11434
  fallbacks:
    - provider: openai
      model: gpt-4o-mini
      api_key: ${OPENAI_API_KEY}

embedding:
  provider: ollama
  model: nomic-embed-text
  api_base: http://localhost:11434
```

```yaml
# Cloud setup (works anywhere, no GPU needed)
reasoning:
  provider: openai
  model: gpt-4o-mini
  api_key: ${OPENAI_API_KEY}

embedding:
  provider: openai
  model: text-embedding-3-small
  api_key: ${OPENAI_API_KEY}
```

See [agentb.yaml.example](agentb.yaml.example) for all options including persona modes and multi-agent isolation.

## Persona Modes

Mnemo Cortex adapts to what your agent is doing:

| Mode | Preflight | Context | Use Case |
|------|-----------|---------|----------|
| **default** | Balanced | Neutral | General purpose |
| **strict** | Aggressive fact-checking | Factual bias | Business, finance, ops |
| **creative** | Permissive | Associative (wider net) | Brainstorming, art, prompts |

Pass `"persona": "creative"` in any request, or set it per-agent in config.

## Multi-Agent Isolation

```yaml
agents:
  rocky:
    data_dir: ~/.agentb/agents/rocky
    persona: creative
  bw:
    data_dir: ~/.agentb/agents/bw
    persona: strict
```

Each agent gets its own memory, cache, and index. No data leakage. Pass `"agent_id": "rocky"` in requests.

## Resilient Provider Fallbacks

```yaml
reasoning:
  provider: ollama
  model: qwen2.5:32b-instruct
  circuit_breaker_threshold: 3
  circuit_breaker_cooldown: 60
  fallbacks:
    - provider: openai
      model: gpt-4o-mini
      api_key: ${OPENAI_API_KEY}
    - provider: openrouter
      model: nousresearch/hermes-3-llama-3.1-405b:free
      api_key: ${OPENROUTER_API_KEY}
```

If Ollama hangs, Mnemo Cortex silently falls through the chain. The `/health` endpoint shows which provider is active.

## Framework Adapters

### OpenClaw (full integration)

> 💡 **New to OpenClaw?** Check out our [Paste-and-Go OpenClaw Setup Guide](docs/openclaw-setup-guide.md) to get your agent running in 10 minutes.

Two daemons that work together:

| Component | What it does | Run it via |
|-----------|-------------|------------|
| **Watcher Daemon** | Silently pushes Live Wire tape state into Cortex. | `mnemo-cortex watch` |
| **Refresh Daemon** | Writes `MNEMO-CONTEXT.md` to your workspace for context injection. | `mnemo-cortex refresh` |

Your agent now captures every exchange automatically and gets memory context injected. Zero manual effort, and absolutely zero OpenClaw hooks required.

### Other Frameworks

| Framework | Adapter | Setup |
|-----------|---------|-------|
| **Agent Zero** | Skill file | Copy `adapters/agent-zero/SKILL-AGENTB.md` to Agent Zero skills |
| **Any framework** | HTTP/curl | See `adapters/generic/INTEGRATION.md` |

## Supported Providers

| Provider | Reasoning | Embedding | Cost |
|----------|-----------|-----------|------|
| **Ollama** | ✅ Any model | ✅ nomic-embed-text | Free (local) |
| **OpenAI** | ✅ GPT-4o-mini, GPT-4o | ✅ text-embedding-3-small | ~$0.15/M tokens |
| **Anthropic** | ✅ Claude Sonnet/Haiku | ❌ | ~$0.25/M tokens |
| **OpenRouter** | ✅ Any model | ✅ Any model | Varies (free tier available) |
| **Google** | ✅ Gemini Flash/Pro | ✅ embedding-001 | Free tier available |
| **HuggingFace** | ❌ | ✅ Any model | Free (local) or API |

## Architecture

```
┌─────────────────────────────────────────────┐
│          ⚡ Mnemo Cortex Server             │
│   "I remember everything so your agent      │
│    doesn't have to."                        │
│                                             │
│  ┌────────────┐  ┌────────────┐            │
│  │  Reasoning  │  │  Embedding │  Pluggable │
│  │  + Fallback │  │  + Fallback│  Chains    │
│  └─────┬──────┘  └─────┬──────┘            │
│        │                │                    │
│  ┌─────┴────────────────┴──────┐            │
│  │      Cache Hierarchy        │            │
│  │  L1: Pre-built bundles      │  Fast      │
│  │  L2: Semantic index         │  ↓         │
│  │  L3: Full memory scan       │  Slow      │
│  └────────────┬────────────────┘            │
│               │                              │
│  ┌────────────┴────────────────┐            │
│  │   Multi-Tenant Storage      │            │
│  │  rocky/ │ bw/ │ shared/     │            │
│  └─────────────────────────────┘            │
└─────────────────────────────────────────────┘
                      ▲
                      │
               POST /ingest
                      │
      ┌───────────────┴───────────────┐
      │        Watcher Daemon         │
      │ Automatically reads sessions  │
      └───────────────▲───────────────┘
                      │
            ┌─────────┴─────────┐
            │ OpenClaw Storage  │
            │  ~/.openclaw/     │
            └───────────────────┘

```

## Testing

```bash
pip install -e ".[dev]"
PYTHONPATH=. pytest tests/ -v
```

56 tests covering circuit breaker, provider fallbacks, cache hierarchy, multi-tenant isolation, persona modes, session lifecycle, crash safety, and config loading.

## Contributing

We welcome contributions! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

Adding a new provider is as easy as implementing two methods (`generate`/`embed` + `health_check`) and adding it to the provider map. Framework adapters are even simpler — just a config file and some docs.

## Roadmap

- [x] v0.2.0 — Core server, pluggable providers, framework adapters
- [x] v0.3.0 — Multi-tenant isolation, circuit breaker fallbacks, persona modes
- [x] v0.4.0 — Live Wire (`/ingest`), hot/warm/cold session lifecycle, `/sessions` API, Session Watcher daemon
- [ ] v0.5.0 — `/metrics` (Prometheus), proactive session pre-caching
- [ ] v0.6.0 — SQLite + sqlite-vec storage backend, admin dashboard
- [ ] v1.0.0 — Postgres + pgvector, pip installable, production hardened

## Created By

Guy Hutchins, Rocky Moltman 🦞, Opie (Claude), and AL (ChatGPT 5.4) — built for [Project Sparks](https://projectsparks.ai).

*"I remember everything so your agent doesn't have to."*

## License

MIT
