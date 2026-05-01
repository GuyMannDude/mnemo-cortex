# Mnemo Cortex — Hermes Agent Integration

Give [Hermes Agent](https://github.com/NousResearch/hermes-agent) persistent
semantic memory that survives across sessions. Hermes already has its own
local memory (skills + FTS5 session search). Mnemo adds the cross-agent
shared layer — your Hermes instance can save memories that other agents
(another Hermes, a Claude Desktop user, an OpenClaw bot) read back.

> **Three lines:**
> Hermes remembers what one agent learned.
> Mnemo remembers what all your agents know.
> Hermes + Mnemo gives you both.

This integration uses Hermes's first-class MCP support (v0.12.0+) and the
Node stdio bridge that ships with `mnemo-cortex`. No custom Python, no
Hermes patching — config-only.

> **Verified working as of 2026-05-01** against `mnemo-cortex 2.6.5` and
> `hermes-agent v0.12.0`. End-to-end save+recall tested with
> `nvidia/nemotron-3-super-120b-a12b:free` via OpenRouter (recall worked
> first try) and Ollama-local `hermes3:8b` (save worked, recall flaky on
> 8B). agent_id privacy boundary verified at the Mnemo API level.

## What you need

- **Hermes Agent v0.12.0+** with Python 3.11 ([install guide](https://hermes-agent.nousresearch.com/docs/getting-started/quickstart))
- **Node.js 18+** (the bridge is a Node stdio MCP server)
- **mnemo-cortex 2.6.5+** running somewhere reachable

## Install

### 1. Get Mnemo Cortex running

If you don't have a Mnemo Cortex server yet:

```bash
pip install mnemo-cortex
mnemo-cortex init           # interactive — picks providers, writes ~/.config/agentb/agentb.yaml
mnemo-cortex start          # default port 50001
mnemo-cortex health         # verify it's up
```

> Already running it? Skip to step 2.

### 2. Install the bridge

The bridge is in this repo. Clone the repo (or use an existing checkout)
and `npm install` the bridge dependencies:

```bash
git clone https://github.com/GuyMannDude/mnemo-cortex.git
cd mnemo-cortex/integrations/openclaw-mcp
npm install
```

Two npm dependencies: `@modelcontextprotocol/sdk` and `zod`. No OpenClaw
runtime dependency — the folder name is historical.

> Note the absolute path to `server.js` — you'll need it in step 3.

### 3. Tell Hermes about it

Edit `~/.hermes/config.yaml` (Hermes creates this on first run; if it's
missing, just create it). Add an `mcp_servers` block:

```yaml
mcp_servers:
  mnemo:
    command: node
    args:
      - /absolute/path/to/mnemo-cortex/integrations/openclaw-mcp/server.js
    env:
      MNEMO_URL: http://localhost:50001
      MNEMO_AGENT_ID: hermes
      MNEMO_SHARE: separate
    timeout: 60
    connect_timeout: 30
```

Replace `/absolute/path/to/mnemo-cortex` with your real path. Pick any
`MNEMO_AGENT_ID` you like — memories are scoped to it.

A complete starter config is in [`config.yaml.example`](config.yaml.example).

### 4. Verify

```bash
hermes mcp test mnemo
```

You should see something like:

```
Testing 'mnemo'...
Transport: stdio → node
Auth: none
✓ Connected (555ms)
✓ Tools discovered: 12

  mnemo_recall       Recall memories from Mnemo Cortex for the current agent...
  mnemo_search       Search memories in Mnemo Cortex. By default, searches o...
  mnemo_save         Save a summary or key facts to Mnemo Cortex for future ...
  mnemo_share        Toggle cross-agent memory sharing for this session. Whe...
  wiki_search        Search the WikAI knowledge base — indexed project docs,...
  wiki_read          Read a specific WikAI page by path (relative to ~/wiki/...
  wiki_index         Get the WikAI index — lists all projects, entities, and...
  passport_get_user_context             ...
  passport_observe_behavior             ...
  passport_list_pending_observations    ...
  passport_promote_observation          ...
  passport_forget_or_override           ...
```

Restart Hermes (or just open a new conversation) and the tools are
available immediately.

## Tools you get

| Tool prefix | What it does | Required |
|---|---|---|
| `mnemo_*` (4 tools) | Save / recall / search / share memories | Always — Mnemo core |
| `passport_*` (5 tools) | Read and update the user's portable working-style profile | Always — Passport ships with Mnemo |
| `wiki_*` (3 tools) | Search and read your WikAI knowledge base | Only when `WIKI_DIR` env is set and points to a real dir |
| Brain + session lifecycle (5 tools) | `read_brain_file`, `write_brain_file`, `list_brain_files`, `opie_startup`, `session_end` | Only when `BRAIN_DIR` env is set |

So the minimum useful surface is **9 tools** (4 Mnemo + 5 Passport), and
**12 tools** if you have a wiki, **17 tools** if you have a brain *and* a
wiki.

## Choosing a model for Hermes

This integration is model-agnostic — Hermes picks the LLM, Mnemo provides
the memory. Three common configurations:

Hermes config uses three nested keys: `model.provider`, `model.base_url`,
`model.default`. API keys go in `~/.hermes/.env`.

### OpenRouter (verified working — recommended)

```bash
echo "OPENROUTER_API_KEY=sk-or-v1-..." >> ~/.hermes/.env
chmod 600 ~/.hermes/.env

hermes config set model.provider openrouter
hermes config set model.base_url https://openrouter.ai/api/v1
hermes config set model.default nvidia/nemotron-3-super-120b-a12b:free
```

`nvidia/nemotron-3-super-120b-a12b:free` is a free 120B Mixture-of-Experts
model with 12B active parameters — strong tool-use, good reasoning, and
zero cost. **This is the configuration verified to work end-to-end with
Mnemo's MCP tools as of 2026-05-01.** Other free options to try:
`meta-llama/llama-3.3-70b-instruct:free`, `google/gemma-3-27b-it:free`.

### Ollama (fully local, no key)

Hermes talks to Ollama's OpenAI-compatible API. **Hermes requires a model
with at least 64K context** — pulled models like `llama3.1:8b` (128K) work;
`qwen2.5:32b-instruct` (32K) is rejected at startup.

```bash
ollama serve &
ollama pull llama3.1:8b      # 128K context, tool-capable

# Hermes default base_url for "ollama" provider is localhost:11434/v1.
# Override only if Ollama runs elsewhere:
# export OLLAMA_BASE_URL=http://other-host:11434/v1

hermes config set model.provider custom
hermes config set model.base_url http://localhost:11434/v1
hermes config set model.default llama3.1:8b
```

Tool-use note (empirical, 2026-05-01): 8B-class local models are
**borderline** for multi-tool sequences. `hermes3:8b` saved a memory via
`mnemo_save` first try, but went mute on the recall turn. `llama3.1:8b`
emitted the tool call as plain text instead of invoking it. For reliable
recall flows, prefer 30B+ tool-trained models or use OpenRouter Nemotron
above. Save-only flows tolerate smaller models.

When OPENAI_API_KEY isn't set, some clients reject the empty-key
handshake — set `OPENAI_API_KEY=ollama` (any non-empty string) in
`~/.hermes/.env` if you see authentication errors against Ollama.

### Direct API keys (Anthropic, OpenAI, Gemini, NVIDIA NIM, …)

```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." >> ~/.hermes/.env
hermes config set model.provider anthropic
hermes config set model.default claude-haiku-4-5

# or
echo "OPENAI_API_KEY=sk-..." >> ~/.hermes/.env
hermes config set model.provider openai
hermes config set model.default gpt-4o-mini
```

See Hermes's full provider list at
<https://hermes-agent.nousresearch.com/docs/user-guide/configuration>.

## Memory hygiene

Hermes has its own built-in memory (skills, MEMORY.md, FTS5 session
search). Mnemo Cortex is *additive* — it doesn't replace Hermes's local
memory. The recommended setup:

- **Hermes local memory:** procedural (skills), recent context (FTS5
  search), user profile (Honcho)
- **Mnemo Cortex:** durable cross-session decisions, cross-agent shared
  knowledge, semantic recall over months of history

When you want Hermes to remember something across sessions or share it
with another agent, ask it to use `mnemo_save`. When you want to recall
something a different agent saved, ask it to use `mnemo_search` and
toggle `mnemo_share`.

## Cross-agent sharing

Default behavior (`MNEMO_SHARE: separate`) is privacy-first — your Hermes
instance only sees its own memories. To search across agents:

```
You: "What did Rocky save about the Hoffman Bedding incident?"
Hermes: [calls mnemo_share to enable cross-agent, then mnemo_search]
```

Or set `MNEMO_SHARE: always` in your config to skip the toggle. Set
`MNEMO_SHARE: never` to permanently lock the boundary.

## Curator interaction (Hermes v0.12.0+)

Hermes v0.12.0 added an autonomous "Curator" that grades, consolidates,
and prunes Hermes's local skill library on a 7-day cycle. The Curator
runs only against Hermes's own skills directory (`~/.hermes/skills/`) —
**it does not touch Mnemo's memory store.** The two systems are separate
files on disk. Mnemo memories are durable regardless of Curator activity.

If you write a skill that wraps a Mnemo MCP tool (e.g. a "/save-decision"
skill that calls `mnemo_save` with a specific format), the Curator may
grade or refactor that skill. The underlying `mnemo_save` call still
works — only your skill wrapper is at risk.

## Troubleshooting

### `hermes mcp test mnemo` says "✗ Connection failed"

Most common causes, in order:

1. **Mnemo isn't running.** Run `mnemo-cortex health` in another terminal
   — it should print a healthy JSON status. If it errors, start the
   server with `mnemo-cortex start`.
2. **Wrong path to `server.js`.** The path in `args:` must be absolute and
   point at the real file. Test with `node /your/path/server.js < /dev/null`
   — it should hang waiting for stdin (Ctrl+C to exit), not error.
3. **Bridge deps not installed.** `cd mnemo-cortex/integrations/openclaw-mcp
   && npm install`.
4. **Wrong `MNEMO_URL`.** If your Mnemo runs on a non-default port, update
   the env. Check with `curl http://localhost:50001/health`.

### Tools list shows "Tools discovered: 9" instead of 12

You don't have a wiki dir. The bridge gracefully skips wiki tools when
`WIKI_DIR` is unset or points at a missing dir. This is normal — just
means the wiki integration isn't active for you. Ignore the count.

### Hermes connects but no `mcp_mnemo_*` tools appear in conversation

Hermes auto-injects MCP tools into every conversation, but the discovery
runs at startup. If you edited `~/.hermes/config.yaml` while Hermes was
running, restart it (`hermes` exits with Ctrl+D, then start again).

## What's next

- **Skills**: write a Hermes skill that wraps `mnemo_save` for your
  preferred memory format (decisions, lessons, contacts, etc.). See
  `skills/software-development/hermes-agent-skill-authoring/SKILL.md` in
  the Hermes repo.
- **Multi-agent**: install Mnemo + this integration on a second Hermes
  instance with a different `MNEMO_AGENT_ID`. With `MNEMO_SHARE: always`
  on the searcher, the second instance can recall what the first saved.
- **Brain repo**: set `BRAIN_DIR` to a [mnemo-plan](https://github.com/GuyMannDude/mnemo-plan)
  brain checkout. You get five extra tools (`read_brain_file`,
  `write_brain_file`, `list_brain_files`, `opie_startup`, `session_end`)
  for cross-session lane discipline. See [THE-LANE-PROTOCOL.md](https://github.com/GuyMannDude/mnemo-cortex/blob/master/THE-LANE-PROTOCOL.md).

## Architecture (one diagram)

```
┌────────────┐                    ┌──────────────────┐
│   Hermes   │                    │  mnemo-cortex    │
│            │                    │  (HTTP API)      │
│  ~/.hermes │     stdio MCP      │  port 50001      │
│  /config   │◀─────────────────▶│                  │
│  .yaml     │  (Node bridge as   │  agentb.yaml     │
│            │   subprocess)      │  SQLite + FTS5   │
└────────────┘                    └──────────────────┘
       ▲                                  ▲
       │ (Hermes built-in memory)         │ (cross-agent shared memory)
       ▼                                  ▼
   ~/.hermes/                       ~/.agentb/
   memories/, skills/,              hot/, warm/, cold/,
   sessions/, state.db              passport/

Different files on disk. Different lifecycles. Both available to Hermes
in every conversation. Pick the right tool for the right kind of fact.
```
