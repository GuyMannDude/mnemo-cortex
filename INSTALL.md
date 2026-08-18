# `INSTALL.md` — Install guide for Mnemo Cortex (humans and AI agents)

> **This is the install guide. If you only open one file, open this one.**
>
> It works for a person reading along *and* for an AI agent doing the
> install for you. If you are handing this to an assistant, the whole
> instruction is: **"Install Mnemo Cortex — follow INSTALL.md."**

This page assumes you know **nothing** about the AI host platforms Mnemo
integrates with — Claude Desktop, Claude Code, OpenClaw, LM Studio,
AnythingLLM, Agent Zero, Hermes Agent, Ollama Desktop, and the generic
MCP hosts (Open WebUI, llama.cpp, LobeChat, Jan). Each gets a
one-paragraph orientation below, then a link to its full integration doc.

---

### Which file is which

The repo root has several `robot.*` files. They have similar names and
very different jobs, so here is the whole family in one place:

| File | What it is | Do you read it? |
|---|---|---|
| **`INSTALL.md`** ← you are here | The install **instructions**, in English | **Yes — start here** |
| [`README.md`](./README.md) | What Mnemo Cortex is and why. The long-form story | Yes, if you want the overview |
| [`robot.info`](./robot.info) | A **data file**. Structured JSON describing the product, so an AI can answer questions about Mnemo without scraping this repo | No — it is for machines |
| [`robot.install`](./robot.install) | A **data file**. The JSON settings the installer reads. Edit it *only* if you want to change a default like the port | No — it is for machines |
| [`robot-install.sh`](./robot-install.sh) | The installer program itself | No — you run it, §2a |

**In one line:** `INSTALL.md` tells you *how*. `robot.install` is a
*settings file* the installer reads. They are one character apart and
they are not the same thing — if you landed on a wall of JSON, you
wanted this page.

---

## 1. Two install layers (don't skip this)

Mnemo Cortex is a **server** plus **per-host bridges**. Both layers
need installing. They are independent products you assemble:

```
   ┌────────────────────────┐                ┌──────────────┐
   │  Layer A: Mnemo server │ ◀─── HTTP ────▶│ MCP bridge   │ ◀─── stdio ──▶  Your user's
   │  (Python, port 50001)  │                │ (Node, per-  │                 AI host
   │                        │                │  user agent) │                 (e.g. Claude
   │  Stores memory + facts │                │              │                  Desktop)
   └────────────────────────┘                └──────────────┘
   robot-install.sh                          integrations/<host>/
```

**Layer A: the server** runs once per machine and listens on
`localhost:50001`. It stores all memories and facts. Install it once;
every AI host on the box talks to the same server.

**Layer B: the host bridge** is a small Node.js MCP server that runs
inside whatever AI host your user runs (Claude Desktop, LM Studio,
etc.). It translates between the host's MCP protocol and Mnemo's HTTP
API. There is one bridge per host *type*, but most hosts can be
configured in under five lines once the server is up.

Skip the host bridge and the AI host has no way to call Mnemo. Skip
the server and the bridge has nothing to call.

---

## 2. Install the server (Layer A)

Two ways. Pick one.

### 2a. Scripted — `robot-install.sh`

For agents and CI. Reads [`robot.install`](./robot.install) (JSON
manifest), runs the install, prints a single JSON object on stdout.
Defaults are sensible — a manifest with only the `service` block
already produces a working local install.

```bash
./robot-install.sh
```

If the user has API keys to wire in (OpenRouter for reasoning,
optional Google or OpenAI for embeddings), set them in the
*environment* before running — the installer reads the env var
named in `api_key_env` and writes it into a `0600`-permission env
file alongside the systemd unit. Never paste keys into the JSON
manifest.

Success: `{"ok": true, "steps": {...}, "smoke_test": {"ok": true}}`.
Failure: `{"ok": false, "error": "...", ...}` with exit code 1.

Sandbox test before touching real state:

```bash
MNEMO_INSTALL_VENV_DIR=/tmp/test-venv \
MNEMO_INSTALL_CONFIG_DIR=/tmp/test-config \
MNEMO_INSTALL_SYSTEMD_DIR=/tmp/test-systemd \
MNEMO_INSTALL_DRY_RUN=1 \
./robot-install.sh
```

### 2b. Manual — `mnemo-cortex init`

For humans who want the interactive wizard. See [README §Install
Guide](./README.md#install-guide) Steps 1–4.

---

## 3. Wire your user's AI host to the server (Layer B)

This is the part that depends on what your user runs. Identify the
host first, then follow the matching integration guide.

### 3a. If you don't know what host your user has

Ask. Or check these tells:

| Tell | Likely host |
|---|---|
| User typed `cc` or `claude` in a terminal | **Claude Code** |
| Desktop app, runs locally, no API key required | **Claude Desktop** (Anthropic-built) |
| User mentions "ClawdBot," "Rocky," or "Sparky" | **OpenClaw** |
| Desktop app with a model picker; user picked Qwen / Llama / Mistral | **LM Studio** (most common GUI for local models) |
| User has "workspaces"; can drag PDFs into chat | **AnythingLLM** |
| Docker container; user mentions "Bullwinkle" / "Agent Zero" | **Agent Zero** |
| User runs `hermes ...` in a terminal; mentions Nous Research | **Hermes Agent** |
| User said `ollama launch openclaw` or `ollama run` | **Ollama Desktop** |
| User's assistant is ChatGPT (chatgpt.com); mentions "Custom GPT" or "GPTs" | **ChatGPT** |
| User runs `llama-server` directly | **llama.cpp** |
| Browser-based, multi-model dashboard | **Open WebUI** |
| Chinese-friendly UI, MIT-licensed clone of ChatGPT | **LobeChat** |
| Cross-platform desktop, "AI playground" framing | **Jan** |

If none match, ask the user: *"What AI assistant or model host are
you using? I need the name so I can wire memory in correctly."*

### 3b. Per-host integration guides

Each path below is a full README with copy-pasteable config and
verified gotchas. Read the one that matches your user's host.

| Host | Integration dir | What it is in one line |
|---|---|---|
| **Claude Code** | [`integrations/claude-code/`](./integrations/claude-code/) | Anthropic's terminal CLI agent — runs in your user's shell, reads/writes files, executes commands. |
| **Claude Desktop** | [`integrations/claude-desktop/`](./integrations/claude-desktop/) | Anthropic's desktop app. Drag-and-drop `.mcpb` bundle install — no clone, no Node, no JSON editing. Step-by-step OS guides: [Windows](./docs/install-claude-desktop-windows.md) · [Linux](./docs/install-claude-desktop-linux.md). |
| **OpenClaw** | [`integrations/mcp-bridge/`](./integrations/mcp-bridge/) | Open-source local AI agent framework (the "ClawdBot" platform). Generic MCP bridge wires Mnemo in via one config block. |
| **LM Studio** | [`integrations/lmstudio/`](./integrations/lmstudio/) | Desktop GUI for running local LLMs (Qwen, Llama, etc.). Native MCP support since v0.3.17 — edit `mcp.json` and restart. |
| **AnythingLLM** | [`integrations/anythingllm/`](./integrations/anythingllm/) | Desktop GUI with workspace-scoped chat. Two-step setup: MCP config + flip workspace to "Automatic mode" so memory fires without `@agent` prefix. |
| **Agent Zero** | [`integrations/agent-zero/`](./integrations/agent-zero/) | Autonomous Docker-based agent (the "Bullwinkle / BW" platform). In-container MCP setup. |
| **Hermes Agent** | [`integrations/hermes/`](./integrations/hermes/) | Nous Research's open-weights agent framework (v0.12.0+). Config-only via `hermes mcp add`. |
| **Ollama Desktop** | [`integrations/ollama-desktop/`](./integrations/ollama-desktop/) | Ollama as the local LLM, OpenClaw as the MCP host, terminal launcher (`ollama launch openclaw`). Note: Ollama Desktop's own chat window does **not** support MCP. |
| **ChatGPT (Custom GPT)** | [`integrations/chatgpt/`](./integrations/chatgpt/) | OpenAI's ChatGPT via Custom GPT **Actions** (REST over public HTTPS, not MCP) — a small authenticated gate pins ChatGPT to one memory tenant. ⚠️ OpenAI tier rules apply: Custom GPT Actions need Plus+; full custom **MCP** save+recall in the main app needs Business/Enterprise (Plus MCP connectors are read-only). Guide: [docs/install-chatgpt.md](./docs/install-chatgpt.md). |
| **Open WebUI / llama.cpp / LobeChat / Jan / other MCP host** | [README §Use With Any Local LLM](./README.md#-use-with-any-local-llm) | Generic MCP setup. Each has a short config snippet in the README. |

### 3c. Things to get right for every host

1. **Absolute paths only.** Every host's MCP config requires the
   *absolute* path to `integrations/mcp-bridge/server.js`. Relative
   paths break silently — the host launches the bridge from the
   wrong cwd and `node` throws ENOENT.
2. **`MNEMO_AGENT_ID` must be unique per host.** Set it to
   `lmstudio`, `claude-desktop`, `cc`, etc. If two hosts share an
   agent ID, their memories collide and cross-agent dreaming can't
   tell them apart.
3. **`MNEMO_URL` must point at the server.** Default
   `http://localhost:50001`. If the server runs on another machine
   on the LAN, use that machine's IP and confirm the firewall
   allows the port.
4. **Use a tool-capable model.** Qwen3, Llama 3.2, Mistral, and
   Gemma 2 invoke tools correctly. Small models (under 7B) often
   *narrate* tool calls instead of making them — the response
   reads like "saved with id abc123" but no `mnemo_save` was ever
   called and no memory was stored. If you can't verify a save
   landed in `mnemo-cortex recall`, the model is hallucinating
   tool calls — scale up.

---

## 4. Verify the install

After Layer A and Layer B are both wired, run these checks:

```bash
# Server is up
mnemo-cortex health

# Save a known string from the agent's perspective
# (whatever syntax the host's MCP UI exposes — see integration doc)
# Then in a shell, query the server directly:
curl -s -X POST http://localhost:50001/context \
  -H "Content-Type: application/json" \
  -d '{"prompt": "the known string", "agent_id": "YOUR-AGENT-ID", "max_results": 5}'
```

If `recall` finds the string, the round-trip works. If it doesn't,
walk back the chain:

1. `curl http://localhost:50001/health` — server reachable?
2. Check the MCP host's tool list — is `mnemo_save` registered?
3. Check `MNEMO_AGENT_ID` matches between save and recall scopes.

For deeper diagnostics: `mnemo-cortex doctor`.

Install verified? One step left: **read [CORTEX-OS.md](CORTEX-OS.md)** — the
operating manual that teaches you (the agent) how to actually use what you
just installed: the startup ritual, what to save, when to recall, session
discipline. Installing memory without adopting the operating patterns is
how agents end up with a database they never think to read.

---

## 5. Related installs

- **FrankenClaw (Franky)** — the former "hands" companion to Mnemo's
  "memory". **Withdrawn from public distribution** — do not install;
  see the [withdrawal notice](https://projectsparks.ai/frankenclaw).

- **Disco-Bus** — `https://github.com/GuyMannDude/disco-bus`.
  Standalone agent-to-agent messaging (successor to Sparks Bus,
  which is archived at `GuyMannDude/sparks-bus` with a forwarding
  banner). Not required for Mnemo; install only if your user
  wants multi-agent delivery confirmation.

- **mnemo-plan** — `https://github.com/GuyMannDude/mnemo-plan`.
  Starter template for the brain repo. If your user wants
  persistent *current state* (project pads, lane files) in
  addition to persistent memory, fork this template and point
  the bridge's `BRAIN_DIR` env var at it. The bridge auto-enables
  the brain-file MCP tools when `BRAIN_DIR` exists.

---

## 6. Common pitfalls

| Symptom | Likely cause |
|---|---|
| Agent claims it saved a memory, but `mnemo-cortex recall` returns nothing | Model is narrating tool calls without invoking them. Use a tool-capable model (Qwen3, Llama 3.2, Mistral). |
| Bridge starts, then exits with ENOENT | Relative path in the host's MCP config. Use the *absolute* path to `server.js`. |
| `recall` returns "No chunks" for every query | Embedding model name doesn't match provider. See [README §Troubleshooting](./README.md#troubleshooting). |
| Server unreachable from another machine | Firewall. `ufw allow from <subnet> to any port 50001`. Default bind is `127.0.0.1`; switch to `0.0.0.0` in the manifest's `service.host` only if you want LAN access (and add auth). |
| Memories from two hosts show under one agent | `MNEMO_AGENT_ID` not set or duplicated. Each host needs a unique value. |
| Stage 0.5 fact extractions stop arriving overnight | Dreamer cron not installed, or reasoning provider env var missing from the systemd unit. `mnemo-cortex doctor` will flag both. |

---

## 7. When to bail and ask the user

This install is *for* your user, not *of* your user. If you hit any of
these, stop and ask:

- The user hasn't told you which AI host they run, and §3a's tells
  don't match.
- The user's machine doesn't have Python 3.11+ (server requirement)
  and you don't know if they want WSL2, Docker, or a different host.
- `robot-install.sh` reports `{"ok": false}` with an error you don't
  recognize. Paste the error to the user — don't guess.
- The user has a paid Mnemo deployment somewhere else and you'd be
  installing a second one. Confirm they want a separate instance.

When in doubt: install nothing destructive; surface the question.
