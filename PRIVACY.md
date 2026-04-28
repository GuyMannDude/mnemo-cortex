# Privacy Policy — Mnemo Cortex

**Last updated:** 2026-04-27

Mnemo Cortex is a self-hosted, local-first persistent memory layer. We don't run a service, don't operate a backend, and don't collect telemetry. Your memory data lives on infrastructure you control.

## What the Mnemo Cortex bridge sends, and where

The MCP bridge (the `.mcpb` bundle you install in Claude Desktop, or the `node` process you run from a checkout) connects to **one place only**: the Mnemo Cortex server URL you provide at install time (the `MNEMO_URL` field).

That server can be:

- **A Mnemo Cortex you run locally** (e.g., `http://localhost:50001`) — your data never leaves your machine.
- **A Mnemo Cortex you run on your own network** (e.g., a home server) — your data stays on your hardware.
- **A Mnemo Cortex hosted by a third party** — only if you point at one. We do not operate any hosted instance.

The bridge sends:

- `mnemo_save`: the summary text and key-fact strings you (or your AI agent) explicitly asked to save.
- `mnemo_recall` / `mnemo_search`: the search prompt you (or your AI agent) typed.
- `passport_*`: the structured behavioral observations and promotion decisions you make.
- `read_brain_file` / `wiki_*`: file paths you request reads from, when those tools are enabled.

The bridge does **not** send: telemetry, analytics, error reports, your machine identifiers, or any data unrelated to the explicit tool call.

## What the Mnemo Cortex server stores

The server you run stores:

- The text you explicitly saved through `mnemo_save`, indexed by semantic embedding.
- An `agent_id` tag (whatever you configured) and a `session_id` (auto-generated from your agent_id and a timestamp).
- The Developer's Passport YAML if you use those tools — promotions, audit log, pending observations.

The server does not store: your conversations as a whole, your prompts to your LLM, or anything you didn't explicitly hand to the `mnemo_save` / `passport_observe_behavior` tools.

## Embeddings and reasoning

Mnemo Cortex uses an **embedding model** (default: `nomic-embed-text` via Ollama) to index memories for semantic search. Embeddings are computed locally on your Mnemo Cortex server. The text you send to the server is forwarded to whatever embedding model that server is configured to use.

If you configure your Mnemo Cortex server to use a hosted embedding model (e.g., OpenAI, Voyage, Cohere via OpenRouter), then the text you save will transit to that provider for embedding. **That choice is yours, not ours** — we ship with a local Ollama default specifically so you don't have to.

Some optional features (cross-agent dreaming, the LLM-based memory compactor) can be configured to use a reasoning LLM. That's also under your control via your server config; the bridge bundle doesn't talk to any LLM directly.

## Your control

- **Read everything**: the SQLite DB on your server is open. `sqlite3 mnemo.db` and look at it whenever you want.
- **Delete everything**: stop the server and delete the DB file. There is no remote backup unless you set one up.
- **Forget specific items**: use `passport_forget_or_override` for behavioral claims; for memory entries, delete from the SQLite DB directly. A forget-by-API tool is on the roadmap but not yet shipped.
- **Stop the bridge**: uninstall the bundle in Claude Desktop, or remove the entry from `claude_desktop_config.json`. The bundle leaves no residue on your machine outside its install directory.

## Source code

The bridge is open source: <https://github.com/GuyMannDude/mnemo-cortex/tree/master/integrations/openclaw-mcp>

The server is open source: <https://github.com/GuyMannDude/mnemo-cortex>

If anything in this policy doesn't match what the code does, the code is the source of truth — and we'd consider that a bug. File an issue at <https://github.com/GuyMannDude/mnemo-cortex/issues>.

## Contact

Questions, concerns, or corrections: support@projectsparks.ai or open an issue on the GitHub repo.
