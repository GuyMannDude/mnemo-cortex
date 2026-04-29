# Mnemo Cortex — Agent Zero Integration

Persistent cross-session, cross-agent memory for Agent Zero containers. Whatever your Agent Zero instance is doing — research, file courier, code execution, autonomous loops — it remembers across restarts and shares memory with your other agents.

Agent Zero ships with native MCP client support via its `mcp_servers` setting. We use that path: the bridge runs *inside* the Agent Zero container, agent zero invokes it as an MCP tool when the LLM decides to.

## Prerequisites

- **Agent Zero** running in Docker (image `agent0ai/agent-zero` or compatible)
- **A running Mnemo Cortex server** — see the [main install guide](../../README.md)
- **The Agent Zero container can reach `MNEMO_URL`** — easy if Mnemo is on the same host (use `host.docker.internal:50001`); over Tailscale if Mnemo is remote (Tailscale on the *host* extends to containers via host networking on Windows/Mac, on Linux you may need explicit DNS or `--add-host`)
- **A tool-capable LLM** chosen for the agent. Qwen3 (any size), GPT-4/4o, Claude 3.5+, and Mistral 7B v0.3 are confirmed tool-callers. See [Gotchas](#gotchas).

## Install

### 1. Get the bridge code into the container

You have two options. Pick one:

**A. Clone inside the container (simplest):**
```bash
docker exec <container-name> bash -c "
  cd /a0/usr &&
  git clone https://github.com/GuyMannDude/mnemo-cortex.git &&
  cd mnemo-cortex/integrations/openclaw-mcp &&
  npm install
"
```

**B. Mount the host clone into the container:**
Recreate the container with `-v /host/path/to/mnemo-cortex:/a0/usr/mnemo-cortex:ro`. Saves disk if you run multiple Agent Zero instances on one host.

Either way, you end up with `/a0/usr/mnemo-cortex/integrations/openclaw-mcp/server.js` reachable from inside the container.

### 2. Wire Mnemo MCP into Agent Zero's settings

Agent Zero stores config at `/a0/usr/settings.json` inside the container. Find the `mcp_servers` field (it's a JSON-encoded string) and add the `mnemo-cortex` entry:

```json
{
  "mcp_servers": "{\"mcpServers\": {\"mnemo-cortex\": {\"command\": \"node\", \"args\": [\"/a0/usr/mnemo-cortex/integrations/openclaw-mcp/server.js\"], \"env\": {\"MNEMO_URL\": \"http://YOUR_MNEMO_HOST:50001\", \"MNEMO_AGENT_ID\": \"your-agent-name\", \"MNEMO_SHARE\": \"separate\"}}}}"
}
```

> Note the double-encoding: `mcp_servers` is a *string* containing JSON, not a nested object. That's how Agent Zero stores it.

A clean Python patch script that handles this without escape-hell:

```python
# patch-settings.py — run inside the container or as a one-shot
import json
p = "/a0/usr/settings.json"
d = json.load(open(p))
d["mcp_servers"] = json.dumps({
  "mcpServers": {
    "mnemo-cortex": {
      "command": "node",
      "args": ["/a0/usr/mnemo-cortex/integrations/openclaw-mcp/server.js"],
      "env": {
        "MNEMO_URL": "http://YOUR_MNEMO_HOST:50001",
        "MNEMO_AGENT_ID": "your-agent-name",
        "MNEMO_SHARE": "separate"
      }
    }
  }
})
d.setdefault("mcp_client_init_timeout", 10)
d.setdefault("mcp_client_tool_timeout", 120)
json.dump(d, open(p, "w"), indent=2)
print("patched")
```

Replace `YOUR_MNEMO_HOST` with where your Mnemo server is reachable from inside the container (often `host.docker.internal` on Mac/Windows, the Tailscale hostname for remote, or the explicit IP). Replace `your-agent-name` with a unique ID per agent (`bw`, `cliff`, `research-bot`, etc.).

### 3. Restart the container so Agent Zero reloads settings

```bash
docker restart <container-name>
```

Agent Zero reads `settings.json` at startup. Settings changes do NOT hot-reload reliably — restart is the safe path.

## Verify

Once the container is back up, run a smoke test of the bridge from inside:

```bash
docker exec <container-name> bash -c "
  cd /a0/usr/mnemo-cortex/integrations/openclaw-mcp &&
  ( echo '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-11-25\",\"capabilities\":{},\"clientInfo\":{\"name\":\"test\",\"version\":\"1\"}}}'
    sleep 1
    echo '{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\",\"params\":{}}'
    sleep 2 ) | MNEMO_URL=http://YOUR_MNEMO_HOST:50001 MNEMO_AGENT_ID=your-agent-name node server.js 2>&1 | head -10
"
```

Expected output: `[mnemo-mcp] Connected to Mnemo Cortex (N memories, share: separate)` followed by an MCP tools listing. If you see "Connected" you're good.

For a full round-trip test: open the Agent Zero UI (port 80 inside the container, mapped to whatever host port you assigned), give the agent a task that involves remembering something, and ask it to recall in a fresh session. If it finds the memory, you're wired.

## Gotchas

### 1. Tool-capable model required

Same warning as every Mnemo integration: **non-tool-capable models will narrate fake save IDs without actually calling the tool.** Confirmed working: Qwen3 (any size), GPT-4/4o, Claude 3.5+, Mistral 7B Instruct v0.3, Nemotron-120B-free. Avoid older Llama 3.1 variants for memory tasks.

### 2. Network reachability into the container

`MNEMO_URL=http://localhost:50001` from inside an Agent Zero container points at the *container*'s localhost, not your host. Use:

- **Mnemo on the same host (Mac/Windows Docker):** `http://host.docker.internal:50001`
- **Mnemo on the same host (Linux Docker):** add `--add-host=host.docker.internal:host-gateway` at container create, or use the host's LAN IP
- **Mnemo elsewhere via Tailscale:** install Tailscale on the *host*; on Mac/Windows the container inherits Tailnet routing, on Linux you may need explicit DNS or routing
- **Mnemo on the public internet:** use the public hostname; auth is on you

Verify before fighting Agent Zero config:
```bash
docker exec <container> curl -s http://YOUR_MNEMO_HOST:50001/health
```

### 3. `mcp_servers` is a string, not an object

Agent Zero's settings.json stores `mcp_servers` as a JSON-encoded *string* containing the full server config. Not a nested object. Trips a lot of people on first edit. The Python patch script above handles it correctly.

### 4. Multiple Agent Zero containers on one host

Each container is its own Mnemo agent. Pick distinct `MNEMO_AGENT_ID` values per container so memories don't collide. Example pattern (Project Sparks naming): `bw` for the research agent, `cliff` for the file courier, `cleo` for the code-runner — each gets its own memory lane in Mnemo.

### 5. Don't confuse Agent Zero's native memory with Mnemo

Agent Zero has its own embedding-based memory at `/a0/tmp/memory/` using HuggingFace sentence-transformers or Ollama embeddings (whatever you've configured). That's *separate* from Mnemo. Adding Mnemo MCP doesn't replace Agent Zero's local memory — they coexist. Local memory is fast and private to that agent. Mnemo is cross-session, cross-agent. Use both.

## Sharing & Privacy

| Mode | `MNEMO_SHARE=` | Behavior |
|---|---|---|
| **Separate** (default) | `separate` | Each agent sees only its own Mnemo memories |
| **Always** | `always` | Cross-agent search always on |
| **Never** | `never` | Cross-agent search permanently off |

Use `always` if you have a trusted team of agents that should learn from each other (research bot reads what code bot shipped, etc.). Use `separate` for clean isolation.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MNEMO_URL` | `http://localhost:50001` | Mnemo Cortex API address (often `http://host.docker.internal:50001` from a container) |
| `MNEMO_AGENT_ID` | `openclaw` (rename per agent) | Unique identity in the memory system |
| `MNEMO_SHARE` | `separate` | Cross-agent sharing mode |
| `BRAIN_DIR` | `~/mnemo-plan/brain` | Optional — enables brain-file tools when pointed at an existing dir inside the container |
| `WIKI_DIR` | unset | Optional — enables wiki tools when pointed at an existing dir |

## How It Works

```
[Agent Zero Web UI on container :80]
      │  (LLM decides to use a memory tool)
      ▼
[Agent Zero MCP client] ──spawns via stdio──▶ [openclaw-mcp/server.js (in-container)]
                                                       │
                                                       │ HTTP POST to /writeback, /context
                                                       ▼
                                               [Mnemo Cortex API at MNEMO_URL]
                                                       │
                                                       ▼
                                               [Mnemo memory under agent_id=YOURS]
```

The bridge runs as a child process of Agent Zero, connected via stdio. Each tool call from the LLM becomes a JSON-RPC message to the bridge, which translates to a REST call to Mnemo. Embeddings happen server-side — no embedding-model mismatch is possible from this client.

## Workflow

See the [Session Guide](../../SESSION-GUIDE.md). Same patterns apply whether your agent is autonomous or human-driven.

## Next Step

**Read [THE-LANE-PROTOCOL.md](../../THE-LANE-PROTOCOL.md) to learn the session ritual that makes Mnemo actually work.**

## License

MIT

---

*Part of [Mnemo Cortex](https://github.com/GuyMannDude/mnemo-cortex) by Project Sparks*
