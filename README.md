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

## The Live Wire (`/ingest`)

This is the feature that changes everything. Rocky calls `/ingest` after every single exchange with you. If Anthropic pulls the plug, if the server crashes, if the power goes out — every conversation up to the last exchange is already on disk.

```bash
# Rocky calls this after every prompt/response pair
curl -X POST http://localhost:50001/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "What was our Easter pricing?",
    "response": "Easter gnomes were $20-30 each",
    "agent_id": "rocky"
  }'
# Response: {"status": "captured", "session_id": "2026-03-05_143022_a1b2c3", "entry_number": 7}
```

**Session Lifecycle:**
| Tier | Age | Storage | Search Speed | What Happens |
|------|-----|---------|-------------|--------------|
| **HOT** | Days 1-3 | Raw JSONL | Instant (keyword) | Every exchange, as it happens |
| **WARM** | Days 4-30 | Summarized + compressed | Fast (L2 semantic) | Auto-summarized, embedded, indexed |
| **COLD** | Day 30+ | Compressed archive | Slow (L3 scan) | Deep storage, still searchable |

No manual saves. No handoff scripts. No lost sessions. The superhero never sleeps.

## Quick Start

### Option 1: Install Script
```bash
curl -fsSL https://raw.githubusercontent.com/GuyMannDude/mnemo-cortex/main/install.sh | bash
nano ~/.config/agentb/agentb.yaml   # pick your providers
agentb                               # start the superhero
```

### Option 2: Docker
```bash
git clone https://github.com/GuyMannDude/mnemo-cortex.git
cd mnemo-cortex
cp agentb.yaml.example agentb.yaml
docker compose up -d
```

### Option 3: Manual
```bash
git clone https://github.com/GuyMannDude/mnemo-cortex.git
cd mnemo-cortex
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp agentb.yaml.example agentb.yaml
python -m agentb.server
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

| Framework | Adapter | Setup |
|-----------|---------|-------|
| **OpenClaw** | Bootstrap hook | `cp -r adapters/openclaw/agentb-context ~/.openclaw/workspace/hooks/` |
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
```

## Testing

```bash
pip install -r requirements.txt
PYTHONPATH=. pytest tests/ -v
```

44 tests covering circuit breaker, provider fallbacks, cache hierarchy, multi-tenant isolation, persona modes, and config loading.

## Roadmap

- [x] v0.2.0 — Core server, pluggable providers, framework adapters
- [x] v0.3.0 — Multi-tenant isolation, circuit breaker fallbacks, persona modes
- [x] v0.4.0 — Live Wire (`/ingest`), hot/warm/cold session lifecycle, `/sessions` API
- [ ] v0.5.0 — `/metrics` (Prometheus), proactive session pre-caching
- [ ] v0.6.0 — SQLite + sqlite-vec storage backend, admin dashboard
- [ ] v1.0.0 — Postgres + pgvector, pip installable, production hardened

## Created By

Guy Hoffman, Rocky Moltman 🦞, and Opie (Claude) — built for [Project Sparks](https://projectsparks.ai).

*"I remember everything so your agent doesn't have to."*

## License

MIT
