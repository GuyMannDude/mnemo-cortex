# Connect ChatGPT to Mnemo Cortex (Custom GPT Actions)

Mnemo Cortex gives ChatGPT a memory that you own. A Custom GPT can save
decisions, ideas, and identity facts to your own Mnemo server, then recall
them in a later conversation — memory that lives on your hardware, exports as
plain files, and moves with you if you ever change AI providers.

This guide connects ChatGPT to a Mnemo Cortex server you already run. It does
not install the server. If you do not have one yet, begin with the
[Mnemo Cortex install guide](../README.md#install-guide).

Unlike the Claude Desktop and LM Studio integrations, ChatGPT cannot run a
local MCP bridge — OpenAI's servers must reach your memory over public HTTPS.
So this integration ships a small **gate**: a two-route, authenticated,
tenant-pinned proxy that stands between the internet and your private Mnemo
server. The gate code lives in
[`integrations/chatgpt/`](../integrations/chatgpt/).

## ⚠️ OpenAI plan requirements (read first)

OpenAI gates connector capability by subscription tier, and the rules change
often — verify against OpenAI's current documentation before you start. As of
July 2026:

- **Custom GPT Actions** (what this guide uses) require a paid plan
  (**ChatGPT Plus or higher**) to create a Custom GPT. Actions support both
  recall *and* save.
- **Custom MCP connectors in the main ChatGPT app** are a different feature:
  full custom MCP with save+recall requires **Business or Enterprise**; on
  Plus, custom connectors are effectively **read-only**. This repo does not
  ship a remote MCP endpoint for ChatGPT — the Actions route below is the
  supported path.
- A Custom GPT is a **separate chat surface**. It does not share ChatGPT's
  built-in memory or your existing conversation threads. Your main ChatGPT
  keeps working exactly as before; the Custom GPT is the one with Mnemo
  memory.

## How it fits together

```
ChatGPT (Custom GPT Action, bearer token)
        │  public HTTPS
        ▼
Mnemo Gate — 127.0.0.1:50002, published via Tailscale Funnel or reverse proxy
  • only /recall and /save exist
  • pins every request to one tenant (default: chatgpt)
  • rate limit, 8KB body cap, audit log, generic errors
        │  private, loopback or LAN
        ▼
Mnemo Cortex server — 127.0.0.1:50001 (never exposed)
```

Your Mnemo API key never leaves the gate machine. ChatGPT holds only the
gate's own token, which unlocks exactly two operations on exactly one memory
tenant.

## Step 1: Run the gate

On the machine that runs (or can reach) your Mnemo server:

1. Create the two secret files (each a single line, 32+ characters):
   - `~/.mnemo-gate/token` — a fresh random token for ChatGPT. Generate one:
     ```bash
     mkdir -p ~/.mnemo-gate && python3 -c "import secrets; print(secrets.token_urlsafe(36))" > ~/.mnemo-gate/token
     ```
   - `~/.mnemo-auth-token` — your Mnemo server API key (you likely have this
     already from the server install).
2. Start the gate from `integrations/chatgpt/`:

   ```bash
   python -m uvicorn server:create_app --factory \
     --host 127.0.0.1 --port 50002 --no-access-log
   ```

   On Windows, use the bundled scripts instead — `run-gate.ps1` to run once,
   `install-task.ps1` to install an auto-starting Scheduled Task:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\run-gate.ps1
   powershell -ExecutionPolicy Bypass -File .\install-task.ps1
   ```

   On Linux, a systemd user service keeps it running:

   ```ini
   # ~/.config/systemd/user/mnemo-gate.service
   [Unit]
   Description=Mnemo ChatGPT Gate
   After=network.target

   [Service]
   WorkingDirectory=%h/mnemo-cortex/integrations/chatgpt
   ExecStart=/usr/bin/python3 -m uvicorn server:create_app --factory --host 127.0.0.1 --port 50002 --no-access-log
   Restart=on-failure
   RestartSec=5
   RestartSteps=4
   RestartMaxDelaySec=60

   [Install]
   WantedBy=default.target
   ```

   ```bash
   systemctl --user enable --now mnemo-gate.service
   ```

3. Check it locally — an unauthorized request must bounce:

   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:50002/recall
   # expect: 401
   ```

Configuration knobs (tenant, rate limit, upstream URL) are environment
variables — see the [integration README](../integrations/chatgpt/README.md).

## Step 2: Publish the gate over HTTPS

ChatGPT's servers must reach the gate at a public HTTPS URL. Two good options:

**Tailscale Funnel** (simplest — free, automatic TLS, no port forwarding):

```bash
tailscale funnel --bg 50002
tailscale funnel status   # note your https://MACHINE.TAILNET.ts.net URL
```

Kill switch — one command takes the public endpoint down while Mnemo and the
local gate stay up:

```bash
tailscale funnel --https=443 off
```

The background Funnel configuration is stored in Tailscale's service state,
so it survives reboots.

**Any HTTPS reverse proxy** (Caddy, nginx + certbot, Cloudflare Tunnel):
forward `https://your-domain` → `127.0.0.1:50002`. Do not terminate plain
HTTP publicly, and do not expose port 50001 (the Mnemo server itself) —
only the gate goes public.

## Step 3: Create the Custom GPT

1. In ChatGPT: **Explore GPTs → Create** (requires Plus or higher).
2. In **Configure → Actions → Create new action**, paste the contents of
   [`integrations/chatgpt/openapi.json`](../integrations/chatgpt/openapi.json),
   replacing `https://YOUR-GATE-HOSTNAME` in `servers` with your real HTTPS
   URL from Step 2.
3. Set **Authentication → API Key → Bearer**, and paste the value of
   `~/.mnemo-gate/token` (the gate token — *never* your Mnemo server key).
4. In **Instructions**, tell the GPT how to use its memory. A working
   template:

   > You have persistent memory through two actions: recallMemory and
   > saveMemory.
   >
   > At the start of a conversation, if the user's first message refers to
   > past work, people, or decisions, call recallMemory with a short prompt
   > describing the topic before answering.
   >
   > When the user asks you to remember something (or ends a session with
   > something worth keeping), call saveMemory with a unique session_id, a
   > 2-5 sentence summary, up to 10 key_facts, and the best-fitting category
   > (session_log, idea, decision, identity, or relationship).
   >
   > Only save what the user said or decided — never invent memories, and
   > never save your own speculation as fact. If a recall returns nothing,
   > say so plainly.

5. Save the GPT (visibility: **Only me** is the sensible default — anyone
   who can chat with the GPT can read and write this memory tenant).

## Prove that memory works

In your Custom GPT, ask:

> Remember this: my Mnemo ChatGPT setup works.

You should see the GPT call `saveMemory` and ask for permission the first
time ("Allow" / "Always allow"). Then open a **new** conversation with the
same GPT and ask:

> What did I ask you to remember about my ChatGPT setup?

You should see a `recallMemory` call, not just a confident answer. If the
second chat recalls the sentence, the round trip works. You can also verify
from the server side (on the gate machine):

```bash
curl -s -X POST http://127.0.0.1:50001/context \
  -H "X-API-KEY: $(cat ~/.mnemo-auth-token)" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Mnemo ChatGPT setup works", "agent_id": "chatgpt", "max_results": 3}'
```

## Security notes

- The gate pins everything to one tenant (`chatgpt` by default). Even a
  request that claims another `agent_id` is silently forced back — ChatGPT
  can never read or write your other agents' memories.
- Saves are stamped `source=user` and tagged `chatgpt-gate`, so
  gate-originated memories are always identifiable and bulk-removable later.
- Rate limit is 10 requests/hour by default (`MNEMO_GATE_RATE_LIMIT`) —
  generous for conversational use, hostile to scraping.
- Every request is audit-logged to `~/.mnemo-gate/audit.jsonl` (a 160-char
  snippet, not the full body), rotated at 5MB.
- Treat the gate token like a password. If it leaks, generate a new one,
  update the file, restart the gate, and update the GPT's auth setting.

## Troubleshooting

**401 Unauthorized** — the GPT's bearer token doesn't match
`~/.mnemo-gate/token`. Re-paste it in the Action's authentication settings.
Restart the gate after changing the token file.

**429 Rate limit exceeded** — more than the hourly allowance. Wait, or raise
`MNEMO_GATE_RATE_LIMIT` and restart the gate.

**502 / 504 from the gate** — the gate is up but Mnemo isn't reachable.
Check the server: `curl http://127.0.0.1:50001/health` on the gate machine,
and confirm `MNEMO_GATE_UPSTREAM_URL` if the server is remote.

**ChatGPT says the action failed / can't reach the server** — the public
HTTPS URL is down. Check `tailscale funnel status` (or your reverse proxy),
and confirm the `servers` URL in the Action's schema matches it exactly.

**The GPT answers from imagination instead of calling recallMemory** —
tighten the Instructions (step 3.4) and ask explicitly: "Use recallMemory to
check." A promise to remember without a visible action call is not memory.

## What to do next

Have a look at [CORTEX-OS.md](../CORTEX-OS.md) — the agent-side operating
manual — for what's worth saving and when to recall. The `chatgpt` tenant is
a first-class agent on your server: it participates in cross-agent dreaming
and is queryable over the server API like any other agent.
